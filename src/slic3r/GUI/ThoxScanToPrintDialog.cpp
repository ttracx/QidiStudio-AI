#include "ThoxScanToPrintDialog.hpp"

#include <nlohmann/json.hpp>
#include <boost/log/trivial.hpp>

#include <thread>

#include "GUI_App.hpp"
#include "Plater.hpp"
#include "I18N.hpp"

namespace Slic3r {
namespace GUI {

using json = nlohmann::json;

namespace {
constexpr int POLL_INTERVAL_MS = 1500;
const wxColour WARN_TEXT(0x88, 0x66, 0x00);
const wxColour ERROR_TEXT(0xC0, 0x39, 0x2B);
} // namespace

BEGIN_EVENT_TABLE(ThoxScanToPrintDialog, wxDialog)
    EVT_TIMER(wxID_ANY, ThoxScanToPrintDialog::onTimer)
END_EVENT_TABLE()

ThoxScanToPrintDialog::ThoxScanToPrintDialog(wxWindow*          parent,
                                             Plater*            plater,
                                             const std::string& server_url)
    : wxDialog(parent, wxID_ANY, _L("Scan to Print"), wxDefaultPosition,
               wxSize(620, 640), wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER)
    , m_timer(this)
    , m_plater(plater)
    , m_client(std::make_unique<ThoxAgentClient>(server_url))
{
    buildUi();
    CenterOnParent();
}

ThoxScanToPrintDialog::~ThoxScanToPrintDialog()
{
    if (m_timer.IsRunning())
        m_timer.Stop();
    m_generation.fetch_add(1);
}

void ThoxScanToPrintDialog::buildUi()
{
    auto* root = new wxBoxSizer(wxVERTICAL);

    auto* intro = new wxStaticText(
        this, wxID_ANY,
        _L("Place a single object near the centre-front of the bed. The printer "
           "photographs it from a ladder of bed heights and reconstructs it into "
           "a printable plate."));
    intro->Wrap(560);
    root->Add(intro, 0, wxALL, 12);

    // The rig's hard limit, stated before any control.
    auto* limits = new wxStaticText(
        this, wxID_ANY,
        _L("This printer has one fixed camera and a bed that moves only in Z, so "
           "nothing rotates the object. A single pass cannot see the far side. "
           "Measured on a 40 x 25 x 15 mm test object, one pass reported 52 mm of "
           "depth against 25 mm of truth; four passes brought every axis within "
           "2 mm."));
    limits->Wrap(560);
    limits->SetForegroundColour(WARN_TEXT);
    root->Add(limits, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);

    root->Add(buildCoverageChoice(this), 0, wxEXPAND | wxLEFT | wxRIGHT, 12);
    root->Add(buildGeometryFields(this), 0, wxEXPAND | wxALL, 12);

    m_plan_summary = new wxStaticText(this, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                      wxSize(560, 40), wxST_NO_AUTORESIZE);
    root->Add(m_plan_summary, 0, wxEXPAND | wxLEFT | wxRIGHT, 12);

    m_progress = new wxGauge(this, wxID_ANY, 100, wxDefaultPosition, wxSize(-1, 12));
    root->Add(m_progress, 0, wxEXPAND | wxALL, 12);

    m_status = new wxStaticText(this, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                wxSize(560, 60), wxST_NO_AUTORESIZE);
    root->Add(m_status, 0, wxEXPAND | wxLEFT | wxRIGHT, 12);

    auto* buttons = new wxBoxSizer(wxHORIZONTAL);
    auto* preview = new wxButton(this, wxID_ANY, _L("Preview plan"));
    preview->SetToolTip(_L("Shows the Z stations that would be used. Moves nothing."));
    preview->Bind(wxEVT_BUTTON, &ThoxScanToPrintDialog::onPreviewPlan, this);

    m_reference_button = new wxButton(this, wxID_ANY, _L("Capture empty bed"));
    m_reference_button->SetToolTip(
        _L("Captures reference frames of the EMPTY bed, once per machine. "
           "Background subtraction compares against these, so an object left on "
           "the bed becomes invisible to every future scan."));
    m_reference_button->Bind(wxEVT_BUTTON, &ThoxScanToPrintDialog::onCaptureReference, this);

    m_scan_button = new wxButton(this, wxID_ANY, _L("Start scan"));
    m_scan_button->Bind(wxEVT_BUTTON, &ThoxScanToPrintDialog::onStartScan, this);

    buttons->Add(preview, 0, wxRIGHT, 8);
    buttons->Add(m_reference_button, 0, wxRIGHT, 8);
    buttons->AddStretchSpacer();
    buttons->Add(new wxButton(this, wxID_CANCEL, _L("Close")), 0, wxRIGHT, 8);
    buttons->Add(m_scan_button, 0);
    root->Add(buttons, 0, wxEXPAND | wxALL, 12);

    SetSizer(root);
    Layout();
}

wxSizer* ThoxScanToPrintDialog::buildCoverageChoice(wxWindow* parent)
{
    auto* box = new wxStaticBoxSizer(wxVERTICAL, parent, _L("Coverage"));

    m_single_pass = new wxRadioButton(
        parent, wxID_ANY, _L("One pass - shape preview (dimensions NOT reliable)"),
        wxDefaultPosition, wxDefaultSize, wxRB_GROUP);
    m_four_pass = new wxRadioButton(
        parent, wxID_ANY,
        _L("Four passes - measured (you rotate the object 90 degrees between passes)"));
    m_four_pass->SetValue(true);

    box->Add(m_single_pass, 0, wxALL, 4);
    box->Add(m_four_pass, 0, wxALL, 4);
    return box;
}

wxSizer* ThoxScanToPrintDialog::buildGeometryFields(wxWindow* parent)
{
    auto* grid = new wxFlexGridSizer(2, 8, 12);
    grid->AddGrowableCol(1);

    grid->Add(new wxStaticText(parent, wxID_ANY, _L("Max object height (mm)")), 0,
              wxALIGN_CENTER_VERTICAL);
    m_height = new wxSpinCtrlDouble(parent, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                    wxSize(120, -1), wxSP_ARROW_KEYS, 1.0, 200.0, 40.0, 1.0);
    m_height->SetToolTip(
        _L("An upper bound. Z clearance is reserved from this before anything "
           "moves, so over-estimating is the safe direction to be wrong."));
    grid->Add(m_height, 0);

    grid->Add(new wxStaticText(parent, wxID_ANY, _L("Footprint radius (mm)")), 0,
              wxALIGN_CENTER_VERTICAL);
    m_radius = new wxSpinCtrlDouble(parent, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                    wxSize(120, -1), wxSP_ARROW_KEYS, 5.0, 135.0, 40.0, 1.0);
    grid->Add(m_radius, 0);

    grid->Add(new wxStaticText(parent, wxID_ANY, _L("Z stations per pass")), 0,
              wxALIGN_CENTER_VERTICAL);
    m_stations = new wxSpinCtrl(parent, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                wxSize(120, -1), wxSP_ARROW_KEYS, 2, 60, 12);
    grid->Add(m_stations, 0);

    grid->Add(new wxStaticText(parent, wxID_ANY, wxEmptyString), 0);
    m_make_tray = new wxCheckBox(parent, wxID_ANY, _L("Also generate a fitted tray"));
    m_make_tray->SetValue(true);
    grid->Add(m_make_tray, 0);

    return grid;
}

ThoxAgentClient::ScanParams ThoxScanToPrintDialog::collectParams() const
{
    ThoxAgentClient::ScanParams params;
    params.object_height_mm    = m_height->GetValue();
    params.footprint_radius_mm = m_radius->GetValue();
    params.stations            = m_stations->GetValue();
    params.azimuths            = m_four_pass->GetValue() ? 4 : 1;
    params.make_tray           = m_make_tray->IsChecked();
    return params;
}

void ThoxScanToPrintDialog::setStatus(const wxString& text, bool is_error)
{
    m_status->SetForegroundColour(is_error ? ERROR_TEXT : *wxBLACK);
    m_status->SetLabel(text);
    Layout();
}

void ThoxScanToPrintDialog::reportResult(const ThoxResult& result)
{
    if (result.refused) {
        // Refused means the printer was not touched and the operator can fix
        // it - most often by homing Z, which stays a deliberate human action
        // because homing drives the nozzle down onto the plate.
        setStatus(wxString::FromUTF8(result.message.c_str()), false);
        m_status->SetForegroundColour(WARN_TEXT);
        return;
    }
    setStatus(wxString::FromUTF8(result.message.c_str()), true);
}

void ThoxScanToPrintDialog::onPreviewPlan(wxCommandEvent&)
{
    setStatus(_L("Planning..."));
    const auto     params     = collectParams();
    const unsigned generation = m_generation.load();

    std::thread([this, params, generation]() {
        const ThoxResult result = m_client->planScan(params);
        CallAfter([this, result, generation]() {
            if (generation != m_generation.load())
                return;
            if (!result.success) {
                m_plan_summary->SetLabel(wxEmptyString);
                reportResult(result);
                return;
            }
            try {
                const json parsed  = json::parse(result.body);
                wxString   summary = wxString::FromUTF8(
                    parsed.value("summary", "").c_str());
                summary << "\n" << _L("tier ")
                        << wxString::FromUTF8(parsed.value("tier_label", "").c_str());
                if (!parsed.value("dimensionally_reliable", false))
                    summary << _L("  -  shape preview only");
                m_plan_summary->SetLabel(summary);
                setStatus(_L("Plan ready. Nothing has moved."));
            } catch (const std::exception&) {
                setStatus(_L("Could not read the plan"), true);
            }
            Layout();
        });
    }).detach();
}

void ThoxScanToPrintDialog::onCaptureReference(wxCommandEvent&)
{
    wxMessageDialog dialog(
        this,
        _L("The bed must be COMPLETELY EMPTY for this.\n\n"
           "Reference frames are what background subtraction compares against. "
           "An object left on the bed now becomes invisible to every future "
           "scan.\n\nIs the bed empty?"),
        _L("Capture empty bed"), wxYES_NO | wxNO_DEFAULT | wxICON_WARNING);
    if (dialog.ShowModal() != wxID_YES)
        return;

    setStatus(_L("Capturing reference frames..."));
    const auto     params     = collectParams();
    const unsigned generation = m_generation.load();

    std::thread([this, params, generation]() {
        const ThoxResult result = m_client->captureReferenceLadder(params);
        CallAfter([this, result, generation]() {
            if (generation != m_generation.load())
                return;
            if (!result.success) {
                reportResult(result);
                return;
            }
            m_job_id = result.message;
            m_progress->Pulse();
            m_timer.Start(POLL_INTERVAL_MS);
        });
    }).detach();
}

void ThoxScanToPrintDialog::onStartScan(wxCommandEvent&)
{
    if (m_single_pass->GetValue()) {
        wxMessageDialog dialog(
            this,
            _L("A single pass cannot see the far side of the object, and its "
               "depth and height measurements are not reliable.\n\n"
               "Use it for a shape preview, not for dimensions. Continue?"),
            _L("Single pass"), wxYES_NO | wxNO_DEFAULT | wxICON_INFORMATION);
        if (dialog.ShowModal() != wxID_YES)
            return;
    }

    setStatus(_L("Starting scan..."));
    m_scan_button->Disable();
    const auto     params     = collectParams();
    const unsigned generation = m_generation.load();

    std::thread([this, params, generation]() {
        const ThoxResult result = m_client->runScan(params);
        CallAfter([this, result, generation]() {
            if (generation != m_generation.load())
                return;
            if (!result.success) {
                m_scan_button->Enable();
                reportResult(result);
                return;
            }
            m_job_id = result.message;
            m_progress->Pulse();
            m_timer.Start(POLL_INTERVAL_MS);
            setStatus(_L("Scanning. The bed will move between captures."));
        });
    }).detach();
}

void ThoxScanToPrintDialog::onTimer(wxTimerEvent&) { pollJob(); }

void ThoxScanToPrintDialog::pollJob()
{
    if (m_job_id.empty())
        return;
    const std::string job_id     = m_job_id;
    const unsigned    generation = m_generation.load();

    std::thread([this, job_id, generation]() {
        const ThoxResult result = m_client->pollJob(job_id);
        CallAfter([this, result, generation]() {
            if (generation != m_generation.load())
                return;
            if (!result.success)
                return; // transient; the next tick retries
            try {
                const json  parsed = json::parse(result.body);
                const auto  state  = parsed.value("state", "running");
                if (state == "running") {
                    m_progress->Pulse();
                    return;
                }
                m_timer.Stop();
                m_job_id.clear();
                m_scan_button->Enable();
                if (state == "failed") {
                    setStatus(_L("The scan failed. See the sidecar log."), true);
                    return;
                }
                onScanFinished(parsed.value("result", json::object()).dump());
            } catch (const std::exception&) {
                m_timer.Stop();
                m_scan_button->Enable();
                setStatus(_L("Could not read the job result"), true);
            }
        });
    }).detach();
}

void ThoxScanToPrintDialog::onScanFinished(const std::string& body)
{
    try {
        const json parsed = json::parse(body);

        // A reference-ladder job returns a count rather than a scan result.
        if (parsed.contains("captured")) {
            m_progress->SetValue(100);
            setStatus(wxString::Format(_L("Captured %d reference frames."),
                                       parsed.value("captured", 0)));
            return;
        }

        const std::string state = parsed.value("state", "");
        if (state == "refused") {
            m_progress->SetValue(0);
            setStatus(wxString::FromUTF8(parsed.value("error", "").c_str()));
            m_status->SetForegroundColour(WARN_TEXT);
            return;
        }
        if (state != "complete") {
            m_progress->SetValue(0);
            setStatus(wxString::FromUTF8(parsed.value("error", "The scan did not complete").c_str()),
                      true);
            return;
        }

        m_progress->SetValue(100);

        wxString summary = _L("Scan complete.");
        if (parsed.contains("measurements") && parsed["measurements"].is_object()) {
            const auto& measurements = parsed["measurements"];
            auto        axis = [&](const char* key) {
                const auto& entry = measurements[key];
                return wxString::Format("%.1f", entry.value("value_mm", 0.0));
            };
            summary << wxString::Format(_L("  %s x %s x %s mm  [%s]"), axis("width_mm"),
                                        axis("depth_mm"), axis("height_mm"),
                                        wxString::FromUTF8(
                                            measurements.value("worst_reliability", "").c_str()));
        }
        for (const auto& caveat : parsed.value("caveats", json::array()))
            if (caveat.is_string())
                summary << "\n- " << wxString::FromUTF8(caveat.get<std::string>().c_str());
        setStatus(summary);

        // Import the mesh into the Plater so the operator can slice it here.
        std::string mesh_path;
        for (const auto& artifact : parsed.value("artifacts", json::array())) {
            if (artifact.is_object() && artifact.value("kind", "") == "mesh_stl") {
                mesh_path = artifact.value("path", "");
                break;
            }
        }
        if (!mesh_path.empty() && m_plater != nullptr) {
            m_plater->load_files(std::vector<std::string>{mesh_path}, true, false);
            setStatus(summary + "\n" + _L("Imported into the plater."));
        }
    } catch (const std::exception& exc) {
        BOOST_LOG_TRIVIAL(warning) << "[THOX] scan result parse failed: " << exc.what();
        setStatus(_L("Could not read the scan result"), true);
    }
    Layout();
}

}} // namespace Slic3r::GUI
