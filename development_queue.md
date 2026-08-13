# QidiStudio-AI Development Queue

## P0 — validation before destructive autonomy

- [ ] Run `ai_pipeline/tests/test_print_health.py` on the target Mac Python environment.
- [ ] Verify Q2 Moonraker camera discovery against `10.1.10.153`; set `THOX_QIDI_CAMERA_SNAPSHOT_URL` if vendor webcam registration differs.
- [ ] Export the exact Q2 material/profile configurations used in production and validate every generated override key.
- [ ] Run disposable calibration prints for pause/resume/cancel/upload/restart lifecycle.
- [ ] Build a labeled camera set across Matte Black, Space Gray, Arctic White parts and black/white/green/transparent PETG to tune false-positive thresholds.
- [ ] Validate first-layer failure, spaghetti, layer-shift, blob/clog, and support-failure examples against all enabled vision providers.
- [ ] Keep `THOX_ALLOW_AGENT_CANCEL=false` and `THOX_ALLOW_AGENT_REPRINT=false` until the above is complete.

## P1 — closed-loop production hardening

- [ ] Persist provider latency/cost/token metrics per assessment.
- [ ] Add temporal vision evidence using the previous N frames, not only the current frame.
- [ ] Add defect-specific confirmation-frame thresholds (e.g. collision faster than stringing).
- [ ] Add profile/material-specific remediation policy tables instead of one global mapping.
- [ ] Add a post-revision preflight that parses generated G-code for max temperatures, bed bounds, and unexpected commands before upload.
- [ ] Add cryptographic/audit identity to every machine action and revision.
- [ ] Add THOX Forger push notifications when an action-required or retry-gate event occurs.

## P1 — object scan → ready plate

- [ ] Add Q2 camera intrinsic calibration and printable ArUco/ChArUco fiducial plate.
- [ ] Implement a guided 6–8 view capture session with user rotation prompts and capture-quality scoring.
- [ ] Estimate visible XY scale from calibrated bed/fiducials and require a known measurement for any unobservable dimension below confidence threshold.
- [ ] Feed scan session frames directly into the existing multi-image TRELLIS pipeline without manual file selection.
- [ ] Automatically repair, orient, bed-fit, and generate a Q2 3MF/G-code candidate.
- [ ] Run printability preflight, then expose a single **Review Plate / Print** action in QidiStudio and THOX Forger.

## P2 — QidiStudio native UI

- [ ] Add a native `Print Health…` menu/tool item next to the existing `AI Photo-to-3D Mesh…` integration.
- [ ] Add a C++ client wrapper for Print Health endpoints using the existing libcurl patterns in `AIPipelineClient`.
- [ ] Embed the same camera/overlay/status controls in the QidiStudio monitor page while preserving the standalone web UI for rapid iteration.
- [ ] Add preferences for provider selection, thresholds, autonomy mode, and local sidecar lifecycle.

## P2 — fleet / learning loop

- [ ] Generalize state to multiple Qidi printers keyed by printer ID.
- [ ] Build anonymized opt-in defect/revision outcome datasets for threshold tuning.
- [ ] Track whether each remediation improved, worsened, or did not change the print and feed this into policy selection.
- [ ] Add material/nozzle/profile cohorts and per-device policy versions.

## Release gates

- [ ] Unit tests green.
- [ ] Static/type checks green for THOX Forger companion branch.
- [ ] Q2 camera live in both local UI and THOX Forger.
- [ ] No generic agent raw-G-code endpoint.
- [ ] Emergency stop remains human-confirmed.
- [ ] Closed-loop retry limit proven.
- [ ] A failed replacement slice cannot cancel the currently paused print.
- [ ] A replacement G-code diff is visible before/after every reprint.
