#include "AIPhotoTo3DDialog.hpp"
#include "AIPipelineClient.hpp"
#include "Plater.hpp"
#include "GUI_App.hpp"
#include "MainFrame.hpp"
#include "GUI_ObjectList.hpp"
#include "format.hpp"
#include "I18N.hpp"
#include "wxExtensions.hpp"

#include <wx/filedlg.h>
#include <wx/dirdlg.h>
#include <wx/dnd.h>
#include <wx/busyinfo.h>
#include <wx/sizer.h>
#include <wx/stattext.h>
#include <wx/statline.h>
#include <wx/listctrl.h>
#include <wx/choice.h>
#include <wx/spinctrl.h>
#include <wx/checkbox.h>
#include <wx/gauge.h>
#include <wx/button.h>
#include <wx/hyperlink.h>
#include <wx/filedlg.h>
#include <wx/wfstream.h>
#include <wx/protocol/http.h>
#include <wx/sstream.h>
#include <wx/url.h>
#include <wx/busyinfo.h>
#include <wx/ffile.h>
#include <wx/dir.h>
#include <wx/image.h>
#include <wx/artprov.h>

#include <nlohmann/json.hpp>
#include <utility>

namespace Slic3r {
namespace GUI {

// ---------------------------------------------------------------------------
// Drop target for image files
// ---------------------------------------------------------------------------
class ImageDropTarget : public wxFileDropTarget
{
public:
    ImageDropTarget(AIPhotoTo3DDialog* dialog) : m_dialog(dialog) {}

    bool OnDropFiles(wxCoord x, wxCoord y, const wxArrayString& filenames) override
    {
        wxArrayString image_files;
        for (const auto& f : filenames) {
            wxString ext = wxFileName(f).GetExt().Lower();
            if (ext == "png" || ext == "jpg" || ext == "jpeg" || ext == "bmp" || ext == "webp" || ext == "tiff") {
                image_files.Add(f);
            }
        }
        if (image_files.IsEmpty()) {
            wxMessageBox(_L("Only image files (PNG, JPG, JPEG, BMP, WEBP, TIFF) are supported."),
                        _L("Unsupported file type"), wxICON_WARNING);
            return false;
        }
        // Can't call m_dialog->add_image_files directly because it's private
        // But we can use a custom event
        wxCommandEvent evt(wxEVT_COMMAND_TEXT_UPDATED, m_dialog->GetId());
        evt.SetString(image_files[0]);  // Simplified: send first file
        wxPostEvent(m_dialog, evt);
        return true;
    }

private:
    AIPhotoTo3DDialog* m_dialog;
};

// ---------------------------------------------------------------------------
// AIPhotoTo3DDialog implementation
// ---------------------------------------------------------------------------

BEGIN_EVENT_TABLE(AIPhotoTo3DDialog, wxDialog)
    EVT_BUTTON(wxID_ADD, &AIPhotoTo3DDialog::on_add_images)
    EVT_BUTTON(wxID_REMOVE, &AIPhotoTo3DDialog::on_remove_image)
    EVT_BUTTON(wxID_CLEAR, &AIPhotoTo3DDialog::on_clear_images)
    EVT_BUTTON(wxID_OK, &AIPhotoTo3DDialog::on_generate)
    EVT_BUTTON(wxID_CANCEL, &AIPhotoTo3DDialog::on_cancel)
    EVT_BUTTON(wxID_APPLY, &AIPhotoTo3DDialog::on_import_to_plater)
    EVT_BUTTON(wxID_SAVE, &AIPhotoTo3DDialog::on_save_as)
    EVT_CHOICE(wxID_HIGHEST + 1, &AIPhotoTo3DDialog::on_backend_changed)
    EVT_CHOICE(wxID_HIGHEST + 2, &AIPhotoTo3DDialog::on_quality_changed)
    EVT_TIMER(wxID_HIGHEST + 3, &AIPhotoTo3DDialog::on_timer)
    EVT_CLOSE(&AIPhotoTo3DDialog::on_close)
END_EVENT_TABLE()

AIPhotoTo3DDialog::AIPhotoTo3DDialog(wxWindow* parent, Plater* plater)
    : wxDialog(parent, wxID_ANY, _L("AI Photo-to-3D Mesh Generator"),
               wxDefaultPosition, wxSize(800, 700),
               wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER | wxMAXIMIZE_BOX),
      m_plater(plater), m_timer(this, wxID_HIGHEST + 3)
{
    SetIcon(wxIcon(wxStandardPaths::Get().GetResourcesDir() + "/icons/QIDIStudio.ico", wxBITMAP_TYPE_ICO));

    build();

    // Check server status
    m_server_running = IsServerRunning();
    if (!m_server_running) {
        m_status_text->SetLabel(_L("AI server not detected. Click 'Start Server' or run start_server.sh"));
        m_server_link->SetLabel(_L("Start AI Pipeline Server..."));
    } else {
        m_status_text->SetLabel(_L("AI server ready. Add photos and click Generate."));
        m_server_link->SetLabel(_L("AI Server: Running"));
    }

    // Enable drag-and-drop
    SetDropTarget(new ImageDropTarget(this));

    Fit();
    CenterOnParent();
}

AIPhotoTo3DDialog::~AIPhotoTo3DDialog()
{
    m_cancel_requested = true;
    if (m_worker_thread.joinable()) {
        m_worker_thread.join();
    }
    m_timer.Stop();
    if (!m_last_result.output_path.empty()) {
        wxRemoveFile(wxString::FromUTF8(m_last_result.output_path));
    }
}

// ---------------------------------------------------------------------------
// UI Construction
// ---------------------------------------------------------------------------

void AIPhotoTo3DDialog::build()
{
    auto* main_sizer = new wxBoxSizer(wxVERTICAL);

    // Title
    auto* title = new wxStaticText(this, wxID_ANY,
        _L("AI Photo-to-3D Mesh Generator"),
        wxDefaultPosition, wxDefaultSize, wxALIGN_CENTER);
    title->SetFont(wxFont(14, wxFONTFAMILY_DEFAULT, wxFONTSTYLE_NORMAL, wxFONTWEIGHT_BOLD));
    main_sizer->Add(title, 0, wxALL | wxALIGN_CENTER, 10);

    auto* subtitle = new wxStaticText(this, wxID_ANY,
        _L("Convert photos of physical devices into watertight 3D-printable meshes using AI"),
        wxDefaultPosition, wxDefaultSize, wxALIGN_CENTER);
    subtitle->SetFont(wxFont(9, wxFONTFAMILY_DEFAULT, wxFONTSTYLE_ITALIC, wxFONTWEIGHT_NORMAL));
    subtitle->SetForegroundColour(wxColour(120, 120, 120));
    main_sizer->Add(subtitle, 0, wxALL | wxALIGN_CENTER, 5);

    // Server link
    m_server_link = new wxHyperlinkCtrl(this, wxID_ANY,
        _L("AI Server: Checking..."), "http://127.0.0.1:7861",
        wxDefaultPosition, wxDefaultSize, wxHL_DEFAULT_STYLE);
    main_sizer->Add(m_server_link, 0, wxALL | wxALIGN_CENTER, 5);

    // Separator
    main_sizer->Add(new wxStaticLine(this), 0, wxEXPAND | wxALL, 5);

    // Image drop zone + list
    build_image_drop_zone(main_sizer);

    // Settings panel
    build_settings_panel(main_sizer);

    // Progress panel
    build_progress_panel(main_sizer);

    // Action buttons
    build_action_buttons(main_sizer);

    SetSizer(main_sizer);
    Layout();
}

void AIPhotoTo3DDialog::build_image_drop_zone(wxSizer* sizer)
{
    auto* img_sizer = new wxStaticBoxSizer(wxVERTICAL, this, _L("Input Photos"));
    auto* box = img_sizer->GetStaticBox();

    // Image list
    m_image_list = new wxListView(box, wxID_ANY, wxDefaultPosition,
        wxSize(750, 150), wxLC_REPORT | wxLC_SINGLE_SEL);
    m_image_list->AppendColumn("#", wxLIST_FORMAT_LEFT, 40);
    m_image_list->AppendColumn("Filename", wxLIST_FORMAT_LEFT, 300);
    m_image_list->AppendColumn("Path", wxLIST_FORMAT_LEFT, 400);
    img_sizer->Add(m_image_list, 1, wxALL | wxEXPAND, 5);

    // Buttons row
    auto* btn_sizer = new wxBoxSizer(wxHORIZONTAL);
    m_add_images_btn = new wxButton(box, wxID_ADD, _L("Add Photos..."));
    m_remove_btn = new wxButton(box, wxID_REMOVE, _L("Remove"));
    m_clear_btn = new wxButton(box, wxID_CLEAR, _L("Clear All"));
    btn_sizer->Add(m_add_images_btn, 0, wxALL, 5);
    btn_sizer->Add(m_remove_btn, 0, wxALL, 5);
    btn_sizer->Add(m_clear_btn, 0, wxALL, 5);
    btn_sizer->AddStretchSpacer();
    auto* hint = new wxStaticText(box, wxID_ANY,
        _L("Drag & drop image files here, or click Add Photos"));
    btn_sizer->Add(hint, 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    img_sizer->Add(btn_sizer, 0, wxEXPAND | wxALL, 5);

    sizer->Add(img_sizer, 1, wxEXPAND | wxALL, 5);
}

void AIPhotoTo3DDialog::build_settings_panel(wxSizer* sizer)
{
    auto* settings_box = new wxStaticBoxSizer(wxVERTICAL, this, _L("Generation Settings"));
    auto* box = settings_box->GetStaticBox();

    auto* grid = new wxFlexGridSizer(2, 5, 5);
    grid->AddGrowableCol(1, 1);

    // Backend selector
    grid->Add(new wxStaticText(box, wxID_ANY, _L("AI Backend:")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_backend_choice = new wxChoice(box, wxID_HIGHEST + 1);
    m_backend_choice->Append(_L("Auto (TRELLIS with TripoSR fallback)"));
    m_backend_choice->Append(_L("TRELLIS (High Quality, slower)"));
    m_backend_choice->Append(_L("TripoSR (Fast, lower quality)"));
    m_backend_choice->SetSelection(0);
    grid->Add(m_backend_choice, 1, wxALL | wxEXPAND, 5);

    // Quality preset
    grid->Add(new wxStaticText(box, wxID_ANY, _L("Quality:")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_quality_choice = new wxChoice(box, wxID_HIGHEST + 2);
    m_quality_choice->Append(_L("Draft (quick preview)"));
    m_quality_choice->Append(_L("Medium"));
    m_quality_choice->Append(_L("High (recommended for devices)"));
    m_quality_choice->Append(_L("Ultra (maximum detail)"));
    m_quality_choice->SetSelection(2);
    grid->Add(m_quality_choice, 1, wxALL | wxEXPAND, 5);

    // Output format
    grid->Add(new wxStaticText(box, wxID_ANY, _L("Output Format:")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_format_choice = new wxChoice(box, wxID_ANY);
    m_format_choice->Append("STL");
    m_format_choice->Append("OBJ");
    m_format_choice->Append("GLB");
    m_format_choice->Append("3MF");
    m_format_choice->SetSelection(0);
    grid->Add(m_format_choice, 1, wxALL | wxEXPAND, 5);

    // Seed
    grid->Add(new wxStaticText(box, wxID_ANY, _L("Random Seed:")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_seed_ctrl = new wxSpinCtrl(box, wxID_ANY, "1", wxDefaultPosition, wxDefaultSize, wxSP_ARROW_KEYS, 0, 2147483647, 1);
    grid->Add(m_seed_ctrl, 1, wxALL | wxEXPAND, 5);

    // Max faces
    grid->Add(new wxStaticText(box, wxID_ANY, _L("Max Faces:")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_max_faces_ctrl = new wxSpinCtrl(box, wxID_ANY, "500000", wxDefaultPosition, wxDefaultSize, wxSP_ARROW_KEYS, 1000, 5000000, 500000);
    grid->Add(m_max_faces_ctrl, 1, wxALL | wxEXPAND, 5);

    // Target dimensions
    grid->Add(new wxStaticText(box, wxID_ANY, _L("Target Width (mm):")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_width_ctrl = new wxSpinCtrl(box, wxID_ANY, "0", wxDefaultPosition, wxDefaultSize, wxSP_ARROW_KEYS, 0, 10000, 0);
    grid->Add(m_width_ctrl, 1, wxALL | wxEXPAND, 5);

    grid->Add(new wxStaticText(box, wxID_ANY, _L("Target Depth (mm):")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_depth_ctrl = new wxSpinCtrl(box, wxID_ANY, "0", wxDefaultPosition, wxDefaultSize, wxSP_ARROW_KEYS, 0, 10000, 0);
    grid->Add(m_depth_ctrl, 1, wxALL | wxEXPAND, 5);

    grid->Add(new wxStaticText(box, wxID_ANY, _L("Target Height (mm):")), 0, wxALL | wxALIGN_CENTER_VERTICAL, 5);
    m_height_ctrl = new wxSpinCtrl(box, wxID_ANY, "0", wxDefaultPosition, wxDefaultSize, wxSP_ARROW_KEYS, 0, 10000, 0);
    grid->Add(m_height_ctrl, 1, wxALL | wxEXPAND, 5);

    settings_box->Add(grid, 0, wxEXPAND | wxALL, 5);

    // Checkboxes
    auto* chk_sizer = new wxBoxSizer(wxHORIZONTAL);
    m_flatten_check = new wxCheckBox(box, wxID_ANY, _L("Flatten bottom for build-plate adhesion"));
    m_flatten_check->SetValue(true);
    m_remove_bg_check = new wxCheckBox(box, wxID_ANY, _L("Auto-remove image background"));
    m_remove_bg_check->SetValue(true);
    chk_sizer->Add(m_flatten_check, 0, wxALL, 5);
    chk_sizer->Add(m_remove_bg_check, 0, wxALL, 5);
    settings_box->Add(chk_sizer, 0, wxEXPAND | wxALL, 5);

    sizer->Add(settings_box, 0, wxEXPAND | wxALL, 5);
}

void AIPhotoTo3DDialog::build_progress_panel(wxSizer* sizer)
{
    auto* prog_box = new wxStaticBoxSizer(wxVERTICAL, this, _L("Progress"));
    auto* box = prog_box->GetStaticBox();

    m_progress_bar = new wxGauge(box, wxID_ANY, 100, wxDefaultPosition, wxSize(750, 20), wxGA_HORIZONTAL);
    prog_box->Add(m_progress_bar, 0, wxALL | wxEXPAND, 5);

    m_status_text = new wxStaticText(box, wxID_ANY, _L("Ready"), wxDefaultPosition, wxDefaultSize);
    m_status_text->SetFont(wxFont(9, wxFONTFAMILY_DEFAULT, wxFONTSTYLE_NORMAL, wxFONTWEIGHT_NORMAL));
    prog_box->Add(m_status_text, 0, wxALL | wxEXPAND, 5);

    sizer->Add(prog_box, 0, wxEXPAND | wxALL, 5);
}

void AIPhotoTo3DDialog::build_action_buttons(wxSizer* sizer)
{
    auto* btn_sizer = new wxBoxSizer(wxHORIZONTAL);

    m_generate_btn = new wxButton(this, wxID_OK, _L("Generate 3D Mesh"));
    m_generate_btn->SetFont(wxFont(10, wxFONTFAMILY_DEFAULT, wxFONTSTYLE_NORMAL, wxFONTWEIGHT_BOLD));
    m_cancel_btn = new wxButton(this, wxID_CANCEL, _L("Cancel"));
    m_cancel_btn->Enable(false);
    m_import_btn = new wxButton(this, wxID_APPLY, _L("Import to Plate"));
    m_import_btn->Enable(false);
    m_save_btn = new wxButton(this, wxID_SAVE, _L("Save As..."));
    m_save_btn->Enable(false);

    btn_sizer->Add(m_generate_btn, 0, wxALL, 5);
    btn_sizer->Add(m_cancel_btn, 0, wxALL, 5);
    btn_sizer->AddStretchSpacer();
    btn_sizer->Add(m_save_btn, 0, wxALL, 5);
    btn_sizer->Add(m_import_btn, 0, wxALL, 5);

    sizer->Add(btn_sizer, 0, wxEXPAND | wxALL, 5);
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

void AIPhotoTo3DDialog::on_add_images(wxCommandEvent& event)
{
    wxFileDialog dlg(this, _L("Select Photos"), "", "",
        _L("Image files (*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.tiff)|*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.tiff|All files (*.*)|*.*"),
        wxFD_OPEN | wxFD_MULTIPLE | wxFD_FILE_MUST_EXIST);

    if (dlg.ShowModal() == wxID_OK) {
        wxArrayString paths;
        dlg.GetPaths(paths);
        add_image_files(paths);
    }
}

void AIPhotoTo3DDialog::on_remove_image(wxCommandEvent& event)
{
    long sel = m_image_list->GetFirstSelected();
    if (sel >= 0 && sel < (long)m_images.size()) {
        m_images.erase(m_images.begin() + sel);
        update_image_list();
    }
}

void AIPhotoTo3DDialog::on_clear_images(wxCommandEvent& event)
{
    m_images.clear();
    update_image_list();
}

void AIPhotoTo3DDialog::on_generate(wxCommandEvent& event)
{
    if (m_images.empty()) {
        wxMessageBox(_L("Please add at least one photo first."), _L("No images"), wxICON_WARNING);
        return;
    }

    if (!m_server_running && !IsServerRunning()) {
        auto result = wxMessageBox(
            _L("AI Pipeline Server is not running.\n\nWould you like to start it now?\n"
               "This requires Python 3.10+ with CUDA support."),
            _L("Server Not Running"),
            wxYES_NO | wxICON_QUESTION);
        if (result == wxYES) {
            LaunchServer();
            // Wait for server
            m_status_text->SetLabel(_L("Waiting for AI server to start..."));
            m_progress_bar->SetValue(10);
            for (int i = 0; i < 30; i++) {  // 30 seconds timeout
                wxMillisecondSleep(1000);
                if (IsServerRunning()) {
                    m_server_running = true;
                    break;
                }
                m_progress_bar->SetValue(10 + i * 2);
            }
            if (!m_server_running) {
                wxMessageBox(_L("Failed to start AI server. Please run start_server.sh manually."),
                             _L("Server Error"), wxICON_ERROR);
                m_progress_bar->SetValue(0);
                return;
            }
        } else {
            return;
        }
    }

    start_generation();
}

void AIPhotoTo3DDialog::on_cancel(wxCommandEvent& event)
{
    m_cancel_requested = true;
    m_status_text->SetLabel(_L("Cancelling..."));
}

void AIPhotoTo3DDialog::on_import_to_plater(wxCommandEvent& event)
{
    if (!m_last_result.success || m_last_result.output_path.empty()) {
        return;
    }
    import_to_plater(m_last_result.output_path);
    EndModal(wxID_APPLY);
}

void AIPhotoTo3DDialog::on_save_as(wxCommandEvent& event)
{
    if (!m_last_result.success || m_last_result.output_path.empty()) {
        return;
    }

    wxString wildcard;
    wxString ext;
    switch (m_params.format) {
        case OutputFormat::STL:    wildcard = "STL files (*.stl)|*.stl"; ext = "stl"; break;
        case OutputFormat::OBJ:    wildcard = "OBJ files (*.obj)|*.obj"; ext = "obj"; break;
        case OutputFormat::GLB:    wildcard = "GLB files (*.glb)|*.glb"; ext = "glb"; break;
        case OutputFormat::ThreeMF: wildcard = "3MF files (*.3mf)|*.3mf"; ext = "3mf"; break;
    }

    wxFileDialog dlg(this, _L("Save 3D Mesh"), "", "",
        wildcard, wxFD_SAVE | wxFD_OVERWRITE_PROMPT);
    if (dlg.ShowModal() == wxID_OK) {
        wxString target = dlg.GetPath();
        wxCopyFile(m_last_result.output_path, target);
    }
}

void AIPhotoTo3DDialog::on_backend_changed(wxCommandEvent& event)
{
    int sel = m_backend_choice->GetSelection();
    m_params.backend = static_cast<Backend>(sel == 0 ? 2 : sel - 1);  // 0=Auto, 1=TRELLIS, 2=TripoSR
}

void AIPhotoTo3DDialog::on_quality_changed(wxCommandEvent& event)
{
    int sel = m_quality_choice->GetSelection();
    m_params.quality = static_cast<Quality>(sel);
}

void AIPhotoTo3DDialog::on_timer(wxTimerEvent& event)
{
    m_progress_bar->SetValue(m_progress_percent.load());
    if (!m_progress_message.empty()) {
        m_status_text->SetLabel(wxString::FromUTF8(m_progress_message));
    }
}

void AIPhotoTo3DDialog::on_close(wxCloseEvent& event)
{
    m_cancel_requested = true;
    if (m_worker_thread.joinable()) {
        m_worker_thread.join();
    }
    event.Skip();
}

// ---------------------------------------------------------------------------
// Image management
// ---------------------------------------------------------------------------

void AIPhotoTo3DDialog::add_image_files(const wxArrayString& paths)
{
    for (const auto& path : paths) {
        ImageEntry entry;
        entry.path = path.ToUTF8().data();
        entry.name = wxFileName(path).GetFullName().ToUTF8().data();

        // Generate thumbnail
        wxImage img;
        if (img.LoadFile(path, wxBITMAP_TYPE_ANY)) {
            img.Rescale(64, 64, wxIMAGE_QUALITY_HIGH);
            entry.thumbnail = wxBitmap(img);
        }

        m_images.push_back(entry);
    }
    update_image_list();
}

void AIPhotoTo3DDialog::update_image_list()
{
    m_image_list->DeleteAllItems();
    for (size_t i = 0; i < m_images.size(); i++) {
        long idx = m_image_list->InsertItem(i, wxString::Format("%d", (int)i + 1));
        m_image_list->SetItem(idx, 1, wxString::FromUTF8(m_images[i].name));
        m_image_list->SetItem(idx, 2, wxString::FromUTF8(m_images[i].path));
    }
    m_generate_btn->Enable(!m_images.empty());
}

// ---------------------------------------------------------------------------
// Generation
// ---------------------------------------------------------------------------

void AIPhotoTo3DDialog::start_generation()
{
    // Gather params from UI
    int backend_sel = m_backend_choice->GetSelection();
    m_params.backend = (backend_sel == 0) ? Backend::Auto :
                       (backend_sel == 1) ? Backend::TRELLIS : Backend::TripoSR;
    m_params.quality = static_cast<Quality>(m_quality_choice->GetSelection());
    m_params.seed = m_seed_ctrl->GetValue();
    m_params.flatten_bottom = m_flatten_check->GetValue();
    m_params.remove_bg = m_remove_bg_check->GetValue();
    m_params.max_faces = m_max_faces_ctrl->GetValue();
    m_params.width_mm = m_width_ctrl->GetValue();
    m_params.depth_mm = m_depth_ctrl->GetValue();
    m_params.height_mm = m_height_ctrl->GetValue();

    int fmt_sel = m_format_choice->GetSelection();
    m_params.format = static_cast<OutputFormat>(fmt_sel);

    // UI state
    m_generating = true;
    m_cancel_requested = false;
    m_progress_percent = 0;
    m_progress_message = "Starting generation...";
    m_generate_btn->Enable(false);
    m_cancel_btn->Enable(true);
    m_import_btn->Enable(false);
    m_save_btn->Enable(false);
    m_progress_bar->SetValue(0);
    m_timer.Start(100);

    // Start worker thread
    if (m_worker_thread.joinable()) {
        m_worker_thread.join();
    }
    if (!m_last_result.output_path.empty()) {
        wxRemoveFile(wxString::FromUTF8(m_last_result.output_path));
        m_last_result = {};
    }
    m_worker_thread = std::thread(&AIPhotoTo3DDialog::generation_thread_fn, this);
}

void AIPhotoTo3DDialog::generation_thread_fn()
{
    update_progress("Preparing images...", 5);

    // Collect image paths
    std::vector<std::string> image_paths;
    for (const auto& img : m_images) {
        image_paths.push_back(img.path);
    }

    update_progress("Sending images to AI server...", 10);

    // Send request
    GenerateResult result = send_generate_request(m_params, image_paths);

    if (m_cancel_requested) {
        m_progress_message = "Cancelled";
        m_progress_percent = 0;
    } else if (result.success) {
        m_progress_message = wxString::Format(
            "Done! %d verts, %d faces, watertight=%s (%.1fs)",
            result.vertex_count, result.face_count,
            result.is_watertight ? "yes" : "no",
            result.elapsed_seconds
        ).ToUTF8().data();
        m_progress_percent = 100;
    } else {
        m_progress_message = "Error: " + result.error_message;
        m_progress_percent = 0;
    }

    m_last_result = result;
    m_generating = false;

    // Stop timer on main thread
    CallAfter([this]() {
        m_timer.Stop();
        m_progress_bar->SetValue(m_progress_percent.load());
        m_status_text->SetLabel(wxString::FromUTF8(m_progress_message));
        m_generate_btn->Enable(true);
        m_cancel_btn->Enable(false);

        if (m_last_result.success) {
            m_import_btn->Enable(true);
            m_save_btn->Enable(true);
        }
    });
}

void AIPhotoTo3DDialog::update_progress(const std::string& message, int percent)
{
    m_progress_message = message;
    m_progress_percent = percent;
}

// ---------------------------------------------------------------------------
// Server communication
// ---------------------------------------------------------------------------

AIPhotoTo3DDialog::GenerateResult
AIPhotoTo3DDialog::send_generate_request(const GenerateParams& params,
                                          const std::vector<std::string>& image_paths)
{
    GenerateResult result;
    AIGenerateParams request_params;
    request_params.backend = backend_to_string(params.backend);
    request_params.quality = quality_to_string(params.quality);
    request_params.seed = params.seed;
    request_params.flatten_bottom = params.flatten_bottom;
    request_params.remove_bg = params.remove_bg;
    request_params.max_faces = params.max_faces;
    request_params.format = format_to_string(params.format);
    request_params.width_mm = params.width_mm;
    request_params.depth_mm = params.depth_mm;
    request_params.height_mm = params.height_mm;
    request_params.server_url = params.server_url;

    AIPipelineClient client(params.server_url);
    AIGenerateResult client_result = client.generate(
        image_paths,
        request_params,
        [this](const std::string& message, int percent) {
            update_progress(message, percent);
        },
        &m_cancel_requested
    );

    result.success = client_result.success;
    result.error_message = std::move(client_result.error_message);
    result.output_path = std::move(client_result.output_path);
    result.vertex_count = client_result.vertex_count;
    result.face_count = client_result.face_count;
    result.is_watertight = client_result.is_watertight;
    result.is_manifold = client_result.is_manifold;
    result.backend_used = std::move(client_result.backend_used);
    result.elapsed_seconds = client_result.elapsed_seconds;
    return result;
}

// ---------------------------------------------------------------------------
// Import to plater
// ---------------------------------------------------------------------------

void AIPhotoTo3DDialog::import_to_plater(const std::string& mesh_path)
{
    if (!m_plater) {
        wxLogError("Plater not available");
        return;
    }

    // Use the Plater's load_model method
    // This will add the mesh as a new model object on the plate
    wxArrayString paths;
    paths.Add(wxString::FromUTF8(mesh_path));
    m_plater->load_files(paths);

    wxLogMessage("ThoxForge: Imported AI-generated mesh to plater: %s", mesh_path);
}

// ---------------------------------------------------------------------------
// Server management
// ---------------------------------------------------------------------------

bool AIPhotoTo3DDialog::IsServerRunning(const std::string& url)
{
    // Check if the AI pipeline server is accessible via HTTP GET /health
    // Uses a simple socket connection
    wxURL health_url(url + "/health");
    if (health_url.GetError() != wxURL_NOERR) {
        return false;
    }

    // Try to connect with a short timeout
    wxHTTP* http = static_cast<wxHTTP*>(&health_url.GetProtocol());
    http->SetTimeout(2);  // 2 second timeout

    try {
        wxInputStream* stream = health_url.GetInputStream();
        if (stream && stream->IsOk()) {
            wxString response;
            wxStringOutputStream out(&response);
            stream->Read(out);
            delete stream;
            return response.Contains("ok");
        }
    } catch (...) {
        return false;
    }

    return false;
}

bool AIPhotoTo3DDialog::LaunchServer(const std::string& script_path)
{
    // Look for the AI pipeline start script
    wxString script = wxString::FromUTF8(script_path);
    if (script.IsEmpty()) {
        // Try common locations
        wxArrayString search_paths;
        wxString app_dir = wxStandardPaths::Get().GetResourcesDir();
        search_paths.Add(app_dir + "/ai_pipeline/start_server.sh");
        search_paths.Add(app_dir + "/../ai_pipeline/start_server.sh");
        search_paths.Add(wxGetHomeDir() + "/thox-forge/QidiStudio-AI/ai_pipeline/start_server.sh");

        for (const auto& p : search_paths) {
            if (wxFileExists(p)) {
                script = p;
                break;
            }
        }
    }

    if (script.IsEmpty()) {
        wxLogError("ThoxForge: Could not find start_server.sh");
        return false;
    }

    // Launch directly in the background. Depending on xterm made the button
    // fail on a standard macOS installation.
    const wxString cmd = "bash \"" + script + "\" --preload";
    return wxExecute(cmd, wxEXEC_ASYNC) != 0;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

std::string AIPhotoTo3DDialog::backend_to_string(Backend b)
{
    switch (b) {
        case Backend::TRELLIS: return "trellis";
        case Backend::TripoSR: return "triposr";
        case Backend::Auto:    return "auto";
    }
    return "auto";
}

std::string AIPhotoTo3DDialog::quality_to_string(Quality q)
{
    switch (q) {
        case Quality::Draft: return "draft";
        case Quality::Medium: return "medium";
        case Quality::High:   return "high";
        case Quality::Ultra:  return "ultra";
    }
    return "high";
}

std::string AIPhotoTo3DDialog::format_to_string(OutputFormat f)
{
    switch (f) {
        case OutputFormat::STL:    return "stl";
        case OutputFormat::OBJ:    return "obj";
        case OutputFormat::GLB:    return "glb";
        case OutputFormat::ThreeMF: return "3mf";
    }
    return "stl";
}

}} // namespace Slic3r::GUI
