"""Heuristic question classifier for educational content."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INTERROGATIVE_RE = re.compile(
    r"\b(?:what|which|who|whom|whose|when|where|why|how|"
    r"calculate|compute|determine|find|solve|evaluate|simplify|"
    r"identify|select|choose|state|define|compare|explain|"
    r"true\s+or\s+false)\b",
    re.IGNORECASE,
)
_MCQ_OPTION_RE = re.compile(
    r"(?:^|\s)(?:\(?[A-E]\)|[A-E]\.|[1-4]\))\s+\S+",
    re.MULTILINE,
)
_QUESTION_LABEL_RE = re.compile(
    r"(?:^|\b)(?:question|problem|q)\s*#?\s*\d+\b",
    re.IGNORECASE,
)
_BLANK_RE = re.compile(r"_{3,}")
_EQUATION_PROMPT_RE = re.compile(r"(?:=\s*\?|\bsolve\s+for\s+[a-zA-Z]\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QuestionClassification:
    is_question: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)


def classify_question(
    text: str,
    *,
    min_confidence: float = 0.4,
    min_length: int = 8,
) -> QuestionClassification:
    """Classify whether extracted text represents an educational question."""
    cleaned = text.strip()
    if len(cleaned) < min_length:
        return QuestionClassification(
            is_question=False, confidence=0.0, reasons=["text_too_short"]
        )

    score = 0.0
    reasons: list[str] = []

    if "?" in cleaned:
        score += 0.45
        reasons.append("question_mark")

    if _INTERROGATIVE_RE.search(cleaned):
        score += 0.35
        reasons.append("interrogative_or_directive")

    mcq_matches = _MCQ_OPTION_RE.findall(cleaned)
    if len(mcq_matches) >= 2:
        score += 0.40
        reasons.append("multiple_choice_options")

    if _QUESTION_LABEL_RE.search(cleaned):
        score += 0.25
        reasons.append("question_label")

    if _BLANK_RE.search(cleaned):
        score += 0.25
        reasons.append("fill_in_blank")

    if _EQUATION_PROMPT_RE.search(cleaned):
        score += 0.30
        reasons.append("equation_prompt")

    confidence = min(1.0, round(score, 3))
    return QuestionClassification(
        is_question=confidence >= min_confidence,
        confidence=confidence,
        reasons=reasons,
    )
