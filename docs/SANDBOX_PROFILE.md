# Sandbox Profile — untrusted-content processing (SEC-004)

> **Purpose:** the single reference for how hostile bytes (customer PDFs,
> images) are contained while the platform parses them. Read together with
> `docs/THREAT_MODEL.md` §4.2 (hostile files) — this document is the
> mitigation detail behind residual risk R5.

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
| `RLIMIT_AS` | budget per run (default 1 GiB) | decompression bombs get `MemoryError`, not the host's RAM |
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

## 3. Container layer (deployment requirement)

There is no production container build yet (deployment lands with the
release epic). **This profile is a REQUIREMENT for that work, not a
suggestion** — the worker/converter container MUST run with:

```yaml
# docker-compose fragment for the worker service
user: "10001:10001"            # non-root; RLIMIT_NPROC and file modes hold
read_only: true                 # read-only root filesystem
tmpfs:
  - /tmp:size=2g,mode=1777      # the ONLY writable path; scratch dirs live here
cap_drop: [ALL]                 # no capabilities whatsoever
security_opt:
  - no-new-privileges:true      # setuid binaries cannot escalate
  - seccomp:unconfined-is-forbidden  # use the runtime's DEFAULT seccomp profile (never "unconfined")
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

Network policy: the worker needs egress to Postgres, object storage, and
configured model providers **only**. If conversion is split into its own
service, that service gets `network_mode: none` (it reads input and writes
output through mounted scratch space) — the process layer already assumes
no network, so the split is a deployment change, not a code change.

## 4. Verification

- CI runs the process-layer suite on every push (worker test job).
- The container profile must be verified at deployment time: start the
  worker under the profile above and run the worker test suite
  (`uv run pytest apps/worker`) inside it. A checklist item for this
  lives in the release epic (REL) — the pilot does not go live on an
  unprofiled container.
