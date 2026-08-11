# HANDOFF.md — ThoxForge (QidiStudio-AI) Agent Handoff

**Last updated:** 2026-08-11
**Repo:** ttracx/QidiStudio-AI (fork of QIDITECH/QIDIStudio)

## Quick Start for New Agents

1. Read `README.md` — architecture, AI pipeline, and build instructions
2. The AI pipeline sidecar runs on port 7861: `ai_pipeline/server.py`
3. C++ GUI integration: `AIPhotoTo3DDialog` + `AIPipelineClient` via libcurl
4. Upstream remote: QIDITECH/QIDIStudio — keep in sync

## Agent Team Dispatch Protocol

| Team | Domain | Scope |
|------|--------|-------|
| **Pipeline Team** | AI mesh generation (TRELLIS, TripoSR), mesh repair | `ai_pipeline/` |
| **GUI Team** | C++ wxWidgets integration, File→Import menu | `src/slic3r/GUI/` |
| **Build Team** | CMake, macOS bundling, dependency management | Root build files |
| **Docs Team** | README, handoff, architecture docs | Root docs |

### Rules for Parallel Agent Teams
- Pipeline (Python) and GUI (C++) are independent — can work in parallel
- Mesh repair pipeline must produce watertight/manifold output
- Contract with QidiStudio-AI's AIPipelineClient: form field "flatten", file part "images[]", response headers X-ThoxForge-*
- Always push to a feature branch first, then PR to main

## Current State (2026-08-11)

- AI pipeline: Flask sidecar on port 7861, TRELLIS + TripoSR backends
- Mesh repair: trimesh watertight/manifold/flatten/scale
- GUI: File→Import→AI Photo-to-3D Mesh menu item
- All smoke tests pass on macOS
- License: AGPL-3.0 (inherited from QIDIStudio)

## Key Commands
```bash
# Start AI pipeline sidecar
cd ai_pipeline && python3 server.py

# Build QidiStudio (macOS)
mkdir build && cd build && cmake .. && make -j$(sysctl -n hw.ncpu)

# Push (macOS LibreSSL workaround)
git -c http.version=HTTP/1.1 -c http.postBuffer=524288000 push
```
