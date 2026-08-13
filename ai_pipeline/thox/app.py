"""Standalone THOX app, for running the printer-agent layer on its own.

``server.py`` imports torch, trimesh, pymeshlab and open3d at module scope,
because generative photo-to-3D genuinely needs them. The printer-agent layer
needs numpy, Pillow, requests and Flask - about 40 MB against several
gigabytes.

Those are different deployments. Print-health monitoring wants to run
continuously next to the printer, possibly on a small always-on box; mesh
generation wants a GPU and runs on demand. Forcing one install to carry the
other's dependencies would mean either no monitoring on the small box, or a
CUDA stack installed to watch a webcam.

So the blueprint mounts on either app:

* ``python -m thox.app`` serves only ``/thox/*`` with the light dependency set.
* ``python server.py`` serves the photo-to-3D endpoints **and** mounts the same
  blueprint when its imports succeed.

The security posture is identical either way, and deliberately copied rather
than imported - importing it from ``server.py`` would pull in torch and defeat
the entire point of this module.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from .routes import register

logger = logging.getLogger("thox")

#: Frames and JSON only; nothing here accepts a large upload.
MAX_CONTENT_LENGTH = 16 * 1024 * 1024


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def create_app(local_only: bool = True) -> Flask:
    """Build the standalone app."""
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH, LOCAL_ONLY=local_only)

    @app.before_request
    def enforce_local_boundary():
        """Reject DNS rebinding and cross-origin browser requests.

        This layer can pause and cancel prints, so an unauthenticated port that
        any web page could reach would let a visited site stop someone's print.
        """
        if not app.config["LOCAL_ONLY"]:
            return None
        peer = request.remote_addr or ""
        host = urlsplit(f"//{request.host}").hostname or ""
        if not _is_loopback(peer) or not _is_loopback(host):
            return jsonify({"error": "The THOX API is local-only"}), 403
        origin = request.headers.get("Origin")
        if origin and not _is_loopback(urlsplit(origin).hostname or ""):
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
        return jsonify({"error": "Request exceeds the size limit"}), 413

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "thox-printer-agent"})

    register(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="THOX printer-agent server (scan-to-print + print health)"
    )
    parser.add_argument("--port", type=int, default=7862)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Allow non-loopback clients. NO AUTHENTICATION IS PROVIDED, and "
            "this API can pause and cancel prints - only use it on a trusted "
            "network, behind something that does authenticate."
        ),
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Start the print-health monitor immediately on boot",
    )
    args = parser.parse_args()

    if not _is_loopback(args.host) and not args.allow_remote:
        parser.error("non-loopback --host requires --allow-remote")
    if args.debug and not _is_loopback(args.host):
        parser.error("--debug may only be used with a loopback host")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    app = create_app(local_only=not args.allow_remote)

    if args.monitor:
        from .routes import monitor

        try:
            monitor().start()
        except Exception as exc:
            logger.error(
                "could not start the monitor (%s); the API is still up",
                type(exc).__name__,
            )

    logger.info("=" * 60)
    logger.info("THOX printer-agent server")
    logger.info("  http://%s:%s/thox/health", args.host, args.port)
    logger.info("  local-only: %s", not args.allow_remote)
    logger.info("=" * 60)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
