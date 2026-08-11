# ThoxForge — QidiStudio + AI Photo-to-3D Mesh Pipeline

> Fork of [QIDIStudio](https://github.com/QIDITECH/QIDIStudio) with an integrated AI pipeline that converts photos of physical devices into watertight (manifold) 3D-printable meshes.

## Readiness status

This repository is a **host-tested MVP**, not a signed production release.

| Area | Evidence-backed status |
|---|---|
| Local sidecar API | Contract/security tests pass without model weights |
| Mesh repair/export | Synthetic smoke tests pass |
| Desktop request path | Real libcurl multipart client is wired; full QIDIStudio build is pending |
| TRELLIS/TripoSR inference | Requires a compatible NVIDIA/CUDA host and locally installed model runtime; not validated in this repository snapshot |
| macOS distribution | No signed/notarized app artifact is present |
| Windows/Linux distribution | CI definitions exist; current fork CI results and install artifacts must be verified before release |

See [security_model.md](docs/security_model.md) and
[development_queue.md](docs/development_queue.md) for exact boundaries and
release gates.

## What It Does

ThoxForge adds an **AI Photo-to-3D** menu item to QidiStudio's File → Import menu. Click it, drop in one or more photos of a physical device, and the system:

1. **Removes the background** from each photo (rembg/U2Net)
2. **Generates a 3D mesh** using Microsoft TRELLIS (primary) or TripoSR (fallback)
3. **Repairs and makes watertight** — removes degenerate faces, fills holes, fixes winding/normals, flattens the bottom for build-plate adhesion, scales to real-world mm dimensions
4. **Imports the STL/3MF directly into the plater** — ready to slice and print

```
[Photos] → [Background Removal] → [AI Mesh Gen (TRELLIS/TripoSR)] → [Mesh Repair] → [Manifold STL] → [QidiStudio Plater]
```

## Why This Exists

Single-photo AI mesh generators (TRELLIS, TripoSR) produce impressive 3D shapes but:
- The output is rarely watertight/manifold — slicers reject it
- The bottom is curved — won't adhere to the build plate
- The scale is unknown — not real-world dimensions
- There's no integration with 3D printing software

ThoxForge automates the full pipeline: photo → AI mesh → repair → printable STL → slicer, all inside QidiStudio.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│           QidiStudio (C++ / wxWidgets)               │
│                                                      │
│  File → Import → "AI Photo-to-3D Mesh..."            │
│  ┌─────────────────────────────────────────────┐    │
│  │  AIPhotoTo3DDialog (GUI)                      │    │
│  │  • Image drop zone + file picker             │    │
│  │  • Backend selector (TRELLIS/TripoSR/Auto)   │    │
│  │  • Quality presets (Draft/Medium/High/Ultra) │    │
│  │  • Target dimensions (mm)                    │    │
│  │  • Progress bar + status                     │    │
│  │  • Import to Plater / Save As                 │    │
│  └──────────────────┬──────────────────────────┘    │
│                     │ HTTP :7861 (libcurl)             │
│  ┌──────────────────▼──────────────────────────┐    │
│  │  AIPipelineClient (C++ libcurl HTTP client)  │    │
│  │  Multipart file upload → binary mesh download│    │
│  └──────────────────┬──────────────────────────┘    │
│                     │                                 │
└─────────────────────┼─────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────┐
│        AI Pipeline Server (Python / Flask)            │
│                                                        │
│  ai_pipeline/server.py — port 7861                     │
│  ┌─────────────────────────────────────────────┐      │
│  │  1. Background Removal (rembg)               │      │
│  │  2. AI Mesh Generation                       │      │
│  │     • TRELLIS (microsoft/TRELLIS-image-large)│      │
│  │     • TripoSR (stabilityai/TripoSR)          │      │
│  │  3. Mesh Repair (trimesh + pymeshlab)        │      │
│  │     • Make watertight/manifold               │      │
│  │     • Flatten bottom                         │      │
│  │     • Scale to dimensions                    │      │
│  │     • Simplify                               │      │
│  │  4. Export (STL/OBJ/GLB/3MF)                 │      │
│  └─────────────────────────────────────────────┘      │
└────────────────────────────────────────────────────────┘
```

## Requirements

### QidiStudio (C++ side)
- Same as [QidiStudio build requirements](https://github.com/QIDITECH/QIDIStudio#building):
  - CMake 3.13+
  - wxWidgets 3.1+
  - Boost, TBB, glew, and other deps (see `deps/`)
  - Visual Studio (Windows) or Xcode (macOS) or GCC (Linux)
  - **libcurl** (already a QidiStudio dependency)

### AI Pipeline Server (Python side)
- **Python 3.10 or 3.11**
- **NVIDIA GPU with CUDA** (12GB+ VRAM for TRELLIS, 6GB for TripoSR)
- **CUDA Toolkit 12.1+**
- **Visual Studio with "Desktop development with C++"** (Windows, for building CUDA extensions)
- 128GB RAM recommended (for TRELLIS model loading)

## Setup

### 1. Clone This Repo

```bash
git clone <your-fork-url> QidiStudio-AI
cd QidiStudio-AI
```

### 2. Set Up the AI Pipeline Server

```bash
cd ai_pipeline

# Create virtual environment
python -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Clone TRELLIS (primary backend)
git clone https://github.com/microsoft/TRELLIS.git
cd TRELLIS
pip install -r requirements.txt
pip install extensions/nvdiffrast
pip install extensions/diff-gaussian-rasterization
pip install extensions/simple-knn
cd ..

# Clone TripoSR (fallback backend, optional but recommended)
git clone https://github.com/VAST-AI-Research/TripoSR.git
cd TripoSR
pip install -r requirements.txt
cd ..

# Start the server
python server.py --port 7861 --preload
```

Or use the convenience script:
```bash
./start_server.sh --preload
```

The server runs at `http://127.0.0.1:7861`. On first run, TRELLIS downloads ~16GB of model weights from Hugging Face.

That first-run download is the only intended model-acquisition network path.
For an offline/private deployment, pre-stage the model repositories and Python
packages before disconnecting the host. Runtime image processing and generation
use the local sidecar and local model runtime. No telemetry is added by
ThoxForge.

The API is local-only by default. Non-loopback hosts are rejected unless the
operator supplies both a non-loopback `--host` and `--allow-remote`. Remote mode
does not provide authentication or transport encryption and is not approved for
sensitive data.

### 3. Build QidiStudio

Follow the [standard QidiStudio build instructions](https://github.com/QIDITECH/QIDIStudio#building), with one addition:

The AI pipeline files are automatically included via `src/slic3r/CMakeLists.txt`. No additional CMake flags needed.

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j
```

### 4. Run

1. Start the AI Pipeline Server (step 2 above)
2. Launch QidiStudio
3. **File → Import → AI Photo-to-3D Mesh...**
4. Add photos (drag-drop or browse)
5. Select backend, quality, target dimensions
6. Click **Generate 3D Mesh**
7. Click **Import to Plate** or **Save As...**

## Using the AI Pipeline

### Quality Presets

| Preset | Speed | Quality | Use Case |
|--------|-------|---------|----------|
| Draft | ~5s | Rough shape | Quick preview |
| Medium | ~15s | Good | General use |
| High | ~30s | Excellent | Devices (default) |
| Ultra | ~60s | Maximum | Final production |

### Tips for Best Results

1. **Use isolated photos**: Remove busy backgrounds before upload (or enable auto-remove background)
2. **Multiple photos help**: TRELLIS supports multi-image input for better reconstruction of hidden sides
3. **Good lighting**: Even, diffused lighting produces better meshes
4. **Flat surface**: Place device on a flat surface for at least one photo
5. **Set target dimensions**: Enter real-world mm dimensions for accurate scaling
6. **Keep flatten bottom on**: Ensures the mesh adheres to the build plate

### Backend Selection

- **Auto**: Uses TRELLIS, falls back to TripoSR if TRELLIS fails (recommended)
- **TRELLIS**: Best geometric accuracy for structured devices. Requires more GPU/RAM.
- **TripoSR**: Fast single-image reconstruction. Good for quick previews.

## API Reference

The AI Pipeline Server exposes a REST API:

```bash
# Check server health
curl http://127.0.0.1:7861/health

# Generate mesh from image
curl -X POST http://127.0.0.1:7861/generate \
  -F "images[]=@device_photo.jpg" \
  -F "backend=auto" \
  -F "quality=high" \
  -F "flatten=true" \
  -F "remove_bg=true" \
  -F "format=stl" \
  -o output.stl

# Check response headers for metadata
curl -X POST http://127.0.0.1:7861/generate \
  -F "images[]=@device_photo.jpg" \
  -D headers.txt \
  -o output.stl

# headers.txt will contain:
# X-ThoxForge-Watertight: true
# X-ThoxForge-Manifold: true
# X-ThoxForge-Vertices: 12345
# X-ThoxForge-Faces: 24690
# X-ThoxForge-Backend: trellis
# X-ThoxForge-Elapsed: 28.3
```

See [AGENT_TEAM.md](AGENT_TEAM.md) for full API documentation.

### Sidecar validation

These checks do not download model weights or require a GPU:

```bash
cd ai_pipeline
python -m pip install --requirement requirements-ci.txt
python -m compileall -q server.py test_server_api.py
python -m unittest -v test_server_api.py
python test_pipeline.py
```

Request controls include a 64 MiB total request limit, eight-image maximum,
16 MiB per-image maximum, image decode/pixel validation, bounded mesh settings,
loopback host enforcement, browser origin rejection, and sanitized error
responses.

## Packaging and release boundary

`BuildMac.sh` can produce an unsigned local `.app` after the QIDIStudio
dependencies are built. This repository does not currently include a hardened
runtime/signing configuration, Developer ID evidence, notarization submission,
stapling evidence, or a distributable DMG/PKG. Do not label a local build as a
notarized macOS release.

Minimum macOS release proof still required:

```bash
codesign --verify --deep --strict --verbose=2 QIDIStudio.app
spctl --assess --type execute --verbose=4 QIDIStudio.app
xcrun stapler validate QIDIStudio.app
```

Run those commands against the actual exported artifact after signing and
notarization; source settings alone are not distribution proof.

## File Structure

```
QidiStudio-AI/
├── ai_pipeline/                 # Python AI Pipeline Server
│   ├── server.py                # Flask HTTP server with mesh generation + repair
│   ├── requirements.txt         # Python dependencies
│   ├── start_server.sh          # Convenience startup script
│   ├── TRELLIS/                 # Cloned TRELLIS repo (not committed)
│   └── TripoSR/                 # Cloned TripoSR repo (not committed)
├── src/slic3r/GUI/
│   ├── AIPhotoTo3DDialog.hpp    # Dialog header (wxWidgets)
│   ├── AIPhotoTo3DDialog.cpp    # Dialog implementation
│   ├── AIPipelineClient.hpp     # HTTP client header (libcurl)
│   └── AIPipelineClient.cpp     # HTTP client implementation
├── src/slic3r/CMakeLists.txt    # Modified to include new files
├── src/slic3r/GUI/MainFrame.cpp # Modified to add menu entry
├── AGENT_TEAM.md                # Agent architecture documentation
└── README.md                    # This file
```

## Resources & References

- [Microsoft TRELLIS](https://github.com/microsoft/TRELLIS) — Structured 3D Latents (CVPR'25)
- [TripoSR](https://github.com/VAST-AI-Research/TripoSR) — Fast image-to-3D model
- [AutoForge](https://github.com/hvoss-tech/AutoForge) — Image-to-STL heightmap (reference)
- [Modly3D](https://github.com/lightningpixel/modly) — Desktop AI image-to-3D (reference)
- [QIDIStudio](https://github.com/QIDITECH/QIDIStudio) — Base slicer
- [BambuStudio](https://github.com/bambulab/BambuStudio) — Upstream
- [PrusaSlicer](https://github.com/prusa3d/PrusaSlicer) — Original upstream
- [trimesh](https://github.com/mikedh/trimesh) — Python mesh processing
- [PyMeshLab](https://github.com/cnr-isti-vclab/PyMeshLab) — Advanced mesh repair
- [rembg](https://github.com/danielgatis/rembg) — Background removal

## License

QIDIStudio is licensed under **GNU AGPL v3**. This fork inherits the same license.
All new files (AI pipeline, GUI integration) are also AGPL v3.

See [LICENSE](LICENSE) for details.

## For AI Agents

**Read [`HANDOFF.md`](HANDOFF.md) first.** It tells you the current state, active branches, and what each agent team owns.

### Agent Team Dispatch
| Team | Domain | Scope |
|------|--------|-------|
| **Pipeline Team** | AI mesh generation (TRELLIS, TripoSR), mesh repair | `ai_pipeline/` |
| **GUI Team** | C++ wxWidgets integration, File→Import menu | `src/slic3r/GUI/` |
| **Build Team** | CMake, macOS bundling, dependency management | Root build files |
| **Docs Team** | README, handoff, architecture docs | Root docs |

**Rules:**
- Pipeline (Python) and GUI (C++) are independent — can work in parallel
- Mesh repair pipeline must produce watertight/manifold output
- Contract with AIPipelineClient: form field "flatten", file part "images[]", response headers X-ThoxForge-*
- Push to feature branches, PR to main

## Contributing

This is a fork for THOX.ai internal use. PRs welcome.

```bash
git checkout -b feature/your-feature
git commit -m "feat: your feature"
git push origin feature/your-feature
```

## Acknowledgments

- **QIDI Tech** for QIDIStudio
- **Bambu Lab** for BambuStudio
- **Prusa Research** for PrusaSlicer
- **Microsoft** for TRELLIS
- **StabilityAI + VAST-AI** for TripoSR
- The open-source 3D printing community
