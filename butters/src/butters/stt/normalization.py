"""Conservative, configurable transcript normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import tomllib

from butters.stt.model import STTEngineError


@dataclass(frozen=True, slots=True)
class DomainVocabulary:
    hotwords: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]


def load_domain_vocabulary(path: Path) -> DomainVocabulary:
    try:
        with Path(path).open("rb") as source:
            data = tomllib.load(source)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError) as exc:
        raise STTEngineError(f"cannot load domain vocabulary {path}: {exc}") from exc
    domain = data.get("domain", {})
    aliases = data.get("aliases", {})
    if not isinstance(domain, dict) or not isinstance(aliases, dict):
        raise STTEngineError("domain vocabulary must contain TOML tables")
    terms = domain.get("terms", [])
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise STTEngineError("domain.terms must be an array of strings")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in aliases.items()):
        raise STTEngineError("all transcript aliases must map strings to strings")
    ordered_aliases = tuple(sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True))
    return DomainVocabulary(tuple(terms), ordered_aliases)


def normalize_transcript(raw: str, vocabulary: DomainVocabulary) -> str:
    """Apply only configured whole-phrase aliases, preserving all other text."""

    normalized = raw
    for spoken, canonical in vocabulary.aliases:
        pattern = re.compile(
            rf"(?<![\w]){re.escape(spoken)}(?![\w])",
            flags=re.IGNORECASE,
        )
        normalized = pattern.sub(
            lambda _match, replacement=canonical: replacement,
            normalized,
        )
    return normalized
