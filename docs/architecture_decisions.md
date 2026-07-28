# Architecture Decisions

## ADR-001: Loopback-only AI sidecar

- Decision: bind to `127.0.0.1` and enforce loopback peer, Host, and browser
  Origin values by default.
- Context: source photos, model requests, and meshes may be confidential.
- Options: hosted API, LAN service, local sidecar.
- Tradeoffs: local execution requires operator hardware and setup but avoids
  mandatory data transfer.
- Security impact: reduces network exposure; remote opt-in remains
  unauthenticated and unsuitable for sensitive use.
- Local-first impact: positive.
- Compliance impact: makes data-flow review simpler but is not itself a
  compliance certification.
- Final choice: local sidecar with explicit `--allow-remote` escape hatch.
- Follow-up: remove or secure remote mode before enterprise deployment.

## ADR-002: Model runtimes remain separately installed

- Decision: do not vendor TRELLIS, TripoSR, CUDA, or model weights in this
  source repository.
- Context: these assets are large and have independent licenses and hardware
  constraints.
- Tradeoffs: smaller source repository; setup is not yet reproducible offline.
- Security impact: setup can contact package/model repositories.
- Local-first impact: runtime remains local after assets are staged.
- Compliance impact: production deployments need a reviewed SBOM, hashes, and
  license inventory.
- Final choice: separate runtime for MVP.
- Follow-up: create an offline bundle with signed manifest.

## ADR-003: Unsigned artifacts are development builds

- Decision: do not describe locally built `.app` bundles as release-ready.
- Context: no Developer ID, hardened runtime, notarization, stapling, or
  Gatekeeper evidence is present.
- Tradeoffs: honest release boundary while signing infrastructure is pending.
- Final choice: host-tested MVP status.
- Follow-up: add dedicated release workflow and evidence manifest.
