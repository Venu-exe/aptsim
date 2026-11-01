# Architecture

This document describes the internal contracts of `aptsim`. It is aimed at
researchers who want to extend the framework without weakening the lab
boundary.

## 1. Design goals

1. **Reproducible telemetry.** A simulation run is a deterministic sequence of
   structured events plus a small set of lab files. Seeded randomness,
   configurable time scaling and ephemeral loopback ports make runs comparable.
2. **Inert by construction.** Any capability whose real implementation would be
   dangerous is replaced by a telemetry producer, not by a disabled switch.
   There is no code path that spawns a process, writes outside the lab or opens
   a non-loopback socket.
3. **Independent modules behind a central engine.** Modules do not import each
   other except through well-defined helpers (`fileops` functions, `c2`
   protocol, `dispatcher`). Shared services are injected through
   `ExecutionContext`.
4. **Recoverable execution.** Every stage is checkpointed. Interruptions are
   first-class (`interrupted` status, exit code 3) and `resume` continues
   without replaying completed stages.
5. **Everything leaves evidence.** Events, manifests, artifacts, state and
   summaries are written to the session directory, which is never deleted by
   cleanup.

## 2. Component map

| Component | Module | Responsibility |
|---|---|---|
| Execution engine | `core/engine.py` | Stage parsing, module construction, timeouts, retry/continue/abort policy, checkpoints, summary |
| Config manager | `core/config.py` | Layered JSON, env expansion, overrides, safety validation, digest |
| Event system | `core/events.py` | Ordered async fan-out, thread-safe emission, JSONL/memory/pretty sinks |
| Session manager | `core/session.py` | Session identity, layout, metadata, heartbeat task |
| State store | `core/state.py` | Atomic JSON checkpoints, completion set, namespaces, restart counter |
| Path guard | `core/paths.py` | Lab root validation, traversal/symlink resolution, host blocklist |
| Artifact journal | `core/artifacts.py` | Append-only lab-relative ledger of created/mutated files |
| Plugin registry | `core/registry.py` | Built-in modules and validated local plugin imports |
| Payload module | `modules/payload.py` | Multi-stage flow, dependency checks, dynamic stage plugins |
| Process module | `modules/process.py` | Fake PID table, parent/child chain, synthetic injection telemetry |
| File module | `modules/fileops.py` | Discovery, hashes, entropy, reversible edits/transform |
| Persistence module | `modules/persistence.py` | Lab-only startup artifacts + restart counter |
| Anti-analysis module | `modules/anti_analysis.py` | Mock indicator scoring, log-only decisions |
| Network module | `modules/network.py` | Loopback session, heartbeat/poll/task loop |
| Dispatcher | `modules/dispatcher.py` | Allowlisted, read-only task handlers |
| C2 protocol | `c2/protocol.py` | JSONL envelope, HMAC, nonce, skew, size limits |
| C2 server | `c2/server.py` | Session tracking, scripted scenario delivery |
| C2 client | `c2/client.py` | Request/response RPC over one connection |
| Cleanup | `recovery/cleanup.py` | Restore transform/edit, remove artifacts, integrity check |

## 3. Execution lifecycle

```
pending -> running -> completed
                  \-> interrupted -> (resume) -> running -> completed
                  \-> failed      -> (inspect / resume or rerun)
```

`Engine.run()`:

1. parse stages once (`Stage` dataclasses);
2. for each enabled stage not already completed:
   * `StateStore.mark_running(stage, index)` — writes `state.json` atomically;
   * emit `lifecycle.stage.start`;
   * construct a fresh module instance per attempt via the registry;
   * `setup` → `run` (wrapped in `asyncio.wait_for`) → `teardown`;
   * on failure retry with `runtime.retry.backoff * attempt`; on exhaustion,
     record `stage.failed`, then honour `on_error`;
   * on success `mark_completed` and emit `stage.completed` with duration;
3. after all stages: `status=completed`, write `summary.json`, emit
   `run.completed`.

`stop_after` is checked **after** a stage completes, so the checkpoint always
contains a consistent stage boundary. The engine emits
`lifecycle.run.crash_simulated`, marks `interrupted`, and raises
`SimulatedCrash`; the CLI maps it to exit code 3.

`resume` loads the state file, increments `restarts`, emits
`run.resume` with the completed list, and skips stages below the first pending
index. Modules with their own cursors (payload) read them from the state
namespace, so a crash inside a module does not replay its completed sub-steps.

### Retry semantics

`on_error` values:

* `abort` — raise `ModuleError`; engine marks the run `failed`.
* `continue` — mark the stage completed so resume does not retry it, record the
  error in `namespaces.failures`, continue to the next stage.
* `retry` — accepted by config validation as an alias for the retry loop; the
  number of attempts is `1 + retries` regardless of policy.

Timeouts are per attempt and apply to `run()` only; `setup`/`teardown` are
expected to be quick and must not block.

## 4. Concurrency model

* One asyncio event loop. The engine itself is sequential per stage.
* `EventBus` owns one writer task. Producers call `await bus.emit(...)`;
  worker threads call `bus.emit_threadsafe(...)`, which schedules
  `queue.put_nowait` on the loop with `call_soon_threadsafe`. Sequence numbers
  are allocated under a `threading.Lock`, so thread-emitted events keep unique,
  ordered sequence numbers even though delivery order across threads is
  best-effort.
* `fileops` hashes files in a `ThreadPoolExecutor`; the worker emits
  `file.metadata.collected` events through the thread-safe path.
* `dispatcher` bounds handler concurrency with an `asyncio.Semaphore` and
  applies `asyncio.wait_for` per task.
* The network module runs heartbeats and polling concurrently on one client
  connection. `C2Client` serialises requests with an `asyncio.Lock`, so frames
  never interleave.
* `Heartbeat` writes `heartbeat.json` on an interval and is cancelled during
  shutdown.

## 5. Data model

### Event

```text
Event(seq, ts, t_rel, session_id, category, action, module, severity,
      correlation, data)
```

Immutable, JSON-serialisable, monotonic `seq`, wall-clock `ts` and
session-relative `t_rel`. Sinks receive events in one total order per run.

### State

```json
{
  "session_id": "s-...",
  "status": "running|interrupted|completed|failed",
  "cursor": {"stage_id": "s5-fileops", "stage_index": 4},
  "completed": ["s1-payload", "..."],
  "failed_stage": null,
  "error": null,
  "restarts": 0,
  "payload": {"stage_index": 3, "completed": ["..."], "capabilities": ["..."]},
  "failures": {}
}
```

Module namespaces are stored at the top level next to the reserved keys
(`payload`, `failures`, ...). `StateStore.save` writes a temp file and uses
`os.replace` for atomicity.

### Session layout

```
lab/sessions/<id>/
  session.json          identity, pid, status, metadata
  config.snapshot.json  resolved configuration
  events.jsonl          ordered event stream
  heartbeat.json        liveness evidence
  state.json            checkpoint
  artifacts.jsonl       mutation ledger
  transform-manifest.json
  edits-manifest.json
  file-baseline.json    pre-mutation SHA-256 baseline
  processes.json        synthetic process table
  anti-analysis.json    mock indicator assessments
  c2-server.json        loopback endpoint metadata
  c2-session.json       dispatcher/server statistics
  persistence-report.json
  summary.json
  timeline.txt
  cleanup-report.json   written by cleanup
```

`lab/cleanup-reports/<session>.json` keeps a report even if the session
directory is moved.

## 6. File operation pipeline and integrity

Order inside `s5-fileops`:

1. `discovery` — `iter_files` walks without following symlinks; metadata and
   hashes are computed in a thread pool; `file-baseline.json` is written.
2. `edit` — configured files are backed up under
   `.backup/<session>/edits/<relative>`, the marker is appended, the edit
   manifest records `orig_sha256` and `sha256`.
3. `transform` — each eligible file is backed up, transformed in place, and
   recorded with `orig_sha256`, `sha256`, `key_hex`, `baseline_sha256`.

Manifest paths are always lab-relative. Backups are journaled as
`edit-backup` / `transform-backup`, so cleanup can report residue.

`restore_transform` and `restore_edits`:

* resolve and guard every path;
* treat a file matching the original or baseline hash as already restored
  (idempotency);
* verify the transformed hash before decrypting (detect post-transform edits);
* verify the decrypted hash against the original;
* verify the backup hash when a backup exists;
* write atomically and re-verify;
* delete the backup only after success;
* collect per-file errors instead of stopping, and never overwrite content that
  fails verification.

Cleanup then removes journaled simulated artifacts (persistence files and the
restart counter), prunes empty directories, checks every baseline hash, writes
the report and returns `ok=false` if anything failed.

## 7. Plugin contract

* File must be inside `<lab>/plugins`, suffix `.py`, ≤256 KiB.
* Module attributes: `PLUGIN_API = 1`, `MODULE` = `SimModule` subclass.
* The loader computes SHA-256 before import; the event
  `payload.plugin.loaded` contains `sha256`, `path`, `trust=lab-local`.
* Imported module names are derived from the digest (`aptsim_plugin_<digest>`),
  so reloads are stable within a process.
* Plugins are trusted local code. There is no remote fetch, no signature
  verification and no sandboxing — treat the plugins directory like source
  code.

## 8. Extension points

| Want to add | Where |
|---|---|
| New pipeline behaviour | subclass `SimModule`, register in `ModuleRegistry`, add a stage |
| New payload stage | `payload.stages` entry, optional local plugin |
| New C2 task | handler in `Dispatcher._handlers` + `dispatcher.allowed_task_types` + summary method |
| New persistence shape | template in `persistence.TEMPLATES` + config mechanism |
| New anti-analysis rule | config `rules[]`; predicate must be in `PREDICATES` |
| New sink | implement `write(event)` / `close()`; append to the bus |
| New CLI command | parser entry + `async def command_*` |

When adding a handler or module, keep the invariants in
[SAFETY.md](SAFETY.md): no host writes, no process creation, no non-loopback
network, no behaviour-changing evasion.

## 9. Failure semantics

* Missing lab → `init` is invoked automatically by `run` when the victim tree
  is empty.
* Missing victim tree inside a module → `FileNotFoundError`, stage fails
  according to policy.
* Missing plugin → `PluginError`, payload stage fails; `on_error=abort` stops
  the run.
* Manifest tampering → `IntegrityError`, file left untouched, cleanup reports
  `ok=false`.
* C2 bad frame → signed error, connection closed; bad signature cannot trigger
  any handler.
* Event sink failure increments `bus.dropped` but never kills the writer.

## 10. Trade-offs

* **Ordered events vs throughput.** A single writer task favours deterministic
  logs over maximum event rate; this is a lab tool, not a production sensor.
* **Manifest-kept keys.** Required for reversibility and verification; it is
  the main intentional deviation from ransomware behaviour.
* **Scenario player instead of a task queue.** Prevents the project from
  becoming a remote-control framework at the cost of flexibility.
* **Mock indicators.** Keeps the module from accidentally becoming a real
  environment fingerprinting tool; scoring is only as interesting as the
  configured scenario.
