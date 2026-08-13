"""THOX printer-agent layer for QidiStudio-AI.

Two capabilities layered onto the existing AI pipeline, sharing its server,
its port and its security posture:

**Scan to print** - drive the printer's own bed as a calibrated stage, capture
the object from a ladder of heights, reconstruct a visual hull, and emit a
plate for the Q2's 270 x 270 x 256 mm volume.

**Live print health** - watch any running job with a parallel ensemble of vision
models, score defects by severity and confidence, and act through Moonraker
under a configurable autonomy policy with safety interlocks on every command.

Read ``docs/THOX_PRINT_HEALTH.md`` and ``docs/THOX_SCAN_TO_PRINT.md`` before
trusting a number or enabling autonomous action.
"""

__version__ = "0.1.0"
