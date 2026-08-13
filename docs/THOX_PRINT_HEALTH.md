# Live print health and closed-loop control

Watch any running job with a parallel ensemble of vision models, score defects
by severity and confidence, and act through Moonraker under a configurable
autonomy policy.

This is the `ai_pipeline/thox/` layer. It mounts on the existing AI pipeline
server, or runs standalone with a much lighter dependency set.

## What it can and cannot tell you

Read this before enabling autonomous action.

**Confidence numbers are not calibrated probabilities.** Providers report 0..1,
but the scales are not comparable across providers. A vision-language model's
`0.9` means "confidently phrased", not "90% of frames like this are failures".
Calibration would need labelled captures from this specific machine and camera.
That is why auto-pause ships **disabled**, why consecutive confirmations are
required, and why the severity an operator sees is dominated by the defect kind
rather than by a model's self-report.

**The classical provider is a detector, not a classifier.** Frame differencing
can say "a lot changed suddenly" and "this frame is too dark to judge". It
cannot tell spaghetti from warping — those look nothing alike to a human and
identical to a difference image. It exists to be cheap, always-on, and to
escalate the cadence so the language models get asked sooner.

**With no model configured, you have a tripwire.** The UI says so explicitly
rather than showing a reassuring green tick. A monitor that overstates its
coverage is worse than no monitor.

**Verified false positive.** With finished parts still on the plate and no job
running, the VLM reported "white blobs on the print bed" at 0.90 confidence —
a correct description of the pixels and a completely wrong conclusion. This is
why the monitor samples **only while `state == printing`**.

## Detection

| Defect | Urgency | Pausing helps? |
|---|---|---|
| `spaghetti` | critical | yes |
| `detachment` | critical | yes |
| `print_came_loose` | critical | yes |
| `layer_shift` | critical | yes |
| `nozzle_clog` | critical | yes |
| `adhesion` | serious | yes |
| `first_layer` | serious | yes |
| `warping` | serious | **no** |
| `under_extrusion` | serious | no |
| `over_extrusion` | cosmetic | no |
| `stringing` | cosmetic | no |
| `blob` | cosmetic | no |
| `camera_fault` | observational | n/a |

"Pausing helps" is load-bearing. Pausing a print with stringing achieves nothing
except a blob where the nozzle sat, so the agent must not helpfully pause for
it. Pausing a detaching print lets someone rescue it.

Severity blends consequence and certainty, weighted toward consequence:

```
severity = 0.65 * kind.severity_floor + 0.35 * confidence
```

A *confident* report of stringing therefore stays below the action threshold
while a *tentative* report of spaghetti clears it — because what an operator is
choosing between is consequences, not the model's certainty.

## Sampling

Two speeds, because a language-model call costs seconds to minutes:

| Situation | Interval | Who runs |
|---|---|---|
| Routine | 45 s | tripwire only (~20 ms) |
| First 3 layers | 20 s | tripwire + models |
| Anything suspected | 12 s | tripwire + models |
| Every 10th routine sample | — | tripwire + models |

The periodic deep check exists because a frame-to-frame change detector is
structurally blind to slow failures such as gradual warping.

A defect must appear on `confirm_frames` (default 3) **consecutive** samples
before anything happens. Because cadence escalates on suspicion, confirmation
takes about 36 s, not three normal intervals.

## Autonomy

| Level | Agent may |
|---|---|
| `observe` | nothing — alerts only |
| `suggest` *(default)* | nothing — raises a suggestion a human confirms |
| `auto_pause` | pause, above the confidence threshold |

**The agent can never cancel or reprint, at any level.** Pausing is reversible
and costs minutes. Cancelling discards hours of work and filament on the
strength of a model's opinion about a 640×480 JPEG. This is enforced in
`interlock.py`, asserted by tests at every autonomy level, and is not
configurable.

Other guards:

- **Re-checked before every action.** A print can be started from the printer's
  touchscreen at any moment, so state is never cached.
- **Cooldown.** Autonomous actions are rate-limited (default 300 s) so a
  flapping detector cannot pause/resume in a loop.
- **Positive state lists.** Each action declares the states it is legal in;
  anything unrecognized refuses rather than falling through.
- **`M112` is unreachable.** No code path from the agent reaches the firmware
  halt.

## Revise and reprint

A confirmed defect maps to bounded parameter changes, split by whether they can
be applied without re-slicing:

**In place** — nozzle/bed/chamber temperature, flow, feedrate. Injected as
G-code into a copy of the original file, so a corrected reprint can start
immediately.

**Requires re-slicing** — brim/raft, supports, retraction, first-layer height,
orientation. There is no honest way to inject these: retraction distance is
baked into thousands of individual extrusion moves. They are emitted as a config
patch with a changelog, and the revision says plainly that it is not directly
printable.

Overrides are injected **after** the start sequence, detected by the first
extruding move. Placed before the slicer's own `M109`/`M190`, they would be
silently overwritten and the "revised" file would behave identically to the
original.

Every adjustment is clamped to a safe absolute range on every attempt, and the
loop stops after `max_reprint_attempts` (default 2) rather than printing the
same failure repeatedly with slightly different numbers.

## Running it

Full pipeline (needs torch, TRELLIS, trimesh):

```bash
python ai_pipeline/server.py --port 7861     # mounts /thox automatically
```

Print health only (needs flask, requests, numpy, Pillow — about 40 MB):

```bash
python -m thox.app --port 7862 --monitor
```

The split is deliberate. Monitoring wants to run continuously next to the
printer, possibly on a small always-on box; mesh generation wants a GPU on
demand. Forcing one install to carry the other's dependencies would mean either
no monitoring on the small box or a CUDA stack installed to watch a webcam.

## Configuration

Environment only, `THOX_*` prefix, never logged. See `.env.example`.

```bash
THOX_PRINTER_HOST=10.1.10.153        # REQUIRED, no default on purpose
THOX_AUTONOMY=suggest                # observe | suggest | auto_pause
THOX_PROVIDERS=cv_motion,ollama_local
THOX_OLLAMA_BASE_URL=http://10.1.10.7:11434
```

`THOX_OPENAI_API_KEY=ollama` — a common local-shim convention — is treated as
**unset**, so the provider reports itself unavailable instead of joining the
ensemble and failing every call.

## API

All routes are loopback-only by default. This API can pause and cancel prints,
so an unauthenticated port any web page could reach would let a visited site
stop someone's print.

| Method | Route | Notes |
|---|---|---|
| GET | `/thox/health` | layer status, providers, redacted settings |
| GET | `/thox/printer` | state, cameras, legal actions per actor |
| POST | `/thox/monitor/start` · `/stop` | |
| GET | `/thox/monitor/state` | latest verdict, suspicions, thresholds |
| GET | `/thox/monitor/frame` | most recent analyzed JPEG |
| GET | `/thox/events?since=N` | incremental event log |
| POST | `/thox/control/{pause,resume,cancel,reprint}` | |
| POST | `/thox/revise/plan` · `/apply` | |
| POST | `/thox/scan/{plan,run,reference}` | |
| GET | `/thox/jobs/{id}` | poll background work |

**409 means refused, not broken.** The printer was not touched and the operator
can clear the condition. Reserving 500 for genuine faults is what lets a UI say
"finish your print first" instead of "error".

## A camera bug worth knowing

Moonraker reports camera URLs as relative paths and says nothing about which
port serves them. Resolving them against Moonraker's own origin is the obvious
guess and is **wrong on the Q2**: `:7125/webcam/?action=snapshot` returns 404
while plain port 80 returns a 26 KB JPEG, because the stock image proxies
crowsnest through nginx on 80. `moonraker.py` probes candidate origins and
caches the one that actually serves frames.
