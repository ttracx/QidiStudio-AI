#ifndef slic3r_ThoxScanToPrintDialog_hpp_
#define slic3r_ThoxScanToPrintDialog_hpp_

#include <wx/wx.h>
#include <wx/timer.h>
#include <wx/spinctrl.h>

#include <atomic>
#include <memory>
#include <string>

#include "ThoxAgentClient.hpp"

namespace Slic3r {
namespace GUI {

class Plater;

/**
 * ThoxScanToPrintDialog
 * =====================
 * Place an object on the bed, scan it, import the result into the Plater.
 *
 * Complements AIPhotoTo3DDialog rather than replacing it. That dialog takes
 * photos the operator supplies and *generates* a plausible whole object with
 * TRELLIS or TripoSR. This one drives the printer's own bed as a calibrated
 * translation stage and *measures* the object sitting on it. Generative
 * inference invents the parts it cannot see; this produces a measured visual
 * hull of the side the camera can actually observe. Different tools, and the
 * dialog says which is which so an operator can choose deliberately.
 *
 * The honesty this dialog is built around: the printer has ONE fixed camera and
 * a bed that moves only in Z, so nothing rotates the object. A single pass
 * cannot see the far side. Measured on a 40 x 25 x 15 mm test object, a single
 * pass reported depth of 52 mm against 25 mm of truth; four passes with manual
 * rotation brought every axis within 2 mm. The coverage choice is therefore the
 * first control an operator meets, not an advanced setting, and single-pass
 * results are labelled a shape preview rather than a measurement.
 */

class ThoxScanToPrintDialog : public wxDialog
{
public:
    ThoxScanToPrintDialog(wxWindow* parent, Plater* plater,
                          const std::string& server_url = "http://127.0.0.1:7861");
    ~ThoxScanToPrintDialog() override;

private:
    void buildUi();
    wxSizer* buildCoverageChoice(wxWindow* parent);
    wxSizer* buildGeometryFields(wxWindow* parent);

    void onPreviewPlan(wxCommandEvent& event);
    void onCaptureReference(wxCommandEvent& event);
    void onStartScan(wxCommandEvent& event);
    void onTimer(wxTimerEvent& event);

    ThoxAgentClient::ScanParams collectParams() const;
    void setStatus(const wxString& text, bool is_error = false);
    void reportResult(const ThoxResult& result);
    void pollJob();
    void onScanFinished(const std::string& body);

    // -- widgets ------------------------------------------------------------
    wxRadioButton* m_single_pass  = nullptr;
    wxRadioButton* m_four_pass    = nullptr;
    wxSpinCtrlDouble* m_height    = nullptr;
    wxSpinCtrlDouble* m_radius    = nullptr;
    wxSpinCtrl*    m_stations     = nullptr;
    wxCheckBox*    m_make_tray    = nullptr;
    wxStaticText*  m_status       = nullptr;
    wxStaticText*  m_plan_summary = nullptr;
    wxGauge*       m_progress     = nullptr;
    wxButton*      m_scan_button  = nullptr;
    wxButton*      m_reference_button = nullptr;
    wxTimer        m_timer;

    Plater*                          m_plater = nullptr;
    std::unique_ptr<ThoxAgentClient> m_client;
    std::string                      m_job_id;
    std::atomic<unsigned>            m_generation{0};

    DECLARE_EVENT_TABLE()
};

}} // namespace Slic3r::GUI

#endif // slic3r_ThoxScanToPrintDialog_hpp_
