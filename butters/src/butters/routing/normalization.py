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

# These hesitation sounds are ignored only by deterministic concept matching.
# Raw and STT-normalized transcripts retain them for diagnostics.
BENIGN_FILLERS = frozenset({"uh", "um", "ah"})


def normalize_request(text: str) -> str:
    """Normalize only concepts needed for routing; do not rewrite the transcript."""

    value = unicodedata.normalize("NFKC", text).casefold()
    value = value.replace("co₂", "co2").replace("pm₂.₅", "pm2.5")
    value = re.sub(r"\bwhat['’]?s\b", "what is", value)
    value = re.sub(r"\bhow['’]?s\b", "how is", value)
    value = re.sub(r"\bwhich['’]?s\b", "which is", value)
    value = re.sub(r"[^a-z0-9.%]+", " ", value)
    # A period is meaningful only inside a numeric metric token such as PM2.5.
    # Sentence punctuation must not attach to aliases or number words.
    value = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", value)
    value = value.replace("%", " ")
    words = [
        NUMBER_WORDS.get(word, word)
        for word in value.split()
        if word not in BENIGN_FILLERS
    ]
    value = " ".join(words)
    value = re.sub(r"\bp\s*m\s*2\s*(?:point|dot)\s*5\b", "pm2.5", value)
    value = re.sub(r"\bp\s*m\s*2[.]5\b", "pm2.5", value)
    value = re.sub(r"\bp\s*m\s*25\b", "pm2.5", value)
    value = re.sub(r"\bc\s*o\s*2\b", "co2", value)
    return value


def phrase_position(text: str, phrase: str) -> int | None:
    """Index of the first whole-word phrase match, or None when it is absent."""

    normalized = normalize_request(phrase)
    match = re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text)
    return match.start() if match is not None else None


def contains_phrase(text: str, phrase: str) -> bool:
    return phrase_position(text, phrase) is not None
