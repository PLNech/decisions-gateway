"""Request shape inspection and size caps, applied before anything reaches the backend.

Only counts leave this module. Error messages are fixed strings plus numbers: they never echo input.
Malformed bodies are not judged here: they pass with what could be counted and the backend rejects them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

QUESTION_TYPES = ("noul", "choice", "score")
NOUL_OPTIONS = 2  # a yes/no question scores two outcomes


class CapError(Exception):
    """Rejected request. str() is safe to return to the client and to log; .shape holds the counts."""

    def __init__(self, message: str, shape: Shape):
        super().__init__(message)
        self.shape = shape


@dataclass
class Shape:
    n_questions: int = 0
    n_options_total: int = 0
    types: dict[str, int] = field(default_factory=dict)
    model: str | None = None


def count_options(question: dict) -> int:
    qtype = question.get("type")
    if qtype == "noul":
        return NOUL_OPTIONS
    criteria = question.get("criteria")
    if isinstance(criteria, (dict, list)):
        return len(criteria)
    return 0


def inspect(body: bytes, max_questions: int, max_options: int, max_budget: int) -> Shape:
    """Parse the JSON body, measure it, and enforce the caps."""
    shape = Shape()
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return shape
    if not isinstance(doc, dict):
        return shape
    model = doc.get("model")
    shape.model = model if isinstance(model, str) else None
    questions = doc.get("questions")
    if not isinstance(questions, dict):
        return shape
    shape.n_questions = len(questions)
    if shape.n_questions > max_questions:
        raise CapError(f"too many questions: {shape.n_questions} > {max_questions}", shape)
    for question in questions.values():
        question = question if isinstance(question, dict) else {}
        qtype = question.get("type")
        bucket = qtype if qtype in QUESTION_TYPES else "other"
        shape.types[bucket] = shape.types.get(bucket, 0) + 1
        n = count_options(question)
        if qtype != "noul" and n > max_options:
            raise CapError(f"too many options in one question: {n} > {max_options}", shape)
        shape.n_options_total += n
    if shape.n_options_total > max_budget:
        raise CapError(f"question x option budget exceeded: {shape.n_options_total} > {max_budget}", shape)
    return shape
