#!/usr/bin/env python3
"""
ThoxForge AI Pipeline Server
=============================
Local HTTP sidecar that converts photos of physical devices into
watertight (manifold) 3D-printable meshes.

Architecture:
    [C++ GUI (QidiStudio)] ──HTTP──> [This Python Server]
                                          ├─ Background removal (rembg)
                                          ├─ Mesh generation (TRELLIS / TripoSR)
                                          ├─ Mesh repair (trimesh + pymeshlab)
                                          └─ Manifold verification + STL/3MF export

Usage:
    python server.py --port 7861
    python server.py --port 7861 --backend trellis   # default, high quality
    python server.py --port 7861 --backend triposr   # fast fallback
    python server.py --port 7861 --backend auto      # try TRELLIS, fallback TripoSR
"""

import os
import sys
import io
import time
import tempfile
import argparse
import logging
import ipaddress
import uuid
import warnings
from typing import Optional, List, Tuple
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, UnidentifiedImageError

# Flask for HTTP API
from flask import Flask, request, jsonify, send_file

# Mesh processing
import trimesh
import trimesh.repair

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("thoxforge")

# ---------------------------------------------------------------------------
# Pipeline backends
# ---------------------------------------------------------------------------

class MeshGenerationBackend:
    """Abstract base for mesh generation backends."""
    name = "base"

    def load(self, model_path: Optional[str] = None):
        raise NotImplementedError

    def generate(
        self,
        images: List[Image.Image],
        seed: int = 1,
        quality: str = "high",
        **kwargs
    ) -> trimesh.Trimesh:
        """Return a trimesh.Trimesh from one or more images."""
        raise NotImplementedError


class TRELLISBackend(MeshGenerationBackend):
    """Microsoft TRELLIS — high quality image-to-3D pipeline."""
    name = "trellis"

    def __init__(self):
        self.pipeline = None
        self.postprocessing = None
        self._loaded = False

    def load(self, model_path: Optional[str] = None):
        if self._loaded:
            return
        model_id = model_path or "microsoft/TRELLIS-image-large"
        logger.info(f"[TRELLIS] Loading pipeline from {model_id}...")
        os.environ.setdefault('SPCONV_ALGO', 'native')

        from trellis.pipelines import TrellisImageTo3DPipeline
        from trellis.utils import postprocessing_utils

        self.pipeline = TrellisImageTo3DPipeline.from_pretrained(model_id)
        self.pipeline.cuda()
        self.postprocessing = postprocessing_utils
        self._loaded = True
        logger.info("[TRELLIS] Pipeline loaded and moved to CUDA.")

    def generate(
        self,
        images: List[Image.Image],
        seed: int = 1,
        quality: str = "high",
        **kwargs
    ) -> trimesh.Trimesh:
        self.load()

        # Quality presets
        presets = {
            "draft":  {"ss_steps": 4,  "ss_cfg": 7.5,  "slat_steps": 4,  "slat_cfg": 3.0,  "simplify": 0.95, "tex": 512},
            "medium": {"ss_steps": 8,  "ss_cfg": 7.5,  "slat_steps": 8,  "slat_cfg": 3.0,  "simplify": 0.95, "tex": 1024},
            "high":   {"ss_steps": 12, "ss_cfg": 7.5,  "slat_steps": 12, "slat_cfg": 3.0,  "simplify": 0.95, "tex": 1024},
            "ultra":  {"ss_steps": 16, "ss_cfg": 7.5,  "slat_steps": 16, "slat_cfg": 3.0,  "simplify": 0.90, "tex": 2048},
        }
        p = presets.get(quality, presets["high"])

        if len(images) == 1:
            logger.info(f"[TRELLIS] Running single-image inference (quality={quality})...")
            outputs = self.pipeline.run(
                images[0],
                seed=seed,
                formats=["gaussian", "mesh"],
                preprocess_image=False,
                sparse_structure_sampler_params={
                    "steps": p["ss_steps"],
                    "cfg_strength": p["ss_cfg"],
                },
                slat_sampler_params={
                    "steps": p["slat_steps"],
                    "cfg_strength": p["slat_cfg"],
                },
            )
        else:
            logger.info(f"[TRELLIS] Running multi-image inference ({len(images)} images, quality={quality})...")
            outputs = self.pipeline.run_multi_image(
                images,
                seed=seed,
                formats=["gaussian", "mesh"],
                preprocess_image=False,
                sparse_structure_sampler_params={
                    "steps": p["ss_steps"],
                    "cfg_strength": p["ss_cfg"],
                },
                slat_sampler_params={
                    "steps": p["slat_steps"],
                    "cfg_strength": p["slat_cfg"],
                },
                mode="multidiffusion",
            )

        # Extract GLB
        logger.info("[TRELLIS] Extracting GLB mesh...")
        glb = self.postprocessing.to_glb(
            outputs['gaussian'][0],
            outputs['mesh'][0],
            simplify=p["simplify"],
            texture_size=p["tex"],
            verbose=False,
        )

        # Load GLB into trimesh for post-processing
        with tempfile.NamedTemporaryFile(suffix='.glb', delete=False) as f:
            glb.export(f.name)
            mesh = trimesh.load(f.name, force='mesh')
        os.unlink(f.name)  # cleanup

        # Free GPU memory
        import torch
        torch.cuda.empty_cache()

        logger.info(f"[TRELLIS] Generated mesh: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")
        return mesh


class TripoSRBackend(MeshGenerationBackend):
    """TripoSR — fast single-image-to-3D pipeline."""
    name = "triposr"

    def __init__(self):
        self.model = None
        self._loaded = False

    def load(self, model_path: Optional[str] = None):
        if self._loaded:
            return
        model_id = model_path or "stabilityai/TripoSR"
        logger.info(f"[TripoSR] Loading model from {model_id}...")

        import torch
        from tsr.system import TSR

        self.model = TSR.from_pretrained(
            model_id,
            config_name="config.yaml",
            weight_name="model.ckpt",
        )
        self.model.renderer.set_device("cuda")
        self._loaded = True
        logger.info("[TripoSR] Model loaded.")

    def generate(
        self,
        images: List[Image.Image],
        seed: int = 1,
        quality: str = "high",
        **kwargs
    ) -> trimesh.Trimesh:
        self.load()

        import torch

        # TripoSR only uses the first image
        image = images[0]

        # Quality presets
        step_map = {"draft": 8, "medium": 32, "high": 64, "ultra": 128}
        steps = step_map.get(quality, 64)

        logger.info(f"[TripoSR] Running inference (quality={quality}, steps={steps})...")

        with torch.no_grad():
            scene_codes = self.model([image], device="cuda")
            meshes = self.model.extract_mesh(scene_codes, resolution=256 if quality != "ultra" else 512)

        mesh = meshes[0]
        # TripoSR returns a trimesh-like object
        if not isinstance(mesh, trimesh.Trimesh):
            mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces)

        torch.cuda.empty_cache()
        logger.info(f"[TripoSR] Generated mesh: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")
        return mesh


# ---------------------------------------------------------------------------
# Mesh Repair Pipeline
# ---------------------------------------------------------------------------

class MeshRepairPipeline:
    """
    Automated mesh repair to produce watertight/manifold meshes
    suitable for 3D printing. Mirrors what the Blender "3D Print Toolbox"
    does, but headless and scriptable.
    """

    @staticmethod
    def repair(
        mesh: trimesh.Trimesh,
        flatten_bottom: bool = True,
        target_dimensions_mm: Optional[Tuple[float, float, float]] = None,
        max_faces: int = 500000,
    ) -> trimesh.Trimesh:
        """
        Repair pipeline:
        1. Remove degenerate faces/vertices
        2. Fill holes
        3. Fix winding / normals
        4. Merge vertices
        5. Make watertight
        6. Flatten bottom for build-plate adhesion
        7. Scale to target dimensions
        8. Simplify if over max_faces
        """
        logger.info(f"[Repair] Input: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")

        # Step 1: Remove degenerate faces and duplicate faces
        # trimesh 4.x API: use update_faces with nondegenerate_faces mask
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.remove_unreferenced_vertices()

        # Step 2: Merge close vertices (welding)
        # trimesh 4.x: use digits_vertex for precision control instead of distance
        mesh.merge_vertices(merge_tex=True, digits_vertex=4)
        logger.info(f"[Repair] After merge: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")

        # Step 3: Fill holes
        trimesh.repair.fill_holes(mesh)
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fix_inversion(mesh)
        logger.info(f"[Repair] After hole-fill: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")

        # Step 4: Make watertight — if still not, use convex hull fallback
        if not mesh.is_watertight:
            logger.warning("[Repair] Mesh not watertight after fill_holes. Attempting pymeshlab repair...")
            mesh = MeshRepairPipeline._pymeshlab_repair(mesh)

        # Step 5: Fix normals
        trimesh.repair.fix_normals(mesh)

        # Step 6: Flatten bottom for build plate
        if flatten_bottom:
            mesh = MeshRepairPipeline._flatten_bottom(mesh)
            logger.info("[Repair] Flattened bottom face for build-plate adhesion")

        # Step 7: Scale to target dimensions
        if target_dimensions_mm:
            mesh = MeshRepairPipeline._scale_to_dimensions(mesh, target_dimensions_mm)
            logger.info(f"[Repair] Scaled to target: {target_dimensions_mm} mm")

        # Step 8: Simplify if too many faces
        if len(mesh.faces) > max_faces:
            logger.info(f"[Repair] Simplifying from {len(mesh.faces)} to ~{max_faces} faces...")
            mesh = mesh.simplify_quadric_decimation(max_faces)
            # Re-repair after simplification
            trimesh.repair.fill_holes(mesh)
            trimesh.repair.fix_winding(mesh)
            trimesh.repair.fix_normals(mesh)

        logger.info(f"[Repair] Final: {len(mesh.vertices)} verts, {len(mesh.faces)} faces, "
                     f"watertight={mesh.is_watertight}, watertight_v={mesh.is_watertight}")

        return mesh

    @staticmethod
    def _pymeshlab_repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
        """Use PyMeshLab for advanced repair if trimesh can't make it watertight."""
        try:
            import pymeshlab
        except ImportError:
            logger.warning("[Repair] pymeshlab not available, using trimesh convex hull fallback")
            return MeshRepairPipeline._convex_hull_fallback(mesh)

        with tempfile.NamedTemporaryFile(suffix='.obj', delete=False) as f:
            mesh.export(f.name)
            ms = pymeshlab.MeshSet()
            ms.load_new_mesh(f.name)

            # Close holes
            ms.meshing_close_holes(maxholesize=30, selected=False)
            # Repair non-manifold
            ms.meshing_repair_non_manifold_edges()
            ms.meshing_repair_non_manifold_vertices()
            # Remove degenerate faces
            ms.meshing_remove_t_vertices()
            # Reorient faces
            ms.meshing_re_orient_faces()
            # Clean
            ms.meshing_remove_unreferenced_vertices()

            with tempfile.NamedTemporaryFile(suffix='.obj', delete=False) as out:
                ms.save_current_mesh(out.name)
                repaired = trimesh.load(out.name, force='mesh')

            os.unlink(out.name)
        os.unlink(f.name)

        return repaired

    @staticmethod
    def _convex_hull_fallback(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
        """Last-resort: use convex hull if mesh can't be made watertight."""
        logger.warning("[Repair] Using convex hull fallback for watertightness")
        hull = mesh.convex_hull
        return hull

    @staticmethod
    def _flatten_bottom(mesh: trimesh.Trimesh, threshold_mm: float = 0.5) -> trimesh.Trimesh:
        """
        Flatten the bottom of the mesh so it sits flat on the build plate.
        Cuts the mesh at the lowest Z + threshold and fills the cross-section.
        """
        bounds = mesh.bounds
        z_min = bounds[0, 2]
        z_max = bounds[1, 2]

        if z_max - z_min < threshold_mm * 2:
            return mesh  # Too thin to flatten

        # Slice at threshold height
        slice_height = z_min + threshold_mm
        top = mesh.slice_plane([0, 0, slice_height], [0, 0, -1])

        if top is None or len(top.vertices) == 0:
            return mesh

        # Create a flat bottom cap from the cross-section
        cross_section = mesh.section(plane_origin=[0, 0, slice_height], plane_normal=[0, 0, 1])
        if cross_section is not None:
            path_2d, transform = cross_section.to_planar()
            for polygon in path_2d.polygons_full:
                # Use trimesh.creation.triangulate_polygon (trimesh 4.x)
                # Returns (vertices, faces) tuple, not Trimesh
                try:
                    verts_2d, faces_idx = trimesh.creation.triangulate_polygon(polygon)
                    verts_3d = np.column_stack([verts_2d, np.full(len(verts_2d), slice_height)])
                    cap = trimesh.Trimesh(vertices=verts_3d, faces=faces_idx)
                except Exception:
                    # Fallback: extrude polygon to thin solid
                    try:
                        cap = trimesh.creation.extrude_polygon(polygon, height=0.01)
                        cap.apply_translation([0, 0, slice_height - 0.01])
                    except Exception:
                        continue
                if cap is not None and len(cap.vertices) > 0:
                    # Apply the transform from to_planar() to map back to 3D
                    if transform is not None:
                        cap.apply_transform(transform)
                    # Ensure at correct Z height
                    cap.apply_translation([0, 0, slice_height - cap.vertices[:, 2].min()])
                    top = trimesh.util.concatenate([top, cap])

        # Re-merge and repair
        top.merge_vertices(digits_vertex=4)
        trimesh.repair.fill_holes(top)
        trimesh.repair.fix_winding(top)
        trimesh.repair.fix_normals(top)

        return top

    @staticmethod
    def _scale_to_dimensions(mesh: trimesh.Trimesh, target_mm: Tuple[float, float, float]) -> trimesh.Trimesh:
        """Scale mesh so its bounding box matches target dimensions in mm."""
        current = mesh.bounding_box.extents
        scale = np.array(target_mm) / np.maximum(current, 1e-6)
        mesh.apply_scale(scale)
        return mesh


# ---------------------------------------------------------------------------
# Background Removal
# ---------------------------------------------------------------------------

def remove_background(image: Image.Image) -> Image.Image:
    """Remove background from an image using rembg."""
    try:
        from rembg import remove
        result = remove(image)
        # Ensure white/transparent background
        if result.mode == 'RGBA':
            # Convert to white background
            bg = Image.new('RGB', result.size, (255, 255, 255))
            bg.paste(result, mask=result.split()[3])
            return bg
        return result.convert('RGB')
    except ImportError:
        logger.warning("rembg not installed, skipping background removal")
        return image.convert('RGB')


# ---------------------------------------------------------------------------
# Flask Server
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.update(
    # Bound request memory/disk use before Flask parses multipart bodies.
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    LOCAL_ONLY=True,
)

MAX_IMAGES = 8
MAX_IMAGE_FILE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "BMP", "WEBP", "TIFF"}
ALLOWED_BACKENDS = {"trellis", "triposr", "auto"}
ALLOWED_QUALITIES = {"draft", "medium", "high", "ultra"}
ALLOWED_OUTPUT_FORMATS = {"stl", "obj", "glb", "3mf"}
MAX_FACES_MIN = 1_000
MAX_FACES_MAX = 5_000_000

# Global backend instances
_backends = {}
_active_backend = "trellis"


class APIValidationError(ValueError):
    """A safe, client-visible request validation error."""


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


@app.before_request
def enforce_local_boundary():
    """Reject DNS rebinding and browser cross-origin requests in local mode."""
    if not app.config["LOCAL_ONLY"]:
        return None

    peer = request.remote_addr or ""
    host = urlsplit(f"//{request.host}").hostname or ""
    if not _is_loopback(peer) or not _is_loopback(host):
        return jsonify({"error": "The ThoxForge API is local-only"}), 403

    origin = request.headers.get("Origin")
    if origin:
        origin_host = urlsplit(origin).hostname or ""
        if not _is_loopback(origin_host):
            return jsonify({"error": "Cross-origin browser requests are not allowed"}), 403
    return None


@app.after_request
def add_security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Request exceeds the 64 MiB limit"}), 413


def _validated_choice(value: str, field: str, allowed: set[str]) -> str:
    normalized = str(value).lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise APIValidationError(f"{field} must be one of: {choices}")
    return normalized


def _validated_int(value, field: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise APIValidationError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise APIValidationError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _validated_bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise APIValidationError(f"{field} must be true or false")


def _validated_dimensions(values) -> Optional[Tuple[float, float, float]]:
    if not values or all(value in (None, "") for value in values):
        return None
    if len(values) != 3 or any(value in (None, "") for value in values):
        raise APIValidationError("width_mm, depth_mm, and height_mm must be provided together")
    try:
        dimensions = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise APIValidationError("Target dimensions must be numbers") from exc
    if any(not 0 < value <= 10_000 for value in dimensions):
        raise APIValidationError("Target dimensions must be greater than 0 and at most 10000 mm")
    return dimensions


def _load_image(data: bytes) -> Image.Image:
    if not data:
        raise APIValidationError("Uploaded images must not be empty")
    if len(data) > MAX_IMAGE_FILE_BYTES:
        raise APIValidationError("Each image must be 16 MiB or smaller")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(io.BytesIO(data))
            if probe.format not in ALLOWED_IMAGE_FORMATS:
                raise APIValidationError("Unsupported image format")
            width, height = probe.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise APIValidationError("Each image must contain at most 40 million pixels")
            probe.verify()
        return Image.open(io.BytesIO(data)).convert("RGB")
    except APIValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError) as exc:
        raise APIValidationError("Invalid or unsafe image") from exc


def _load_images(raw_images: List[bytes], remove_bg: bool) -> List[Image.Image]:
    if not raw_images:
        raise APIValidationError("No images provided")
    if len(raw_images) > MAX_IMAGES:
        raise APIValidationError(f"At most {MAX_IMAGES} images are allowed")
    images = [_load_image(data) for data in raw_images]
    return [remove_background(image) for image in images] if remove_bg else images


def _export_mesh_bytes(mesh: trimesh.Trimesh, output_format: str) -> bytes:
    exported = mesh.export(file_type=output_format)
    if isinstance(exported, str):
        return exported.encode("utf-8")
    return bytes(exported)


def _safe_internal_error(exc: Exception):
    reference = uuid.uuid4().hex[:12]
    # Never serialize exception text or tracebacks: model/runtime errors can
    # contain local paths, prompts, environment details, or model identifiers.
    logger.error(
        "[Server] Internal error reference=%s type=%s",
        reference,
        type(exc).__name__,
    )
    return jsonify({
        "error": "Mesh generation failed",
        "reference": reference,
    }), 500


def get_backend(name: str) -> MeshGenerationBackend:
    if name not in _backends:
        if name == "trellis":
            _backends[name] = TRELLISBackend()
        elif name == "triposr":
            _backends[name] = TripoSRBackend()
        else:
            raise ValueError(f"Unknown backend: {name}")
    return _backends[name]


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "backends": list(_backends.keys()),
        "active_backend": _active_backend,
        "cuda_available": _check_cuda(),
    })


@app.route('/backends', methods=['GET', 'POST'])
def backends():
    """List or set active backend."""
    global _active_backend
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "A JSON object is required"}), 400
        try:
            _active_backend = _validated_choice(
                data.get("backend", "trellis"), "backend", ALLOWED_BACKENDS
            )
        except APIValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"active_backend": _active_backend})
    return jsonify({
        "active_backend": _active_backend,
        "available": ["trellis", "triposr"],
    })


@app.route('/generate', methods=['POST'])
def generate_mesh():
    """
    Main endpoint: accept one or more images, return a repaired STL file.

    Parameters (multipart/form-data):
        images[]    - One or more image files
        backend     - 'trellis', 'triposr', or 'auto' (default: from config)
        quality     - 'draft', 'medium', 'high', 'ultra' (default: 'high')
        seed        - Random seed (default: 1)
        flatten     - Flatten bottom for printing (default: true)
        width_mm    - Target width in mm (optional)
        depth_mm    - Target depth in mm (optional)
        height_mm   - Target height in mm (optional)
        max_faces   - Maximum face count (default: 500000)
        format      - Output format: 'stl', 'obj', '3mf', 'glb' (default: 'stl')
        remove_bg   - Auto-remove background (default: true)

    Returns:
        File download (binary mesh file)
    """
    start_time = time.time()

    try:
        # Parse inputs
        files = request.files.getlist('images[]')
        if not files or len(files) == 0:
            files = request.files.getlist('images')
        if not files or len(files) == 0:
            return jsonify({"error": "No images provided"}), 400
        if len(files) > MAX_IMAGES:
            return jsonify({"error": f"At most {MAX_IMAGES} images are allowed"}), 400

        backend_name = _validated_choice(
            request.form.get('backend', _active_backend), "backend", ALLOWED_BACKENDS
        )
        quality = _validated_choice(
            request.form.get('quality', 'high'), "quality", ALLOWED_QUALITIES
        )
        seed = _validated_int(request.form.get('seed', 1), "seed", -2_147_483_648, 2_147_483_647)
        flatten = _validated_bool(request.form.get('flatten', 'true'), "flatten")
        remove_bg = _validated_bool(request.form.get('remove_bg', 'true'), "remove_bg")
        max_faces = _validated_int(
            request.form.get('max_faces', 500000),
            "max_faces",
            MAX_FACES_MIN,
            MAX_FACES_MAX,
        )
        output_format = _validated_choice(
            request.form.get('format', 'stl'), "format", ALLOWED_OUTPUT_FORMATS
        )

        target_dims = _validated_dimensions([
            request.form.get('width_mm'),
            request.form.get('depth_mm'),
            request.form.get('height_mm'),
        ])

        images = _load_images([file.read(MAX_IMAGE_FILE_BYTES + 1) for file in files], remove_bg)

        logger.info(f"[Server] Received {len(images)} images, backend={backend_name}, "
                     f"quality={quality}, format={output_format}")

        # Generate mesh
        if backend_name == "auto":
            # Try TRELLIS first, fallback to TripoSR
            try:
                backend = get_backend("trellis")
                mesh = backend.generate(images, seed=seed, quality=quality)
            except Exception as exc:
                reference = uuid.uuid4().hex[:12]
                logger.warning(
                    "[Server] TRELLIS failed reference=%s type=%s; "
                    "falling back to TripoSR",
                    reference,
                    type(exc).__name__,
                )
                backend = get_backend("triposr")
                mesh = backend.generate(images, seed=seed, quality=quality)
        else:
            backend = get_backend(backend_name)
            mesh = backend.generate(images, seed=seed, quality=quality)

        # Repair mesh
        mesh = MeshRepairPipeline.repair(
            mesh,
            flatten_bottom=flatten,
            target_dimensions_mm=target_dims,
            max_faces=max_faces,
        )

        # Verify manifold
        is_watertight = mesh.is_watertight
        is_manifold = mesh.is_winding_consistent

        # Export
        mesh_bytes = _export_mesh_bytes(mesh, output_format)
        elapsed = time.time() - start_time
        logger.info(f"[Server] Done in {elapsed:.1f}s. "
                    f"Watertight={is_watertight}, Manifold={is_manifold}")

        resp = send_file(
            io.BytesIO(mesh_bytes),
            as_attachment=True,
            download_name=f"thoxforge_output.{output_format}",
            mimetype='application/octet-stream'
        )
        resp.headers['X-ThoxForge-Watertight'] = str(is_watertight)
        resp.headers['X-ThoxForge-Manifold'] = str(is_manifold)
        resp.headers['X-ThoxForge-Vertices'] = str(len(mesh.vertices))
        resp.headers['X-ThoxForge-Faces'] = str(len(mesh.faces))
        resp.headers['X-ThoxForge-Backend'] = backend.name
        resp.headers['X-ThoxForge-Elapsed'] = str(round(elapsed, 1))
        return resp

    except APIValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return _safe_internal_error(exc)


@app.route('/generate_json', methods=['POST'])
def generate_mesh_json():
    """
    Same as /generate but returns JSON metadata + base64-encoded mesh.
    Useful for embedded clients that can't handle binary downloads.
    """
    import base64
    start_time = time.time()

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise APIValidationError("A JSON object is required")
        images_b64 = data.get('images', [])
        if not isinstance(images_b64, list):
            raise APIValidationError("images must be an array")

        backend_name = _validated_choice(
            data.get('backend', _active_backend), "backend", ALLOWED_BACKENDS
        )
        quality = _validated_choice(data.get('quality', 'high'), "quality", ALLOWED_QUALITIES)
        seed = _validated_int(data.get('seed', 1), "seed", -2_147_483_648, 2_147_483_647)
        flatten = _validated_bool(data.get('flatten', True), "flatten")
        remove_bg = _validated_bool(data.get('remove_bg', True), "remove_bg")
        max_faces = _validated_int(
            data.get('max_faces', 500000), "max_faces", MAX_FACES_MIN, MAX_FACES_MAX
        )
        output_format = _validated_choice(
            data.get('format', 'stl'), "format", ALLOWED_OUTPUT_FORMATS
        )

        target_dims = _validated_dimensions(data.get('target_dimensions_mm'))

        try:
            raw_images = [base64.b64decode(item, validate=True) for item in images_b64]
        except (TypeError, ValueError) as exc:
            raise APIValidationError("images must contain valid base64 strings") from exc
        images = _load_images(raw_images, remove_bg)

        # Generate
        if backend_name == "auto":
            try:
                backend = get_backend("trellis")
                mesh = backend.generate(images, seed=seed, quality=quality)
            except Exception as e:
                logger.warning(f" TRELLIS failed: {e}, falling back to TripoSR")
                backend = get_backend("triposr")
                mesh = backend.generate(images, seed=seed, quality=quality)
        else:
            backend = get_backend(backend_name)
            mesh = backend.generate(images, seed=seed, quality=quality)

        # Repair
        mesh = MeshRepairPipeline.repair(
            mesh,
            flatten_bottom=flatten,
            target_dimensions_mm=target_dims,
            max_faces=max_faces,
        )

        # Export to base64
        mesh_b64 = base64.b64encode(
            _export_mesh_bytes(mesh, output_format)
        ).decode('utf-8')

        elapsed = time.time() - start_time
        return jsonify({
            "mesh": mesh_b64,
            "format": output_format,
            "vertices": len(mesh.vertices),
            "faces": len(mesh.faces),
            "watertight": mesh.is_watertight,
            "manifold": mesh.is_winding_consistent,
            "backend": backend.name,
            "elapsed_s": round(elapsed, 1),
        })

    except APIValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return _safe_internal_error(exc)


def _check_cuda():
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ThoxForge AI Pipeline Server")
    parser.add_argument('--port', type=int, default=7861, help='Port to listen on')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind')
    parser.add_argument('--backend', default='trellis', choices=['trellis', 'triposr', 'auto'],
                        help='Default mesh generation backend')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--preload', action='store_true', help='Preload model on startup')
    parser.add_argument(
        '--allow-remote',
        action='store_true',
        help='Explicitly allow non-loopback clients (no authentication is provided)',
    )
    args = parser.parse_args()

    if not _is_loopback(args.host) and not args.allow_remote:
        parser.error("non-loopback --host requires --allow-remote")
    if args.debug and not _is_loopback(args.host):
        parser.error("--debug may only be used with a loopback host")

    global _active_backend
    _active_backend = args.backend
    app.config["LOCAL_ONLY"] = not args.allow_remote

    logger.info("=" * 60)
    logger.info("ThoxForge AI Pipeline Server")
    logger.info("=" * 60)
    logger.info(f"  Host: {args.host}")
    logger.info(f"  Port: {args.port}")
    logger.info(f"  Backend: {args.backend}")
    logger.info(f"  CUDA: {_check_cuda()}")
    logger.info("=" * 60)

    if args.preload:
        logger.info("Preloading model...")
        backend = get_backend(args.backend if args.backend != "auto" else "trellis")
        backend.load()

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == '__main__':
    main()
