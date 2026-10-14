"""The privacy rule, end to end: request content never reaches logs, metrics, stats, error bodies or disk."""

import threading
import time
from pathlib import Path

CANARY = "zq-canary-7731"


def canary_body(n=1, state=f"customer {CANARY} wants a refund"):
    return {
        "model": "model-a",
        "state": state,
        "questions": {
            f"{CANARY}-q{i}": {
                "type": "choice",
                "instructions": f"Which team? {CANARY}",
                "criteria": {f"{CANARY}-a": f"money {CANARY}", "b": CANARY},
            }
            for i in range(n)
        },
    }


def test_canary_never_leaks(make_gateway, capfd):
    client, gw, fake, buf = make_gateway(
        max_body_bytes=16384,
        backend_max_in_flight=1,
        client_max_in_flight=1,
        known_clients=["bench", "held", "other-client", "late"],
    )
    error_bodies = []

    def post(body, expect, path="/v1/systemone", **kw):
        status, _, data = client.request("POST", path, body=body, **kw)
        assert status == expect, (status, expect)
        if status != 200:
            error_bodies.append(data)

    post(canary_body(), 200, headers={"X-Decision-Client": "bench"})
    post(canary_body(), 200, path=f"/v1/systemone?{CANARY}=1")
    post(canary_body(), 200, path=f"/v1/{CANARY}")
    post(canary_body(n=33), 400)  # too many questions
    post(canary_body(n=3) | {"pad": CANARY * 1300}, 413)  # body cap
    post(canary_body(), 415, ctype="text/plain")
    post(canary_body(), 401, auth=False)
    post(canary_body(), 403, headers={"Host": f"{CANARY}.example"})
    post(canary_body(state="force-422"), 422)  # the fake echoes the input in its 422 body
    # caller-chosen label text: the body model and the client header must never become labels
    post(canary_body() | {"model": CANARY}, 200, headers={"X-Decision-Client": CANARY})
    post(canary_body(n=33) | {"model": CANARY}, 400, headers={"X-Decision-Client": CANARY})
    post(canary_body() | {"model": CANARY}, 401, auth=False, headers={"X-Decision-Client": CANARY})

    fake.gate.clear()  # hold one request in the backend, then trip both admission caps
    holder = threading.Thread(
        target=client.request,
        args=("POST", "/v1/systemone"),
        kwargs={"body": canary_body(), "headers": {"X-Decision-Client": "held"}},
    )
    holder.start()
    end = time.monotonic() + 5
    while fake.arrived < 6 and time.monotonic() < end:
        time.sleep(0.01)
    post(canary_body(), 429, headers={"X-Decision-Client": "held"})
    post(canary_body(), 503, headers={"X-Decision-Client": "other-client"})
    post(canary_body(), 503, headers={"X-Decision-Client": "late", "X-Decision-Deadline-Ms": "50"})
    fake.gate.set()
    holder.join(5)

    metrics = client.request("GET", "/metrics")[2]
    stats = client.request("GET", "/v1/stats")[2]
    gw.flush()
    disk = b"".join(p.read_bytes() for p in Path(gw.store.dir).iterdir() if p.is_file())
    out, err = capfd.readouterr()

    assert len(error_bodies) == 11
    assert b"backend rejected the request" in error_bodies[5]
    assert gw.store.count() > 0 and b"bench" in disk  # the store holds rows, just not content
    assert CANARY.encode() not in metrics
    assert CANARY.encode() not in stats
    assert CANARY.encode() not in disk
    assert all(CANARY.encode() not in b for b in error_bodies)
    assert CANARY not in buf.getvalue() and buf.getvalue().count("status=") == 16
    assert CANARY not in out and CANARY not in err
