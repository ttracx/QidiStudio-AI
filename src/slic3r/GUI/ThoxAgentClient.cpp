#include "ThoxAgentClient.hpp"

#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <boost/log/trivial.hpp>

namespace Slic3r {
namespace GUI {

using json = nlohmann::json;

namespace {

// Bounded response buffer. The sidecar is local and its JSON replies are small;
// a runaway body should not be able to exhaust GUI memory.
constexpr size_t MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

struct ResponseCtx
{
    std::string buffer;
    bool        truncated = false;
};

} // namespace

size_t ThoxAgentClient::writeCallback(void* contents, size_t size, size_t nmemb, void* userp)
{
    auto*  ctx  = static_cast<ResponseCtx*>(userp);
    size_t real = size * nmemb;
    if (ctx->buffer.size() + real > MAX_RESPONSE_BYTES) {
        ctx->truncated = true;
        return 0; // aborts the transfer
    }
    ctx->buffer.append(static_cast<char*>(contents), real);
    return real;
}

ThoxAgentClient::ThoxAgentClient(const std::string& server_url)
    : m_server_url(server_url)
{
    if (!m_server_url.empty() && m_server_url.back() == '/')
        m_server_url.pop_back();
}

ThoxAgentClient::~ThoxAgentClient() = default;

ThoxResult ThoxAgentClient::request(const std::string& method,
                                    const std::string& path,
                                    const std::string& json_body,
                                    long               timeout_s)
{
    ThoxResult result;

    CURL* curl = curl_easy_init();
    if (!curl) {
        result.message = "Could not initialise HTTP client";
        return result;
    }

    ResponseCtx       ctx;
    const std::string url     = m_server_url + path;
    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    headers = curl_slist_append(headers, "Accept: application/json");

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, timeout_s);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 3L);
    // NOSIGNAL matters here: without it libcurl's alarm-based DNS timeout is
    // not thread-safe, and every call from this client runs on a worker thread.
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    if (method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_body.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long) json_body.size());
    }

    const CURLcode code = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.http_status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (code != CURLE_OK) {
        result.message = ctx.truncated
                             ? "Response from the THOX service was too large"
                             : std::string("Cannot reach the THOX service: ") +
                                   curl_easy_strerror(code);
        return result;
    }

    result.body = ctx.buffer;

    // Parse before branching on status: a 409 carries the reason we need.
    std::string error_text;
    try {
        if (!ctx.buffer.empty()) {
            const json parsed = json::parse(ctx.buffer);
            if (parsed.is_object()) {
                result.reason = parsed.value("reason", "");
                error_text    = parsed.value("error", "");
                if (error_text.empty())
                    error_text = parsed.value("message", "");
            }
        }
    } catch (const std::exception&) {
        // A non-JSON body is only a problem if the request also failed; the
        // status code below still tells us what happened.
    }

    if (result.http_status == 409) {
        // Refused, not broken. The printer was not touched.
        result.refused = true;
        result.message = error_text.empty() ? "The printer declined this action"
                                            : error_text;
        return result;
    }
    if (result.http_status >= 400) {
        result.message = error_text.empty()
                             ? "THOX request failed (HTTP " +
                                   std::to_string(result.http_status) + ")"
                             : error_text;
        return result;
    }

    result.success = true;
    if (result.message.empty())
        result.message = error_text;
    return result;
}

bool ThoxAgentClient::isAvailable()
{
    const ThoxResult result = request("GET", "/thox/health", "", 4);
    if (!result.success)
        return false;
    try {
        return json::parse(result.body).value("ok", false);
    } catch (const std::exception&) {
        return false;
    }
}

ThoxResult ThoxAgentClient::getPrinter() { return request("GET", "/thox/printer", "", 12); }

std::string ThoxAgentClient::cameraStreamUrl()
{
    const ThoxResult result = getPrinter();
    if (!result.success)
        return {};
    try {
        const json parsed  = json::parse(result.body);
        const auto cameras = parsed.value("cameras", json::array());
        if (!cameras.empty() && cameras[0].is_object())
            return cameras[0].value("stream_url", "");
    } catch (const std::exception&) {
    }
    return {};
}

ThoxResult ThoxAgentClient::startMonitor() { return request("POST", "/thox/monitor/start", "{}", 30); }
ThoxResult ThoxAgentClient::stopMonitor() { return request("POST", "/thox/monitor/stop", "{}", 20); }

bool ThoxAgentClient::getHealthState(ThoxHealthState& out)
{
    const ThoxResult result = request("GET", "/thox/monitor/state", "", 10);
    if (!result.success)
        return false;

    try {
        const json parsed = json::parse(result.body);
        out.running   = parsed.value("running", false);
        out.autonomy  = parsed.value("autonomy", "");
        out.watching  = parsed.value("watching", "");
        out.samples   = parsed.value("samples", 0);
        out.last_error = parsed.value("last_error", "");

        if (parsed.contains("job") && parsed["job"].is_object()) {
            const auto& job   = parsed["job"];
            out.printer_state = job.value("state", "");
            out.progress      = job.value("progress", 0.0);
            if (job.contains("current_layer") && job["current_layer"].is_number())
                out.current_layer = job["current_layer"].get<int>();
            if (job.contains("total_layer") && job["total_layer"].is_number())
                out.total_layers = job["total_layer"].get<int>();
        }

        if (parsed.contains("ensemble") && parsed["ensemble"].is_object()) {
            const auto& ensemble = parsed["ensemble"];
            out.has_classifier   = ensemble.value("has_classifier", false);
            for (const auto& name : ensemble.value("active", json::array()))
                if (name.is_string())
                    out.active_providers.push_back(name.get<std::string>());
        }

        if (parsed.contains("verdict") && parsed["verdict"].is_object()) {
            const auto& verdict = parsed["verdict"];
            out.severity     = verdict.value("severity", 0.0);
            out.confidence   = verdict.value("confidence", 0.0);
            out.urgency      = verdict.value("urgency", "");
            out.summary      = verdict.value("summary", "");
            out.camera_fault = verdict.value("camera_fault", "");
            for (const auto& item : verdict.value("detections", json::array())) {
                if (!item.is_object())
                    continue;
                ThoxDetection detection;
                detection.kind       = item.value("kind", "");
                detection.label      = item.value("label", "");
                detection.urgency    = item.value("urgency", "");
                detection.confidence = item.value("confidence", 0.0);
                detection.severity   = item.value("severity", 0.0);
                detection.note       = item.value("note", "");
                if (item.contains("bbox_norm") && item["bbox_norm"].is_array())
                    for (const auto& value : item["bbox_norm"])
                        if (value.is_number())
                            detection.bbox_norm.push_back(value.get<double>());
                out.detections.push_back(std::move(detection));
            }
        }

        for (const auto& item : parsed.value("suspicions", json::array())) {
            if (!item.is_object())
                continue;
            out.suspicions.push_back(
                item.value("label", item.value("kind", "")) + " (" +
                std::to_string(item.value("count", 0)) + "/" +
                std::to_string(item.value("needed", 0)) + ")");
        }
        return true;
    } catch (const std::exception& exc) {
        BOOST_LOG_TRIVIAL(warning)
            << "[THOX] could not parse monitor state: " << exc.what();
        return false;
    }
}

std::vector<unsigned char> ThoxAgentClient::getLatestFrame()
{
    std::vector<unsigned char> frame;

    CURL* curl = curl_easy_init();
    if (!curl)
        return frame;

    ResponseCtx       ctx;
    const std::string url = m_server_url + "/thox/monitor/frame";
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 10L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    const CURLcode code = curl_easy_perform(curl);
    long           status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(curl);

    // A 404 is normal before the first sample; treat it as "nothing yet".
    if (code != CURLE_OK || status != 200 || ctx.truncated)
        return frame;
    if (ctx.buffer.size() < 2 || static_cast<unsigned char>(ctx.buffer[0]) != 0xFF ||
        static_cast<unsigned char>(ctx.buffer[1]) != 0xD8)
        return frame; // not a JPEG

    frame.assign(ctx.buffer.begin(), ctx.buffer.end());
    return frame;
}

bool ThoxAgentClient::getEvents(long long                since_seq,
                                std::vector<ThoxEvent>&  out,
                                long long&               last_seq)
{
    const ThoxResult result =
        request("GET", "/thox/events?since=" + std::to_string(since_seq) + "&limit=200", "", 10);
    if (!result.success)
        return false;
    try {
        const json parsed = json::parse(result.body);
        last_seq          = parsed.value("last_seq", since_seq);
        for (const auto& item : parsed.value("events", json::array())) {
            if (!item.is_object())
                continue;
            ThoxEvent event;
            event.seq      = item.value("seq", 0LL);
            event.at       = item.value("at", 0.0);
            event.kind     = item.value("kind", "");
            event.message  = item.value("message", "");
            event.severity = item.value("severity", 0.0);
            event.notable  = item.value("notable", false);
            out.push_back(std::move(event));
        }
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

ThoxResult ThoxAgentClient::control(const std::string& action, const std::string& why)
{
    json body;
    // Always "human": this client is driven by a button press in the GUI, and
    // labelling it "agent" would subject the operator to the autonomy policy
    // that exists to restrain the monitor.
    body["actor"] = "human";
    if (!why.empty())
        body["why"] = why;
    return request("POST", "/thox/control/" + action, body.dump(), 30);
}

ThoxResult ThoxAgentClient::reprint(const std::string& filename)
{
    json body;
    body["actor"] = "human";
    if (!filename.empty())
        body["filename"] = filename;
    return request("POST", "/thox/control/reprint", body.dump(), 30);
}

ThoxResult ThoxAgentClient::planRevision(const std::string& defect,
                                         const std::string& gcode_path,
                                         int                attempt)
{
    json body;
    body["defect"]  = defect;
    body["attempt"] = attempt;
    if (!gcode_path.empty())
        body["gcode_path"] = gcode_path;
    return request("POST", "/thox/revise/plan", body.dump(), 20);
}

ThoxResult ThoxAgentClient::applyRevision(const std::string& defect,
                                          const std::string& gcode_path,
                                          int                attempt)
{
    json body;
    body["defect"]     = defect;
    body["gcode_path"] = gcode_path;
    body["attempt"]    = attempt;
    // Rewriting a large G-code file can take a while.
    return request("POST", "/thox/revise/apply", body.dump(), 120);
}

static json scan_body(const ThoxAgentClient::ScanParams& params)
{
    json body;
    body["center_x_mm"]         = params.center_x_mm;
    body["center_y_mm"]         = params.center_y_mm;
    body["footprint_radius_mm"] = params.footprint_radius_mm;
    body["object_height_mm"]    = params.object_height_mm;
    body["stations"]            = params.stations;
    body["azimuths"]            = params.azimuths;
    body["make_tray"]           = params.make_tray;
    return body;
}

ThoxResult ThoxAgentClient::planScan(const ScanParams& params)
{
    return request("POST", "/thox/scan/plan", scan_body(params).dump(), 20);
}

ThoxResult ThoxAgentClient::runScan(const ScanParams& params)
{
    ThoxResult result = request("POST", "/thox/scan/run", scan_body(params).dump(), 30);
    if (result.success) {
        try {
            result.message = json::parse(result.body).value("job_id", "");
        } catch (const std::exception&) {
        }
    }
    return result;
}

ThoxResult ThoxAgentClient::captureReferenceLadder(const ScanParams& params)
{
    ThoxResult result =
        request("POST", "/thox/scan/reference", scan_body(params).dump(), 30);
    if (result.success) {
        try {
            result.message = json::parse(result.body).value("job_id", "");
        } catch (const std::exception&) {
        }
    }
    return result;
}

ThoxResult ThoxAgentClient::pollJob(const std::string& job_id)
{
    return request("GET", "/thox/jobs/" + job_id, "", 10);
}

}} // namespace Slic3r::GUI
