# QidiStudio-AI MVP Catalog

Priority formula:

```text
Priority = (Market Value × 0.4) + (Technical Feasibility × 0.3) + (Time-to-Market × 0.2) + (Strategic Importance × 0.1)
```

Scores use a 1–10 scale.

| MVP | Vertical slice | MV | TF | TTM | SI | Priority | Status |
|---|---|---:|---:|---:|---:|---:|---|
| Print Health Detect | Q2 camera → 3 parallel vision providers → fused defects/overlay | 10 | 9 | 9 | 10 | 9.5 | Implemented on feature branch |
| Safe Auto-Pause | fused critical event → guardrails → Moonraker pause + notification | 10 | 10 | 10 | 10 | 10.0 | Implemented on feature branch |
| Revise + Reprint | diagnosis → bounded slicer diff → new G-code → guarded upload/reprint | 10 | 8 | 8 | 10 | 9.0 | Implemented; printer-profile validation required |
| THOX Forger Panel | authenticated proxy → camera overlay → controls/history/events | 9 | 9 | 9 | 10 | 9.1 | Implemented in companion thox-forge branch |
| Scan Capture | Q2 camera + clamped calibration poses → stored multi-view frames | 8 | 9 | 9 | 9 | 8.6 | Implemented |
| Scan-to-Mesh | multi-view captures → TRELLIS/TripoSR → repair + known scale | 9 | 8 | 7 | 9 | 8.4 | Existing mesh pipeline + new capture integration |
| Scan-to-Ready Plate | calibrated dimensions → mesh → Q2 profile → ready G-code/3MF | 10 | 6 | 6 | 9 | 7.9 | Next vertical slice |
| Automated Orientation | multi-view geometry + bed fit + supports → approved orientation | 8 | 5 | 5 | 8 | 6.5 | Human/scan gate only for current MVP |

## MVP 1 — Print Health Detect

**AI:** OpenAI + Ollama local + Ollama Cloud run concurrently and emit the same defect schema.

**Backend:** camera/Moonraker acquisition, deterministic fusion, adaptive cadence, persistent event history.

**Frontend:** Q2 live frame, normalized defect bounding boxes, severity/confidence, provider status.

## MVP 2 — Safe Auto-Pause

**AI:** high-confidence multi-model critical classification.

**Backend:** policy engine allows autonomous pause only; destructive actions remain separately gated.

**Frontend:** mode selector, action-required notification, operator resume/cancel controls.

## MVP 3 — Revise + Reprint

**AI:** fused visual diagnosis only.

**Backend:** deterministic defect-to-parameter policy, hard bounds, Qidi profile override, diff, reslice, upload, retry budget.

**Frontend:** revision list, exact before/after settings, retry counter, manual revise+reprint.

## MVP 4 — Object Scan

**AI:** existing multi-image TRELLIS / TripoSR generation.

**Backend:** Q2 camera captures with optional hard-clamped calibration pose; saved scan frame set.

**Frontend:** capture control in local Print Health UI and THOX Forger.

Current limitation: a fixed monocular camera is not enough for fit-critical hidden dimensions. Production scan-to-plate should add calibrated fiducials and/or an object rotation workflow before removing the known-dimension requirement.
