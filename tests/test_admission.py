import threading
import time

from decisions_gateway.admission import Admission


def test_backend_cap_sheds_at_once_without_deadline():
    a = Admission(backend_cap=2, client_cap=10)
    assert a.acquire("b", "x", None) is None
    assert a.acquire("b", "y", None) is None
    assert a.acquire("b", "z", None) == "overloaded"
    assert a.in_flight() == {"b": 2}
    a.release("b", "x")
    assert a.acquire("b", "z", None) is None


def test_client_cap_counts_waiters():
    a = Admission(backend_cap=10, client_cap=2)
    assert a.acquire("b", "x", None) is None
    assert a.acquire("b", "x", None) is None
    assert a.acquire("b", "x", None) == "concurrency_cap"
    assert a.acquire("b", "other-client", None) is None


def test_deadline_waits_then_sheds():
    a = Admission(backend_cap=1, client_cap=10)
    assert a.acquire("b", "x", None) is None
    t0 = time.monotonic()
    assert a.acquire("b", "y", 0.05) == "deadline"
    assert time.monotonic() - t0 >= 0.05
    assert a.clients["y"] == 0  # a shed waiter gives its client slot back


def test_deadline_wait_gets_slot_when_released():
    a = Admission(backend_cap=1, client_cap=10)
    assert a.acquire("b", "x", None) is None
    threading.Timer(0.05, a.release, args=("b", "x")).start()
    assert a.acquire("b", "y", 2.0) is None
    assert a.in_flight() == {"b": 1}
