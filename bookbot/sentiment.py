"""A rough positive/negative reading of the message a book was mentioned in."""

import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_QUERY = re.compile(r"\{[^{}\n]*\}")
# VADER's own suggested cutoffs for its compound score, which runs from -1 to 1.
THRESHOLD = 0.05

_analyzer = SentimentIntensityAnalyzer()


def score_message(content: str) -> tuple[str, float]:
    """Return (label, score) for a message, ignoring the {titles} in it.

    Titles are left out because many carry feeling of their own ("The Hate U Give",
    "The Love Hypothesis") that says nothing about what the person thinks of the book.
    A message that mentions several books gives every one of them the same score.
    """
    score = _analyzer.polarity_scores(_QUERY.sub(" ", content))["compound"]
    if score >= THRESHOLD:
        return "positive", score
    if score <= -THRESHOLD:
        return "negative", score
    return "neutral", score
