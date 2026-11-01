# Detection content examples

These Sigma-style rules describe the **host telemetry a real intrusion would
produce** for the behaviours `aptsim` simulates. They are starting points for
detection engineering, not detections of `aptsim` itself.

`aptsim` writes JSONL events; the mapping below shows which real telemetry
source would carry the equivalent signal if the behaviour were not simulated.

| aptsim event | Real telemetry (example) | Notes |
|---|---|---|
| `persistence.registration.simulated` (xdg) | Sysmon EID 11 file create, auditd `PATH` watch | `~/.config/autostart/*.desktop` |
| `persistence.registration.simulated` (cron) | auditd `PATH` watch on `/etc/cron.d`, `/var/spool/cron` | package manager parent expected |
| `persistence.registration.simulated` (systemd) | auditd `PATH` on `~/.config/systemd/user` | `systemctl --user enable` |
| `process.spawn.simulated` | Sysmon EID 1, auditd `EXECVE` | parent/child chain |
| `file.transformed` | Sysmon EID 11 / 2, fanotify | burst of writes + high entropy |
| `file.edited` | Sysmon EID 2 | append to document |
| `network.session.registered` | Sysmon EID 3, Zeek `conn.log` | loopback only in the lab |
| `anti_analysis.evasion.simulated` | EDR process/environment enumeration | sequence, not a single event |

Useful `jq` queries against `events.jsonl`:

```bash
# every rejection with reason
jq -c 'select(.category=="command" and .data.status!="ok") | {task:.data.task_id,status:.data.status,err:.data.error}' events.jsonl

# file mutations in order
jq -c 'select(.category=="file" and (.action|test("transformed|edited"))) | {t:.t_rel,action,path:.data.path}' events.jsonl

# stage durations
jq -r 'select(.category=="lifecycle" and .action=="stage.completed") | "\(.data.stage_id)\t\(.data.duration_s)s"' events.jsonl
```

## Rules

* [`sim_xdg_autostart_persistence.yml`](sim_xdg_autostart_persistence.yml)
* [`sim_cron_persistence.yml`](sim_cron_persistence.yml)
* [`sim_mass_file_modification.yml`](sim_mass_file_modification.yml)

Field names follow Sigma's Sysmon/auditd conventions; adapt `logsource` to your
pipeline. Test rules against the committed sample events in
`docs/sample-run/session/` before deployment.
