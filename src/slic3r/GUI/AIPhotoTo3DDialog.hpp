#ifndef slic3r_AIPhotoTo3DDialog_hpp_
#define slic3r_AIPhotoTo3DDialog_hpp_

#include <wx/wx.h>
#include <wx/dataview.h>
#include <wx/propgrid/propgrid.h>
#include <wx/hyperlink.h>
#include <vector>
#include <thread>
#include <atomic>
#include <memory>
#include <string>

#include "GUI/ImGuiWrapper.hpp"

namespace Slic3r {
namespace GUI {

/**
 * AIPhotoTo3DDialog
 * =================
 * Dialog for converting one or more photos of a physical device into a
 * watertight (manifold) 3D-printable mesh using AI image-to-3D reconstruction.
 *
 * Architecture:
 *   ┌─────────────────────────┐     HTTP/JSON      ┌──────────────────────┐
 *   │  AIPhotoTo3DDialog (C++) │ ───────────────── │  AI Pipeline Server  │
 *   │  (wxWidgets GUI)        │ ← STL/3MF binary   │  (Python sidecar)    │
 *   │                         │                    │  TRELLIS / TripoSR   │
 *   │  ┌───────────────────┐  │                    │  + trimesh repair    │
 *   │  │ Image drop zone   │  │                    └──────────────────────┘
 *   │  │ Backend selector  │  │
 *   │  │ Quality preset    │  │
 *   │  │ Dimension inputs  │  │
 *   │  │ Progress bar      │  │
 *   │  │ Preview canvas    │  │
 *   │  │ Import / Save     │  │
 *   │  └───────────────────┘  │
 *   └─────────────────────────┘
 *
 * The dialog launches the Python sidecar if not already running, sends
 * images via multipart POST to /generate, receives a repaired STL,
 * and imports it directly into the Plater.
 */

class AIPhotoTo3DDialog : public wxDialog
{
public:
    // Backend choices
    enum class Backend {
        TRELLIS,   // High quality, slower
        TripoSR,   // Fast, lower quality
        Auto       // Try TRELLIS, fallback TripoSR
    };

    // Quality presets
    enum class Quality {
        Draft,     // Quick preview
        Medium,    // Balanced
        High,      // Default for devices
        Ultra      // Maximum detail
    };

    // Output format
    enum class OutputFormat {
        STL,
        OBJ,
        GLB,
        ThreeMF
    };

    struct GenerateParams {
        Backend         backend        = Backend::Auto;
        Quality         quality        = Quality::High;
        int             seed           = 1;
        bool            flatten_bottom = true;
        bool            remove_bg      = true;
        int             max_faces      = 500000;
        OutputFormat    format         = OutputFormat::STL;
        // Target dimensions in mm (0 = auto/keep original)
        double          width_mm       = 0.0;
        double          depth_mm       = 0.0;
        double          height_mm      = 0.0;
        // Server connection
        std::string     server_url     = "http://127.0.0.1:7861";
    };

    struct GenerateResult {
        bool            success        = false;
        std::string     error_message;
        std::string     output_path;        // Path to downloaded mesh file
        int             vertex_count  = 0;
        int             face_count     = 0;
        bool            is_watertight  = false;
        bool            is_manifold    = false;
        std::string     backend_used;
        double          elapsed_seconds = 0.0;
    };

    AIPhotoTo3DDialog(wxWindow* parent, class Plater* plater = nullptr);
    ~AIPhotoTo3DDialog();

    // Check if the AI pipeline server is running and accessible
    static bool IsServerRunning(const std::string& url = "http://127.0.0.1:7861");
    // Launch the Python sidecar server (non-blocking)
    static bool LaunchServer(const std::string& script_path = "");

private:
    // UI Construction
    void        build();
    void        build_image_drop_zone(wxSizer* sizer);
    void        build_settings_panel(wxSizer* sizer);
    void        build_progress_panel(wxSizer* sizer);
    void        build_action_buttons(wxSizer* sizer);

    // Event handlers
    void        on_add_images(wxCommandEvent& event);
    void        on_remove_image(wxCommandEvent& event);
    void        on_clear_images(wxCommandEvent& event);
    void        on_generate(wxCommandEvent& event);
    void        on_cancel(wxCommandEvent& event);
    void        on_import_to_plater(wxCommandEvent& event);
    void        on_save_as(wxCommandEvent& event);
    void        on_backend_changed(wxCommandEvent& event);
    void        on_quality_changed(wxCommandEvent& event);
    void        on_paint(wxPaintEvent& event);
    void        on_drop_files(wxDropFilesEvent& event);
    void        on_timer(wxTimerEvent& event);
    void        on_close(wxCloseEvent& event);

    // Image management
    void        add_image_files(const wxArrayString& paths);
    void        update_image_list();

    // Generation
    void        start_generation();
    void        generation_thread_fn();
    void        update_progress(const std::string& message, int percent);
    void        on_generation_complete();

    // Server communication
    GenerateResult send_generate_request(const GenerateParams& params,
                                          const std::vector<std::string>& image_paths);
    bool        download_mesh(const std::string& url,
                              const std::string& output_path,
                              GenerateResult& result);

    // Import to plater
    void        import_to_plater(const std::string& mesh_path);

    // Helpers
    static std::string backend_to_string(Backend b);
    static std::string quality_to_string(Quality q);
    static std::string format_to_string(OutputFormat f);

    // Data
    Plater*                     m_plater;
    GenerateParams              m_params;

    // Image list
    struct ImageEntry {
        std::string path;
        std::string name;
        wxBitmap    thumbnail;
    };
    std::vector<ImageEntry>    m_images;

    // UI Controls
    wxListView*                 m_image_list     = nullptr;
    wxChoice*                   m_backend_choice = nullptr;
    wxChoice*                   m_quality_choice = nullptr;
    wxCheckBox*                 m_flatten_check  = nullptr;
    wxCheckBox*                 m_remove_bg_check = nullptr;
    wxSpinCtrl*                 m_width_ctrl     = nullptr;
    wxSpinCtrl*                 m_depth_ctrl     = nullptr;
    wxSpinCtrl*                 m_height_ctrl    = nullptr;
    wxSpinCtrl*                 m_seed_ctrl      = nullptr;
    wxSpinCtrl*                 m_max_faces_ctrl = nullptr;
    wxChoice*                   m_format_choice  = nullptr;
    wxGauge*                    m_progress_bar  = nullptr;
    wxStaticText*               m_status_text   = nullptr;
    wxButton*                   m_generate_btn   = nullptr;
    wxButton*                   m_cancel_btn     = nullptr;
    wxButton*                   m_import_btn     = nullptr;
    wxButton*                   m_save_btn       = nullptr;
    wxButton*                   m_add_images_btn = nullptr;
    wxButton*                   m_remove_btn     = nullptr;
    wxButton*                   m_clear_btn      = nullptr;
    wxHyperlinkCtrl*            m_server_link   = nullptr;

    // Thread state
    std::thread                 m_worker_thread;
    std::atomic<bool>           m_generating{false};
    std::atomic<bool>           m_cancel_requested{false};
    std::atomic<int>            m_progress_percent{0};
    std::string                 m_progress_message;
    GenerateResult              m_last_result;
    wxTimer                     m_timer;

    // Server status
    bool                        m_server_running = false;
};

}} // namespace Slic3r::GUI

#endif // slic3r_AIPhotoTo3DDialog_hpp_