# Install, cutover and rollback

This runbook installs decisions-gateway as a hardened system unit on `127.0.0.1:8017`, in front of a model
server on `127.0.0.1:8117`. It covers two cases:

- a fresh install, where nothing listens on `:8017` yet;
- a cutover, where the model server itself listens on `:8017` today and clients must keep that URL.

Commands that need root are marked with `sudo`. Everything else runs as the account the gateway will run as.

## 0. Prerequisites

- Linux with systemd (tested with systemd 255), Python 3.11 or later at `/usr/bin/python3`.
- A model server that answers `POST /v1/systemone` with a Bearer key and `GET /health` without one.
- The project checked out somewhere stable. Below, `PREFIX` is its absolute path:

```bash
PREFIX=$HOME/src/decisions-gateway        # or /opt/decisions-gateway
git clone <repository url> "$PREFIX"
cd "$PREFIX"
```

## 1. Key, config and state directory

```bash
install -d -m 700 ~/.config/decisions-gateway ~/.local/state/decisions-gateway

# A new random key, written straight to a mode 600 file and never printed.
( umask 077 && python3 -c 'import secrets; print("DECISIONS_GATEWAY_API_KEY=" + secrets.token_urlsafe(32))' \
    > ~/.config/decisions-gateway/key.env )

# Optional: every key has a default. Edit the copy, for example known_clients or the backend port.
install -m 600 deploy/config.example.toml ~/.config/decisions-gateway/config.toml
```

Clients present the same key as `Authorization: Bearer <key>`. By default the gateway also presents it to the
backend; to use a separate backend key, set `key_file` and `key_var` under `[backends.<name>]`.

The key file can live elsewhere: set `key_file` in the config, or `DECISIONS_GATEWAY_KEY_FILE` in the
environment. Under systemd, add `Environment=DECISIONS_GATEWAY_KEY_FILE=...` and a `BindReadOnlyPaths=` for
its directory, since the unit hides the rest of the home directory.

## 2. Pre-checks

```bash
pipx install poetry                     # if needed; only for the tests, the service runs on the stdlib
poetry install && poetry run pytest -q

mkdir -p /tmp/dg-units
deploy/render-units.sh /tmp/dg-units     # DG_USER, DG_GROUP, DG_HOME, DG_PREFIX default to you and this checkout
systemd-analyze verify /tmp/dg-units/decisions-gateway.service
```

`render-units.sh` replaces the `@USER@`, `@GROUP@`, `@HOME@` and `@PREFIX@` placeholders. Running
`systemd-analyze verify` on the raw template fails on purpose: `WorkingDirectory=@PREFIX@` is not an absolute
path, so an unrendered template can never be started by mistake.

For the backend, `deploy/systemd/backend.service.example` is a hardened starting point (loopback-only network,
read-only program and model directories, `CPUQuota=`). Adapt its `ExecStart=` to your server, then render it
with `BACKEND_DIR=... MODEL_DIR=... deploy/render-units.sh /tmp/dg-units`; it lands as
`/tmp/dg-units/backend.service`. Rename it to your server's unit name when installing.

## 3. Fresh install

```bash
sudo install -m 644 /tmp/dg-units/decisions-gateway.service /etc/systemd/system/decisions-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now decisions-gateway
journalctl -u decisions-gateway -n 5 --no-pager
# expect: listening on 127.0.0.1:8017, default backend default at 127.0.0.1:8117
```

Go to step 5.

## 4. Cutover: the model server owns :8017 today

The goal is to move the server to `:8117` and put the gateway on `:8017`. Clients keep the same URL and key
if the server's key becomes the gateway's key: point `key_file` and `key_var` in the gateway config at the
file the server already reads, and bind its directory read-only in the gateway unit.

```bash
# 1. Make the server listen on 8117: edit its unit (or render backend.service.example with port 8117).
#    Keep a copy of the current unit for rollback.
sudo cp /etc/systemd/system/model-server.service /root/model-server.service.before-gateway
sudo install -m 644 /tmp/dg-units/backend.service /etc/systemd/system/model-server.service

# 2. Install the gateway, then switch.
sudo install -m 644 /tmp/dg-units/decisions-gateway.service /etc/systemd/system/decisions-gateway.service
sudo systemctl daemon-reload
sudo systemctl restart model-server             # frees :8017, comes back on :8117
sudo systemctl enable --now decisions-gateway   # takes :8017
until curl -sf http://127.0.0.1:8117/health >/dev/null; do sleep 5; done; echo backend ready
```

While the server warms up, the gateway answers `502` with a JSON envelope (`backend unreachable`) and
`Retry-After`, instead of a refused connection.

## 5. Checks

```bash
# open health: status "ok" once the backend is reachable, no activity counters
curl -s http://127.0.0.1:8017/health; echo

# expected codes on the right
curl -s -o /dev/null -w '%{http_code} want 401\n' -X POST http://127.0.0.1:8017/v1/systemone \
    -H 'Content-Type: application/json' -d '{}'
curl -s -o /dev/null -w '%{http_code} want 403\n' -H 'Host: evil.example' http://127.0.0.1:8017/health
curl -s -o /dev/null -w '%{http_code} want 401\n' http://127.0.0.1:8017/metrics
```

With the key: one tiny synthetic decision, then `/v1/stats` and `/metrics`. The key is read in-process by the
gateway's own reader, so it never appears in argv, shell history or output.

```bash
cd "$PREFIX" && python3 - <<'EOF'
import json, urllib.request
from decisions_gateway.config import load_config, read_key
cfg = load_config()
key = read_key(cfg.key_file, cfg.key_var)
H = {"Authorization": "Bearer " + key, "X-Decision-Client": "install-check"}
def call(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8017" + path, headers=H, method="POST" if body else "GET",
                                 data=json.dumps(body).encode() if body else None)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, r.headers.get("Server-Timing"), r.read()
st, timing, _ = call("/v1/systemone", {"state": "The sky is blue.",
                     "questions": {"q": {"type": "noul", "instructions": "Is this about weather?"}}})
print(st, "want 200 |", timing)
print(call("/v1/stats")[2].decode()[:400])
print("\n".join(l for l in call("/metrics")[2].decode().splitlines() if "requests_total{" in l))
EOF

journalctl -u decisions-gateway -n 3 --no-pager   # one line per request: ts, client, status, n_questions, ms
systemd-analyze security decisions-gateway | tail -1
ls -l ~/.local/state/decisions-gateway            # metrics.db mode 600, after the first flush (60 s)
```

`install-check` counts as `other` unless it is listed in `known_clients`.

## Rollback

Fresh install:

```bash
sudo systemctl disable --now decisions-gateway
sudo rm /etc/systemd/system/decisions-gateway.service && sudo systemctl daemon-reload
```

Cutover:

```bash
sudo systemctl disable --now decisions-gateway                     # frees :8017
sudo install -m 644 /root/model-server.service.before-gateway /etc/systemd/system/model-server.service
sudo systemctl daemon-reload && sudo systemctl restart model-server  # back on :8017
```

A `CPUQuota=` applied with `systemctl set-property` lives in a drop-in and survives the rollback. Check with
`systemctl show model-server -p CPUQuotaPerSecUSec`.

The state directory holds only aggregates; delete it if you no longer want them.

## Behaviour changes clients may notice after a cutover

- `/metrics` is on the same port behind the same Bearer key: one listener, fewer surfaces. A scraper needs
  the key.
- Admission: by default at most 8 requests in flight to the backend (more get `503` with `Retry-After`) and 4
  concurrent per client label (`429` with `Retry-After`). Clients without `X-Decision-Client` share the
  `unknown` bucket and unlisted names share `other`, so a benchmark running more than 4 parallel requests
  without a listed name sees `429`: list it in `known_clients` or raise `client_max_in_flight`.
  `X-Decision-Deadline-Ms: <n>` waits up to n ms for a backend slot instead of failing at once (then `503`).
- Caps: more than 32 questions, more than 26 options in one choice or score question, or more than 256
  questions x options in total (a noul question counts 2) gets `400` before reaching the backend.
- Backend 4xx bodies, which often echo the input, are replaced by
  `{"error":{"code":N,"message":"backend rejected the request"}}` with the same status.
- `Server-Timing` keeps the backend's entries, with its `total` renamed `backend`, then appends
  `gateway;dur=<overhead>` and `total;dur=<gateway wall time>`.
