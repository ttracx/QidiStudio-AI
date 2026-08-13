#ifndef slic3r_ThoxPrintHealthPanel_hpp_
#define slic3r_ThoxPrintHealthPanel_hpp_

#include <wx/wx.h>
#include <wx/timer.h>
#include <wx/listctrl.h>

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "ThoxAgentClient.hpp"

namespace Slic3r {
namespace GUI {

/**
 * ThoxPrintHealthPanel
 * ====================
 * Live print-health panel: camera view with defect overlay, severity, the
 * pause / resume / cancel / reprint controls, and the event log.
 *
 *   +---------------------------------------------------------------+
 *   |  Print Health          [ Start monitoring ]   autonomy: suggest|
 *   +----------------------------------+----------------------------+
 *   |                                  |  Status                    |
 *   |    camera frame                  |    severity bar            |
 *   |    + defect boxes drawn on top   |    detected defects        |
 *   |                                  |    suspicions (2/3)        |
 *   |                                  |----------------------------|
 *   |                                  |  [Pause] [Resume]          |
 *   |                                  |  [Cancel] [Reprint]        |
 *   +----------------------------------+----------------------------+
 *   |  Event log                                                    |
 *   +---------------------------------------------------------------+
 *
 * Threading. Every HTTP call blocks, so all of them run on a detached worker
 * and results come back to the GUI through CallAfter. The panel owns an
 * atomic generation counter; a reply whose generation is stale is dropped,
 * which is what stops a slow in-flight poll from overwriting fresher state
 * after the user has clicked something.
 *
 * Editorial decision worth stating: the panel shows what the system *cannot*
 * do as prominently as what it can. When no language-model provider is
 * configured, it says "change detection only - no defect classification"
 * rather than displaying a reassuring green tick that means very little. A
 * monitor that overstates its coverage is worse than no monitor, because the
 * operator stops checking.
 */

class ThoxPrintHealthPanel : public wxPanel
{
public:
    ThoxPrintHealthPanel(wxWindow* parent, const std::string& server_url = "http://127.0.0.1:7861");
    ~ThoxPrintHealthPanel() override;

    // Begin/stop polling the sidecar. Called when the tab is shown/hidden so a
    // hidden panel costs nothing.
    void activate();
    void deactivate();

private:
    // -- construction -------------------------------------------------------
    void buildUi();
    wxSizer* buildStatusColumn(wxWindow* parent);
    wxSizer* buildControlButtons(wxWindow* parent);

    // -- polling ------------------------------------------------------------
    void onTimer(wxTimerEvent& event);
    void pollAsync();
    void applyState(const ThoxHealthState& state);
    void applyEvents(const std::vector<ThoxEvent>& events);
    void applyFrame(const std::vector<unsigned char>& jpeg);

    // -- actions ------------------------------------------------------------
    void onToggleMonitor(wxCommandEvent& event);
    void onControl(const std::string& action, bool confirm);
    void onReviseAndReprint(wxCommandEvent& event);
    void runAsync(std::function<ThoxResult()> work, const std::string& label);
    // Renders a ThoxResult. A refusal is shown as advice, not as an error,
    // because the printer was not touched and the operator can fix it.
    void reportResult(const ThoxResult& result, const std::string& label);

    // -- painting -----------------------------------------------------------
    void onPaintCamera(wxPaintEvent& event);

    // -- widgets ------------------------------------------------------------
    wxPanel*      m_camera_panel   = nullptr;
    wxStaticText* m_headline       = nullptr;
    wxStaticText* m_detail         = nullptr;
    wxStaticText* m_capability     = nullptr;
    wxStaticText* m_autonomy_label = nullptr;
    wxGauge*      m_severity_gauge = nullptr;
    wxListCtrl*   m_event_list     = nullptr;
    wxButton*     m_monitor_button = nullptr;
    wxButton*     m_pause_button   = nullptr;
    wxButton*     m_resume_button  = nullptr;
    wxButton*     m_cancel_button  = nullptr;
    wxButton*     m_reprint_button = nullptr;
    wxTimer       m_timer;

    // -- state --------------------------------------------------------------
    std::unique_ptr<ThoxAgentClient> m_client;
    std::mutex                       m_frame_mutex;
    wxImage                          m_frame;          // guarded by m_frame_mutex
    ThoxHealthState                  m_state;          // GUI thread only
    long long                        m_last_event_seq = 0;
    std::atomic<unsigned>            m_generation{0};
    std::atomic<bool>                m_busy{false};
    bool                             m_active = false;

    DECLARE_EVENT_TABLE()
};

}} // namespace Slic3r::GUI

#endif // slic3r_ThoxPrintHealthPanel_hpp_
