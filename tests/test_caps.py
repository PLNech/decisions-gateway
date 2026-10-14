import json

import pytest

from decisions_gateway.caps import CapError, inspect

CANARY = "zq-canary-7731"


def body(questions, model="model-a"):
    return json.dumps({"model": model, "state": CANARY, "questions": questions}).encode()


def caps(b):
    return inspect(b, max_questions=32, max_options=26, max_budget=256)


def test_shape_counts():
    s = caps(
        body(
            {
                "a": {"type": "noul", "instructions": "x"},
                "b": {"type": "choice", "instructions": "y", "criteria": {"p": "1", "q": "2", "r": "3"}},
                "c": {"type": "score", "instructions": "z", "criteria": ["lo", "mid", "hi", "max"]},
                "d": {"type": "weird"},
            }
        )
    )
    assert s.n_questions == 4
    assert s.n_options_total == 2 + 3 + 4 + 0
    assert s.types == {"noul": 1, "choice": 1, "score": 1, "other": 1}
    assert s.model == "model-a"


@pytest.mark.parametrize(
    "questions, needle",
    [
        ({f"q{i}": {"type": "noul"} for i in range(33)}, "too many questions"),
        ({"c": {"type": "choice", "criteria": {f"{CANARY}{i}": "" for i in range(27)}}}, "too many options"),
        ({f"q{i}": {"type": "choice", "criteria": [CANARY] * 26} for i in range(10)}, "budget"),
    ],
)
def test_caps_reject_without_echo(questions, needle):
    with pytest.raises(CapError) as exc:
        caps(body(questions))
    assert needle in str(exc.value)
    assert CANARY not in str(exc.value)


def test_malformed_bodies_pass_through_unmeasured():
    # the backend owns validation; the gateway only measures what it can count
    for raw in (b"{" + CANARY.encode(), b"\xff", b'"x"', body([CANARY])):
        s = caps(raw)
        assert s.n_questions == 0 and s.n_options_total == 0
    s = caps(body({"q": CANARY}))
    assert s.n_questions == 1 and s.types == {"other": 1}


def test_hostile_size_is_rejected():
    # the security-review case: 1024 questions x 26 options
    hostile = {f"q{i}": {"type": "choice", "criteria": [str(j) for j in range(26)]} for i in range(1024)}
    with pytest.raises(CapError):
        caps(body(hostile))
