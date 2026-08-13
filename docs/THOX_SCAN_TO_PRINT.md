# Scan to print

Place an object on the bed; get back a printable plate. The printer photographs
it from a ladder of bed heights, the silhouettes are carved into a watertight
mesh, and the result is plated for the Q2's 270 × 270 × 256 mm volume.

## How this differs from AI Photo-to-3D

Both live in this repo and they are not the same tool.

| | **AI Photo-to-3D** (`server.py`) | **Scan to Print** (`thox/scan/`) |
|---|---|---|
| Input | photos you supply | the printer's own camera |
| Method | generative (TRELLIS / TripoSR) | measured (visual hull) |
| Unseen surfaces | **invented**, plausibly | **not produced** |
| Scale | inferred | metric, from the calibrated bed |
| Needs | GPU, torch, TRELLIS | numpy, Pillow |

Generative inference gives you a whole object from one photo and will confidently
invent the back of it. Scanning gives you a measured hull of the side the camera
can actually see. Pick by whether you need a *plausible whole* or a *trustworthy
part*.

## What the rig can and cannot measure

Probed live from the machine, not assumed:

| Property | Value |
|---|---|
| Kinematics | `corexy` — toolhead owns X/Y |
| Bed axes | **Z only** |
| Cameras | **exactly one**, frame-fixed |
| Sensor | 640 × 480 MJPEG, 15 fps |
| Z travel | −2 … 265 mm |

**Nothing rotates the object.** The camera is bolted to the frame, so toolhead
motion buys zero new viewpoints. A single automated pass sees only the side
facing the camera.

What the rig *does* give is a **metrically calibrated translation stage**. Every
baseline is a Klipper Z command, known far more precisely than the camera can
resolve. Ordinary photogrammetry is scale-ambiguous and needs a ruler in shot;
here millimetres come out because millimetres went in.

Because the camera sits at a fixed point while the object rides past it, a Z
sweep also samples an **arc of elevation angles** — about 36° at one azimuth —
so it is not merely a zoom.

## Measured accuracy

From `tests/` — a 40 × 25 × 15 mm box rendered through the same calibration the
carver uses, then reconstructed. This isolates geometric error from sensor and
segmentation error, so treat it as a **best case**.

Errors are signed because their sign is not random: a visual hull is provably a
superset of the object, so a correct pipeline over-estimates.

### Four passes (you rotate the object 90° between each)

| Axis | Measured | True | Error |
|---|---|---|---|
| Width | 42.00 | 40.0 | **+2.00 mm** |
| Depth | 27.00 | 25.0 | **+2.00 mm** |
| Height | 16.00 | 15.0 | **+1.00 mm** |
| Volume | 15 834 mm³ | 15 000 | 1.06× |

**This is the mode worth using.**

### One pass (walk-away)

| Axis | Measured | True | Error |
|---|---|---|---|
| Width | 44.00 | 40.0 | +4.00 mm |
| Depth | **52.00** | 25.0 | **+27.00 mm** |
| Height | 23.00 | 15.0 | +8.00 mm |
| Volume | 24 906 mm³ | 15 000 | 1.66× |

**One pass is not a dimensioning mode.** With nothing rotating the object, the
axis running away from the camera is essentially unconstrained. Every
single-pass measurement is graded `unreliable` in code; quoting a ±4.5 mm
tolerance next to a 27 mm error would be worse than quoting nothing.

### Never recovered

Concave features, pockets, undercuts, surface texture, threads and fillets below
~2 mm. Space carving is blind to anything that does not change a silhouette —
that is a property of the method, not a tuning parameter.

## What it is genuinely good for

Not "clone the object" — **"measure it well enough to print something that fits
it"**. Trays, cradles, holders. Fit depends on bounding geometry and footprint,
which are the two things this rig measures best. The pipeline emits a fitted
tray alongside the replica for exactly this reason.

## Not a substitute for a real scanner

A consumer structured-light unit delivers 0.05–0.1 mm with true 360° coverage
from an actual turntable. This exists because the printer is already on the
network, already has a camera, and already knows its own geometry to the micron.

## Using it

**In QIDI Studio:** File ▸ Import ▸ *Scan Object on Bed…*

**Over HTTP:**

```bash
curl -X POST localhost:7861/thox/scan/reference \
     -d '{"object_height_mm":30}'          # once per machine, EMPTY bed
curl -X POST localhost:7861/thox/scan/plan  # preview; moves nothing
curl -X POST localhost:7861/thox/scan/run \
     -d '{"azimuths":4,"object_height_mm":30}'
```

The reference ladder is captured once and cached by Z. An empty-bed frame
depends only on Z, not on the object, so every later scan reuses it — otherwise
each scan would need the bed cleared and a second full sweep.

## Safety

Scanning is the only thing here that moves the printer for a non-printing
reason, so it adds an interlock above the G-code gate:

- refuses while a job is `printing` or `paused`, **re-checked before every
  move**;
- refuses unless Z is homed — homing drives the nozzle down to probe, so with a
  finished print still on the plate that is a collision. It stays a deliberate
  human action;
- refuses above 60 °C, because the operator's hands go near the bed;
- Z-only moves, envelope-clamped, with the object's height reserved from the
  top of the window before anything moves;
- `M400` plus a position poll plus a dwell before every frame. `M400` returns
  when Klipper finishes *planning*, while a 250 mm bed on lead screws is still
  ringing — a frame grabbed mid-ring is wrong in a way nothing downstream can
  detect.
