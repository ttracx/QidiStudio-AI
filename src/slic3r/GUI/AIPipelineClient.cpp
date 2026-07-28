#include "AIPipelineClient.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <fstream>
#include <sstream>
#include <cstring>
#include <map>
#include <algorithm>
#include <iterator>
#include <mutex>

#include <boost/filesystem.hpp>

namespace Slic3r {
namespace GUI {

namespace {

bool is_loopback_server_url(const std::string& url)
{
    static const std::vector<std::string> prefixes = {
        "http://127.0.0.1",
        "http://localhost",
        "http://[::1]",
    };
    for (const std::string& prefix : prefixes) {
        if (url.compare(0, prefix.size(), prefix) == 0) {
            const size_t next = prefix.size();
            return url.size() == next || url[next] == ':' || url[next] == '/';
        }
    }
    return false;
}

void initialize_curl_once()
{
    static std::once_flag init_flag;
    std::call_once(init_flag, []() { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

} // namespace

// ---------------------------------------------------------------------------
// libcurl callbacks
// ---------------------------------------------------------------------------

struct DownloadContext {
    std::ofstream           file;
    std::string             buffer;         // For non-file responses
    std::map<std::string, std::string> headers;
    bool                    write_to_file = false;
};

size_t AIPipelineClient::writeCallback(void* contents, size_t size, size_t nmemb, void* userp)
{
    size_t total_size = size * nmemb;
    auto* ctx = static_cast<DownloadContext*>(userp);

    if (ctx->write_to_file && ctx->file.is_open()) {
        ctx->file.write(static_cast<const char*>(contents), total_size);
    } else {
        ctx->buffer.append(static_cast<const char*>(contents), total_size);
    }

    return total_size;
}

size_t AIPipelineClient::headerCallback(void* contents, size_t size, size_t nmemb, void* userp)
{
    size_t total_size = size * nmemb;
    auto* ctx = static_cast<DownloadContext*>(userp);
    std::string header(static_cast<const char*>(contents), total_size);

    // Parse "Key: Value" headers
    auto colon_pos = header.find(':');
    if (colon_pos != std::string::npos) {
        std::string key = header.substr(0, colon_pos);
        std::string value = header.substr(colon_pos + 1);
        // Trim whitespace
        while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.erase(0, 1);
        while (!value.empty() && (value.back() == '\r' || value.back() == '\n' || value.back() == ' ')) value.pop_back();
        ctx->headers[key] = value;
    }

    return total_size;
}

// ---------------------------------------------------------------------------
// AIPipelineClient
// ---------------------------------------------------------------------------

AIPipelineClient::AIPipelineClient(const std::string& server_url)
    : m_server_url(server_url)
{
    initialize_curl_once();
}

AIPipelineClient::~AIPipelineClient() = default;

bool AIPipelineClient::isServerRunning()
{
    if (!is_loopback_server_url(m_server_url))
        return false;

    CURL* curl = curl_easy_init();
    if (!curl) return false;

    DownloadContext ctx;
    std::string url = m_server_url + "/health";

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 3L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 2L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    CURLcode res = curl_easy_perform(curl);
    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_easy_cleanup(curl);

    if (res == CURLE_OK && http_code == 200) {
        try {
            auto json = nlohmann::json::parse(ctx.buffer);
            return json.value("status", "") == "ok";
        } catch (...) {
            return false;
        }
    }

    return false;
}

std::string AIPipelineClient::getHealth()
{
    if (!is_loopback_server_url(m_server_url))
        return "{}";

    CURL* curl = curl_easy_init();
    if (!curl) return "{}";

    DownloadContext ctx;
    std::string url = m_server_url + "/health";

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 5L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    CURLcode res = curl_easy_perform(curl);
    curl_easy_cleanup(curl);

    if (res == CURLE_OK) {
        return ctx.buffer;
    }
    return "{}";
}

AIGenerateResult AIPipelineClient::generate(
    const std::vector<std::string>& image_paths,
    const AIGenerateParams& params,
    ProgressCallback progress_cb,
    std::atomic<bool>* cancel_flag
)
{
    AIGenerateResult result;
    result.success = false;

    if (image_paths.empty()) {
        result.error_message = "No images provided";
        return result;
    }
    if (!is_loopback_server_url(params.server_url)) {
        result.error_message = "AI server URL must use local loopback HTTP";
        return result;
    }

    if (progress_cb) progress_cb("Preparing request...", 5);

    CURL* curl = curl_easy_init();
    if (!curl) {
        result.error_message = "Failed to initialize HTTP client";
        return result;
    }

    // Create multipart form
    curl_mime* form = curl_mime_init(curl);
    curl_mimepart* part = nullptr;

    // Add form fields
    auto add_field = [&](const char* name, const std::string& value) {
        part = curl_mime_addpart(form);
        curl_mime_name(part, name);
        curl_mime_data(part, value.c_str(), value.size());
    };

    add_field("backend", params.backend);
    add_field("quality", params.quality);
    add_field("seed", std::to_string(params.seed));
    add_field("flatten", params.flatten_bottom ? "true" : "false");
    add_field("remove_bg", params.remove_bg ? "true" : "false");
    add_field("max_faces", std::to_string(params.max_faces));
    add_field("format", params.format);

    if (params.width_mm > 0 && params.depth_mm > 0 && params.height_mm > 0) {
        add_field("width_mm", std::to_string(params.width_mm));
        add_field("depth_mm", std::to_string(params.depth_mm));
        add_field("height_mm", std::to_string(params.height_mm));
    }

    // Add image files
    for (size_t i = 0; i < image_paths.size(); i++) {
        if (cancel_flag && cancel_flag->load()) {
            result.error_message = "Cancelled by user";
            curl_mime_free(form);
            curl_easy_cleanup(curl);
            return result;
        }

        if (progress_cb) {
            progress_cb("Adding image " + std::to_string(i + 1) + "/" +
                        std::to_string(image_paths.size()) + "...",
                        5 + static_cast<int>((i * 5) / image_paths.size()));
        }

        part = curl_mime_addpart(form);
        curl_mime_name(part, "images[]");
        curl_mime_filedata(part, image_paths[i].c_str());
        // Set content type based on extension
        std::string path = image_paths[i];
        std::string ext = path.substr(path.find_last_of('.') + 1);
        std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
        if (ext == "png") curl_mime_type(part, "image/png");
        else if (ext == "jpg" || ext == "jpeg") curl_mime_type(part, "image/jpeg");
        else if (ext == "bmp") curl_mime_type(part, "image/bmp");
        else if (ext == "webp") curl_mime_type(part, "image/webp");
        else curl_mime_type(part, "application/octet-stream");
    }

    // Prepare output file
    std::string ext = params.format;
    if (ext == "3mf") ext = "3mf";
    // Generate temp file path
    // Use Boost's cross-platform temp directory and high-entropy unique path.
    // The previous mkstemp/unistd implementation did not compile on Windows.
    const boost::filesystem::path temp_path_fs =
        boost::filesystem::temp_directory_path() /
        boost::filesystem::unique_path("thoxforge_output_%%%%-%%%%-%%%%-%%%%." + ext);
    const std::string temp_path = temp_path_fs.string();

    // Set up download context
    DownloadContext ctx;
    ctx.write_to_file = true;
    ctx.file.open(temp_path, std::ios::binary);
    if (!ctx.file.is_open()) {
        result.error_message = "Failed to open temporary output file";
        curl_mime_free(form);
        curl_easy_cleanup(curl);
        return result;
    }

    std::string url = params.server_url + "/generate";

    if (progress_cb) progress_cb("Sending to AI server...", 15);

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_MIMEPOST, form);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, headerCallback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 600L);  // 10 minute timeout for AI inference
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 1L);

    if (progress_cb) progress_cb("AI inference in progress (10-60 seconds)...", 30);

    CURLcode res = curl_easy_perform(curl);

    ctx.file.close();

    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

    curl_mime_free(form);
    curl_easy_cleanup(curl);

    if (cancel_flag && cancel_flag->load()) {
        result.error_message = "Cancelled by user";
        std::remove(temp_path.c_str());
        return result;
    }

    if (res != CURLE_OK) {
        result.error_message = "HTTP error: " + std::string(curl_easy_strerror(res));
        std::remove(temp_path.c_str());
        return result;
    }

    if (http_code != 200) {
        // Error responses were written to the temporary file because the
        // response status is only available after transfer completion.
        std::ifstream error_stream(temp_path, std::ios::binary);
        std::string error_body(
            (std::istreambuf_iterator<char>(error_stream)),
            std::istreambuf_iterator<char>()
        );
        result.error_message = "Server returned HTTP " + std::to_string(http_code);
        if (!error_body.empty()) {
            try {
                auto json = nlohmann::json::parse(error_body);
                if (json.contains("error")) {
                    result.error_message += ": " + json["error"].get<std::string>();
                }
            } catch (...) {}
        }
        std::remove(temp_path.c_str());
        return result;
    }

    // Parse metadata from response headers
    auto get_header = [&](const std::string& key) -> std::string {
        auto it = ctx.headers.find(key);
        return (it != ctx.headers.end()) ? it->second : "";
    };

    result.success        = true;
    result.output_path    = temp_path;
    result.vertex_count   = std::atoi(get_header("X-ThoxForge-Vertices").c_str());
    result.face_count     = std::atoi(get_header("X-ThoxForge-Faces").c_str());
    result.is_watertight  = get_header("X-ThoxForge-Watertight") == "true";
    result.is_manifold    = get_header("X-ThoxForge-Manifold") == "true";
    result.backend_used   = get_header("X-ThoxForge-Backend");
    result.elapsed_seconds = std::atof(get_header("X-ThoxForge-Elapsed").c_str());

    if (progress_cb) progress_cb("Done!", 100);

    return result;
}

}} // namespace Slic3r::GUI
