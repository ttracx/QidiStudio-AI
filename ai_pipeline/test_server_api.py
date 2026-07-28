#!/usr/bin/env python3
"""Security and contract tests for the local ThoxForge HTTP sidecar."""

import base64
import io
import os
import sys
import unittest
from unittest import mock

import trimesh
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


class _FakeBackend:
    name = "triposr"

    def generate(self, images, **_kwargs):
        if not images:
            raise AssertionError("validated images should reach the backend")
        return trimesh.creation.box(extents=(10.0, 10.0, 10.0))


def _png_bytes(size=(8, 8)):
    output = io.BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


class SidecarAPITests(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True, LOCAL_ONLY=True)
        self.client = server.app.test_client()

    def test_health_is_local_and_not_cacheable(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_rejects_non_loopback_host_and_cross_origin_browser(self):
        remote_host = self.client.get("/health", headers={"Host": "attacker.example"})
        self.assertEqual(remote_host.status_code, 403)

        remote_origin = self.client.get(
            "/health", headers={"Origin": "https://attacker.example"}
        )
        self.assertEqual(remote_origin.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", remote_origin.headers)

    def test_backend_change_validates_choice(self):
        response = self.client.post("/backends", json={"backend": "shell"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("backend must be one of", response.json["error"])

    def test_generate_validates_file_type_before_backend(self):
        response = self.client.post(
            "/generate",
            data={"images[]": (io.BytesIO(b"not an image"), "input.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Invalid or unsafe image")

    def test_generate_rejects_ambiguous_boolean(self):
        response = self.client.post(
            "/generate",
            data={
                "images[]": (io.BytesIO(_png_bytes()), "input.png"),
                "remove_bg": "sometimes",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "remove_bg must be true or false")

    @mock.patch.object(server, "get_backend", return_value=_FakeBackend())
    def test_generate_returns_mesh_and_truthful_backend(self, _get_backend):
        response = self.client.post(
            "/generate",
            data={
                "images[]": (io.BytesIO(_png_bytes()), "input.png"),
                "backend": "auto",
                "remove_bg": "false",
                "flatten": "false",
                "format": "stl",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data), 80)
        self.assertEqual(response.headers["X-ThoxForge-Backend"], "triposr")
        self.assertEqual(response.headers["X-ThoxForge-Watertight"], "True")

    @mock.patch.object(server, "get_backend", return_value=_FakeBackend())
    def test_json_generation_contract(self, _get_backend):
        response = self.client.post(
            "/generate_json",
            json={
                "images": [base64.b64encode(_png_bytes()).decode("ascii")],
                "backend": "triposr",
                "remove_bg": False,
                "flatten": False,
                "format": "stl",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["backend"], "triposr")
        self.assertGreater(len(base64.b64decode(response.json["mesh"])), 80)

    @mock.patch.object(server, "get_backend", side_effect=RuntimeError("secret path"))
    def test_internal_errors_do_not_expose_tracebacks(self, _get_backend):
        with self.assertLogs(server.logger, level="ERROR") as captured:
            response = self.client.post(
                "/generate",
                data={
                    "images[]": (io.BytesIO(_png_bytes()), "input.png"),
                    "remove_bg": "false",
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"], "Mesh generation failed")
        self.assertIn("reference", response.json)
        self.assertNotIn("traceback", response.json)
        self.assertNotIn("secret path", response.get_data(as_text=True))
        self.assertNotIn("secret path", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
