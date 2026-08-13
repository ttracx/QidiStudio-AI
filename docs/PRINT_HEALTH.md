# THOX / QidiStudio-AI Print Health

## Purpose

Print Health turns the Qidi Q2 camera + Moonraker interface into a closed-loop manufacturing supervisor while keeping compute-heavy AI off the printer controller.

The Q2 controller is treated as a **camera / telemetry / motion endpoint**. OpenAI, Ollama Cloud, local Ollama, mesh generation, revision logic, and the THOX Forger UI run on the Mac/desktop or cloud.

## Architecture

```text
Qidi Q2 (10.1.10.153)
  ├─ built-in camera
  ├─ Moonraker :7125
  ├─ Klipper telemetry
  └─ print lifecycle control
           │
           ▼
Mac / desktop: ai_pipeline/unified_server.py :7861
  ├─ existing Photo-to-3D TRELLIS / TripoSR pipeline
  ├─ PrintHealthService
  │   ├─ OpenAI vision ───────────────┐
  │   ├─ Ollama local on Mac ─────────┼─ parallel assessment
  │   └─ Ollama Cloud ────────────────┘
  │                │
  │                ▼
  │       deterministic fusion
  │                │
  │       adaptive camera sampling
  │                │
  │       autonomy / safety policy
  │                │
  │        Moonraker pause/resume
  │                │
  │     diagnose → bounded changes
  │                │
  │       QidiStudio/QIDISlicer
  │                │
  │       revised G-code + diff
  │                │
  └────────── upload / reprint
           │
           ├─ /print-health local UI
           └─ THOX Forger authenticated proxy + Print Health panel
```

## Defects

The vision schema recognizes:

- spaghetti / detached part
- first-layer and bed-adhesion failure
- warping
- layer shift
- stringing
- under-extrusion
- over-extrusion
- blobs
- clogs
- support failure
- collision
- visible smoke / fire

Every provider returns a quality score, overall confidence, severity, recommended action, localized 0..1000 bounding boxes, and a short evidence-based diagnosis.

## Multi-model fusion

OpenAI, local Ollama, and Ollama Cloud are invoked concurrently. Fusion is deterministic rather than delegated to another language model:

1. weight severity by provider confidence;
2. combine matching defect probabilities;
3. down-rank single-provider detections when other providers disagree;
4. require a configurable number of independent providers before autonomous critical action;
5. preserve provider failures in the event stream rather than silently treating a missing model as agreement.

Default: at least **2** providers for autonomous critical action.

## Adaptive sampling

Default camera cadence:

| State | Interval |
|---|---:|
| Healthy | 15 s |
| Suspected issue | 5 s |
| Warning | 2.5 s |
| Critical | 1 s |

The monitor only spends vision-model calls while the printer is printing, or while Print Health itself has paused the job.

## Autonomy modes

### `observe`

Detect and log only.

### `assist`

Detect, notify, and recommend an action. Wait for the operator.

### `autopause` — recommended starting mode

A high-confidence, multi-model critical failure can autonomously **pause** the print. Pause is reversible. Lower-confidence findings remain suggestions.

### `closed_loop`

After repeated critical confirmation, Print Health may generate a replacement slice and reprint only when all destructive autonomy gates are explicitly enabled:

```bash
THOX_PRINT_HEALTH_MODE=closed_loop
THOX_ALLOW_AGENT_CANCEL=true
THOX_ALLOW_AGENT_REPRINT=true
```

`THOX_MAX_REPRINTS` defaults to `2`; after that the system stops and asks for operator review.

Restart is separately gated by `THOX_ALLOW_AGENT_RESTART`.

Emergency stop is **never autonomous**. A possible smoke/fire detection creates an urgent action-required event for explicit human confirmation.

## Revision engine

The model does **not** rewrite arbitrary raw G-code.

On a failed print:

1. the failed job is paused;
2. fused defects are converted by deterministic policy to a bounded `ChangeSet`;
3. the original source model + Qidi base profile are re-sliced;
4. known Qidi/Prusa profile values are modified within hard bounds;
5. a before/after JSON diff is saved;
6. only after a replacement G-code exists does the system cancel the failed job;
7. revised G-code is uploaded through Moonraker;
8. it starts automatically only when requested/allowed;
9. the new attempt is monitored again.

Supported revision parameters include:

- nozzle and bed temperatures
- material flow / extrusion multiplier
- perimeter / infill / solid / support / bridge speeds
- acceleration
- retraction length and speed
- first-layer speed and height when the profile exposes those values
- brim
- raft layers
- supports and support spacing/density
- explicit 90-degree orientation revisions

Automatic orientation remains disabled from a single fixed camera; a scan/human stage must explicitly approve it.

## Local setup on the Mac

```bash
cd ai_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install/start Ollama separately and pull a local vision model, then configure environment variables from `print_health.env.example`.

Minimum practical configuration:

```bash
export THOX_QIDI_HOST=10.1.10.153
export OPENAI_API_KEY='...'
export THOX_OLLAMA_LOCAL_URL=http://127.0.0.1:11434
export THOX_OLLAMA_LOCAL_MODEL=qwen3-vl:4b
export THOX_PRINT_HEALTH_MODE=autopause

python unified_server.py --host 127.0.0.1 --port 7861 --backend auto --start-monitor
```

Open:

```text
http://127.0.0.1:7861/print-health
```

The original mesh-generation API remains available on the same port.

## THOX Forger integration

THOX Forger proxies Print Health so browser clients never need direct access to the sidecar token.

Configure the Forge orchestrator:

```bash
THOX_QIDI_AI_URL=http://127.0.0.1:7861
THOX_QIDI_AI_TOKEN=
```

If Forge and QidiStudio-AI run on different hosts, expose the unified sidecar only over a protected network path. When `--allow-remote` is used, `THOX_PRINT_HEALTH_TOKEN` is mandatory. Also protect the service with Tailscale ACLs or an authenticated reverse proxy because the pre-existing mesh endpoints are part of the same Flask application.

## Object scan → printable model workflow

The same integration includes calibrated Q2 camera capture through `/api/scan/capture`.

```text
Place object on clean Q2 bed
  ↓
Move toolhead to optional known calibration pose
  ↓
Capture camera frame
  ↓
Rotate object to another known view
  ↓
Repeat 4–8 views
  ↓
Existing /generate multi-image TRELLIS pipeline
  ↓
mesh repair / watertight validation
  ↓
scale to known target dimensions
  ↓
import 3MF/STL to QidiStudio
  ↓
slice with validated Q2 profile
```

`THOX_ALLOW_SCAN_MOTION=false` by default. When enabled, Print Health exposes only hard-clamped scan poses; there is no generic agent raw-G-code tool.

A fixed monocular printer camera cannot reliably infer every hidden dimension from one image. Fit-critical scan-to-print therefore needs multi-view captures plus known dimensions or a calibrated fiducial/measurement step. The existing Photo-to-3D API already accepts multiple images, so the capture system is designed to feed that pipeline.

## API

Main routes on the unified sidecar:

```text
GET  /api/print-health/state
GET  /api/print-health/camera
GET  /api/print-health/events
GET  /api/print-health/revisions
GET  /api/print-health/stream
POST /api/print-health/start
POST /api/print-health/stop
POST /api/print-health/analyze
POST /api/print-health/job
POST /api/print-health/mode
POST /api/print-health/control
POST /api/print-health/remediate
POST /api/scan/capture
GET  /print-health
```

## Validation

Run the deterministic unit tests from `ai_pipeline`:

```bash
python -m unittest discover -s tests -p 'test_print_health.py' -v
```

Before enabling closed-loop destructive autonomy, validate at minimum:

1. camera snapshot discovery on the Q2;
2. Moonraker pause/resume/cancel endpoints;
3. OpenAI/local Ollama/Ollama Cloud provider behavior;
4. false-positive rates for each material/color and lighting condition;
5. first-layer vs later-layer threshold tuning;
6. every generated override against the actual exported Q2 profile;
7. upload and restart behavior with a disposable calibration print;
8. max-retry stop condition;
9. emergency-stop human confirmation path.
