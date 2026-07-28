# Security Model

## Assets and threat model

Protected assets include source photographs, generated meshes, model prompts and
settings, printer/project metadata, local model weights, and build/signing
credentials. Threats include localhost cross-site requests, DNS rebinding,
oversized/decompression-bomb images, resource exhaustion, malicious file
content, sensitive exception disclosure, dependency compromise, and accidental
remote exposure.

## Trust boundaries

1. Operator-selected local files enter QIDIStudio.
2. QIDIStudio sends them over loopback HTTP to the Python sidecar.
3. The sidecar passes decoded images to a locally installed model runtime.
4. Generated mesh bytes return to an OS temporary file and then the plater or an
   operator-selected save path.
5. Package and model acquisition are separate setup-time network boundaries.

## Implemented controls

- Loopback bind and loopback peer/Host validation by default.
- Cross-origin browser requests rejected; permissive CORS removed.
- Desktop client accepts only loopback HTTP sidecar URLs.
- 64 MiB request, eight-image, 16 MiB per-image, and 40-million-pixel limits.
- Image format/decode verification and bounded numeric/enum settings.
- Generic client errors with server-side reference IDs; no traceback response.
- `no-store`, `nosniff`, and same-origin resource-policy headers.
- High-entropy OS temporary paths; dialog removes its latest generated
  temporary output on replacement or close.
- CI uses TLS verification and least-privilege read permissions.
- No ThoxForge telemetry.

## Authentication, authorization, and encryption

Local mode relies on operating-system user/session isolation and has no
application authentication. Loopback traffic is HTTP, not TLS. Remote mode is
an explicit development escape hatch with no authentication or encryption and
must not process sensitive data. Multi-user enterprise deployments require an
authenticated transport and workspace authorization model.

## Secrets and logging

No secrets belong in source or environment examples. The runtime does not
require cloud API keys. Logs contain operational status, counts, backend names,
and internal exception detail; operators should protect log access. Source
photos and mesh bytes are not intentionally logged.

## Retention and encryption

The sidecar processes uploaded images in memory. The desktop client writes the
response to the OS temporary directory and deletes its latest temporary output
when replaced or the dialog closes. User-saved meshes follow filesystem policy.
Application-level encryption and configurable retention are not implemented.

## Audit logging

Current logs are diagnostic, not a tamper-evident audit trail. There is no user
identity, approval record, immutable event store, or retention configuration.
Do not claim SOC 2, HIPAA, GDPR, or other compliance completion.

## Known risks and required mitigations

- Model/package setup can use public networks: provide an offline signed bundle,
  SBOM, hashes, and license review.
- Model inference is nondeterministic and may generate unsafe/inaccurate
  geometry: require human review and physical validation.
- Upstream QIDIStudio has independent network features: inventory and control
  them before making whole-application offline claims.
- Remote mode lacks security: disable it in production or add mutual
  authentication, TLS, authorization, rate limits, and audit records.
- No signed/notarized desktop artifact exists: complete platform signing and
  clean-host verification.
