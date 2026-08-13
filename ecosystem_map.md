# QidiStudio-AI Ecosystem Map

## Product boundary

QidiStudio-AI is the desktop manufacturing edge of the THOX Forge ecosystem. It combines the existing QidiStudio slicer, AI photo-to-3D reconstruction, Qidi Q2 Moonraker control, and closed-loop Print Health.

```text
Physical object / printable source
            │
            ├───────────────┐
            ▼               ▼
Q2 camera scan       Existing CAD / 3MF / STL
            │               │
            ▼               │
TRELLIS / TripoSR           │
            │               │
mesh repair / scale         │
            └───────┬───────┘
                    ▼
          QidiStudio / QIDISlicer
                    │
                    ▼
              G-code revision
                    │
                    ▼
          Moonraker → Qidi Q2
                    │
             live camera frames
                    │
                    ▼
          Print Health vision fleet
          ├─ OpenAI
          ├─ Ollama Cloud
          └─ Ollama local on Mac
                    │
                    ▼
       deterministic fusion + policy
          ├─ continue
          ├─ notify / suggest
          ├─ pause / resume
          └─ revise + guarded reprint
                    │
                    ▼
       THOX Forger Print Health panel
```

## Runtime placement

| Component | Runtime | Responsibility |
|---|---|---|
| Qidi Q2 controller | `10.1.10.153` | Camera, Klipper/Moonraker, physical print execution |
| QidiStudio-AI unified sidecar | Mac/desktop | Vision orchestration, scan capture, revision engine, local UI |
| Ollama local | Mac/desktop | Private local vision pass |
| OpenAI | Cloud | Independent multimodal vision pass |
| Ollama Cloud | Cloud | Independent multimodal vision pass |
| THOX Forger | Web + desktop orchestrator | Operator console, authenticated remote controls, history |

## Safety boundaries

- No LLM is given unrestricted raw G-code control.
- Camera scan motion is an explicit allowlist with clamped Q2 coordinates.
- Auto-pause is the default autonomous physical intervention.
- Cancel, restart, and reprint have separate operator-controlled gates.
- Emergency stop requires explicit human confirmation.
- Revision uses the original source model/profile and deterministic bounded changes.
- Automatic retries stop at a configured retry budget.

## Core integration files

```text
ai_pipeline/
  unified_server.py
  server.py                         # existing Photo-to-3D pipeline
  print_health/
    models.py
    providers.py
    moonraker.py
    remediation.py
    core.py
    api.py
    static/print_health.html
  tests/test_print_health.py

docs/PRINT_HEALTH.md
ai_pipeline/print_health.env.example
```

## Upstream/downstream contracts

**Upstream:** Q2 camera, Moonraker telemetry, source CAD/mesh, Qidi profile, OpenAI/Ollama provider APIs.

**Downstream:** revised G-code, Q2 print lifecycle, event/revision history, THOX Forger UI, scan captures feeding the existing multi-image mesh generator.
