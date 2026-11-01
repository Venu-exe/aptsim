# Using aptsim against a real pipeline

`aptsim` produces a session log at `lab/sessions/<id>/events.jsonl`: one JSON
object per line, 100+ events per run. This document is about getting that data
into a real SIEM, dashboard, or detection test — and being clear about what it
can and cannot prove.

> Only send this data to systems you own or administer. It is synthetic, but
> your pipeline should treat it exactly like production input.

## 1. One-command replay

```bash
# latest run -> NDJSON on stdout
python3 scripts/replay_events.py --dry-run > /tmp/aptsim.ndjson

# a specific session
python3 scripts/replay_events.py --events lab/sessions/<id>/events.jsonl --dry-run

# Splunk HTTP Event Collector
python3 scripts/replay_events.py --format splunk \
  --url https://splunk.example:8088 --token "$HEC_TOKEN" \
  --index lab --sourcetype aptsim:json --rate 0.02

# Elastic / OpenSearch document API
python3 scripts/replay_events.py --format elastic \
  --url https://es.example:9200 --token "$ES_TOKEN" \
  --index aptsim-events --rate 0.02

# any collector that accepts one JSON object per POST
python3 scripts/replay_events.py --format raw --url http://collector:9000/ingest
```

The script retries each event three times, prints progress to stderr, and exits
non-zero if it cannot deliver. It uses only the Python standard library.

## 2. Static shipping configs

### Filebeat → Elastic

```yaml
filebeat.inputs:
  - type: filestream
    id: aptsim-events
    paths:
      - /var/log/aptsim/events.jsonl
    parsers:
      - ndjson:
          target: ""
          add_error_key: true
    fields:
      dataset: aptsim.events
    fields_under_root: true

processors:
  - timestamp:
      field: ts
      layouts: ["UNIX"]
      test: ["event.ts > 0"]

output.elasticsearch:
  hosts: ["https://es.example:9200"]
  index: "aptsim-events-%{+yyyy.MM.dd}"
```

Copy the session file to the watched path after each run:

```bash
install -m 0640 lab/sessions/*/events.jsonl /var/log/aptsim/events.jsonl
```

### Splunk

`inputs.conf`:

```ini
[monitor:///var/log/aptsim/events.jsonl]
sourcetype = aptsim:json
index = lab
```

`props.conf`:

```ini
[aptsim:json]
SHOULD_LINEMERGE = false
LINE_BREAKER = ([\r\n]+)
TIME_PREFIX = "ts":\s*
TIME_FORMAT = %s.%f
MAX_TIMESTAMP_LOOKAHEAD = 24
TRUNCATE = 0
```

### Wazuh

```xml
<localfile>
  <log_format>json</log_format>
  <location>/var/log/aptsim/events.jsonl</location>
</localfile>
```

## 3. Field reference

| Field | Example | Use |
|---|---|---|
| `seq` | `42` | event ordering within a session |
| `ts` | `1793523198.34` | Unix seconds; use for `_time` / `@timestamp` |
| `t_rel` | `1.75` | seconds since run start; good for sequence rules |
| `session_id` | `s-2026…` | correlate a whole run |
| `category` | `file`, `process`, `network`, `command` | coarse routing |
| `action` | `transformed`, `spawn.simulated`, `task.completed` | detection key |
| `module` | `fileops`, `process`, `c2.server` | source component |
| `severity` | `info`, `warning`, `error` | triage |
| `data.*` | `path`, `pid`, `status`, `sha256` | specifics |

A practical key is `category + "." + action`, e.g. `file.transformed`.

## 4. Example detections

**SPL**

```spl
index=lab sourcetype=aptsim:json category=file action=transformed
| stats count by session_id, data.path
| where count > 5
```

```spl
index=lab sourcetype=aptsim:json category=command data.status=rejected
| table _time session_id data.task_id data.error
```

**KQL**

```kql
aptsim-events-*
| where category == "persistence" and action == "registration.simulated"
| project @timestamp, session_id, data.path
```

```kql
aptsim-events-*
| where category == "network" and action == "server.started"
| summarize count() by bin(@timestamp, 1m), data.port
```

**Correlation idea:** file burst + loopback session + rejected command within
the same `session_id` and a 2-minute window. That is the shape a real intrusion
would produce; here it is guaranteed to be present.

## 5. What a real sensor will and will not see

If you run `aptsim` on an endpoint that has auditd, Falco, or an EDR agent,
expect:

| Behaviour | Visible to a host sensor? |
|---|---|
| 6 files rewritten in `lab/victim` | yes — file-write events, high entropy, burst |
| loopback TCP session | yes — `connect`/`accept` on `127.0.0.1` |
| startup templates created | only as file creates in `lab/persistence`; no real autostart path |
| fake process chain | **no** — no process is created |
| sandbox "evasion" | **no** — config values, never host probing |
| "encryption" | ordinary writes; no crypto API calls |

So host sensors validate the *file and network* half of a detection, and the
event log validates the *process, persistence, command* half. Use both.

## 6. Why not just run real tooling?

If the question is "would my EDR catch a real APT", `aptsim` cannot answer it:
it never executes. Use authorized adversary emulation for that (Atomic Red
Team, Caldera, MITRE ATT&CK Evaluations) in a scoped lab, and use `aptsim` for
reproducible data, reversible impact demonstrations, and pipeline tests.

## 7. Scope

- Isolated VM or network segment; no production hosts.
- Written authorization before touching any system you do not own.
- Snapshot before, `aptsim cleanup` after, `aptsim verify` to confirm.
- Do not forward the events to third parties.
