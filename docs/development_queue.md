# Development Queue

| Priority | Status | Task | Acceptance and tests | Security/docs |
|---:|---|---|---|---|
| 9.2 | In progress | Prove full desktop build on macOS, Windows, Linux | Clean-host CI builds compile the dialog/client and publish checksummed artifacts | Run `30545217599` completed dependency configuration and compiled 424/712 macOS application targets, then found an incorrect same-directory `ImGuiWrapper.hpp` include; rerun required |
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
was validated in this repository pass. Clean-host run `30545217599` passed the
dependency and source-manifest blockers, configured the application, and
compiled 424/712 macOS application targets before
`AIPhotoTo3DDialog.hpp` used the invalid same-directory include
`GUI/ImGuiWrapper.hpp`. The corrected include is `ImGuiWrapper.hpp`, consistent
with other GUI sources. A green rerun is still required; partial compilation is
not desktop build proof.
