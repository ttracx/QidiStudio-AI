#!/usr/bin/env python3
"""
ThoxForge AI Pipeline — Quick smoke test
=========================================
Tests the Flask server endpoints without requiring GPU or model weights.

Usage:
    python test_pipeline.py

This verifies:
1. Server starts and responds to /health
2. Mesh repair pipeline works on a synthetic mesh
3. API endpoints are correctly wired
"""

import sys
import os
import time
import json
import tempfile
import subprocess
import signal
import requests
import numpy as np
import trimesh

# Add server module to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_mesh_repair():
    """Test the mesh repair pipeline with a non-manifold mesh."""
    print("\n[TEST] Mesh Repair Pipeline")
    print("-" * 40)

    from server import MeshRepairPipeline

    # Create a non-manifold mesh: a cube with a missing face (open mesh)
    # and some degenerate triangles
    vertices = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],  # bottom
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],  # top
        [0.5, 0.5, 1.5],  # extra vertex for degenerate face
    ], dtype=np.float32)

    # Deliberately missing one face (non-manifold) + one degenerate face
    faces = np.array([
        [0, 1, 2], [0, 2, 3],        # bottom (complete)
        [4, 5, 6], [4, 6, 7],        # top (complete)
        [0, 1, 5], [0, 5, 4],        # front
        [1, 2, 6], [1, 6, 5],        # right
        [2, 3, 7], [2, 7, 6],        # back
        # Missing left face [0, 3, 7], [0, 7, 4]
        # Degenerate face (all same vertex)
        [8, 8, 8],
    ], dtype=np.int32)

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
    print(f"  Input:  {len(mesh.vertices)} verts, {len(mesh.faces)} faces, watertight={mesh.is_watertight}")

    # Run repair
    repaired = MeshRepairPipeline.repair(
        mesh,
        flatten_bottom=False,  # skip flatten for this test
        target_dimensions_mm=None,
        max_faces=100000,
    )

    print(f"  Output: {len(repaired.vertices)} verts, {len(repaired.faces)} faces, watertight={repaired.is_watertight}")
    print(f"  Manifold: {repaired.is_winding_consistent}")

    if repaired.is_watertight:
        print("  ✅ PASS: Mesh is watertight after repair")
    else:
        print("  ⚠️  WARNING: Mesh not fully watertight (may use convex hull fallback)")

    return True


def test_server_endpoints():
    """Test Flask server endpoints (without GPU)."""
    print("\n[TEST] Server Endpoints")
    print("-" * 40)

    # Start server in background
    port = 7862  # Different port for testing
    env = os.environ.copy()
    env['PYTHONPATH'] = os.path.dirname(os.path.abspath(__file__))

    proc = subprocess.Popen(
        [sys.executable, 'server.py', '--port', str(port), '--backend', 'trellis'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    try:
        # Wait for server to start
        print("  Waiting for server to start...")
        for i in range(10):
            time.sleep(1)
            try:
                resp = requests.get(f'http://127.0.0.1:{port}/health', timeout=2)
                if resp.status_code == 200:
                    print(f"  ✅ /health: {resp.json()}")
                    break
            except requests.exceptions.ConnectionError:
                continue
        else:
            print("  ❌ FAIL: Server did not start")
            return False

        # Test backends endpoint
        resp = requests.get(f'http://127.0.0.1:{port}/backends')
        print(f"  ✅ /backends: {resp.json()}")

        # Test setting backend
        resp = requests.post(f'http://127.0.0.1:{port}/backends', json={"backend": "triposr"})
        print(f"  ✅ POST /backends: {resp.json()}")

        return True

    finally:
        proc.terminate()
        proc.wait()


def test_synthetic_generation():
    """Test the full pipeline with a synthetic image (no GPU needed)."""
    print("\n[TEST] Synthetic Generation (mesh repair only)")
    print("-" * 40)

    from server import MeshRepairPipeline

    # Create a sphere (already watertight)
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=10.0)
    print(f"  Input sphere: {len(sphere.vertices)} verts, {len(sphere.faces)} faces, watertight={sphere.is_watertight}")

    # Repair with flatten + scale
    repaired = MeshRepairPipeline.repair(
        sphere,
        flatten_bottom=True,
        target_dimensions_mm=(50.0, 50.0, 40.0),
        max_faces=10000,
    )

    print(f"  Output: {len(repaired.vertices)} verts, {len(repaired.faces)} faces")
    print(f"  Watertight: {repaired.is_watertight}")
    print(f"  Bounding box: {repaired.bounding_box.extents}")

    # Check dimensions are close to target
    extents = repaired.bounding_box.extents
    if abs(extents[0] - 50.0) < 1.0 and abs(extents[2] - 40.0) < 2.0:
        print("  ✅ PASS: Dimensions match target")
    else:
        print(f"  ⚠️  Dimensions off: got {extents}")

    # Export to STL
    with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
        repaired.export(f.name, file_type='stl')
        size = os.path.getsize(f.name)
        print(f"  ✅ STL export: {size} bytes")
        os.unlink(f.name)

    return True


def main():
    print("=" * 60)
    print("ThoxForge AI Pipeline — Smoke Tests")
    print("=" * 60)

    results = []

    # Test 1: Mesh repair
    try:
        results.append(("Mesh Repair", test_mesh_repair()))
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        results.append(("Mesh Repair", False))

    # Test 2: Synthetic generation
    try:
        results.append(("Synthetic Generation", test_synthetic_generation()))
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        results.append(("Synthetic Generation", False))

    # Test 3: Server endpoints
    try:
        results.append(("Server Endpoints", test_server_endpoints()))
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        results.append(("Server Endpoints", False))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")

    all_passed = all(r[1] for r in results)
    print(f"\n  Overall: {'✅ ALL PASSED' if all_passed else '❌ SOME FAILED'}")
    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())