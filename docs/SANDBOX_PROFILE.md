# Sandbox Profile — untrusted-content processing (SEC-004)

> **Purpose:** the single reference for how hostile bytes (customer PDFs,
> images) are contained while the platform parses them. Read together with
> [`THREAT_MODEL.md`](THREAT_MODEL.md) (hostile files and resource exhaustion)
> — this document is the mitigation detail behind residual risk R3.

## 1. What runs inside the sandbox

Every code path that parses untrusted document bytes runs in a separate
child process launched through the shared profile in
`apps/worker/src/soa_worker/sandbox.py` — never in the worker process:

| Surface | Child | Parent |
| --- | --- | --- |
| Page rendering (PRC-004) | `python -m soa_worker.render_sandbox` (PDFium / Pillow) | `apps/worker/src/soa_worker/rendering.py` |
| Native PDF text (AIO-002) | `python -m soa_worker.native_text_sandbox` (PDFium) | `apps/worker/src/soa_worker/native_text_adapter.py` |
| OCR (AIO-004) | `tesseract` CLI | `apps/worker/src/soa_worker/tesseract_ocr.py` |

Before any bytes reach these children, the API has already applied
magic-byte/media validation (ING-003), size/page/pixel caps (ING-005), and
the malware-scan quarantine gate (ING-004). The sandbox is the layer for
what survives those checks and is still hostile.

## 2. Process layer (implemented and CI-tested)

### Launch profile (parent side)

- **Secret-free environment.** The child environment is built from an
  explicit passthrough list (`ENV_PASSTHROUGH`: `PATH`, locale, timezone,
  `TESSDATA_PREFIX`) plus `PYTHONPATH` for python children. Database URLs,
  storage keys, provider credentials, and every `SOA_*` setting are
  withheld — an exploited parser finds nothing in its own environment.
- **Temp and home isolation.** `TMPDIR` and `HOME` point inside the
  per-run scratch directory; the working directory is the scratch
  directory. Everything a child writes is deleted with the run and never
  lands in a shared location.
- **Read-only input.** The document bytes are written once by the parent
  and chmod'ed read-only before the child starts; the original can never
  be modified.
- **Own session, group kill.** Children start with
  `start_new_session=True`; a wall-clock timeout kills the entire process
  group (`kill_process_tree`), so a child that spawned helpers cannot
  leave orphans burning CPU.
- **Wall-clock budget in the parent.** The parent — not the child —
  enforces the timeout; a hung or malicious child is killed and the
  failure classified retryable-operational, never silently waited on.

### Child lockdown (child side, before any parser code runs)

| Control | Value | Why |
| --- | --- | --- |
| `RLIMIT_CPU` | budget per run (default 30 s) | infinite-loop content burns out, kernel-enforced |
| `RLIMIT_AS` | budget per run (default 1 GiB; Linux production) | decompression bombs get `MemoryError`, not the host's RAM |
| `RLIMIT_CORE` | 0 | a core dump of untrusted document memory is a data leak |
| `RLIMIT_FSIZE` | = memory budget | a single written file cannot exceed the run's budget |
| `RLIMIT_NOFILE` | 128 | input + page outputs + library loading, nothing more |
| `RLIMIT_NPROC` | 0 (python children) | parsers never fork; enforced by the kernel for non-root users |
| Python network denial | `socket.*` raises `PermissionError` | a renderer has no business on the network |
| Python process denial | `os.fork/exec*/spawn*/system`, `subprocess` raise `PermissionError` | parsers never launch processes |
| `OMP_THREAD_LIMIT=1` | tesseract | single-threaded, predictable resource use |
| `Image.MAX_IMAGE_PIXELS` | per-page pixel budget | Pillow decompression bombs raise instead of allocating |
| PDFium active content | rasterized, never executed | embedded JavaScript is inert data |

Tests: `apps/worker/tests/test_sandbox_hardening.py` (environment
withholding, temp confinement, lockdown denials, rlimit pinning,
process-group kill), `apps/worker/tests/test_rendering.py` and
`apps/worker/tests/test_native_text_adapter.py` (timeout kill, malformed
and active-content classification, bounded rasters).

### Honest limits of the process layer

- The python-level socket/fork/exec denials stop **Python-level** attacks.
  A native-code exploit inside PDFium/Pillow/tesseract is still bound by
  the rlimits and finds no secrets or writable shared state, but only the
  container layer below fully denies it syscalls.
- `RLIMIT_NPROC` is not enforced for root — one more reason the container
  profile mandates a non-root user.
- The children share the worker's container today (subprocess isolation,
  not a separate service). Moving conversion to a dedicated
  network-isolated service is a deployment-time option the profile below
  already anticipates.
- macOS exposes `RLIMIT_AS` but rejects lowering it for a running Python
  process. Local macOS runs therefore enforce every other process control
  but skip this one limit; Linux CI and production containers enforce it,
  and the production cgroup memory limit remains mandatory. This exception
  is explicit in `ADDRESS_SPACE_LIMIT_SUPPORTED` and covered by the sandbox
  hardening test rather than being silently swallowed.

## 3. Container layer (image built; deployment evidence pending)

The worker image exists, runs as uid `10001`, and contains only its locked
runtime environment plus the required OCR/fonts. That proves the image's
non-root baseline, not the target platform's complete isolation. Before
customer documents, run the release digest with the strongest equivalent the
chosen runtime supports and record evidence for: read-only root where
available, bounded writable scratch, default seccomp/sandboxing, no privilege
escalation/capabilities, PID/memory/CPU limits, and only required network
egress. For a Docker-compatible runtime the intended profile is:

```yaml
# docker-compose fragment for the worker service
user: "10001:10001"            # non-root; RLIMIT_NPROC and file modes hold
read_only: true                 # read-only root filesystem
tmpfs:
  - /tmp:size=2g,mode=1777      # the ONLY writable path; scratch dirs live here
cap_drop: [ALL]                 # no capabilities whatsoever
security_opt:
  - no-new-privileges:true      # setuid binaries cannot escalate
# Do not set seccomp=unconfined. Docker's default profile applies when no
# seccomp override is supplied; use seccomp=/path/to/reviewed-profile.json only
# when the deployment owns and tests that profile.
pids_limit: 256                 # fork bombs die at the cgroup
mem_limit: 4g
cpus: 2
```

Kubernetes equivalent (`securityContext`):

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: { drop: [ALL] }
  seccompProfile: { type: RuntimeDefault }
```

Network policy: the worker needs egress to Postgres, object storage, the
configured telemetry collector, managed secret service, outbox/ERP targets,
and enabled model providers **only**. The current converter is a subprocess in
the worker container. If it is split into its own service, that service gets no
network and exchanges only bounded input/output through scratch/object storage.

## 4. Verification

- CI runs the process-layer suite on every push (worker test job).
- CI builds/scans the non-root image, but the container profile must still be
  verified at deployment time. Start the exact release digest under the target
  controls, run hostile-file/sandbox tests and a representative workload, and
  retain the runtime configuration with the release evidence. The pilot does
  not go live on an unverified profile.
