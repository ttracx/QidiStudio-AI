#!/usr/bin/env python3
"""Unified THOX/QidiStudio AI sidecar.

This preserves every existing endpoint from ``server.py`` and adds the closed-loop
Print Health API/UI on the same port. Use this entry point instead of server.py:

    python unified_server.py --host 127.0.0.1 --port 7861 --backend auto

For THOX Forger over Tailscale/LAN, use ``--allow-remote`` only with a strong
``THOX_PRINT_HEALTH_TOKEN``. The legacy mesh endpoints remain unauthenticated when
remote mode is enabled, so production deployments should still put the whole service
behind Tailscale ACLs or a reverse proxy with authentication.
"""
from __future__ import annotations

import argparse
import logging
import os

import server as mesh_server
from print_health import PrintHealthService, PrintHealthSettings, create_print_health_blueprint

logger = logging.getLogger("thoxforge.unified")

app = mesh_server.app
print_health = PrintHealthService(PrintHealthSettings.from_env())
app.register_blueprint(create_print_health_blueprint(print_health))

# Enrich the legacy health payload without replacing it. A separate endpoint is
# exposed so existing C++ clients that parse /health remain backwards compatible.
@app.get("/health/print-health")
def print_health_health():
    state = print_health.state()
    return {
        "status": "ok",
        "running": state["running"],
        "printer": state["printer"],
        "mode": state["mode"],
        "providers": state["providers"],
        "last_error": state["last_error"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified ThoxForge AI + Print Health Server")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--backend", default="auto", choices=["trellis", "triposr", "auto"])
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--preload", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--start-monitor", action="store_true")
    args = parser.parse_args()

    if not mesh_server._is_loopback(args.host) and not args.allow_remote:
        parser.error("non-loopback --host requires --allow-remote")
    if args.debug and not mesh_server._is_loopback(args.host):
        parser.error("--debug may only be used with a loopback host")
    if args.allow_remote and not os.getenv("THOX_PRINT_HEALTH_TOKEN", "").strip():
        parser.error("--allow-remote requires THOX_PRINT_HEALTH_TOKEN")

    mesh_server._active_backend = args.backend
    app.config["LOCAL_ONLY"] = not args.allow_remote

    if args.preload:
        backend = mesh_server.get_backend(args.backend if args.backend != "auto" else "trellis")
        backend.load()
    if args.start_monitor:
        print_health.start()

    logger.info("Unified AI sidecar: http://%s:%s", args.host, args.port)
    logger.info("Print Health UI: /print-health")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
