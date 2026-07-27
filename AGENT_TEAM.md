# ThoxForge Agent Team

## Overview

ThoxForge uses a multi-agent architecture to convert photos of physical devices into watertight 3D-printable meshes. The system is integrated directly into QidiStudio as a fork, adding an AI pipeline alongside the existing slicer functionality.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    QidiStudio (C++ GUI)                      │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  File → Import → "AI Photo-to-3D Mesh..."              │  │
│  │  AIPhotoTo3DDialog (wxWidgets)                        │  │
│  │  AIPipelineClient (libcurl HTTP)                      │  │
│  └───────────────────────┬───────────────────────────────┘  │
│                          │ HTTP :7861                         │
│  ┌───────────────────────▼───────────────────────────────┐  │
│  │            AI Pipeline Server (Python)                │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Agent 1: Background Removal (rembg)            │  │  │
│  │  │  Strips backgrounds from input photos           │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Agent 2: AI Mesh Generation                     │  │  │
│  │  │  ┌─ TRELLIS Backend (primary, high quality) ──┐  │  │  │
│  │  │  │  Microsoft TRELLIS-image-large               │  │  │  │
│  │  │  │  Single + multi-image support               │  │  │  │
│  │  │  │  Structured 3D latents → GLB                │  │  │  │
│  │  │  └─────────────────────────────────────────────┘  │  │  │
│  │  │  ┌─ TripoSR Backend (fallback, fast) ─────────┐  │  │  │
│  │  │  │  stabilityai/TripoSR                        │  │  │  │
│  │  │  │  Single image → mesh in seconds            │  │  │  │
│  │  │  └─────────────────────────────────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Agent 3: Mesh Repair & Manifoldification       │  │  │
│  │  │  trimesh + pymeshlab + open3d                   │  │  │
│  │  │  • Remove degenerate faces                      │  │  │
│  │  │  • Fill holes                                   │  │  │
│  │  │  • Fix winding/normals                          │  │  │
│  │  │  • Merge vertices                               │  │  │
│  │  │  • Flatten bottom for build plate              │  │  │
│  │  │  • Scale to target dimensions (mm)             │  │  │
│  │  │  • Simplify to max face count                   │  │  │
│  │  │  • Verify watertight + manifold                 │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Agent 4: Export & Format Conversion             │  │  │
│  │  │  STL / OBJ / GLB / 3MF output                    │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Agent Roles

### Agent 1: Background Removal
- **Library**: rembg (U2Net model)
- **Input**: Raw photo (PNG/JPG/BMP/WEBP/TIFF)
- **Output**: Image with transparent/white background
- **Purpose**: AI mesh generators perform significantly better when only the device is visible
- **Fallback**: If rembg unavailable, passes image through unmodified

### Agent 2: AI Mesh Generation
Two backends, selected automatically or manually:

#### TRELLIS (Primary)
- **Model**: microsoft/TRELLIS-image-large (~16GB weights)
- **Architecture**: Structured 3D Latents for Scalable 3D Generation (CVPR'25)
- **Input**: 1+ preprocessed images
- **Output**: 3D Gaussians + mesh → GLB
- **Quality**: Superior geometric accuracy for structured devices
- **Hardware**: NVIDIA GPU (CUDA), 128GB RAM recommended
- **Speed**: 10-60 seconds depending on quality preset

#### TripoSR (Fallback)
- **Model**: stabilityai/TripoSR
- **Architecture**: Large Reconstruction Model (LRM) based
- **Input**: Single image
- **Output**: Mesh (vertices + faces)
- **Quality**: Rough 3D shape, good for quick previews
- **Speed**: 2-10 seconds

### Agent 3: Mesh Repair & Manifoldification
This agent automates what a human would do in Blender's 3D Print Toolbox:
1. **Remove degenerate faces** — zero-area triangles
2. **Remove duplicate faces** — identical triangle copies
3. **Merge vertices** — weld vertices within epsilon distance
4. **Fill holes** — close open boundaries
5. **Fix winding** — ensure consistent face orientation
6. **Fix inversions** — correct negative-volume regions
7. **Fix normals** — outward-pointing surface normals
8. **Make watertight** — if trimesh can't, try pymeshlab; if that fails, convex hull fallback
9. **Flatten bottom** — slice mesh at Z threshold, cap the cross-section, so it sits flat on the build plate
10. **Scale to dimensions** — resize to user-specified real-world mm dimensions
11. **Simplify** — if face count exceeds max, use quadric decimation
12. **Verify** — check is_watertight and is_winding_consistent

### Agent 4: Export & Format Conversion
- **STL**: Binary STL (standard for 3D slicers)
- **OBJ**: Wavefront OBJ (with material if GLB input)
- **GLB**: Binary glTF (preserves textures)
- **3MF**: 3D Manufacturing Format (color + metadata)

## Pipeline Flow

```
[User opens dialog]
       │
       ▼
[Add photos (drag-drop or file picker)]
       │
       ▼
[Configure: backend, quality, dimensions, format]
       │
       ▼
[Click "Generate 3D Mesh"]
       │
       ├──▶ Agent 1: Remove backgrounds (rembg)
       │
       ├──▶ Agent 2: AI inference (TRELLIS or TripoSR)
       │    └── Downloads model weights on first run (~16GB for TRELLIS)
       │
       ├──▶ Agent 3: Repair & make manifold
       │    ├── Remove degenerate/duplicate faces
       │    ├── Merge vertices, fill holes
       │    ├── Fix winding, normals, inversions
       │    ├── Flatten bottom for build plate
       │    ├── Scale to target dimensions
       │    ├── Simplify if over max face count
       │    └── Verify watertight + manifold
       │
       └──▶ Agent 4: Export to selected format (STL/OBJ/GLB/3MF)
              │
              ▼
        [Download mesh file]
              │
              ├──▶ [Import to Plater] — loads directly into QidiStudio
              └──▶ [Save As...] — save to disk for later use
```

## Quality Presets

| Preset  | TRELLIS Steps | TripoSR Steps | Texture | Simplify | Use Case |
|---------|--------------|---------------|---------|----------|----------|
| Draft   | 4            | 8             | 512px   | 0.95     | Quick preview |
| Medium  | 8            | 32            | 1024px  | 0.95     | Balanced |
| High    | 12           | 64            | 1024px  | 0.95     | Devices (default) |
| Ultra   | 16           | 128           | 2048px  | 0.90     | Maximum detail |

## Hardware Requirements

### Minimum (TripoSR only)
- NVIDIA GPU with 6GB VRAM
- 16GB RAM
- Python 3.10/3.11

### Recommended (TRELLIS + TripoSR)
- NVIDIA GPU with 12GB+ VRAM (RTX 4060 Ti or better)
- 128GB RAM
- Python 3.10/3.11
- CUDA Toolkit 12.1+
- Visual Studio with C++ workload (Windows)

### TRELLIS CUDA Extensions
- nvdiffrast
- diff-gaussian-rasterization
- simple-knn

## API Reference

### Endpoints

| Method | Path             | Description |
|--------|------------------|-------------|
| GET    | /health          | Server health + CUDA status |
| GET    | /backends        | List available backends |
| POST   | /backends        | Switch active backend |
| POST   | /generate        | Multipart upload → binary mesh file |
| POST   | /generate_json   | JSON body → JSON + base64 mesh |

### POST /generate Parameters

| Field       | Type   | Default | Values |
|-------------|--------|---------|--------|
| images[]    | file[] | —       | PNG/JPG/BMP/WEBP/TIFF (required) |
| backend     | string | "trellis" | "trellis", "triposr", "auto" |
| quality     | string | "high"  | "draft", "medium", "high", "ultra" |
| seed        | int    | 1       | Any integer |
| flatten     | string | "true"  | "true", "false" |
| remove_bg   | string | "true"  | "true", "false" |
| max_faces   | int    | 500000  | 1000-5000000 |
| format      | string | "stl"   | "stl", "obj", "glb", "3mf" |
| width_mm    | float  | 0       | Target width (0 = auto) |
| depth_mm    | float  | 0       | Target depth (0 = auto) |
| height_mm   | float  | 0       | Target height (0 = auto) |

### Response Headers

| Header                    | Description |
|---------------------------|-------------|
| X-ThoxForge-Watertight    | "true"/"false" |
| X-ThoxForge-Manifold      | "true"/"false" |
| X-ThoxForge-Vertices      | Vertex count |
| X-ThoxForge-Faces         | Face count |
| X-ThoxForge-Backend       | Backend used |
| X-ThoxForge-Elapsed       | Time in seconds |

Response body: binary mesh file (STL/OBJ/GLB/3MF)