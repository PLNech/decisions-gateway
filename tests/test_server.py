import threading
import time

from decisions_gateway import metric_names as names
from decisions_gateway.server import server_timing


def decision(n=1, state="a customer asks for a refund"):
    return {
        "model": "model-a",
        "state": state,
        "questions": {f"q{i}": {"type": "noul", "instructions": "Refund?"} for i in range(n)},
    }


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_health_is_open_and_hides_counters(make_gateway):
    client, *_ = make_gateway()
    status, _, doc = client.json("GET", "/health", auth=False)
    assert status == 200 and doc["status"] == "ok"
    backend = doc["backends"]["default"]
    assert backend == {"reachable": True, "status": "ready", "model": "fake-v1", "fingerprint": "f00d"}


def test_auth_required(make_gateway):
    client, *_ = make_gateway()
    for kw in (
        {"auth": False},
        {"headers": {"Authorization": "Bearer wrong"}},
        {"headers": {"Authorization": "Basic x"}},
    ):
        status, headers, doc = client.json("POST", "/v1/systemone", body=decision(), **kw)
        assert status == 401 and doc == {"error": {"code": 401, "message": "missing or invalid bearer token"}}
        assert headers["WWW-Authenticate"] == "Bearer"
    for path in ("/metrics", "/v1/stats"):
        assert client.request("GET", path, auth=False)[0] == 401


def test_host_header_pinned(make_gateway):
    client, *_ = make_gateway()
    for host in ("evil.example", f"evil.example:{client.port}", "127.0.0.1:1", f"127.0.0.2:{client.port}"):
        assert client.request("POST", "/v1/systemone", body=decision(), headers={"Host": host})[0] == 403
        assert client.request("GET", "/health", headers={"Host": host})[0] == 403
    assert client.request("GET", "/health", headers={"Host": f"localhost:{client.port}"})[0] == 200


def test_content_type_and_size(make_gateway):
    client, *_ = make_gateway(max_body_bytes=1024)
    assert client.request("POST", "/v1/systemone", body=decision(), ctype="text/plain")[0] == 415
    assert client.request("POST", "/v1/systemone", body=decision(), ctype="application/x-www-form-urlencoded")[0] == 415
    status, _, doc = client.json("POST", "/v1/systemone", body=decision(state="x" * 2000))
    assert status == 413 and doc["error"]["code"] == 413
    assert client.request("POST", "/v1/systemone", body=decision(), ctype="application/json; charset=utf-8")[0] == 200


def test_question_caps(make_gateway):
    client, gw, fake, _ = make_gateway()
    status, _, doc = client.json("POST", "/v1/systemone", body=decision(n=33))
    assert status == 400 and doc["error"]["message"] == "too many questions: 33 > 32"
    assert fake.arrived == 0  # never forwarded


def test_passthrough(make_gateway):
    client, gw, fake, _ = make_gateway(known_clients=["benchscript"])
    status, headers, doc = client.json(
        "POST", "/v1/systemone", body=decision(n=2), headers={"X-Decision-Client": "Bench Script!"}
    )
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert doc == {
        "model": "fake-v1",
        "answers": {"q0": {"type": "noul", "noul": 0.5}, "q1": {"type": "noul", "noul": 0.5}},
    }
    timing = headers["Server-Timing"]
    assert "queue;dur=12.5" in timing and "inference;dur=75.7" in timing and "backend;dur=90.0" in timing
    assert ", gateway;dur=" in timing and timing.split(", ")[-1].startswith("total;dur=")
    assert client.json("GET", "/v1/models")[2] == {"data": [{"id": "model-a"}]}
    text = client.request("GET", "/metrics")[2].decode()
    assert f'{names.REQUESTS}{{backend="default",model="model-a",client="benchscript",outcome="ok"}} 1' in text
    assert f'{names.QUEUE}_sum{{backend="default",client="benchscript"}} 0.0125' in text


def test_server_timing_rewrite():
    assert (
        server_timing("queue;dur=5, total;dur=9", 1.0, 10.0)
        == "queue;dur=5, backend;dur=9, gateway;dur=1.00, total;dur=10.00"
    )
    assert server_timing(None, None, 3.0) == "total;dur=3.00"


def test_backend_422_body_is_replaced(make_gateway):
    client, *_ = make_gateway()
    status, _, doc = client.json("POST", "/v1/systemone", body=decision(state="force-422"))
    assert status == 422 and doc == {"error": {"code": 422, "message": "backend rejected the request"}}


def test_dead_backend_is_502_and_counted(make_gateway):
    client, gw, *_ = make_gateway(dead_backend=True)
    status, headers, doc = client.json("POST", "/v1/systemone", body=decision())
    assert status == 502 and doc["error"]["message"] == "backend unreachable" and "Retry-After" in headers
    _, _, health = client.json("GET", "/health", auth=False)
    assert health["status"] == "degraded" and health["backends"]["default"]["reachable"] is False
    text = client.request("GET", "/metrics")[2].decode()
    assert f'{names.BACKEND_ERRORS}{{backend="default",reason="connect"}} 2' in text
    assert f'{names.REJECTIONS}{{reason="backend_down",client="unknown"}} 1' in text
    assert f'{names.BACKEND_UP}{{backend="default"}} 0' in text


def test_admission_429_503_deadline(make_gateway):
    client, gw, fake, _ = make_gateway(
        backend_max_in_flight=2, client_max_in_flight=1, known_clients=["a", "b", "c", "d"]
    )
    fake.gate.clear()
    results = {}

    def send(name):
        results[name] = client.request("POST", "/v1/systemone", body=decision(), headers={"X-Decision-Client": name})

    threads = [threading.Thread(target=send, args=(n,)) for n in ("a", "b")]
    threads[0].start()
    assert wait_until(lambda: fake.arrived == 1)
    threads[1].start()
    assert wait_until(lambda: fake.arrived == 2)
    status, headers, _ = client.request("POST", "/v1/systemone", body=decision(), headers={"X-Decision-Client": "a"})
    assert status == 429 and headers["Retry-After"] == "1"
    status, _, body = client.request("POST", "/v1/systemone", body=decision(), headers={"X-Decision-Client": "c"})
    assert status == 503 and b"backend at capacity" in body
    t0 = time.monotonic()
    status, _, body = client.request(
        "POST", "/v1/systemone", body=decision(), headers={"X-Decision-Client": "d", "X-Decision-Deadline-Ms": "100"}
    )
    assert status == 503 and b"deadline" in body and time.monotonic() - t0 >= 0.1
    in_flight = client.request("GET", "/metrics")[2].decode()
    assert f'{names.IN_FLIGHT}{{backend="default"}} 2' in in_flight
    fake.gate.set()
    for t in threads:
        t.join(5)
    assert results["a"][0] == 200 and results["b"][0] == 200
    text = client.request("GET", "/metrics")[2].decode()
    for reason, who in (("concurrency_cap", "a"), ("overloaded", "c"), ("deadline", "d")):
        assert f'{names.REJECTIONS}{{reason="{reason}",client="{who}"}} 1' in text
    assert f'{names.IN_FLIGHT}{{backend="default"}} 0' in text


def test_unlisted_clients_and_models_are_other(make_gateway):
    client, *_ = make_gateway(known_clients=["c0", "c1"])
    for i in range(5):
        client.request("POST", "/v1/systemone", body=decision(), headers={"X-Decision-Client": f"c{i}"})
    client.request("POST", "/v1/systemone", body=decision() | {"model": "made-up"})
    clients = client.json("GET", "/v1/stats")[2]["windows"]["5m"]["clients"]
    assert set(clients) == {"c0", "c1", "other", "unknown"} and clients["other"]["count"] == 3
    text = client.request("GET", "/metrics")[2].decode()
    assert 'model="other",client="unknown"' in text and "made-up" not in text


def test_stats_flush_and_log_line(make_gateway):
    client, gw, fake, buf = make_gateway(known_clients=["bench"])
    client.request("POST", "/v1/systemone", body=decision(n=3), headers={"X-Decision-Client": "bench"})
    stats = client.json("GET", "/v1/stats")[2]
    bench = stats["windows"]["5m"]["clients"]["bench"]
    assert bench["count"] == 1 and bench["error_rate"] == 0 and bench["queue_share"] is not None
    assert stats["in_flight"] == {"default": 0} and stats["backend_up"] == {"default": 1}
    gw.flush()
    assert gw.store.count() == 1
    line = next(ln for ln in buf.getvalue().splitlines() if "client=bench" in ln)
    assert "backend=default status=200 n_questions=3" in line and "queue_ms=12.5 inference_ms=75.7" in line
    assert "/v1/" not in buf.getvalue()


def test_chunked_body_is_411_length_required(make_gateway):
    import socket

    from conftest import KEY

    client, *_ = make_gateway()
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as s:
        s.sendall(
            (
                f"POST /v1/systemone HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\nAuthorization: Bearer {KEY}\r\n"
                "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            ).encode()
        )
        assert s.recv(4096).startswith(b"HTTP/1.1 411")
    text = client.request("GET", "/metrics")[2].decode()
    assert f'{names.REJECTIONS}{{reason="length_required",client="unknown"}} 1' in text


def test_every_rejection_reason_is_in_the_closed_enum():
    import re
    from pathlib import Path

    from decisions_gateway.server import SHED

    src = (Path(__file__).parent.parent / "decisions_gateway" / "server.py").read_text()
    used = set(re.findall(r'reason="([a-z_]+)"', src)) | set(SHED)
    assert used and used <= set(names.REJECTION_REASONS)


def test_env_key_file_override_end_to_end(tmp_path, monkeypatch):
    from conftest import KEY, Client
    from fakes import FakeBackend

    from decisions_gateway.config import KEY_FILE_ENV, load_config
    from decisions_gateway.server import build_server

    key_file = tmp_path / "elsewhere" / "key.env"
    key_file.parent.mkdir()
    key_file.write_text(f"DECISIONS_GATEWAY_API_KEY={KEY}\n")
    fake = FakeBackend(KEY)
    config = tmp_path / "config.toml"
    config.write_text(
        f'listen_port = 0\nstate_dir = "{tmp_path / "state"}"\nkey_file = "{tmp_path / "absent.env"}"\n'
        f"[backends.default]\nport = {fake.port}\n"
    )
    monkeypatch.setenv(KEY_FILE_ENV, str(key_file))
    server = build_server(load_config(str(config)))
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        # the gateway checks clients against, and authenticates to the backend with, the key from the env path
        client = Client(server.server_address[1])
        assert client.request("POST", "/v1/systemone", body=decision())[0] == 200
        assert client.request("POST", "/v1/systemone", body=decision(), auth=False)[0] == 401
    finally:
        server.shutdown()
        server.server_close()
        fake.stop()


def test_accounting_failure_never_blocks_the_response(make_gateway, capfd):
    canary = "zq-canary-5519"
    client, gw, fake, buf = make_gateway()

    def broken_record(rec):
        raise RuntimeError(f"cannot record {canary}")

    gw.metrics.record = broken_record
    body = decision() | {"state": canary}
    status, _, doc = client.json("POST", "/v1/systemone", body=body)
    assert status == 200 and doc == {"model": "fake-v1", "answers": {"q0": {"type": "noul", "noul": 0.5}}}
    status, _, doc = client.json("POST", "/v1/systemone", body=body, auth=False)  # gateway rejections too
    assert status == 401 and doc["error"]["code"] == 401
    assert buf.getvalue().count("accounting failed: RuntimeError") == 2
    out, err = capfd.readouterr()
    assert canary not in buf.getvalue() and canary not in out and canary not in err
