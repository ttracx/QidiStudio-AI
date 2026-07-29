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

## ADR-004: Use the dependency-locked Boost in macOS CI

- Decision: do not install or inject Homebrew Boost into the macOS application
  configure path; use the Boost version built by `deps/`.
- Context: clean-host run `30328398624` built Boost 1.84 in the dependency
  prefix, but Homebrew Boost 1.90 appeared first in `CMAKE_PREFIX_PATH`.
  CMake then required `boost_system` 1.90 and rejected the available,
  internally consistent 1.84 component.
- Options considered: pin a Homebrew formula, upgrade the full dependency
  bundle, or remove the redundant Homebrew Boost.
- Tradeoffs: the dependency build remains longer, but the application and its
  components resolve from one version-locked prefix.
- Security impact: reduces unreviewed dependency drift in clean-host builds.
- Local-first impact: neutral; no runtime network behavior changes.
- Compliance impact: improves build provenance but is not release evidence.
- Final choice: dependency-locked Boost only.
- Follow-up: require a green macOS/Windows/Linux matrix before changing the
  host-tested MVP boundary.

## ADR-005: Bound macOS dependency build concurrency

- Decision: cap macOS CI dependency builds at four concurrent jobs through
  `CMAKE_BUILD_PARALLEL_LEVEL`; make FFmpeg fall back to the dependency
  project's bounded processor count instead of unbounded `make -j`.
- Context: clean-host run `30331910176` progressed beyond dependency
  resolution, but concurrent wxWidgets Ninja and FFmpeg Make processes
  exhausted the macOS runner process limit. The log recorded
  `posix_spawn: Resource temporarily unavailable`.
- Options considered: rerun unchanged, serialize all dependency builds, or
  retain parallel builds with a conservative explicit cap.
- Tradeoffs: four jobs may increase build time compared with an unconstrained
  runner, but avoids nondeterministic process exhaustion while retaining useful
  parallelism.
- Security impact: neutral; no runtime behavior or network boundary changes.
- Local-first impact: neutral.
- Compliance impact: improves clean-host build determinism but is not release
  evidence.
- Final choice: four concurrent jobs in macOS CI and a bounded FFmpeg default.
- Follow-up: require a green macOS/Windows/Linux matrix before changing the
  host-tested MVP boundary.
