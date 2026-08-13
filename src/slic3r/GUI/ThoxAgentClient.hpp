#ifndef slic3r_ThoxAgentClient_hpp_
#define slic3r_ThoxAgentClient_hpp_

#include <string>
#include <vector>
#include <map>
#include <functional>

namespace Slic3r {
namespace GUI {

/**
 * ThoxAgentClient
 * ===============
 * HTTP client for the THOX printer-agent layer, served by the same Python
 * sidecar as the photo-to-3D pipeline (see AIPipelineClient) under /thox.
 *
 * Two capabilities:
 *   Print health - watch a running job with a parallel vision ensemble and
 *                  pause / resume / cancel / reprint through Moonraker.
 *   Scan to print - drive the bed as a calibrated stage, reconstruct the object
 *                  on it, and emit a printable plate.
 *
 * Endpoints:
 *   GET  /thox/health          -> layer status, autonomy, vision providers
 *   GET  /thox/printer         -> printer state, cameras, legal actions
 *   POST /thox/monitor/start   -> begin watching the running job
 *   POST /thox/monitor/stop
 *   GET  /thox/monitor/state   -> latest verdict, suspicions, thresholds
 *   GET  /thox/monitor/frame   -> most recent analyzed JPEG
 *   GET  /thox/events?since=N  -> incremental event log
 *   POST /thox/control/<action>-> pause | resume | cancel | reprint
 *   POST /thox/revise/plan     -> parameter changes for a diagnosed defect
 *   POST /thox/revise/apply    -> write a revised G-code
 *   POST /thox/scan/plan       -> preview a sweep (moves nothing)
 *   POST /thox/scan/run        -> run a scan (returns a job id)
 *   GET  /thox/jobs/<id>       -> poll a background job
 *
 * IMPORTANT - refusals are not failures.
 * The server answers HTTP 409 with a machine-readable `reason` when the
 * interlock declines: a print is running, Z is unhomed, the hotend is hot, or
 * the configured autonomy does not let the agent act on its own. In every one
 * of those cases the printer was NOT touched and the operator can clear the
 * condition. ThoxResult exposes that as `refused` + `reason` so the UI can say
 * "finish your print first" instead of showing an error dialog.
 *
 * All calls are blocking and must be made off the wxWidgets main thread.
 */

struct ThoxResult
{
    bool        success = false;
    // True when the server refused (HTTP 409). The printer was not touched.
    bool        refused = false;
    // Machine-readable refusal code, e.g. "printer_busy", "not_homed",
    // "too_hot", "not_permitted", "cooldown".
    std::string reason;
    // Human-readable message, safe to show an operator.
    std::string message;
    // Raw JSON body, for callers that need more than success/message.
    std::string body;
    long        http_status = 0;
};

// One defect the ensemble reported.
struct ThoxDetection
{
    std::string kind;          // "spaghetti", "adhesion", ...
    std::string label;         // human-readable
    std::string urgency;       // "critical" | "serious" | "cosmetic"
    double      confidence = 0.0;
    double      severity   = 0.0;
    std::string note;
    // Normalized 0..1 box, origin top-left. Empty when the provider gave none.
    std::vector<double> bbox_norm;
};

// A snapshot of what the monitor currently believes.
struct ThoxHealthState
{
    bool        running          = false;
    std::string autonomy;              // "observe" | "suggest" | "auto_pause"
    std::string printer_state;         // "printing", "paused", ...
    std::string watching;              // filename of the job being watched
    int         samples          = 0;
    double      progress         = 0.0;
    int         current_layer    = -1;
    int         total_layers     = -1;
    double      severity         = 0.0;
    double      confidence       = 0.0;
    std::string urgency;
    std::string summary;
    std::string camera_fault;          // non-empty when frames are unusable
    std::string last_error;
    bool        has_classifier   = false;  // false = tripwire only, no VLM
    std::vector<std::string>   active_providers;
    std::vector<ThoxDetection> detections;
    // Kinds seen but not yet confirmed, with progress toward confirmation.
    std::vector<std::string>   suspicions;
};

// One entry from the event log.
struct ThoxEvent
{
    long long   seq = 0;
    double      at  = 0.0;
    std::string kind;     // "alert", "action_taken", "sample", ...
    std::string message;
    double      severity = 0.0;
    bool        notable  = false;
};

class ThoxAgentClient
{
public:
    explicit ThoxAgentClient(const std::string& server_url = "http://127.0.0.1:7861");
    ~ThoxAgentClient();

    void setServerUrl(const std::string& url) { m_server_url = url; }
    const std::string& serverUrl() const { return m_server_url; }

    // True when the sidecar answers and the THOX layer is registered.
    bool isAvailable();

    // Printer state, cameras and which actions are currently legal.
    ThoxResult getPrinter();

    // MJPEG stream URL for the printer's camera, or empty if none.
    // Read from /thox/printer so the port is whatever actually serves frames -
    // on the Q2 that is port 80, NOT Moonraker's 7125, which 404s.
    std::string cameraStreamUrl();

    // -- monitoring ---------------------------------------------------------
    ThoxResult startMonitor();
    ThoxResult stopMonitor();
    bool       getHealthState(ThoxHealthState& out);

    // Most recent analyzed frame as JPEG bytes. Empty if nothing captured yet.
    std::vector<unsigned char> getLatestFrame();

    // Events newer than `since_seq`. Pass the last seq you saw.
    bool getEvents(long long since_seq, std::vector<ThoxEvent>& out, long long& last_seq);

    // -- control ------------------------------------------------------------
    // actor is always "human" from the GUI: a button press is a human decision,
    // and passing "agent" here would subject the operator to the autonomy
    // policy meant to restrain the monitor.
    ThoxResult control(const std::string& action, const std::string& why = "");

    ThoxResult pause(const std::string& why = "")  { return control("pause", why); }
    ThoxResult resume(const std::string& why = "") { return control("resume", why); }
    ThoxResult cancel(const std::string& why = "") { return control("cancel", why); }
    ThoxResult reprint(const std::string& filename = "");

    // -- revise -------------------------------------------------------------
    // Propose parameter changes for a defect. Writes nothing.
    ThoxResult planRevision(const std::string& defect,
                            const std::string& gcode_path = "",
                            int                attempt    = 1);

    // Write a revised G-code with in-place overrides injected.
    ThoxResult applyRevision(const std::string& defect,
                             const std::string& gcode_path,
                             int                attempt = 1);

    // -- scan to print ------------------------------------------------------
    struct ScanParams
    {
        double center_x_mm         = 135.0;
        double center_y_mm         = 110.0;
        double footprint_radius_mm = 40.0;
        double object_height_mm    = 40.0;
        int    stations            = 12;
        // 1 = single pass, shape preview only. 4 = you rotate the object
        // between passes, which is the only mode whose dimensions are usable.
        int    azimuths            = 1;
        bool   make_tray           = true;
    };

    ThoxResult planScan(const ScanParams& params);
    // Returns a job id in `message`; poll with pollJob().
    ThoxResult runScan(const ScanParams& params);
    ThoxResult captureReferenceLadder(const ScanParams& params);
    ThoxResult pollJob(const std::string& job_id);

private:
    std::string m_server_url;

    ThoxResult request(const std::string& method,
                       const std::string& path,
                       const std::string& json_body = "",
                       long               timeout_s = 15);

    static size_t writeCallback(void* contents, size_t size, size_t nmemb, void* userp);
};

}} // namespace Slic3r::GUI

#endif // slic3r_ThoxAgentClient_hpp_
