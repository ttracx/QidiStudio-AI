# MVP Catalog

Priority uses `(Market Value × .40) + (Technical Feasibility × .30) +
(Time-to-Market × .20) + (Strategic Importance × .10)`.

| MVP | Market | Feasibility | TTM | Strategic | Priority | Status |
|---|---:|---:|---:|---:|---:|---|
| Local photo-to-mesh loop | 9 | 7 | 7 | 9 | 8.0 | Host-tested API; desktop and GPU evidence pending |
| Offline runtime bundle | 9 | 6 | 5 | 10 | 7.4 | Planned |
| Signed desktop release | 8 | 6 | 5 | 9 | 7.0 | Blocked on signing/notarization and clean-host build |

## Local photo-to-mesh loop

- Problem: AI mesh tools commonly require hosted uploads and produce
  non-printable geometry.
- Target user: THOX hardware, campaign, and manufacturing teams.
- Required modules: desktop UI, loopback client, local model runtime, repair,
  export, audit-ready status.
- Success: a real photo produces a mesh, the server truthfully reports topology,
  the operator reviews it, and QIDIStudio imports it.
- Risks: model licensing/weights, GPU compatibility, nondeterministic geometry,
  upstream network features, and missing full desktop build evidence.
- Dependencies: QIDIStudio, libcurl, Python runtime, TRELLIS or TripoSR,
  trimesh/PyMeshLab, CUDA hardware.
