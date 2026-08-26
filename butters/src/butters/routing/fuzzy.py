"""Small deterministic edit-distance helpers for registered routing vocabulary.

This module deliberately knows nothing about intents or arbitrary language.  A
caller supplies the allow-listed aliases, and only same-token-count windows are
compared.  Short aliases and numeric identifiers are exact-only because a
single edit to values such as ``VOC``, ``CO2``, or ``box 2`` is too risky.
"""

from __future__ import annotations

from dataclasses import dataclass

from butters.routing.normalization import normalize_request

FUZZY_MARGIN = 0.10


@dataclass(frozen=True, slots=True)
class FuzzyMatch:
    key: str
    alias: str
    phrase: str
    start: int
    end: int
    distance: int
    score: float
    order: int


def token_spans(text: str, phrase: str) -> tuple[tuple[int, int], ...]:
    """Return every exact token span for one already registered phrase."""

    words = text.split()
    wanted = normalize_request(phrase).split()
    if not wanted or len(wanted) > len(words):
        return ()
    width = len(wanted)
    return tuple(
        (start, start + width)
        for start in range(len(words) - width + 1)
        if words[start : start + width] == wanted
    )


def fuzzy_matches(
    text: str,
    vocabulary: tuple[tuple[str, tuple[str, ...], int], ...],
    *,
    excluded_keys: frozenset[str] = frozenset(),
    occupied_spans: tuple[tuple[int, int], ...] = (),
) -> tuple[FuzzyMatch, ...]:
    """Score allow-listed aliases against bounded token windows.

    ``vocabulary`` contains ``(key, aliases, stable_order)`` entries.  Exact
    matching stays with the registries so it can always take precedence.
    """

    words = text.split()
    matches: list[FuzzyMatch] = []
    for key, aliases, order in vocabulary:
        if key in excluded_keys:
            continue
        for alias in aliases:
            normalized_alias = normalize_request(alias)
            alias_words = normalized_alias.split()
            width = len(alias_words)
            if not alias_words or width > len(words):
                continue
            for start in range(len(words) - width + 1):
                end = start + width
                if any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied_spans):
                    continue
                phrase = " ".join(words[start:end])
                scored = _score(normalized_alias, phrase)
                if scored is None:
                    continue
                distance, score = scored
                matches.append(
                    FuzzyMatch(
                        key,
                        normalized_alias,
                        phrase,
                        start,
                        end,
                        distance,
                        score,
                        order,
                    )
                )
    return tuple(matches)


def best_by_key(matches: tuple[FuzzyMatch, ...]) -> tuple[FuzzyMatch, ...]:
    """Keep one deterministic best match for each canonical vocabulary key."""

    selected: dict[str, FuzzyMatch] = {}
    for match in matches:
        previous = selected.get(match.key)
        if previous is None or _rank(match) < _rank(previous):
            selected[match.key] = match
    return tuple(sorted(selected.values(), key=_rank))


def _rank(match: FuzzyMatch) -> tuple[float, int, int, int, str]:
    return (-match.score, match.distance, match.start, match.order, match.alias)


def _score(alias: str, phrase: str) -> tuple[int, float] | None:
    if alias == phrase:
        return None
    alias_words = alias.split()
    phrase_words = phrase.split()
    # Never mutate numeric identity and never fuzzy-match compact vocabulary.
    alias_numbers = tuple(word for word in alias_words if any(ch.isdigit() for ch in word))
    phrase_numbers = tuple(word for word in phrase_words if any(ch.isdigit() for ch in word))
    if alias_numbers or phrase_numbers:
        return None
    # Compact vocabulary tokens carry too much identity for one loose edit:
    # ``VMC index`` must not become ``VOC index``.  Requiring 1--3 character
    # tokens to remain exact still permits the requested longer-word noise such
    # as ``printer rom`` and ``tempature``.
    if len(alias_words) > 1 and any(
        len(alias_word) <= 3 and alias_word != phrase_word
        for alias_word, phrase_word in zip(alias_words, phrase_words, strict=True)
    ):
        return None
    length = max(len(alias), len(phrase))
    if length <= 4:
        return None
    if length <= 7:
        maximum_distance, minimum_score = 1, 0.86
    elif length <= 11:
        maximum_distance, minimum_score = 2, 0.80
    else:
        maximum_distance, minimum_score = 2, 0.84
    if abs(len(alias) - len(phrase)) > maximum_distance:
        return None
    distance = levenshtein_distance(alias, phrase, maximum=maximum_distance)
    if distance > maximum_distance:
        return None
    score = 1.0 - distance / length
    return (distance, score) if score >= minimum_score else None


def levenshtein_distance(left: str, right: str, *, maximum: int | None = None) -> int:
    """Return deterministic character edit distance with an optional cutoff."""

    if left == right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    if maximum is not None and len(right) - len(left) > maximum:
        return maximum + 1
    previous = list(range(len(left) + 1))
    for row, right_character in enumerate(right, start=1):
        current = [row]
        row_minimum = row
        for column, left_character in enumerate(left, start=1):
            value = min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_character != right_character),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if maximum is not None and row_minimum > maximum:
            return maximum + 1
        previous = current
    return previous[-1]
