#include "ThoxPrintHealthPanel.hpp"

#include <wx/mstream.h>
#include <wx/dcbuffer.h>

#include <boost/log/trivial.hpp>

#include "GUI_App.hpp"
#include "I18N.hpp"

namespace Slic3r {
namespace GUI {

namespace {

// Poll cadence. The sidecar samples the printer on its own schedule (45 s
// normally, 12 s once suspicious); this only decides how fresh the panel is,
// so 2 s keeps it responsive without loading the GUI thread.
constexpr int POLL_INTERVAL_MS = 2000;

// THOX brand green. Black ink on this green, never white - white fails AA.
const wxColour THOX_GREEN(0x05, 0xA4, 0x51);
const wxColour SEVERITY_OK(0x2E, 0xA0, 0x43);
const wxColour SEVERITY_WARN(0xD2, 0x9A, 0x22);
const wxColour SEVERITY_BAD(0xC0, 0x39, 0x2B);

wxColour severity_colour(double severity)
{
    if (severity >= 0.75)
        return SEVERITY_BAD;
    if (severity >= 0.4)
        return SEVERITY_WARN;
    return SEVERITY_OK;
}

} // namespace

BEGIN_EVENT_TABLE(ThoxPrintHealthPanel, wxPanel)
    EVT_TIMER(wxID_ANY, ThoxPrintHealthPanel::onTimer)
END_EVENT_TABLE()

ThoxPrintHealthPanel::ThoxPrintHealthPanel(wxWindow* parent, const std::string& server_url)
    : wxPanel(parent, wxID_ANY)
    , m_timer(this)
    , m_client(std::make_unique<ThoxAgentClient>(server_url))
{
    buildUi();
}

ThoxPrintHealthPanel::~ThoxPrintHealthPanel()
{
    deactivate();
    // Bump the generation so any in-flight worker's CallAfter is dropped
    // rather than touching widgets that are being destroyed.
    m_generation.fetch_add(1);
}

// -- construction ------------------------------------------------------------

void ThoxPrintHealthPanel::buildUi()
{
    SetBackgroundColour(*wxWHITE);
    auto* root = new wxBoxSizer(wxVERTICAL);

    // Header
    auto* header = new wxBoxSizer(wxHORIZONTAL);
    auto* title  = new wxStaticText(this, wxID_ANY, _L("Print Health"));
    title->SetFont(title->GetFont().Bold().Scaled(1.3f));
    header->Add(title, 0, wxALIGN_CENTER_VERTICAL | wxALL, 8);
    header->AddStretchSpacer();

    m_autonomy_label = new wxStaticText(this, wxID_ANY, _L("autonomy: unknown"));
    m_autonomy_label->SetForegroundColour(wxColour(0x66, 0x66, 0x66));
    header->Add(m_autonomy_label, 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, 12);

    m_monitor_button = new wxButton(this, wxID_ANY, _L("Start monitoring"));
    m_monitor_button->Bind(wxEVT_BUTTON, &ThoxPrintHealthPanel::onToggleMonitor, this);
    header->Add(m_monitor_button, 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, 8);
    root->Add(header, 0, wxEXPAND);

    // Body: camera on the left, status and controls on the right.
    auto* body = new wxBoxSizer(wxHORIZONTAL);

    m_camera_panel = new wxPanel(this, wxID_ANY, wxDefaultPosition, wxSize(560, 420));
    m_camera_panel->SetBackgroundStyle(wxBG_STYLE_PAINT);
    m_camera_panel->SetBackgroundColour(*wxBLACK);
    m_camera_panel->Bind(wxEVT_PAINT, &ThoxPrintHealthPanel::onPaintCamera, this);
    body->Add(m_camera_panel, 1, wxEXPAND | wxALL, 8);

    body->Add(buildStatusColumn(this), 0, wxEXPAND | wxALL, 8);
    root->Add(body, 1, wxEXPAND);

    // Event log
    auto* log_label = new wxStaticText(this, wxID_ANY, _L("Event log"));
    log_label->SetFont(log_label->GetFont().Bold());
    root->Add(log_label, 0, wxLEFT | wxTOP, 10);

    m_event_list = new wxListCtrl(this, wxID_ANY, wxDefaultPosition, wxSize(-1, 160),
                                  wxLC_REPORT | wxLC_SINGLE_SEL | wxBORDER_SIMPLE);
    m_event_list->AppendColumn(_L("Time"), wxLIST_FORMAT_LEFT, 90);
    m_event_list->AppendColumn(_L("Kind"), wxLIST_FORMAT_LEFT, 130);
    m_event_list->AppendColumn(_L("Message"), wxLIST_FORMAT_LEFT, 700);
    root->Add(m_event_list, 0, wxEXPAND | wxALL, 8);

    SetSizer(root);
    Layout();
}

wxSizer* ThoxPrintHealthPanel::buildStatusColumn(wxWindow* parent)
{
    auto* column = new wxBoxSizer(wxVERTICAL);

    m_headline = new wxStaticText(parent, wxID_ANY, _L("Not monitoring"));
    m_headline->SetFont(m_headline->GetFont().Bold().Scaled(1.15f));
    m_headline->Wrap(300);
    column->Add(m_headline, 0, wxEXPAND | wxBOTTOM, 6);

    m_severity_gauge = new wxGauge(parent, wxID_ANY, 100, wxDefaultPosition, wxSize(300, 14));
    column->Add(m_severity_gauge, 0, wxEXPAND | wxBOTTOM, 8);

    m_detail = new wxStaticText(parent, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                wxSize(300, 170), wxST_NO_AUTORESIZE);
    column->Add(m_detail, 0, wxEXPAND | wxBOTTOM, 8);

    // What the current configuration genuinely covers. Shown always, not just
    // on failure: an operator needs to know a green panel means "the tripwire
    // saw nothing", not "a model inspected this and approved it".
    m_capability = new wxStaticText(parent, wxID_ANY, wxEmptyString, wxDefaultPosition,
                                    wxSize(300, 56), wxST_NO_AUTORESIZE);
    m_capability->SetForegroundColour(wxColour(0x88, 0x66, 0x00));
    column->Add(m_capability, 0, wxEXPAND | wxBOTTOM, 8);

    column->Add(buildControlButtons(parent), 0, wxEXPAND);
    return column;
}

wxSizer* ThoxPrintHealthPanel::buildControlButtons(wxWindow* parent)
{
    auto* grid = new wxGridSizer(2, 2, 6, 6);

    m_pause_button = new wxButton(parent, wxID_ANY, _L("Pause"));
    m_pause_button->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { onControl("pause", false); });

    m_resume_button = new wxButton(parent, wxID_ANY, _L("Resume"));
    m_resume_button->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { onControl("resume", false); });

    m_cancel_button = new wxButton(parent, wxID_ANY, _L("Cancel print"));
    // Confirmed: cancelling discards hours of work and cannot be undone.
    m_cancel_button->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { onControl("cancel", true); });

    m_reprint_button = new wxButton(parent, wxID_ANY, _L("Revise && reprint"));
    m_reprint_button->Bind(wxEVT_BUTTON, &ThoxPrintHealthPanel::onReviseAndReprint, this);

    grid->Add(m_pause_button, 0, wxEXPAND);
    grid->Add(m_resume_button, 0, wxEXPAND);
    grid->Add(m_cancel_button, 0, wxEXPAND);
    grid->Add(m_reprint_button, 0, wxEXPAND);
    return grid;
}

// -- lifecycle ---------------------------------------------------------------

void ThoxPrintHealthPanel::activate()
{
    if (m_active)
        return;
    m_active = true;
    m_timer.Start(POLL_INTERVAL_MS);
    pollAsync();
}

void ThoxPrintHealthPanel::deactivate()
{
    m_active = false;
    if (m_timer.IsRunning())
        m_timer.Stop();
}

void ThoxPrintHealthPanel::onTimer(wxTimerEvent&) { pollAsync(); }

// -- polling -----------------------------------------------------------------

void ThoxPrintHealthPanel::pollAsync()
{
    // One poll in flight at a time. Without this, a slow sidecar produces a
    // growing pile of worker threads all writing the same widgets.
    bool expected = false;
    if (!m_busy.compare_exchange_strong(expected, true))
        return;

    const unsigned generation = m_generation.load();
    std::thread([this, generation]() {
        ThoxHealthState             state;
        std::vector<ThoxEvent>      events;
        std::vector<unsigned char>  frame;
        long long                   last_seq = m_last_event_seq;

        const bool have_state = m_client->getHealthState(state);
        if (have_state) {
            m_client->getEvents(last_seq, events, last_seq);
            frame = m_client->getLatestFrame();
        }

        CallAfter([this, generation, have_state, state, events, frame, last_seq]() {
            m_busy.store(false);
            // Stale reply from before a reset or destruction: drop it.
            if (generation != m_generation.load())
                return;
            if (!have_state) {
                m_headline->SetLabel(_L("THOX service unreachable"));
                m_detail->SetLabel(
                    _L("Start it with:  python -m thox.app --port 7862\n"
                       "or run the AI pipeline server, which mounts the same routes."));
                return;
            }
            m_last_event_seq = last_seq;
            applyState(state);
            if (!events.empty())
                applyEvents(events);
            if (!frame.empty())
                applyFrame(frame);
        });
    }).detach();
}

void ThoxPrintHealthPanel::applyState(const ThoxHealthState& state)
{
    m_state = state;

    m_monitor_button->SetLabel(state.running ? _L("Stop monitoring")
                                             : _L("Start monitoring"));
    m_autonomy_label->SetLabel(wxString::Format(_L("autonomy: %s"), state.autonomy));

    wxString headline;
    if (!state.running)
        headline = _L("Not monitoring");
    else if (state.printer_state != "printing")
        headline = wxString::Format(_L("Idle - printer is %s"), state.printer_state);
    else if (!state.camera_fault.empty())
        headline = _L("Cannot judge - camera issue");
    else if (state.severity <= 0.0)
        headline = _L("No defects flagged");
    else
        headline = wxString::FromUTF8(state.summary.c_str());
    m_headline->SetLabel(headline);
    m_headline->SetForegroundColour(state.running && state.severity > 0
                                        ? severity_colour(state.severity)
                                        : *wxBLACK);

    m_severity_gauge->SetValue(static_cast<int>(state.severity * 100.0));

    wxString detail;
    if (state.running && !state.watching.empty()) {
        detail << wxString::FromUTF8(state.watching.c_str()) << "\n";
        if (state.total_layers > 0 && state.current_layer >= 0)
            detail << wxString::Format(_L("layer %d of %d  -  %.0f%%\n"),
                                       state.current_layer, state.total_layers,
                                       state.progress * 100.0);
        detail << wxString::Format(_L("%d samples analysed\n"), state.samples);
    }
    if (!state.camera_fault.empty())
        detail << "\n" << wxString::FromUTF8(state.camera_fault.c_str()) << "\n";
    for (const auto& detection : state.detections) {
        detail << wxString::Format("\n%s  (%s, %.0f%%)",
                                   wxString::FromUTF8(detection.label.c_str()),
                                   wxString::FromUTF8(detection.urgency.c_str()),
                                   detection.confidence * 100.0);
    }
    for (const auto& suspicion : state.suspicions)
        detail << "\n" << _L("watching: ") << wxString::FromUTF8(suspicion.c_str());
    if (!state.last_error.empty())
        detail << "\n\n" << wxString::FromUTF8(state.last_error.c_str());
    m_detail->SetLabel(detail);

    // Say plainly what this configuration can and cannot see.
    if (!state.has_classifier) {
        m_capability->SetLabel(
            _L("Change detection only - no model is configured to classify "
               "defects. Set THOX_OLLAMA_BASE_URL or an API key for full "
               "detection."));
    } else {
        m_capability->SetLabel(wxEmptyString);
    }

    const bool printing = state.printer_state == "printing";
    const bool paused   = state.printer_state == "paused";
    m_pause_button->Enable(printing);
    m_resume_button->Enable(paused);
    m_cancel_button->Enable(printing || paused);
    m_reprint_button->Enable(!printing);

    Layout();
    m_camera_panel->Refresh();
}

void ThoxPrintHealthPanel::applyEvents(const std::vector<ThoxEvent>& events)
{
    for (const auto& event : events) {
        // Routine samples are the vast majority; showing them would bury the
        // handful of entries that actually matter.
        if (!event.notable)
            continue;
        const time_t when = static_cast<time_t>(event.at);
        wxString     stamp = wxDateTime(when).FormatISOTime();

        const long index = m_event_list->InsertItem(0, stamp);
        m_event_list->SetItem(index, 1, wxString::FromUTF8(event.kind.c_str()));
        m_event_list->SetItem(index, 2, wxString::FromUTF8(event.message.c_str()));
        if (event.severity >= 0.75)
            m_event_list->SetItemTextColour(index, SEVERITY_BAD);
        else if (event.severity >= 0.4)
            m_event_list->SetItemTextColour(index, SEVERITY_WARN);
    }
    // Bound the control so a long print cannot grow it without limit.
    while (m_event_list->GetItemCount() > 300)
        m_event_list->DeleteItem(m_event_list->GetItemCount() - 1);
}

void ThoxPrintHealthPanel::applyFrame(const std::vector<unsigned char>& jpeg)
{
    wxMemoryInputStream stream(jpeg.data(), jpeg.size());
    wxImage             image;
    {
        // wxWidgets logs a modal error dialog for a bad image by default, which
        // would be intolerable on a 2-second poll.
        wxLogNull suppress_dialogs;
        if (!image.LoadFile(stream, wxBITMAP_TYPE_JPEG))
            return;
    }
    {
        std::lock_guard<std::mutex> guard(m_frame_mutex);
        m_frame = image;
    }
    m_camera_panel->Refresh();
}

// -- painting ----------------------------------------------------------------

void ThoxPrintHealthPanel::onPaintCamera(wxPaintEvent&)
{
    wxAutoBufferedPaintDC dc(m_camera_panel);
    dc.SetBackground(wxBrush(*wxBLACK));
    dc.Clear();

    wxImage frame;
    {
        std::lock_guard<std::mutex> guard(m_frame_mutex);
        frame = m_frame;
    }

    const wxSize canvas = m_camera_panel->GetClientSize();
    if (!frame.IsOk() || canvas.x <= 0 || canvas.y <= 0) {
        dc.SetTextForeground(wxColour(0x88, 0x88, 0x88));
        dc.DrawText(_L("No frame yet"), 16, 16);
        return;
    }

    // Letterbox: preserve aspect so the overlay boxes stay aligned with what
    // the model actually saw. Stretching would put every box in the wrong place.
    const double scale = std::min(static_cast<double>(canvas.x) / frame.GetWidth(),
                                 static_cast<double>(canvas.y) / frame.GetHeight());
    const int    width  = std::max(1, static_cast<int>(frame.GetWidth() * scale));
    const int    height = std::max(1, static_cast<int>(frame.GetHeight() * scale));
    const int    offset_x = (canvas.x - width) / 2;
    const int    offset_y = (canvas.y - height) / 2;

    dc.DrawBitmap(wxBitmap(frame.Scale(width, height, wxIMAGE_QUALITY_HIGH)),
                  offset_x, offset_y, false);

    dc.SetBrush(*wxTRANSPARENT_BRUSH);
    for (const auto& detection : m_state.detections) {
        if (detection.bbox_norm.size() != 4)
            continue;
        const wxColour colour = severity_colour(detection.severity);
        dc.SetPen(wxPen(colour, 2));
        const int x0 = offset_x + static_cast<int>(detection.bbox_norm[0] * width);
        const int y0 = offset_y + static_cast<int>(detection.bbox_norm[1] * height);
        const int x1 = offset_x + static_cast<int>(detection.bbox_norm[2] * width);
        const int y1 = offset_y + static_cast<int>(detection.bbox_norm[3] * height);
        dc.DrawRectangle(x0, y0, std::max(1, x1 - x0), std::max(1, y1 - y0));

        const wxString caption = wxString::Format(
            "%s %.0f%%", wxString::FromUTF8(detection.kind.c_str()),
            detection.confidence * 100.0);
        dc.SetTextForeground(*wxBLACK);
        dc.SetBrush(wxBrush(colour));
        dc.SetPen(*wxTRANSPARENT_PEN);
        const wxSize extent = dc.GetTextExtent(caption);
        dc.DrawRectangle(x0, std::max(0, y0 - extent.y - 4), extent.x + 8, extent.y + 4);
        dc.DrawText(caption, x0 + 4, std::max(0, y0 - extent.y - 2));
        dc.SetBrush(*wxTRANSPARENT_BRUSH);
    }
}

// -- actions -----------------------------------------------------------------

void ThoxPrintHealthPanel::onToggleMonitor(wxCommandEvent&)
{
    const bool running = m_state.running;
    runAsync([this, running]() { return running ? m_client->stopMonitor()
                                                : m_client->startMonitor(); },
             running ? "stop monitoring" : "start monitoring");
}

void ThoxPrintHealthPanel::onControl(const std::string& action, bool confirm)
{
    if (confirm) {
        wxMessageDialog dialog(
            this,
            _L("Cancel the running print?\n\nThis cannot be undone and the "
               "part will be lost."),
            _L("Cancel print"), wxYES_NO | wxNO_DEFAULT | wxICON_WARNING);
        if (dialog.ShowModal() != wxID_YES)
            return;
    }
    runAsync([this, action]() { return m_client->control(action, "operator request"); },
             action);
}

void ThoxPrintHealthPanel::onReviseAndReprint(wxCommandEvent&)
{
    // Use the worst confirmed defect as the diagnosis. With nothing detected
    // there is nothing to revise, and guessing would produce arbitrary changes.
    if (m_state.detections.empty()) {
        wxMessageBox(_L("No defect has been detected, so there is nothing to "
                        "revise. Revision changes parameters in response to a "
                        "specific diagnosed failure."),
                     _L("Revise and reprint"), wxOK | wxICON_INFORMATION, this);
        return;
    }
    const std::string defect = m_state.detections.front().kind;
    runAsync([this, defect]() { return m_client->planRevision(defect); },
             "plan revision for " + defect);
}

void ThoxPrintHealthPanel::runAsync(std::function<ThoxResult()> work, const std::string& label)
{
    const unsigned generation = m_generation.load();
    std::thread([this, work, label, generation]() {
        const ThoxResult result = work();
        CallAfter([this, result, label, generation]() {
            if (generation != m_generation.load())
                return;
            reportResult(result, label);
            pollAsync();
        });
    }).detach();
}

void ThoxPrintHealthPanel::reportResult(const ThoxResult& result, const std::string& label)
{
    if (result.success)
        return; // the next poll shows the new state; a dialog would be noise

    if (result.refused) {
        // Refused is advice, not an error: the printer was not touched.
        wxMessageBox(wxString::FromUTF8(result.message.c_str()),
                     _L("The printer declined this action"),
                     wxOK | wxICON_INFORMATION, this);
        return;
    }
    BOOST_LOG_TRIVIAL(warning) << "[THOX] " << label << " failed: " << result.message;
    wxMessageBox(wxString::FromUTF8(result.message.c_str()),
                 _L("THOX request failed"), wxOK | wxICON_ERROR, this);
}

}} // namespace Slic3r::GUI
