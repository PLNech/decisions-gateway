# decisions-gateway

A small local HTTP gateway in front of typed-decision model servers. It listens on `127.0.0.1:8017`, forwards
requests to a backend on another loopback port, and adds authentication, request hygiene, caps, admission
control and observability that never stores request or response text.

The runtime is the Python standard library only (Python 3.11 or later). There are no runtime dependencies.

## Why

A typed-decision server answers short structured questions about a piece of text: yes or no, one of N
options, or a score. Such servers are often research code: they may have no auth, echo the input in error
bodies, accept unbounded requests, and expose no metrics. Putting a small gateway in front of them gives every
backend the same safe edge without patching it:

- one key, one URL, one place for limits;
- a backend overload becomes a fast `503` instead of a pile-up;
- latency and error rates per client, with a guarantee that the text being judged never reaches a log,
  a metric or a file.

## Quickstart

```bash
poetry install
install -d -m 700 ~/.config/decisions-gateway
( umask 077 && python3 -c 'import secrets; print("DECISIONS_GATEWAY_API_KEY=" + secrets.token_urlsafe(32))' \
    > ~/.config/decisions-gateway/key.env )
poetry run decisions-gateway                      # or: python3 -m decisions_gateway
poetry run pytest -q
```

With no config file the gateway listens on `127.0.0.1:8017` and forwards to one backend named `default` at
`127.0.0.1:8117`. Copy [`deploy/config.example.toml`](deploy/config.example.toml) to
`~/.config/decisions-gateway/config.toml` to change anything. For a hardened systemd install, a cutover from a
server that already owns the port, and rollback, see [`deploy/INSTALL.md`](deploy/INSTALL.md).

Command line:

```text
decisions-gateway [--config PATH] [--host HOST] [--port PORT] [--version]
```

## API

The request shape is the SystemOne one: a `state` text and a map of typed `questions`.

```json
{
  "model": "model-a",
  "state": "A customer asks for a refund.",
  "questions": {
    "refund": {"type": "noul", "instructions": "Is this a refund request?"},
    "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": "", "support": ""}},
    "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "medium", "high"]}
  }
}
```

| Method and path | Auth | What it does |
|---|---|---|
| `GET /health` | none | Gateway status plus, per backend, `reachable` and the backend's `status`, `model` and `fingerprint` fields. No activity counters. |
| `POST /v1/<path>` | Bearer | Proxied to the default backend, for example `POST /v1/systemone`. |
| `GET /v1/models` | Bearer | Proxied to the default backend. |
| `GET /metrics` | Bearer | Prometheus text format. |
| `GET /v1/stats` | Bearer | JSON summary over 5 minute, 1 hour and 24 hour windows, per client and per backend. |

Request headers:

- `Authorization: Bearer <key>`: required everywhere except `/health`.
- `Content-Type: application/json` and a `Content-Length`: required on POST.
- `X-Decision-Client: <name>`: optional, the client label used for metrics and the per-client limit.
- `X-Decision-Deadline-Ms: <n>`: optional, wait up to n ms for a backend slot (capped by `read_timeout_s`).

The gateway checks, in this order, before anything reaches the backend:

| Check | Status | Rejection reason |
|---|---|---|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` | 403 | `host` |
| Bearer key matches (constant-time compare) | 401 | `auth` |
| POST content type is `application/json` | 415 | `content_type` |
| `Content-Length` present, no `Transfer-Encoding` | 411 | `length_required` |
| Body no larger than `max_body_bytes` | 413 | `payload_too_large` |
| Questions, options per question, questions x options | 400 | `too_many_questions` |
| Per-client concurrency | 429 | `concurrency_cap` |
| Backend in-flight slots | 503 | `overloaded` or `deadline` |
| Backend reachable | 502 | `backend_down` |

Malformed JSON is not judged by the gateway: it is counted as far as possible and forwarded, and the backend's
validation error comes back through the error envelope.

### Error envelope

Every error the gateway produces, and every backend 4xx, has the same body:

```json
{"error": {"code": 429, "message": "client concurrency limit reached"}}
```

Messages are fixed strings plus numbers (for example `too many questions: 33 > 32`); they never echo input.
Backend 4xx bodies are replaced by `backend rejected the request` with the same status, because typed-decision
servers commonly echo the request in validation errors. `401` carries `WWW-Authenticate: Bearer`; `429`, `502`
and `503` carry `Retry-After`. Backend 2xx and 5xx bodies pass through unchanged.

### Server-Timing

Proxied responses carry a `Server-Timing` header. The backend's own entries are kept, its `total` is renamed
`backend`, and the gateway appends its overhead and its wall time:

```text
Server-Timing: queue;dur=12.5, inference;dur=75.7, backend;dur=90.0, gateway;dur=0.84, total;dur=90.84
```

`queue`, `inference` and `total` from the backend feed the queue, inference and overhead histograms.

## Admission

Two limits protect the backend and keep one client from starving the others:

- **Per backend**: at most `backend_max_in_flight` requests (default 8) hold a backend slot. A request that
  finds the backend full is shed at once with `503 backend at capacity`, unless it sent
  `X-Decision-Deadline-Ms`: then it waits up to that long for a slot and is shed with
  `503 deadline exceeded waiting for a backend slot`.
- **Per client**: at most `client_max_in_flight` requests (default 4) per client label, waiting or running.
  More get `429 client concurrency limit reached`. Clients without the header share the `unknown` label and
  unlisted names share `other`, so they also share one budget.

Shedding early keeps tail latency bounded: a request is either served or refused quickly, it does not queue
behind a backend that cannot keep up.

## Configuration

Every key is optional. [`deploy/config.example.toml`](deploy/config.example.toml) lists all of them with their
defaults; a test keeps it in sync with the code. Unknown keys are an error.

| Key | Default | Meaning |
|---|---|---|
| `listen_host`, `listen_port` | `127.0.0.1`, `8017` | Listener. `--host` and `--port` override them. |
| `default_backend` | `default` | Backend name that receives every request. |
| `log_level` | `INFO` | Python logging level. |
| `key_file` | `~/.config/decisions-gateway/key.env` | File holding the client key as `KEY=value`. |
| `key_var` | `DECISIONS_GATEWAY_API_KEY` | Variable name read from `key_file`. |
| `connect_timeout_s`, `read_timeout_s` | `2.0`, `120.0` | Backend timeouts. |
| `max_body_bytes` | `262144` | Request body cap. |
| `max_questions` | `32` | Questions per request. |
| `max_options` | `26` | Options in one choice or score question. |
| `max_budget` | `256` | Questions x options per request; a noul question counts 2. |
| `backend_max_in_flight` | `8` | Slots per backend. |
| `client_max_in_flight` | `4` | Concurrent requests per client label. |
| `retry_after_s` | `1` | `Retry-After` value. |
| `state_dir` | `~/.local/state/decisions-gateway` | Holds `metrics.db`. |
| `flush_interval_s` | `60.0` | SQLite flush and backend probe interval. |
| `ring_minutes` | `10080` | Minutes of aggregates kept in memory. |
| `retention_days`, `max_rows` | `30`, `500000` | On-disk retention and hard row cap. |
| `label_cap` | `16` | Max entries in each allow-list. |
| `known_clients` | `[]` | Client names that get their own label. |
| `known_models` | `[]` | Body `model` values that get their own label. |
| `[backends.<name>]` | `default` at `127.0.0.1:8117` | `host`, `port`, optional `key_file` and `key_var`. |

The environment variable `DECISIONS_GATEWAY_KEY_FILE`, when set and non-empty, overrides `key_file`. A backend
without its own `key_file` or `key_var` is sent the gateway's key. Key values never appear in a log line or an
error message: the reader names the file and the variable, never the value.

## Metrics

`GET /metrics` exposes, all prefixed `decisions_gateway_`:

| Name | Type | Labels |
|---|---|---|
| `requests_total` | counter | `backend`, `model`, `client`, `outcome` |
| `rejections_total` | counter | `reason`, `client` |
| `backend_errors_total` | counter | `backend`, `reason` (`connect`, `timeout`, `protocol`) |
| `questions_total` | counter | `type` (`noul`, `choice`, `score`, `other`) |
| `request_duration_seconds` | histogram | `backend`, `client` |
| `queue_time_seconds` | histogram | `backend`, `client` |
| `inference_time_seconds` | histogram | `backend`, `client` |
| `overhead_seconds` | histogram | `backend`, `client` |
| `request_bytes` | histogram | `backend`, `client` |
| `questions_per_request` | histogram | `backend`, `client` |
| `in_flight_requests` | gauge | `backend` |
| `backend_up` | gauge | `backend` |
| `build_info` | gauge | `version` |

`outcome` is one of `ok`, `client_error`, `backend_error`, `rejected`. `reason` comes from the closed list in
[`decisions_gateway/metric_names.py`](decisions_gateway/metric_names.py), where every name lives.

Aggregates are kept per minute and flushed to `state_dir/metrics.db` (SQLite, directory mode 700, file mode
600). The process logs one line per proxied request: timestamp, client label, backend, status, question count
and three durations.

## Privacy guarantees

The gateway never stores, logs or exports request or response text. Concretely:

- Labels come only from config allow-lists or closed enums. `X-Decision-Client` and the body `model` field are
  matched against `known_clients` and `known_models`; anything not listed becomes `other`, absent becomes
  `unknown`. Caller-chosen text cannot become a label.
- What is kept is numbers: counts, byte sizes, question and option counts, durations.
- Request paths and query strings are not logged; the standard library access log is silenced, and handler
  errors log only the exception class name.
- Error bodies are fixed strings plus numbers. Backend 4xx bodies are replaced.
- `/health` is open but carries no activity counters, only reachability and three short backend fields.

[`tests/test_privacy.py`](tests/test_privacy.py) proves this end to end. It sends a canary string in every
place a caller controls: the `state` text, question names, instructions, option names and texts, the body
`model` field, the `X-Decision-Client` header, the URL path, the query string and the `Host` header. It drives
the canary through every outcome: success, cap rejection, body too large, wrong content type, missing key,
forbidden host, a backend `422` that echoes its input, `429`, `503` and deadline shedding. It then asserts the
canary appears in none of: `/metrics`, `/v1/stats`, every file in the state directory after a flush, the
gateway's log output, the process stdout and stderr, and every error body. It also asserts the store did write
rows, so an empty file cannot pass by accident.

## Security model

- **Loopback only.** The gateway binds `127.0.0.1` by default and is meant for one machine. Backends are
  reached over plain HTTP, so keep them on loopback too: the Bearer key travels in clear.
- **DNS rebinding.** A browser page can resolve its own domain to `127.0.0.1`. The `Host` check rejects any
  request whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>`.
- **Auth.** One Bearer key for every route except `/health`, compared in constant time. The key is read from a
  file in-process, never taken from argv.
- **Resource bounds.** Body size, question and option caps, a client socket timeout, backend timeouts and
  admission limits bound what one request or one client can cost.
- **Sandbox.** The systemd template runs the gateway with a loopback-only BPF network policy, a hidden home
  except the project, config and state directories, a read-only system, no capabilities and a syscall filter
  (`systemd-analyze security` rates it 1.4, OK).

Report vulnerabilities as described in [`SECURITY.md`](SECURITY.md).

## Non-goals

- No TLS, no remote exposure, no multi-tenant key management. Put a real reverse proxy in front if you need
  any of that.
- No routing between backends yet: every request goes to `default_backend` (`Gateway.pick_backend` is the
  seam for it).
- No request validation beyond size caps; the backend owns the schema.
- No content logging, sampling or tracing of payloads, by design.
- No streaming responses.

## Development

```bash
poetry install
poetry run pytest -q
poetry run ruff check . && poetry run ruff format --check .
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT, see [`LICENSE`](LICENSE).
