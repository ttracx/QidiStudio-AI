# Development Queue

| Priority | Status | Task | Acceptance and tests | Security/docs |
|---:|---|---|---|---|
| 9.2 | In progress | Prove full desktop build on macOS, Windows, Linux | Clean-host CI builds compile the dialog/client and publish checksummed artifacts | Run `30454473776` completed the capped macOS dependency build, then found an internal-build source manifest defect; rerun required after gating ignored public-release-only QIDI sources |
| 9.0 | Blocked | Validate real TRELLIS and TripoSR inference | Approved CUDA host generates and imports fixture meshes; topology claims independently checked | No fixture photos leave host; record model hashes/licenses |
| 8.7 | Complete | Harden local sidecar boundary | API tests cover Host/Origin, limits, validation, sanitized failures | `security_model.md` updated |
| 8.5 | Complete | Replace desktop generation placeholder | Dialog calls real libcurl client; cross-platform temp path used | Loopback URL enforced |
| 8.2 | Pending | Package offline runtime | Signed manifest, SBOM, model/package cache, offline smoke test | No setup-time network required |
| 8.0 | Blocked | Sign/notarize macOS artifact | `codesign`, Gatekeeper, notarization, and stapler evidence pass on exported app | Requires Developer ID and notary credentials |
| 7.8 | Pending | Add generated-asset review gate | UI requires explicit review before import/print and records topology status | Human-in-the-loop control |

## Current validation boundary

Python sidecar contract/security tests and synthetic mesh smoke tests pass on a
host without GPU model weights. No full QIDIStudio binary, real-model inference,
printer hardware, signed package, notarization ticket, or clean-host installer
was validated in this repository pass. Clean-host run `30454473776` completed
the macOS dependency build under the four-job cap, then stopped during
application generation because the source manifest unconditionally named
ignored public-release-only files:
`QIDI/UserPresetSyncManager.cpp` and `.hpp`. Those entries now share the
existing `QDT_RELEASE_TO_PUBLIC` gate with the other ignored QIDI networking
sources. A green rerun is still required; successful dependency compilation
and application configuration progress are not desktop build proof.
