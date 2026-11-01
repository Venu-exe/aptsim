# Safety model

`aptsim` exists to study malware **architecture** and generate detection
content. It is deliberately unable to act like deployed malware. This document
lists the boundaries, where they are enforced, and how to review an extension.

## 1. Non-goals

The framework does not, and must not be extended to:

* harvest credentials, tokens, cookies, browser data or key material;
* install persistence on the host (autostart, cron, systemd, shell rc, launchd);
* replicate itself, spread, or copy itself to other paths;
* encrypt, destroy, exfiltrate or delete user data;
* escalate privileges, abuse sudo/setuid/capabilities, or bypass authentication;
* disable, evade or tamper with security tools, EDR, auditd, SELinux, AppArmor
  or firewalls;
* hide, pack, obfuscate or persist stealthily;
* connect to any non-loopback address or accept operator commands;
* interact with third-party systems, cloud metadata endpoints or the internet;
* spawn processes or load code from untrusted/remote sources.

## 2. Enforcement points

| Boundary | Implementation | Test |
|---|---|---|
| Lab root is disposable | `validate_lab_root` rejects `/`, system trees, home, hidden home dirs | `test_paths`, `test_config` |
| Writes stay inside lab | `LabPaths.guard` / `resolve_relative` / `guard_victim` | `test_paths`, `test_fileops` |
| Symlink escape | `Path.resolve` before the containment check | `test_paths` |
| Host persistence blocked | `BLOCKED_TARGETS`, `assert_not_host_target`, persistence targets limited to `<lab>/persistence` | `test_paths`, `test_config` |
| No process creation | no `subprocess`/`fork`/`exec`/`os.system` in `src/`; static scan | `test_safety` |
| No dynamic code execution | no `eval`/`exec` in `src/`; plugin files are source-imported, not evaluated from strings | `test_safety` |
| Loopback only | config validation, `C2Server`/`C2Client` constructors, network module | `test_config`, `test_c2` |
| No operator control | server delivers a fixed scenario; no task injection endpoint | `test_c2`, `test_engine` |
| Inert command handling | closed allowlist; read-only handlers; path confinement; timeout; no write handler | `test_dispatcher` |
| Evasion is log-only | config + module reject `enforcement != log_only`; no host indicator reads | `test_safety` |
| Reversible transform | keys and backups retained; hash verification; fail closed on tamper | `test_fileops`, `test_recovery` |
| No destructive cleanup | only journaled artifacts and verified backups; purge requires a validated path and `--yes` | `test_recovery` |
| No secret material | synthetic files are clearly fake and scanned for key-like patterns | `test_safety` |

The static scan is intentionally blunt: a new `subprocess` import or `eval(`
call fails the suite. This is a feature, not an obstacle to work around.

## 3. Threat model of the lab itself

**Assets.** The host filesystem outside the lab, the user's credentials and
configuration, the local network, and the integrity of the research data.

**Trust boundaries.** The lab directory is untrusted but disposable. Config and
plugins are trusted local code (they are source files). The C2 peer is
loopback-only and authenticated, but is still treated as an untrusted message
source: every frame is validated, replayed frames are rejected, task types and
paths are allowlisted.

**Failure modes considered**

1. *Misconfigured lab root.* `lab.root=/etc` is rejected at config load; a
   relative or symlinked path is resolved before checks.
2. *Path traversal through task arguments.* The dispatcher rejects absolute
   paths, `..`, and anything outside `victim/` before a handler runs.
3. *Manifest tampering.* Restore verifies both transformed and original hashes;
   a mismatch leaves the file untouched and reports failure.
4. *Runaway cleanup.* Cleanup only removes journaled kinds
   (`persistence`, `persistence-state`, `plugin-copy`) and backup files after
   successful restore. Session logs and manifests are never removed.
5. *Accidental external listener.* The server constructor rejects non-loopback
   hosts; config validation rejects them too.
6. *Sandbox study becoming a real evasion tool.* Indicators come from config
   only; no `/proc`, MAC, hostname, debugger or tool detection is performed;
   decisions are recorded but never applied.

## 4. Transform is not ransomware

The transform is a controlled demonstration:

* per-file keys are derived deterministically and stored in the manifest;
* originals are copied to `.backup/` and verified before the transform;
* file names, extensions and directory structure are unchanged;
* no file is deleted, no shadow copy is touched, no key is destroyed;
* restore verifies SHA-256 and refuses to clobber unexpected content;
* the scope is `lab/victim` only.

It produces high-entropy writes and a realistic "many files modified" event
pattern for detection engineering, while remaining fully reversible.

## 5. C2 is a scenario player

The server cannot be told what to do. Tasks are loaded from configuration and
handed out in fixed batches. Even the standalone client is observe-only. The
dispatcher's handler set is closed, read-only and lab-scoped; adding a write,
shell, download or privilege handler would violate the project's purpose and
must not be merged.

## 6. Review checklist for new modules

Before merging a new behaviour, answer **yes** to all:

- [ ] Does it resolve every path through `LabPaths`?
- [ ] Does it avoid process creation and shell/`eval`/`exec`?
- [ ] Does it avoid non-loopback network access?
- [ ] Does it avoid reading real credentials or host config?
- [ ] Is the harmful effect replaced by telemetry or a reversible operation?
- [ ] Does it emit events with enough context to write a detection?
- [ ] Does it journal every created/mutated file for cleanup?
- [ ] Does it have a unit test, including a negative/boundary test?
- [ ] Does it keep working with `runtime.time_scale=0`?
- [ ] Does `tests/test_safety.py` still pass unmodified?

If a proposed feature requires weakening any interlock, it does not belong in
this repository.

## 7. Operational guidance

* Use a dedicated directory (`./lab`, `/tmp/opencode/aptsim-lab`) on a machine
  you control.
* Do not run as root. Nothing requires it.
* Keep the lab out of version control (`.gitignore` already excludes it).
* Run `aptsim cleanup` after experiments and `verify` before and after.
* If a run is interrupted, inspect `state.json` and `timeline.txt`, then
  `resume` or `cleanup`.
* To reset completely: `./scripts/cleanup_lab.sh --purge --yes` (the script
  refuses unexpected paths).

## 8. Legal and ethical note

The code is intended for authorised security research and education. Running
simulated malicious behaviour against systems you do not own, or reusing parts
of this project to build deployable malware, may be illegal and is contrary to
its purpose. The limitations are not obstacles to be removed; they are the
reason the project is safe to publish and study.
