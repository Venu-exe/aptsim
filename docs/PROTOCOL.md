# C2 wire protocol (v1)

The simulated C2 is a request/response protocol over a single loopback TCP
connection. It exists to demonstrate authenticated session tracking, heartbeat
and task/result correlation. It is a **scenario player**: the server hands out
fixed, allowlisted task descriptors and cannot be commanded by an operator.

## 1. Transport

* TCP, `127.0.0.1` or `::1` only (validated in config and constructors)
* newline-delimited JSON (`\n` terminated), UTF-8
* maximum frame size 64 KiB (StreamReader limit + codec check)
* one request → one response per frame; responses are correlated by order
* idle timeout 60 s (server), request timeout 15 s (client)

## 2. Envelope

Every frame is a JSON object. The signature covers every field except `auth`.

```json
{
  "v": 1,
  "type": "poll",
  "ts": 1770000000.123,
  "nonce": "9f2c1a7b4e8d0c11",
  "session_id": "s-20261101T083148Z-c9f59b",
  "auth": "b1946ac92492d2347c6235b4d2611184..."
}
```

| Field | Type | Notes |
|---|---|---|
| `v` | int | must be `1` |
| `type` | string | message type |
| `ts` | float | Unix seconds; rejected if more than 30 s in the past or future |
| `nonce` | string | random 16 hex chars; replay cache window 60 s |
| `auth` | string | lowercase hex HMAC-SHA256 |
| other | any | message-specific fields, all signed |

### Signature

```text
auth = HMAC-SHA256(key,
                   json.dumps(fields_without_auth,
                              sort_keys=True,
                              separators=(",", ":")))
```

`key` is `network.auth.shared_secret` (minimum 8 bytes; lab default is a
well-known placeholder and must not be reused anywhere real).

### Validation order (receiver)

1. frame length ≤ 64 KiB
2. valid JSON object
3. `auth` present and `hmac.compare_digest` matches
4. `v == 1`
5. `type` non-empty string
6. `ts` parses and is within ±30 s
7. `nonce` present and not seen in the replay window

Any failure raises `ProtocolError`. The server emits
`network.server.frame_rejected`, sends a signed `error` frame and closes the
connection.

## 3. Message types

### `register` (client → server)

```json
{"type":"register","session_id":"s-...","capabilities":["scheduler","beacon"]}
```

Server response:

```json
{"type":"registered","session_id":"s-...","server_time":1770000000.2,
 "heartbeat_interval":0.25,
 "policy":{"tasks":6,"mode":"scripted-lab"}}
```

Re-registering the same session id replaces the record. `session_id` must be
1–64 characters; capabilities are truncated to 32 entries.

### `heartbeat` (client → server)

```json
{"type":"heartbeat","session_id":"s-...","seq":1}
```

Response:

```json
{"type":"heartbeat_ack","session_id":"s-...","server_time":1770000000.5,"beats":1}
```

### `poll` (client → server)

```json
{"type":"poll","session_id":"s-..."}
```

Response:

```json
{"type":"tasks","session_id":"s-...",
 "tasks":[{"task_id":"t-001","type":"report_status","args":{}},
          {"task_id":"t-002","type":"list_files","args":{"path":"victim"}}],
 "remaining":4,"exhausted":false}
```

At most `network.tasks_per_poll` tasks are returned. When the scripted
scenario is exhausted the response has `tasks: []`, `remaining: 0`,
`exhausted: true`.

### `result` (client → server)

```json
{"type":"result","session_id":"s-...","task_id":"t-001",
 "status":"ok","result":{"session_id":"s-...","mode":"simulation-only"},
 "error":null}
```

Response:

```json
{"type":"result_ack","session_id":"s-...","task_id":"t-001"}
```

Statuses: `ok` (handler returned), `rejected` (validation failed before
execution), `timeout` (handler exceeded `dispatcher.task_timeout`), `error`
(handler raised), `skipped` (standalone observer client).

### `error` (server → client)

```json
{"type":"error","code":"bad_frame","message":"invalid frame signature"}
```

Codes: `bad_frame`, `idle_timeout`, `unknown_session`, `unsupported_type`,
`bad_session`. The standalone client raises `ProtocolError` on any error frame.

## 4. Task descriptors

A task is data, not code:

```json
{"task_id":"t-003","type":"hash_files","args":{"path":"victim","limit":5}}
```

Allowlist (`dispatcher.allowed_task_types`):

| Type | Args | Handler behaviour | Side effects |
|---|---|---|---|
| `noop` | `{}` | acknowledges | none |
| `report_status` | `{}` | returns session/state summary | none |
| `list_files` | `{"path":"victim"}` | lists direct children (≤200) | none |
| `hash_files` | `{"path":"victim","limit":1..50}` | SHA-256 of lab files | reads only |
| `sleep` | `{"seconds":0..2}` | simulated delay | none |

Rejected before execution:

* unknown/dangerous type (`shell`, `exec`, `eval`, `write_file`, ...);
* absolute path, `..` segment, path outside `dispatcher.allowed_roots`;
* `limit` outside 1–50 or wrong type;
* `sleep` outside 0–2 s;
* missing/oversized/invalid `task_id`;
* non-object `args`.

There is no handler that writes, executes, downloads, spawns, deletes, reads
credentials, or opens a network connection.

## 5. Session tracking

The server keeps one `SessionRecord` per session id:

```json
{"session_id":"s-...","peer":"127.0.0.1:41322","capabilities":["beacon"],
 "registered_at":1770000000.1,"last_seen":1770000002.4,
 "heartbeats":3,"polls":4,"tasks_sent":6,
 "results":[{"task_id":"t-001","status":"ok","error":null,"received_at":...}]}
```

`tasks_sent` counts descriptors delivered; `results` retains the last 20. The
network module writes a compact copy to `c2-session.json`.

## 6. Scenario delivery

`config/scenarios/c2-demo.json` (and `network.scenario` in the main config)
contains the fixed task list. The default demo includes:

* `report_status`, `list_files`, `hash_files`, `noop` — normal flow;
* `list_files` with `../../etc` — traversal rejection;
* `shell` — allowlist rejection.

`ScenarioPlayer` keeps a per-session cursor and never repeats a task.

## 7. Limits and failure behaviour

| Limit | Value | On violation |
|---|---|---|
| frame size | 64 KiB | `ProtocolError`, error frame, close |
| clock skew | ±30 s | `ProtocolError`, error frame, close |
| replay window | 60 s | `ProtocolError`, error frame, close |
| idle connection | 60 s | error frame, close |
| tasks per session | `dispatcher.max_tasks` (100) | stop polling, emit warning |
| handler timeout | 10 s | result `timeout` |
| handler concurrency | 3 | semaphore queueing |
| poll budget | `network.max_polls` (12) | stop polling |

## 8. Security properties (and non-properties)

Properties:

* message integrity and authenticity with a pre-shared key;
* replay and stale-frame rejection;
* bounded memory/CPU per frame;
* no path escape from task arguments;
* no code execution path from network input;
* no external network reachability.

Non-properties:

* the shared secret is a lab constant, not a key-exchange protocol;
* there is no confidentiality (plaintext loopback), no forward secrecy;
* the server does not authenticate its own identity beyond knowing the key;
* this is not a hardened production C2 protocol and should not be used as one.

These are appropriate for a loopback research harness and deliberately
insufficient for real command-and-control.
