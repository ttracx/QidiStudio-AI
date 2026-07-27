#ifndef slic3r_AIPipelineClient_hpp_
#define slic3r_AIPipelineClient_hpp_

#include <string>
#include <vector>
#include <functional>
#include <atomic>

namespace Slic3r {
namespace GUI {

/**
 * AIPipelineClient
 * ================
 * HTTP client for communicating with the ThoxForge AI Pipeline Server.
 * Uses libcurl (already a QidiStudio dependency) for multipart file upload
 * and binary download.
 *
 * Endpoints:
 *   GET  /health           → {status, backends, active_backend, cuda_available}
 *   GET  /backends         → {active_backend, available[]}
 *   POST /backends         → Set active backend
 *   POST /generate         → Multipart upload → binary mesh file response
 *   POST /generate_json    → JSON body → JSON response with base64 mesh
 */

struct AIGenerateParams {
    std::string backend        = "auto";      // "trellis", "triposr", "auto"
    std::string quality        = "high";      // "draft", "medium", "high", "ultra"
    int         seed           = 1;
    bool        flatten_bottom = true;
    bool        remove_bg      = true;
    int         max_faces      = 500000;
    std::string format         = "stl";       // "stl", "obj", "glb", "3mf"
    double      width_mm       = 0.0;
    double      depth_mm       = 0.0;
    double      height_mm      = 0.0;
    std::string server_url     = "http://127.0.0.1:7861";
};

struct AIGenerateResult {
    bool        success        = false;
    std::string error_message;
    std::string output_path;        // Temp file path with downloaded mesh
    int         vertex_count   = 0;
    int         face_count     = 0;
    bool        is_watertight  = false;
    bool        is_manifold    = false;
    std::string backend_used;
    double      elapsed_seconds = 0.0;
};

class AIPipelineClient
{
public:
    using ProgressCallback = std::function<void(const std::string& message, int percent)>;

    AIPipelineClient(const std::string& server_url = "http://127.0.0.1:7861");
    ~AIPipelineClient();

    // Check if server is running
    bool isServerRunning();

    // Get server health/info
    std::string getHealth();

    // Generate mesh from images
    // image_paths: local file paths to images
    // params: generation parameters
    // progress_cb: optional progress callback (called from worker thread)
    // cancel_flag: atomic flag, set to true to cancel
    AIGenerateResult generate(
        const std::vector<std::string>& image_paths,
        const AIGenerateParams& params,
        ProgressCallback progress_cb = nullptr,
        std::atomic<bool>* cancel_flag = nullptr
    );

private:
    std::string m_server_url;

    // libcurl helpers
    static size_t writeCallback(void* contents, size_t size, size_t nmemb, void* userp);
    static size_t headerCallback(void* contents, size_t size, size_t nmemb, void* userp);
};

}} // namespace Slic3r::GUI

#endif // slic3r_AIPipelineClient_hpp_