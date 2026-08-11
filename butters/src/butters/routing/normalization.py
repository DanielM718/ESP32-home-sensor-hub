"""Conservative matching normalization distinct from transcript correction."""

from __future__ import annotations

import re
import unicodedata

NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def normalize_request(text: str) -> str:
    """Normalize only concepts needed for routing; do not rewrite the transcript."""

    value = unicodedata.normalize("NFKC", text).casefold()
    value = value.replace("co₂", "co2").replace("pm₂.₅", "pm2.5")
    value = re.sub(r"\bwhat['’]?s\b", "what is", value)
    value = re.sub(r"\bhow['’]?s\b", "how is", value)
    value = re.sub(r"\bwhich['’]?s\b", "which is", value)
    value = re.sub(r"[^a-z0-9.%]+", " ", value)
    words = [NUMBER_WORDS.get(word, word) for word in value.split()]
    value = " ".join(words)
    value = re.sub(r"\bp\s*m\s*2\s*(?:point|dot)\s*5\b", "pm2.5", value)
    value = re.sub(r"\bp\s*m\s*2[.]5\b", "pm2.5", value)
    value = re.sub(r"\bc\s*o\s*2\b", "co2", value)
    return value


def contains_phrase(text: str, phrase: str) -> bool:
    normalized = normalize_request(phrase)
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text))
