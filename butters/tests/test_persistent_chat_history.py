"""Durable, per-identity Butters Chat history.

Chat used to be a purely in-memory conversation: reloading recovered whatever
the session still held, and a `butters-web` restart threw the lot away. These
tests cover the durable half — what is stored, who can read it, how long it
survives, and what deleting it is and is not allowed to reach.

WHAT THESE TESTS CAN AND CANNOT SHOW
------------------------------------
This host has no JavaScript runtime (see the header of
`test_chat_markdown_rendering.py`), so no browser code here is executed. The
client-side assertions read the shipped source and check the construct that
is there, exactly as the Markdown tests do. Everything server-side — storage,
ordering, ownership, retention, cascade, and the API surface — is executed.

NO PAID CALLS
-------------
Nothing here reaches a provider. The harness installs a reasoner that reports
itself unavailable, the questions used are answered deterministically, and
titles are derived locally by definition.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest
from beta1_harness import build_app, client

from butters.web.chat_history import (
    DEFAULT_RETENTION_DAYS,
    MAX_CONVERSATIONS_RETURNED,
    MAX_MESSAGE_CHARS,
    MAX_TITLE_CHARS,
    METADATA_FIELDS,
    ChatHistoryStore,
    assistant_metadata,
    derive_title,
)
from butters.web.service import ServiceResponse

ORIGIN = "http://testserver"
ALICE = {"tailscale-user-login": "alice@example.com"}
BOB = {"tailscale-user-login": "bob@example.com"}
ALICE_KEY = "identity:alice@example.com"
BOB_KEY = "identity:bob@example.com"

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/butters/web"
HISTORY_PY = (SOURCE_ROOT / "chat_history.py").read_text(encoding="utf-8")
SERVICE_PY = (SOURCE_ROOT / "service.py").read_text(encoding="utf-8")
APP_PY = (SOURCE_ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (SOURCE_ROOT / "static/assets/app.js").read_text(encoding="utf-8")
INDEX_HTML = (SOURCE_ROOT / "static/index.html").read_text(encoding="utf-8")
CHAT_CSS = (SOURCE_ROOT / "static/assets/chat.css").read_text(encoding="utf-8")

# A deterministic answer the harness can produce without any provider.
LOCAL_QUESTION = "what is the humidity in box three"

MARKDOWN_ANSWER = (
    "## Box three\n"
    "\n"
    "Humidity is **42%**. Notes:\n"
    "\n"
    "- first point\n"
    "- second point\n"
    "\n"
    "```python\n"
    "def read():\n"
    "    return 42  # indented, inside a fence\n"
    "```\n"
    "\n"
    "| sensor | value |\n"
    "| ------ | ----- |\n"
    "| box 3  | 42%   |\n"
)


class Clock:
    """A movable clock, so a 30-day boundary is a test rather than a wait."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance_days(self, days: float) -> None:
        self.now += days * 86400.0


def store(tmp_path: Path, clock: Clock | None = None, **kwargs: object) -> ChatHistoryStore:
    return ChatHistoryStore(
        tmp_path / "chat-history.sqlite3", clock=clock or Clock(), **kwargs
    )


def seed(
    history: ChatHistoryStore,
    owner: str,
    first: str = "hello there",
    answer: str = "Hi.",
    metadata: dict[str, object] | None = None,
) -> str:
    conversation_id = history.start_conversation(owner, first)
    history.append(conversation_id, owner, "user", first)
    history.append(conversation_id, owner, "assistant", answer, metadata=metadata)
    return conversation_id


async def session_for(http, headers: dict[str, str]) -> str:
    response = await http.get("/api/session", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def mutate(headers: dict[str, str], csrf: str) -> dict[str, str]:
    return {**headers, "origin": ORIGIN, "x-butters-csrf": csrf}


# ========================= 1. storage =====================================


def test_a_new_conversation_is_persisted_with_its_messages(tmp_path: Path) -> None:
    history = store(tmp_path)
    conversation_id = seed(history, ALICE_KEY)

    found = history.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    assert found["message_count"] == 2
    assert [item["role"] for item in found["messages"]] == ["user", "assistant"]
    assert history.conversations(ALICE_KEY)[0]["conversation_id"] == conversation_id


def test_messages_keep_the_order_they_were_written_in(tmp_path: Path) -> None:
    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "one")
    for index in range(12):
        history.append(
            conversation_id,
            ALICE_KEY,
            "user" if index % 2 == 0 else "assistant",
            f"message {index}",
        )

    found = history.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    assert [item["text"] for item in found["messages"]] == [
        f"message {index}" for index in range(12)
    ]


def test_ordering_survives_a_reopened_database(tmp_path: Path) -> None:
    """Order comes from a stored sequence, not from insertion luck."""

    first = store(tmp_path)
    conversation_id = first.start_conversation(ALICE_KEY, "one")
    for index in range(5):
        first.append(conversation_id, ALICE_KEY, "user", f"message {index}")

    reopened = ChatHistoryStore(tmp_path / "chat-history.sqlite3", clock=Clock())
    found = reopened.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    assert [item["text"] for item in found["messages"]] == [
        f"message {index}" for index in range(5)
    ]


def test_markdown_survives_the_round_trip_character_for_character(tmp_path: Path) -> None:
    """Fences, tables, list markers and indentation all mean something."""

    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "explain box three")
    history.append(conversation_id, ALICE_KEY, "assistant", MARKDOWN_ANSWER)

    found = history.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    stored = found["messages"][0]["text"]
    # `normalize_message_text` strips a trailing newline and nothing else.
    assert stored == MARKDOWN_ANSWER.strip()
    assert "\n\n" in stored
    assert "    return 42  # indented, inside a fence" in stored
    assert "| box 3  | 42%   |" in stored
    assert stored.count("\n") == MARKDOWN_ANSWER.strip().count("\n")


def test_a_service_restart_can_reopen_stored_history(tmp_path: Path) -> None:
    """A second store over the same file is what a restart actually is."""

    clock = Clock()
    first = store(tmp_path, clock)
    conversation_id = seed(first, ALICE_KEY, "before the restart", MARKDOWN_ANSWER)
    del first

    after = ChatHistoryStore(tmp_path / "chat-history.sqlite3", clock=clock)
    listed = after.conversations(ALICE_KEY)
    assert [item["conversation_id"] for item in listed] == [conversation_id]
    found = after.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    assert found["messages"][1]["text"] == MARKDOWN_ANSWER.strip()


def test_deleting_a_conversation_cascades_to_its_messages(tmp_path: Path) -> None:
    history = store(tmp_path)
    kept = seed(history, ALICE_KEY, "kept")
    removed = seed(history, ALICE_KEY, "removed")

    assert history.delete(removed, ALICE_KEY) is True
    assert history.conversation(removed, ALICE_KEY) is None
    assert history.conversation(kept, ALICE_KEY) is not None

    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        orphans = connection.execute(
            """SELECT COUNT(*) FROM chat_messages WHERE conversation_id NOT IN
            (SELECT conversation_id FROM chat_conversations)"""
        ).fetchone()[0]
        remaining = connection.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (removed,)
        ).fetchone()[0]
    assert orphans == 0
    assert remaining == 0


def test_delete_all_touches_only_the_calling_owner(tmp_path: Path) -> None:
    history = store(tmp_path)
    mine = seed(history, ALICE_KEY, "mine")
    theirs = seed(history, BOB_KEY, "theirs")

    assert history.delete_all(ALICE_KEY) == 1
    assert history.conversations(ALICE_KEY) == ()
    assert history.conversation(mine, ALICE_KEY) is None
    assert history.conversation(theirs, BOB_KEY) is not None

    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        orphans = connection.execute(
            """SELECT COUNT(*) FROM chat_messages WHERE conversation_id NOT IN
            (SELECT conversation_id FROM chat_conversations)"""
        ).fetchone()[0]
    assert orphans == 0


# ========================= 2. retention ===================================


def test_the_default_retention_window_is_thirty_days() -> None:
    assert DEFAULT_RETENTION_DAYS == 30


def test_a_conversation_older_than_the_window_is_purged(tmp_path: Path) -> None:
    clock = Clock()
    history = store(tmp_path, clock)
    conversation_id = seed(history, ALICE_KEY, "long ago")

    clock.advance_days(30.5)
    assert history.purge_expired() == 1
    assert history.conversations(ALICE_KEY) == ()
    assert history.conversation(conversation_id, ALICE_KEY) is None

    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def test_the_boundary_keeps_a_conversation_until_the_window_has_passed(
    tmp_path: Path,
) -> None:
    """Exactly 30 days old is still inside the window; a second later is not."""

    clock = Clock()
    history = store(tmp_path, clock)
    conversation_id = seed(history, ALICE_KEY, "on the boundary")

    clock.advance_days(30)
    assert history.purge_expired() == 0
    assert history.conversation(conversation_id, ALICE_KEY) is not None

    clock.now += 1.0
    assert history.purge_expired() == 1
    assert history.conversation(conversation_id, ALICE_KEY) is None


def test_viewing_a_conversation_does_not_reset_its_age(tmp_path: Path) -> None:
    """`updated_at` is last message activity, never last open."""

    clock = Clock()
    history = store(tmp_path, clock)
    conversation_id = seed(history, ALICE_KEY, "read but not written")
    written_at = history.conversations(ALICE_KEY)[0]["updated_at"]

    clock.advance_days(29)
    assert history.conversation(conversation_id, ALICE_KEY) is not None
    assert history.conversations(ALICE_KEY)[0]["updated_at"] == written_at

    clock.advance_days(1.5)
    assert history.conversation(conversation_id, ALICE_KEY) is None


def test_a_new_message_does_extend_the_window(tmp_path: Path) -> None:
    clock = Clock()
    history = store(tmp_path, clock)
    conversation_id = seed(history, ALICE_KEY, "still in use")

    clock.advance_days(29)
    history.append(conversation_id, ALICE_KEY, "user", "still here")
    clock.advance_days(10)
    assert history.conversation(conversation_id, ALICE_KEY) is not None


def test_expiry_is_purged_opportunistically_on_ordinary_access(tmp_path: Path) -> None:
    """No daemon: the list and open paths do the sweeping."""

    clock = Clock()
    history = store(tmp_path, clock)
    seed(history, ALICE_KEY, "expires")
    clock.advance_days(31)

    assert history.conversations(ALICE_KEY) == ()
    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_conversations"
        ).fetchone()[0] == 0


def test_startup_purges_what_expired_while_the_service_was_down(tmp_path: Path) -> None:
    clock = Clock()
    first = store(tmp_path, clock)
    seed(first, ALICE_KEY, "expired while off")
    clock.advance_days(45)

    ChatHistoryStore(tmp_path / "chat-history.sqlite3", clock=clock)
    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_conversations"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def test_retention_is_configurable_and_defaults_to_thirty(tmp_path: Path) -> None:
    from butters.assistant_config import load_assistant_settings

    assert load_assistant_settings().web.chat_history_retention_days == 30
    assert store(tmp_path, retention_days=7).retention_days == 7


def test_retention_uses_absolute_seconds_not_local_calendar_days() -> None:
    """Epoch arithmetic only: no `datetime.now()`, no local timezone."""

    assert "86400" in HISTORY_PY
    assert "localtime" not in HISTORY_PY
    assert "datetime" not in HISTORY_PY


# ========================= 3. titles ======================================


def test_a_title_is_derived_from_the_first_user_message() -> None:
    assert derive_title("What is the humidity in box three?") == (
        "What is the humidity in box three?"
    )
    assert derive_title("  spaced   out  words  ") == "spaced out words"
    assert derive_title("\n\nsecond line is the first real one\nthird") == (
        "second line is the first real one"
    )


def test_an_empty_first_message_falls_back_to_new_chat() -> None:
    assert derive_title("") == "New chat"
    assert derive_title("   \n\n\t  ") == "New chat"


def test_a_title_is_bounded(tmp_path: Path) -> None:
    long_title = derive_title("word " * 200)
    assert len(long_title) <= MAX_TITLE_CHARS + 1
    assert long_title.endswith("…")

    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "word " * 200)
    assert len(str(history.conversations(ALICE_KEY)[0]["title"])) <= MAX_TITLE_CHARS + 1
    assert conversation_id


def test_titling_is_deterministic_and_makes_no_model_call(tmp_path: Path) -> None:
    """The same text always produces the same title, locally.

    A title that depended on a model could not be reproduced here, and the
    module that produces it imports nothing that could reach a provider.
    """

    assert derive_title("tell me about box three") == derive_title(
        "tell me about box three"
    )
    imported = set(re.findall(r"^(?:from|import)\s+([\w.]+)", HISTORY_PY, re.MULTILINE))
    assert not any(
        name.startswith(("butters.cloud", "butters.ai", "openai", "http", "urllib", "socket"))
        for name in imported
    ), imported
    # Only the person's own words reach the title.
    assert "derive_title(first_user_text)" in HISTORY_PY


def test_a_title_is_never_rewritten_by_what_the_model_answered(tmp_path: Path) -> None:
    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "my own words")
    history.append(conversation_id, ALICE_KEY, "user", "my own words")
    history.append(
        conversation_id, ALICE_KEY, "assistant", "# A Much Better Title\n\nBody."
    )

    assert history.conversations(ALICE_KEY)[0]["title"] == "my own words"


# ========================= 4. message metadata ============================


def response_with_metadata(text: str = "The answer.") -> ServiceResponse:
    return ServiceResponse(
        "trace-1",
        "request-1",
        text,
        text.casefold(),
        "cloud_general",
        ("cloud_general",),
        model="gpt-5.6-terra",
        reasoning_effort="high",
        cloud_used=True,
        routing_mode="adaptive",
        routing_tier="terra",
        routing_reason_codes=("complexity_high", "sensor_absent"),
        estimated_complexity=7,
        tier_escalated=True,
        reasoning_summary="Checked the sensor table, then compared two rows.",
    )


def test_response_text_round_trips_unchanged(tmp_path: Path) -> None:
    history = store(tmp_path)
    response = response_with_metadata(MARKDOWN_ANSWER)
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        response.response_text,
        metadata=assistant_metadata(response),
    )

    found = history.conversation(conversation_id, ALICE_KEY)
    assert found is not None
    assert found["messages"][0]["text"] == MARKDOWN_ANSWER.strip()


def test_routing_metadata_round_trips(tmp_path: Path) -> None:
    history = store(tmp_path)
    response = response_with_metadata()
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        response.response_text,
        metadata=assistant_metadata(response),
    )

    metadata = history.conversation(conversation_id, ALICE_KEY)["messages"][0]["metadata"]
    assert metadata == {
        "cloud_used": True,
        "routing_mode": "adaptive",
        "routing_tier": "terra",
        "routing_reason_codes": ["complexity_high", "sensor_absent"],
        "estimated_complexity": 7,
        "tier_escalated": True,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "reasoning_summary": "Checked the sensor table, then compared two rows.",
    }


def test_the_reasoning_summary_round_trips(tmp_path: Path) -> None:
    history = store(tmp_path)
    response = response_with_metadata()
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        response.response_text,
        metadata=assistant_metadata(response),
    )

    stored = history.conversation(conversation_id, ALICE_KEY)["messages"][0]
    assert stored["metadata"]["reasoning_summary"] == response.reasoning_summary
    # And it is not part of the answer.
    assert response.reasoning_summary not in stored["text"]


def test_metadata_is_never_concatenated_into_the_answer(tmp_path: Path) -> None:
    history = store(tmp_path)
    response = response_with_metadata()
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        response.response_text,
        metadata=assistant_metadata(response),
    )

    stored = history.conversation(conversation_id, ALICE_KEY)["messages"][0]
    assert stored["text"] == "The answer."
    for value in ("gpt-5.6-terra", "terra", "adaptive", "complexity_high"):
        assert value not in stored["text"]


def test_a_local_answer_stores_no_metadata_block(tmp_path: Path) -> None:
    history = store(tmp_path)
    local = ServiceResponse("t", "r", "42% humidity.", "42% humidity.", "sensor", ())
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        local.response_text,
        metadata=assistant_metadata(local),
    )

    assert assistant_metadata(local) is None
    assert history.conversation(conversation_id, ALICE_KEY)["messages"][0]["metadata"] is None


def test_no_hidden_chain_of_thought_field_exists() -> None:
    """Only the provider's own summary is persisted, and only by name."""

    assert set(METADATA_FIELDS) == {
        "cloud_used",
        "routing_mode",
        "routing_tier",
        "routing_reason_codes",
        "estimated_complexity",
        "tier_escalated",
        "model",
        "reasoning_effort",
        "reasoning_summary",
    }
    for forbidden in ("chain_of_thought", "reasoning_trace", "reasoning_tokens", "thinking"):
        assert forbidden not in HISTORY_PY


def test_metadata_is_an_allow_list_not_a_passthrough(tmp_path: Path) -> None:
    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        "answer",
        metadata={"model": "gpt-5.6-terra", "raw_provider_response": {"x": 1}},
    )

    stored = history.conversation(conversation_id, ALICE_KEY)["messages"][0]["metadata"]
    assert stored == {"model": "gpt-5.6-terra"}


def test_metadata_that_looks_like_a_credential_is_refused(tmp_path: Path) -> None:
    from butters.web.chat_history import ChatHistoryError

    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    with pytest.raises(ChatHistoryError):
        history.append(
            conversation_id,
            ALICE_KEY,
            "assistant",
            "answer",
            metadata={"api_key": "sk-not-a-real-key"},
        )


def test_an_oversized_metadata_block_is_dropped_not_truncated(tmp_path: Path) -> None:
    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(
        conversation_id,
        ALICE_KEY,
        "assistant",
        "answer",
        metadata={"reasoning_summary": "x" * 20000},
    )

    stored = history.conversation(conversation_id, ALICE_KEY)["messages"][0]
    assert stored["metadata"] is None
    assert stored["text"] == "answer"


# ========================= 5. bounds ======================================


def test_a_message_is_bounded_at_the_validated_composer_limit(tmp_path: Path) -> None:
    assert MAX_MESSAGE_CHARS == 4000
    assert 'maxlength="4000"' in INDEX_HTML

    history = store(tmp_path)
    conversation_id = history.start_conversation(ALICE_KEY, "question")
    history.append(conversation_id, ALICE_KEY, "user", "y" * 9000)
    stored = history.conversation(conversation_id, ALICE_KEY)["messages"][0]["text"]
    assert len(stored) == MAX_MESSAGE_CHARS


def test_the_history_list_is_bounded(tmp_path: Path) -> None:
    clock = Clock()
    history = store(tmp_path, clock)
    for index in range(MAX_CONVERSATIONS_RETURNED + 12):
        clock.now += 1.0
        seed(history, ALICE_KEY, f"conversation {index}")

    assert len(history.conversations(ALICE_KEY)) == MAX_CONVERSATIONS_RETURNED
    assert len(history.conversations(ALICE_KEY, limit=10_000)) == MAX_CONVERSATIONS_RETURNED


def test_one_identity_cannot_grow_its_row_count_without_limit(tmp_path: Path) -> None:
    clock = Clock()
    history = store(tmp_path, clock, max_conversations_per_owner=5)
    for index in range(20):
        clock.now += 1.0
        seed(history, ALICE_KEY, f"conversation {index}")

    with sqlite3.connect(tmp_path / "chat-history.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_conversations WHERE owner_key=?", (ALICE_KEY,)
        ).fetchone()[0] == 5
        orphans = connection.execute(
            """SELECT COUNT(*) FROM chat_messages WHERE conversation_id NOT IN
            (SELECT conversation_id FROM chat_conversations)"""
        ).fetchone()[0]
    assert orphans == 0


def test_one_transcript_cannot_grow_without_limit(tmp_path: Path) -> None:
    history = store(tmp_path, max_messages_per_conversation=6)
    conversation_id = history.start_conversation(ALICE_KEY, "start")
    for index in range(40):
        history.append(conversation_id, ALICE_KEY, "user", f"message {index}")

    found = history.conversation(conversation_id, ALICE_KEY)
    assert found["message_count"] == 6
    # The newest survive, still in order.
    assert [item["text"] for item in found["messages"]] == [
        f"message {index}" for index in range(34, 40)
    ]


# ========================= 6. authorization ===============================


def test_an_identity_can_list_open_and_delete_its_own_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            conversation_id = seed(service.chat_history, ALICE_KEY, "alice asked this")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)

                listed = await http.get("/api/chat/conversations", headers=ALICE)
                assert listed.status_code == 200
                body = listed.json()
                assert [item["conversation_id"] for item in body["conversations"]] == [
                    conversation_id
                ]
                assert body["retention_days"] == 30

                read = await http.get(
                    f"/api/chat/conversations/{conversation_id}", headers=ALICE
                )
                assert read.status_code == 200
                assert read.json()["title"] == "alice asked this"

                opened = await http.post(
                    f"/api/chat/conversations/{conversation_id}/open",
                    headers=mutate(ALICE, csrf),
                    json={},
                )
                assert opened.status_code == 200
                assert opened.json()["conversation_id"] == conversation_id

                removed = await http.delete(
                    f"/api/chat/conversations/{conversation_id}",
                    headers=mutate(ALICE, csrf),
                )
                assert removed.status_code == 200
                assert service.chat_history.conversations(ALICE_KEY) == ()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_one_identity_cannot_reach_another_identitys_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            bobs = seed(service.chat_history, BOB_KEY, "bob asked this")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)

                listed = await http.get("/api/chat/conversations", headers=ALICE)
                assert listed.json()["conversations"] == []

                read = await http.get(
                    f"/api/chat/conversations/{bobs}", headers=ALICE
                )
                assert read.status_code == 404
                assert read.json()["error"] == "conversation_not_found"
                assert "bob" not in read.text

                opened = await http.post(
                    f"/api/chat/conversations/{bobs}/open",
                    headers=mutate(ALICE, csrf),
                    json={},
                )
                assert opened.status_code == 404

                removed = await http.delete(
                    f"/api/chat/conversations/{bobs}", headers=mutate(ALICE, csrf)
                )
                assert removed.status_code == 404

                # Bob's transcript is exactly as it was.
                assert service.chat_history.conversation(bobs, BOB_KEY) is not None
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_guessed_identifier_cannot_bypass_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                for guess in (
                    "aaaaaaaaaaaaaaaaaaaaaa",
                    "0123456789abcdef0123456789abcdef",
                    "../../etc/passwd",
                    "%2e%2e%2fadmin",
                    "short",
                    "x" * 400,
                ):
                    read = await http.get(
                        f"/api/chat/conversations/{guess}", headers=ALICE
                    )
                    assert read.status_code in {404, 405}, guess
                    removed = await http.delete(
                        f"/api/chat/conversations/{guess}", headers=mutate(ALICE, csrf)
                    )
                    assert removed.status_code in {404, 405}, guess
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_the_client_cannot_claim_a_different_owner(tmp_path: Path) -> None:
    """Owner comes from the session, never from the request."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            bobs = seed(service.chat_history, BOB_KEY, "bob asked this")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)

                for attempt in (
                    {"owner": BOB_KEY},
                    {"owner_key": BOB_KEY},
                    {"identity": "bob@example.com"},
                ):
                    claimed = await http.post(
                        f"/api/chat/conversations/{bobs}/open",
                        headers=mutate(ALICE, csrf),
                        json=attempt,
                    )
                    assert claimed.status_code == 404, attempt

                query = await http.get(
                    f"/api/chat/conversations?owner={BOB_KEY}", headers=ALICE
                )
                assert query.json()["conversations"] == []
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_delete_all_is_scoped_to_the_calling_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            seed(service.chat_history, ALICE_KEY, "alice one")
            seed(service.chat_history, ALICE_KEY, "alice two")
            bobs = seed(service.chat_history, BOB_KEY, "bob one")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                wiped = await http.delete(
                    "/api/chat/conversations", headers=mutate(ALICE, csrf)
                )
                assert wiped.status_code == 200
                assert wiped.json()["deleted"] == 2

            assert service.chat_history.conversations(ALICE_KEY) == ()
            assert service.chat_history.conversation(bobs, BOB_KEY) is not None
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_history_requires_a_session_exactly_as_chat_does(tmp_path: Path) -> None:
    """The same admission policy Chat already had; nothing was widened."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                unauthenticated = await http.get("/api/chat/conversations")
                assert unauthenticated.status_code == 401
                assert unauthenticated.json()["error"] == "invalid_session"
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_mutation_still_needs_origin_and_csrf(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            conversation_id = seed(service.chat_history, ALICE_KEY, "alice asked this")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)

                no_csrf = await http.delete(
                    f"/api/chat/conversations/{conversation_id}",
                    headers={**ALICE, "origin": ORIGIN},
                )
                assert no_csrf.status_code == 403
                assert no_csrf.json()["error"] == "csrf_denied"

                no_origin = await http.delete(
                    f"/api/chat/conversations/{conversation_id}",
                    headers={**ALICE, "x-butters-csrf": csrf},
                )
                assert no_origin.status_code == 403

                wipe = await http.delete(
                    "/api/chat/conversations", headers={**ALICE, "origin": ORIGIN}
                )
                assert wipe.status_code == 403

                assert service.chat_history.conversation(conversation_id, ALICE_KEY)
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_session_belonging_to_another_identity_is_refused(tmp_path: Path) -> None:
    """Reusing Alice's cookie as Bob fails on the existing peer binding."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            seed(service.chat_history, ALICE_KEY, "alice asked this")
            async with client(app) as http:
                await session_for(http, ALICE)
                # Same cookie jar, different claimed identity.
                stolen = await http.get("/api/chat/conversations", headers=BOB)
                assert stolen.status_code == 403
                assert stolen.json()["error"] == "session_identity_denied"
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


# ========================= 7. the live Chat path ==========================


def test_an_ordinary_turn_is_persisted_and_reopenable(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                answered = await http.post(
                    "/api/chat",
                    headers=mutate(ALICE, csrf),
                    json={"text": LOCAL_QUESTION},
                )
                assert answered.status_code == 200
                conversation_id = answered.json()["conversation_id"]
                assert conversation_id

                listed = await http.get("/api/chat/conversations", headers=ALICE)
                body = listed.json()
                assert body["active_conversation_id"] == conversation_id
                assert body["conversations"][0]["title"] == LOCAL_QUESTION
                assert body["conversations"][0]["message_count"] == 2

                read = await http.get(
                    f"/api/chat/conversations/{conversation_id}", headers=ALICE
                )
                stored = read.json()["messages"]
                assert stored[0]["role"] == "user"
                assert stored[0]["text"] == LOCAL_QUESTION
                assert stored[1]["role"] == "assistant"
                assert stored[1]["text"] == answered.json()["response_text"]
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_new_chat_keeps_the_previous_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                first = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                original = first.json()["conversation_id"]

                started = await http.post(
                    "/api/chat/conversations", headers=mutate(ALICE, csrf), json={}
                )
                assert started.status_code == 200
                assert started.json()["conversation_id"] is None
                csrf = started.json()["csrf_token"]

                # Nothing was deleted, and no empty row was created.
                listed = await http.get("/api/chat/conversations", headers=ALICE)
                assert [item["conversation_id"] for item in listed.json()["conversations"]] == [
                    original
                ]
                assert listed.json()["active_conversation_id"] is None

                second = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                assert second.json()["conversation_id"] != original
                assert len(service.chat_history.conversations(ALICE_KEY)) == 2
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_new_chat_alone_creates_no_conversation(tmp_path: Path) -> None:
    """Pressing New chat and walking away leaves nothing behind."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                for _ in range(5):
                    started = await http.post(
                        "/api/chat/conversations", headers=mutate(ALICE, csrf), json={}
                    )
                    assert started.status_code == 200
                    csrf = started.json()["csrf_token"]
                assert service.chat_history.conversations(ALICE_KEY) == ()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_reopening_restores_the_transcript_and_lets_it_continue(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                first = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                original = first.json()["conversation_id"]

                started = await http.post(
                    "/api/chat/conversations", headers=mutate(ALICE, csrf), json={}
                )
                csrf = started.json()["csrf_token"]

                opened = await http.post(
                    f"/api/chat/conversations/{original}/open",
                    headers=mutate(ALICE, csrf),
                    json={},
                )
                assert opened.status_code == 200
                assert len(opened.json()["messages"]) == 2

                # The next turn continues that conversation rather than a new one.
                again = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                assert again.json()["conversation_id"] == original
                assert service.chat_history.conversation(original, ALICE_KEY)[
                    "message_count"
                ] == 4
                assert len(service.chat_history.conversations(ALICE_KEY)) == 1
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_history_survives_a_service_restart_and_reopens(tmp_path: Path) -> None:
    """Two services over one state directory is what a restart looks like."""

    async def scenario() -> None:
        first_app, _first, _settings = build_app(tmp_path)
        try:
            async with client(first_app) as http:
                csrf = await session_for(http, ALICE)
                answered = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                conversation_id = answered.json()["conversation_id"]
        finally:
            await first_app.state.shutdown_workers()

        second_app, second, _settings = build_app(tmp_path)
        try:
            # Process memory is empty; the store is not.
            assert second.sessions.summaries() == ()
            async with client(second_app) as http:
                await session_for(http, ALICE)
                listed = await http.get("/api/chat/conversations", headers=ALICE)
                assert [
                    item["conversation_id"] for item in listed.json()["conversations"]
                ] == [conversation_id]
                read = await http.get(
                    f"/api/chat/conversations/{conversation_id}", headers=ALICE
                )
                assert read.json()["messages"][0]["text"] == LOCAL_QUESTION
        finally:
            await second_app.state.shutdown_workers()

    asyncio.run(scenario())


def test_the_selected_conversation_is_restored_after_a_restart(tmp_path: Path) -> None:
    """The pointer cookie rebinds only what the identity actually owns."""

    async def scenario() -> None:
        first_app, _first, _settings = build_app(tmp_path)
        try:
            async with client(first_app) as http:
                csrf = await session_for(http, ALICE)
                answered = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                conversation_id = answered.json()["conversation_id"]
                pointer = http.cookies.get("butters_chat")
                assert pointer == conversation_id
        finally:
            await first_app.state.shutdown_workers()

        second_app, _second, _settings = build_app(tmp_path)
        try:
            async with client(second_app) as http:
                http.cookies.set("butters_chat", pointer)
                restored = await http.get("/api/session", headers=ALICE)
                assert restored.json()["conversation_id"] == conversation_id
                assert restored.json()["messages"][0]["text"] == LOCAL_QUESTION
        finally:
            await second_app.state.shutdown_workers()

        # A different identity presenting the same pointer gets nothing.
        third_app, _third, _settings = build_app(tmp_path)
        try:
            async with client(third_app) as http:
                http.cookies.set("butters_chat", pointer)
                refused = await http.get("/api/session", headers=BOB)
                assert refused.json()["conversation_id"] is None
                assert refused.json()["messages"] == []
        finally:
            await third_app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_reload_restores_the_metadata_the_badge_line_needs(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                # Stand in for a cloud answer: the same store, the same fields.
                conversation_id = service.chat_history.conversations(ALICE_KEY)[0][
                    "conversation_id"
                ]
                response = response_with_metadata()
                service.chat_history.append(
                    conversation_id,
                    ALICE_KEY,
                    "assistant",
                    response.response_text,
                    metadata=assistant_metadata(response),
                )

                reloaded = await http.get("/api/session", headers=ALICE)
                messages = reloaded.json()["messages"]
                assert messages[-1]["metadata"]["routing_tier"] == "terra"
                assert messages[-1]["metadata"]["tier_escalated"] is True
                assert (
                    messages[-1]["metadata"]["reasoning_summary"]
                    == response.reasoning_summary
                )
                assert messages[-1]["text"] == "The answer."
                assert messages[0]["metadata"] is None
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_deleting_the_open_conversation_returns_chat_to_a_clean_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                answered = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                conversation_id = answered.json()["conversation_id"]

                removed = await http.delete(
                    f"/api/chat/conversations/{conversation_id}",
                    headers=mutate(ALICE, csrf),
                )
                assert removed.status_code == 200
                assert removed.json()["conversation_id"] is None

                fresh = await http.get("/api/session", headers=ALICE)
                assert fresh.json()["messages"] == []
                assert fresh.json()["conversation_id"] is None
                # Deleting one chat does not revoke the session or its token.
                assert fresh.json()["csrf_token"] == csrf
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_deleting_one_conversation_leaves_the_others_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            kept = seed(service.chat_history, ALICE_KEY, "kept one")
            removed = seed(service.chat_history, ALICE_KEY, "removed one")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                response = await http.delete(
                    f"/api/chat/conversations/{removed}", headers=mutate(ALICE, csrf)
                )
                assert response.status_code == 200

            remaining = service.chat_history.conversation(kept, ALICE_KEY)
            assert remaining is not None
            assert remaining["message_count"] == 2
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


# ========================= 8. deletion privacy ============================


def snapshot_other_databases(state_dir: Path) -> dict[str, object]:
    """Everything chat-history deletion is forbidden to touch."""

    counts: dict[str, object] = {}
    for database, tables in (
        ("usage.sqlite3", ("request_usage", "spend_totals", "provider_usage")),
        ("actions.sqlite3", ("action_audit", "action_jobs", "pending_action_plans")),
        ("security.sqlite3", ("passkey_credentials", "portal_identities")),
        ("state.sqlite3", ("ai_provider_profiles", "appearance")),
    ):
        path = state_dir / database
        if not path.exists():
            continue
        with sqlite3.connect(path) as connection:
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in tables:
                if table in present:
                    counts[f"{database}:{table}"] = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"  # table names are fixed above
                    ).fetchone()[0]
    return counts


def test_deleting_chat_history_touches_nothing_else(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                before = snapshot_other_databases(tmp_path)
                assert before, "expected at least one neighbouring table"
                assert before["usage.sqlite3:request_usage"] >= 1

                wiped = await http.delete(
                    "/api/chat/conversations", headers=mutate(ALICE, csrf)
                )
                assert wiped.status_code == 200
                assert service.chat_history.conversations(ALICE_KEY) == ()
                assert snapshot_other_databases(tmp_path) == before
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_transcripts_live_in_their_own_database(tmp_path: Path) -> None:
    """A structural guarantee, not a careful DELETE statement."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
        finally:
            await app.state.shutdown_workers()

        assert service.chat_history.database_path == tmp_path / "chat-history.sqlite3"
        for neighbour in ("usage.sqlite3", "actions.sqlite3", "security.sqlite3", "state.sqlite3"):
            path = tmp_path / neighbour
            if not path.exists():
                continue
            with sqlite3.connect(path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            assert "chat_conversations" not in tables, neighbour
            assert "chat_messages" not in tables, neighbour
            blob = path.read_bytes()
            assert LOCAL_QUESTION.encode() not in blob, neighbour

    asyncio.run(scenario())


def test_deleting_history_revokes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            seed(service.chat_history, ALICE_KEY, "something")
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                await http.delete("/api/chat/conversations", headers=mutate(ALICE, csrf))
                # The session and its CSRF token are untouched, and Chat works.
                answered = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                assert answered.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_the_delete_paths_name_only_the_chat_tables() -> None:
    """Read the statements: nothing outside chat history is addressable."""

    for statement in re.findall(r"DELETE FROM (\w+)", HISTORY_PY):
        assert statement in {"chat_conversations", "chat_messages"}, statement
    for statement in re.findall(r"(?:INSERT INTO|UPDATE)\s+(\w+)", HISTORY_PY):
        assert statement in {"chat_conversations", "chat_messages"}, statement


# ========================= 9. privacy and logging =========================


def test_the_store_never_logs_message_text() -> None:
    assert "logging" not in HISTORY_PY
    failure = SERVICE_PY[SERVICE_PY.index("def _append_history(") :]
    failure = failure[: failure.index("\n    def ", 1)]
    assert "could not persist a %s message to chat history" in failure
    # Only the role is interpolated.
    assert 'warning(\n                "could not persist a %s message to chat history", role\n            )' in failure


def test_a_history_write_failure_never_costs_the_answer(tmp_path: Path) -> None:
    """A read-only or missing store degrades history, never the assistant."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            class Broken:
                retention_days = 30

                def start_conversation(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("attempt to write a readonly database")

                def append(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("attempt to write a readonly database")

                def conversations(self, *_args, **_kwargs):
                    return ()

                def owns(self, *_args, **_kwargs):
                    return False

            service.chat_history = Broken()
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                answered = await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                assert answered.status_code == 200
                assert answered.json()["response_text"]
                assert answered.json()["conversation_id"] is None
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_transcripts_are_absent_from_the_operational_surfaces(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                await http.post(
                    "/api/chat", headers=mutate(ALICE, csrf), json={"text": LOCAL_QUESTION}
                )
                for path in ("/healthz", "/readyz"):
                    response = await http.get(path)
                    assert LOCAL_QUESTION not in response.text
                    assert "conversation" not in response.text.casefold()

                admin = {"tailscale-user-login": "admin@example.com"}
                for path in ("/api/admin/sessions", "/api/admin/usage"):
                    response = await http.get(path, headers=admin)
                    assert response.status_code in {200, 403}
                    assert LOCAL_QUESTION not in response.text
                    assert "chat_conversations" not in response.text
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_an_error_never_echoes_stored_conversation_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            bobs = seed(
                service.chat_history, BOB_KEY, "a private thing bob asked about"
            )
            async with client(app) as http:
                await session_for(http, ALICE)
                refused = await http.get(
                    f"/api/chat/conversations/{bobs}", headers=ALICE
                )
                assert refused.status_code == 404
                assert refused.json() == {
                    "error": "conversation_not_found",
                    "message": "conversation was not found",
                    "safe_to_retry": False,
                }
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_the_database_file_is_owner_only(tmp_path: Path) -> None:
    history = store(tmp_path)
    seed(history, ALICE_KEY)
    assert (history.database_path.stat().st_mode & 0o777) == 0o600


# ========================= 10. TTS isolation ==============================


def test_speech_is_synthesized_from_the_canonical_answer_only() -> None:
    """The speech path reads session messages, which hold canonical text."""

    body = SERVICE_PY[SERVICE_PY.index("def synthesize_trace_response(") :]
    body = body[: body.index("\n    def ", 1)]
    assert "session.messages" in body
    for forbidden in (
        "chat_history",
        "metadata",
        "reasoning_summary",
        "routing_tier",
        "routing_mode",
    ):
        assert forbidden not in body, forbidden


def test_reopening_a_conversation_feeds_tts_canonical_text_only(tmp_path: Path) -> None:
    """After a reopen the in-memory buffer holds answers, not badge lines."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            conversation_id = service.chat_history.start_conversation(
                ALICE_KEY, "question"
            )
            service.chat_history.append(conversation_id, ALICE_KEY, "user", "question")
            response = response_with_metadata()
            service.chat_history.append(
                conversation_id,
                ALICE_KEY,
                "assistant",
                response.response_text,
                metadata=assistant_metadata(response),
            )
            async with client(app) as http:
                csrf = await session_for(http, ALICE)
                opened = await http.post(
                    f"/api/chat/conversations/{conversation_id}/open",
                    headers=mutate(ALICE, csrf),
                    json={},
                )
                assert opened.status_code == 200

            session = next(iter(service.sessions._sessions.values()))
            spoken = [item.text for item in session.messages]
            assert spoken == ["question", "The answer."]
            for text in spoken:
                assert response.reasoning_summary not in text
                assert "terra" not in text
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


# ========================= 11. the browser contract =======================
#
# No JavaScript runs on this host. These read the shipped source.


def test_the_reopen_path_renders_through_the_safe_renderer() -> None:
    reopen = APP_JS[APP_JS.index("async function openConversation(") :]
    reopen = reopen[: reopen.index("\nasync function ", 1)]
    assert "addMessage(message.role, message.text, message.metadata)" in reopen
    assert "innerHTML" not in reopen
    assert "insertAdjacentHTML" not in reopen
    # Only `addMessage` reaches the renderer, and it renders Markdown source.
    assert "renderAssistantMarkdown" not in reopen


def _without_comments(source: str) -> str:
    """Executable JavaScript only, so a comment cannot satisfy an assertion."""

    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", " ", source, flags=re.MULTILINE)


def test_no_path_in_the_client_injects_stored_html() -> None:
    code = _without_comments(APP_JS)
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code
    # The drawer builds its rows with textContent, never with markup.
    drawer = APP_JS[APP_JS.index("function historyRow(") :]
    drawer = drawer[: drawer.index("\nfunction ", 1)]
    assert "textContent" in drawer
    assert "innerHTML" not in _without_comments(drawer)


def test_the_markdown_security_boundary_is_unchanged() -> None:
    assert "html: false,        // the important one" in APP_JS
    assert "DOMPurify.sanitize(markdown.render(source), PURIFY_CONFIG)" in APP_JS
    assert 'markdown.disable("image", true)' in APP_JS
    assert "cdn" not in APP_JS.casefold().replace("cdn_", "")
    assert '<script src="/assets/markdown-it.umd.min.js" defer></script>' in INDEX_HTML
    assert '<script src="/assets/purify.min.js" defer></script>' in INDEX_HTML


def test_the_content_security_policy_is_unchanged() -> None:
    assert (
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        in APP_PY.encode()
    )
    assert "frame-ancestors 'none'" in APP_PY


def _history_css() -> str:
    """Just the history layer of the Chat stylesheet."""

    start = CHAT_CSS.index("/* ------------------------------------------------------------ history --")
    end = CHAT_CSS.index("/* ------------------------------------------------------------- mobile --")
    return CHAT_CSS[start:end]


def test_the_history_drawer_is_a_drawer_at_every_width() -> None:
    """It overlays the conversation; it never takes a permanent column."""

    assert 'id="history-drawer"' in INDEX_HTML
    assert 'class="history-drawer"' in INDEX_HTML
    # The shell's grid rows are unchanged: no track was added for history.
    assert "grid-template-rows: auto 1fr auto auto auto auto;" in CHAT_CSS
    drawer = CHAT_CSS[CHAT_CSS.index(".history-drawer {") :]
    drawer = drawer[: drawer.index("}")]
    assert "position: absolute" in drawer
    assert "width: min(320px, 84%)" in drawer
    # The drawer lays itself out in rows; it adds no column to the shell.
    section = _history_css()
    assert "grid-template-columns" not in section
    assert "grid-template-rows: auto auto auto 1fr auto auto;" in section


def test_the_drawer_is_hidden_by_the_hidden_attribute_alone() -> None:
    """Nothing declares `display` on it while hidden - see base.css."""

    assert 'id="history-drawer" class="history-drawer" aria-label="Saved chats" hidden' in INDEX_HTML
    assert 'id="history-backdrop" class="history-backdrop" hidden' in INDEX_HTML
    drawer = CHAT_CSS[CHAT_CSS.index(".history-drawer {") :]
    assert "display" not in drawer[: drawer.index("}")]
    assert ".history-drawer:not([hidden]) {" in CHAT_CSS
    backdrop = CHAT_CSS[CHAT_CSS.index(".history-backdrop {") :]
    assert "display" not in backdrop[: backdrop.index("}")]


def test_history_touch_targets_stay_reachable() -> None:
    open_rule = CHAT_CSS[CHAT_CSS.index(".history-open {") :]
    open_rule = open_rule[: open_rule.index("}")]
    assert "min-height: var(--tap-min)" in open_rule
    delete_rule = CHAT_CSS[CHAT_CSS.index(".history-delete {") :]
    delete_rule = delete_rule[: delete_rule.index("}")]
    assert "min-height: var(--tap-min)" in delete_rule
    assert "min-width: var(--tap-min)" in delete_rule


def test_the_drawer_uses_design_tokens_rather_than_literals() -> None:
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", _history_css())
    assert literals == [], literals


def test_the_retention_period_is_stated_to_the_reader() -> None:
    assert "Chats are kept for 30 days." in INDEX_HTML
    assert "Chats are kept for ${days}" in APP_JS


def test_destructive_controls_ask_before_they_act() -> None:
    row = APP_JS[APP_JS.index("function historyRow(") :]
    row = row[: row.index("\nfunction ", 1)]
    assert 'remove.dataset.confirming === "true"' in row
    assert 'remove.dataset.confirming = "true"' in row

    wipe = APP_JS[APP_JS.index("async function deleteAllHistory(") :]
    wipe = wipe[: wipe.index("\nhistoryButton", 1)]
    assert 'historyDeleteAll.dataset.confirming !== "true"' in wipe
    assert "Delete everything?" in wipe


def test_both_active_conversation_names_mark_the_current_row() -> None:
    """`/api/session` and the history list name the same fact differently."""

    state = APP_JS[APP_JS.index("function applyConversationState(") :]
    state = state[: state.index("\nfunction ", 1)]
    assert '"conversation_id", "active_conversation_id"' in state
    # And the server really does use both names.
    assert '"active_conversation_id": session.conversation_id' in SERVICE_PY
    assert '"conversation_id": existing.conversation_id' in APP_PY


def test_the_current_conversation_is_marked_in_the_drawer() -> None:
    row = APP_JS[APP_JS.index("function historyRow(") :]
    row = row[: row.index("\nfunction ", 1)]
    assert "item.conversation_id === activeConversationId" in row
    assert 'row.setAttribute("aria-current", current ? "true" : "false")' in row
    assert '.history-item[aria-current="true"] .history-open {' in CHAT_CSS


def test_the_client_never_sends_an_owner() -> None:
    for endpoint in (
        "/api/chat/conversations",
        "/api/chat/conversations/${encodeURIComponent(conversationId)}",
    ):
        assert endpoint in APP_JS
    assert "owner_key" not in APP_JS
    assert "owner=" not in APP_JS
    assert "peer_key" not in APP_JS
