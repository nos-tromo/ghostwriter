"""What the model reads: tool schemas, arguments as models write them, the budget, follow-ups, the system prompt.

Every name, id and value here is invented.
"""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from ghostwriter.agent.context import CLEARED, PastCall, PastTurn, estimate_tokens, fit, user_content
from ghostwriter.agent.limits import (
    HISTORY_ARGUMENT_CHARS,
    HISTORY_RESULT_CHARS,
    INLINE_IMAGE_BYTES,
    PROMPT_TOKENS,
    REASONING_PROMPT_TOKENS,
    TOOL_RESULT_CHARS,
)
from ghostwriter.agent.prompt import PRESENTATIONS, system_prompt
from ghostwriter.agent.tools import (
    REFUSALS,
    Refusal,
    echoed_arguments,
    function_tools,
    parse_arguments,
    result_kind,
    step_detail,
    tool_message,
)
from ghostwriter.db import init_db, make_engine
from ghostwriter.schemas import AttachmentOut
from ghostwriter.store import ChatStore
from ghostwriter.workbench import McpTool, OutputFile, TablePreview, ToolFailure, ToolResult

T0 = datetime(2026, 11, 16, tzinfo=UTC)
DAY = timedelta(days=1)
FOLDER = "/workspace/" + "a" * 64
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
TOOLS = function_tools(
    [
        McpTool("read_document", "Read a document.", SCHEMA),
        McpTool("run_python", "Run Python on the chat's files.", {"type": "object", "properties": {}}),
    ]
)
SYSTEM = system_prompt(today=date(2026, 11, 16), folder=FOLDER, file_days=7, expired=False)
EXPIRED = (
    "Some files and tool results of earlier messages in this chat have been deleted, as 7 days had passed. "
    "Say so when the user asks about them, and ask them to attach the file again."
)


class Clock:
    """A clock set by hand.

    Attributes:
        now: The time it reads.
    """

    def __init__(self) -> None:
        """Start at ``T0``."""
        self.now = T0

    def __call__(self) -> datetime:
        """Read the clock.

        Returns:
            The time it is set to.
        """
        return self.now


@pytest.fixture
def clock() -> Clock:
    """Return the store's clock, at ``T0``."""
    return Clock()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a fresh database with the schema."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def store(engine: Engine, clock: Clock) -> ChatStore:
    """Return a store with a 7-day file period."""
    return ChatStore(engine, file_days=7, notice_days=30, now=clock)


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _round_with_results(n: int, *, chars: int) -> list[dict[str, Any]]:
    """Return one round of the current turn: ``n`` calls and their results, each ``chars`` long."""
    calls = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "read_document", "arguments": json.dumps({"path": f"{FOLDER}/teil-{i}.pdf"})},
        }
        for i in range(n)
    ]
    results = [{"role": "tool", "tool_call_id": f"call_{i}", "content": str(i) * chars} for i in range(n)]
    return [{"role": "assistant", "content": "", "tool_calls": calls}, *results]


def _past_turn(*, user: str, answer: str, rounds: tuple[tuple[PastCall, ...], ...] = ()) -> PastTurn:
    return PastTurn(user=user, rounds=rounds, answer=answer, expired=False)


def _past_users(messages: list[dict[str, Any]]) -> list[str]:
    """Return the users' messages replayed before the current one."""
    return [m["content"] for m in messages if m["role"] == "user"][:-1]


def _output(**changes: Any) -> OutputFile:
    entry: dict[str, Any] = {
        "id": "0" * 32,
        "path": f"{FOLDER}/out/plot.png",
        "name": "plot.png",
        "mime": "image/png",
        "size": 2048,
        "preview": None,
    }
    return OutputFile(**{**entry, **changes})


def test_mcp_tools_become_function_tools() -> None:
    """Each MCP tool is offered as an OpenAI function with its schema as the parameters, in order."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    assert function_tools([McpTool("read_document", "Read a document.", schema)]) == [
        {
            "type": "function",
            "function": {"name": "read_document", "description": "Read a document.", "parameters": schema},
        }
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("  ", {}),
        ('{"path": "/w/a.pdf"}', {"path": "/w/a.pdf"}),
        ('"{\\"path\\": \\"/w/a.pdf\\"}"', {"path": "/w/a.pdf"}),
        ("[1]", None),
        ("not json", None),
        ('"text"', None),
        ("null", None),
        ("[" * 5000 + "]" * 5000, None),
        ('{"a": ' * 40 + "1" + "}" * 40, None),
        ('{"path": "a\\ud800.pdf"}', None),
    ],
)
def test_arguments_are_read_as_models_write_them(raw: str, expected: dict[str, Any] | None) -> None:
    """Empty is no arguments, a string holding an object is decoded once, anything else is refused; never raises."""
    assert parse_arguments(raw) == expected


@pytest.mark.parametrize(
    ("raw", "echoed"),
    [
        ('{"path": "/w/a.pdf"}', '{"path": "/w/a.pdf"}'),
        ('"{\\"path\\": \\"/w/a.pdf\\"}"', '{"path":"/w/a.pdf"}'),
        ('{"a": ' * 40 + "1" + "}" * 40, "{}"),
        ('"{\\"path\\": \\"a\\\\ud800.pdf\\"}"', "{}"),
        ("", "{}"),
        ("[1]", "{}"),
        ("not json", "{}"),
    ],
)
def test_a_calls_arguments_are_echoed_as_a_json_object(raw: str, echoed: str) -> None:
    """Chat templates parse an earlier call's arguments as JSON: anything but an object goes back as ``{}``."""
    assert echoed_arguments(raw) == echoed


@pytest.mark.parametrize(
    ("arguments", "detail"),
    [
        ({"path": f"{FOLDER}/sales.xlsx"}, "sales.xlsx"),
        ({"path": "sales.xlsx", "name": "sums.xlsx"}, "sales.xlsx"),
        ({"name": "sums.xlsx", "rows": []}, "sums.xlsx"),
        ({"query": "Umsatz 2026"}, "Umsatz 2026"),
        ({"query": "x" * 300}, "x" * 120),
        ({"code": "print(1)"}, None),
        ({"path": 7, "query": "Umsatz"}, "Umsatz"),
        ({}, None),
    ],
)
def test_a_step_names_what_it_worked_on(arguments: dict[str, Any], detail: str | None) -> None:
    """The trail shows a file's name, a written file's name or the query searched for; never code."""
    assert step_detail("read_document", arguments) == detail


def test_the_model_reads_a_result_failure_or_refusal_in_bounded_text() -> None:
    """A result is cut with a mark; a failure or refusal is a small JSON object; a busy failure says to wait."""
    long = ToolResult("x" * (TOOL_RESULT_CHARS + 50), ())
    assert len(tool_message(long)) == TOOL_RESULT_CHARS and tool_message(long).endswith("…[cut]")
    assert tool_message(ToolResult("Umsatz 42", ())) == "Umsatz 42"
    failed = tool_message(ToolFailure("not_completed", "y" * 900, True))
    assert json.loads(failed) == {"error": "not_completed", "detail": "y" * 500}
    assert json.loads(tool_message(ToolFailure("queue_full", "busy, retry later", True))) == {
        "error": "queue_full",
        "detail": REFUSALS["busy"],
    }
    assert tool_message(Refusal("call_limit", REFUSALS["call_limit"])) == (
        '{"error":"call_limit","detail":"at most 4 tool calls run per round; call it again in your next round."}'
    )


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        (_output(), "image"),
        (_output(size=INLINE_IMAGE_BYTES), "image"),
        (_output(size=INLINE_IMAGE_BYTES + 1), "file"),
        (_output(mime="image/svg+xml", name="plot.svg"), "file"),
        (_output(mime=XLSX, name="sums.xlsx", preview=TablePreview(("a",), (("1",),), 1, False)), "table"),
        (_output(mime="text/csv", name="sums.csv"), "file"),
    ],
)
def test_outputs_are_shown_as_images_tables_or_files(output: OutputFile, kind: str) -> None:
    """A preview makes a table; a small raster image is shown inline; the rest is a download."""
    assert result_kind(output) == kind


def test_context_fits_the_budget_with_long_history_and_big_results() -> None:
    """Review Focus 4: a long German chat with big results stays in the budget; whole turns go, oldest first."""
    past = [_past_turn(user=f"{n}: " + "Größe " * 700, answer="Übersicht " * 400) for n in range(30)]
    current = [_user("Fasse zusammen"), *_round_with_results(3, chars=12_000)]
    messages = fit(SYSTEM, TOOLS, past, current)
    assert estimate_tokens(messages) + estimate_tokens(TOOLS) <= PROMPT_TOKENS
    assert any(m["role"] == "user" and m["content"] == "Fasse zusammen" for m in messages)
    assert _past_users(messages) == [p.user for p in past[-len(_past_users(messages)) :]]
    assert 0 < len(_past_users(messages)) < len(past)
    assert messages[0] == {"role": "system", "content": SYSTEM} and messages[-len(current) :] == current
    assert CLEARED not in json.dumps(messages, ensure_ascii=False)


def test_the_current_turns_oldest_results_are_cleared_first() -> None:
    """Over the budget with the current turn alone, its oldest results give way; past turns fill what is left."""
    current = [_user("Fasse zusammen"), *_round_with_results(3, chars=12_000)]
    messages = fit(SYSTEM, TOOLS, [_past_turn(user="Vorher", answer="Ja")], current, budget=9_000)
    tools = [m for m in messages if m["role"] == "tool"]
    assert [m["content"] for m in tools] == [CLEARED, "1" * 12_000, "2" * 12_000]
    assert estimate_tokens(messages) + estimate_tokens(TOOLS) <= 9_000
    assert _past_users(messages) == ["Vorher"] and current[2]["content"] == "0" * 12_000


def test_the_current_turns_arguments_are_cut_oldest_first_once_its_results_are_cleared() -> None:
    """Long documents written in a reasoning message: the oldest round's arguments are cut, still JSON objects."""
    current: list[dict[str, Any]] = [_user("Schreib den Bericht und überarbeite ihn zweimal")]
    for n in range(3):
        arguments = json.dumps({"name": f"bericht-{n}.docx", "markdown": f"# Fassung {n}\n" + "Absatz. " * 3_750})
        call = {"id": f"call_{n}", "type": "function", "function": {"name": "write_document", "arguments": arguments}}
        result = json.dumps({"path": f"{FOLDER}/out/bericht-{n}.docx", "size": 31_000, "outputs": []})
        current += [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": f"call_{n}", "content": result},
        ]
    before = json.loads(json.dumps(current))
    messages = fit(SYSTEM, TOOLS, [], current, budget=REASONING_PROMPT_TOKENS)
    assert estimate_tokens(messages) + estimate_tokens(TOOLS) <= REASONING_PROMPT_TOKENS
    sent = [m["tool_calls"][0]["function"]["arguments"] for m in messages if m.get("tool_calls")]
    assert all(isinstance(json.loads(arguments), dict) for arguments in sent)
    assert len(sent[0]) <= HISTORY_ARGUMENT_CHARS and json.loads(sent[0])["markdown"].endswith("…[cut]")
    assert json.loads(sent[0])["name"] == "bericht-0.docx"
    assert sent[1:] == [m["tool_calls"][0]["function"]["arguments"] for m in current[3::2]]
    assert current == before


def test_earlier_turns_are_kept_newest_first_without_a_gap() -> None:
    """A turn that does not fit ends the replay: no older turn is slipped in behind it."""
    past = [
        _past_turn(user="alt", answer="kurz"),
        _past_turn(user="groß", answer="Übersicht " * 3000),
        _past_turn(user="neu", answer="kurz"),
    ]
    messages = fit(SYSTEM, TOOLS, past, [_user("Weiter")], budget=3_000)
    assert _past_users(messages) == ["neu"]


def test_the_tool_schemas_count_against_the_budget() -> None:
    """Large tool schemas leave less room for earlier turns."""
    tools = function_tools([McpTool("run_python", "Run Python. " * 500, SCHEMA)])
    past = [_past_turn(user=f"{n}: " + "Größe " * 100, answer="Übersicht " * 60) for n in range(20)]
    messages = fit(SYSTEM, tools, past, [_user("Weiter")], budget=4_000)
    assert estimate_tokens(messages) + estimate_tokens(tools) <= 4_000
    assert len(_past_users(messages)) < len(_past_users(fit(SYSTEM, [], past, [_user("Weiter")], budget=4_000)))


def test_the_budget_holds_exactly_at_every_size() -> None:
    """Whatever the budget, the messages stay within it, and the next earlier turn would not have fitted."""
    past = [_past_turn(user=f"F{n}", answer=f"A{n}") for n in range(60)]
    current = [_user("Weiter")]
    for budget in range(estimate_tokens([{"role": "system", "content": SYSTEM}, *current]) + 1, 1_200, 3):
        messages = fit(SYSTEM, [], past, current, budget=budget)
        assert estimate_tokens(messages) <= budget
        kept = len(_past_users(messages))
        if kept < len(past):
            nearest = past[-kept - 1]
            more = [messages[0], _user(nearest.user), {"role": "assistant", "content": nearest.answer}, *messages[1:]]
            assert estimate_tokens(more) > budget


def test_earlier_tool_results_follow_up_while_not_expired() -> None:
    """A past turn is replayed natively: its question, its calls with their results, cut, then its answer."""
    code = "print('Umsatz')\n" * 400
    call = PastCall("call_a", "run_python", json.dumps({"code": code}), "Umsatz " * 1000)
    past = [_past_turn(user="Rechne die Summe", answer="Die Summe ist 42.", rounds=((call,),))]
    messages = fit(SYSTEM, TOOLS, past, [_user("Und im Vorjahr?")])
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant", "user"]
    [replayed] = messages[2]["tool_calls"]
    assert (replayed["id"], replayed["type"], replayed["function"]["name"]) == ("call_a", "function", "run_python")
    arguments = replayed["function"]["arguments"]
    assert len(arguments) <= HISTORY_ARGUMENT_CHARS and json.loads(arguments)["code"].startswith("print('Umsatz')")
    assert json.loads(arguments)["code"].endswith("…[cut]")
    assert messages[3]["tool_call_id"] == "call_a" and len(messages[3]["content"]) == HISTORY_RESULT_CHARS
    assert messages[3]["content"].startswith("Umsatz Umsatz") and messages[3]["content"].endswith("…[cut]")
    assert messages[4] == {"role": "assistant", "content": "Die Summe ist 42."}


def test_an_earlier_call_with_bad_arguments_is_replayed_as_an_empty_object() -> None:
    """Arguments that are not an object would fail the template that parses them: ``{}`` goes back instead."""
    call = PastCall("call_a", "read_document", "not json", '{"error":"bad_arguments"}')
    messages = fit(SYSTEM, TOOLS, [_past_turn(user="Lies", answer="Nein.", rounds=((call,),))], [_user("Weiter")])
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_attachments_are_named_with_their_paths_until_they_expire() -> None:
    """The model reads an attachment's path and size, or that it has expired; never its content."""
    live = AttachmentOut(
        id="att-1", file_id="1" * 32, name="sales.xlsx", size=14, mime=XLSX, created_at=T0, expired=False
    )
    gone = live.model_copy(update={"id": "att-2", "name": "alt.pdf", "expired": True})
    text = user_content("Summe der Spalte B", [live, gone], path_of=lambda a: f"{FOLDER}/{a.name}")
    assert text == (
        f"Summe der Spalte B\n[Attached: sales.xlsx ({FOLDER}/sales.xlsx, 14 bytes)]\n[Attached: alt.pdf; expired]"
    )
    assert user_content("", [live], path_of=lambda a: f"{FOLDER}/{a.name}") == (
        f"[Attached: sales.xlsx ({FOLDER}/sales.xlsx, 14 bytes)]"
    )


def test_expired_steps_are_not_replayed_and_the_prompt_says_so(store: ChatStore, clock: Clock) -> None:
    """Once the chat's earliest file has gone, its tool contents are not replayed and the expiry is reported."""
    chat = store.create_chat("user-a")
    store.add_user_message("user-a", chat.id, "Was steht in sales.xlsx?", [])
    answer = store.start_assistant_message(chat.id)
    arguments = json.dumps({"path": f"{FOLDER}/sales.xlsx"})
    store.add_step(
        answer,
        id=str(uuid4()),
        position=0,
        round=1,
        call_id="call_a",
        tool="read_document",
        arguments=arguments,
        result="Umsatz 42",
        status="ok",
    )
    store.finish_assistant_message(answer, "Umsatz: 42")
    store.add_user_message("user-a", chat.id, "Und im Vorjahr?", [])
    live, expired = store.past_turns(chat.id, before_position=2)
    call = PastCall("call_a", "read_document", arguments, "Umsatz 42")
    assert (live, expired) == ([PastTurn("Was steht in sales.xlsx?", ((call,),), "Umsatz: 42", False)], False)
    clock.now = T0 + 7 * DAY
    turns, expired = store.past_turns(chat.id, before_position=2)
    assert (turns, expired) == ([PastTurn("Was steht in sales.xlsx?", (), "Umsatz: 42", True)], True)
    assert system_prompt(today=date(2026, 11, 23), folder=FOLDER, file_days=7, expired=True).endswith(EXPIRED)


def test_the_store_names_attachments_in_the_users_messages(store: ChatStore, clock: Clock) -> None:
    """A message is read with its attachments' paths; an attachment past its period is named expired."""
    chat = store.create_chat("user-a")
    attachment = store.add_attachment(
        chat.id, file_id="1" * 32, path=f"{FOLDER}/sales.xlsx", name="sales.xlsx", size=14, mime=XLSX
    )
    first = store.add_user_message("user-a", chat.id, "Summe der Spalte B", [attachment.id])
    store.finish_assistant_message(store.start_assistant_message(chat.id), "42")
    second = store.add_user_message("user-a", chat.id, "Und Spalte C?", [])
    named = f"Summe der Spalte B\n[Attached: sales.xlsx ({FOLDER}/sales.xlsx, 14 bytes)]"
    assert store.user_turn(chat.id, first.id) == (named, 0)
    assert store.user_turn(chat.id, second.id) == ("Und Spalte C?", 2)
    assert store.past_turns(chat.id, before_position=2) == ([PastTurn(named, (), "42", False)], False)
    clock.now = T0 + 7 * DAY
    gone = "Summe der Spalte B\n[Attached: sales.xlsx; expired]"
    assert store.past_turns(chat.id, before_position=2) == ([PastTurn(gone, (), "42", True)], True)


def test_a_step_reads_back_with_its_detail_until_it_expires(store: ChatStore, clock: Clock) -> None:
    """The trail's detail comes from the stored arguments, and goes with them."""
    chat = store.create_chat("user-a")
    store.add_user_message("user-a", chat.id, "Lies sales.xlsx", [])
    answer = store.start_assistant_message(chat.id)
    step = store.add_step(
        answer,
        id=str(uuid4()),
        position=0,
        round=1,
        call_id="call_a",
        tool="read_document",
        arguments=json.dumps({"path": f"{FOLDER}/sales.xlsx"}),
        result="Umsatz 42",
        status="ok",
    )
    assert step.detail == "sales.xlsx"
    assert store.chat_detail("user-a", chat.id).messages[-1].steps[0].detail == "sales.xlsx"
    clock.now = T0 + 7 * DAY
    assert store.chat_detail("user-a", chat.id).messages[-1].steps[0].detail is None


def test_the_system_prompt_names_the_folder_and_the_period() -> None:
    """With tools the prompt names the chat's folder and the file period; without, it says there are none."""
    assert FOLDER in SYSTEM and "deleted 7 days after" in SYSTEM
    assert SYSTEM.startswith("You are Ghostwriter in this organisation's workspace. Today is 2026-11-16. ")
    assert "You have no tools" not in SYSTEM and not SYSTEM.endswith(EXPIRED)
    bare = system_prompt(today=date(2026, 11, 16), folder=None, file_days=7, expired=False)
    assert bare == (
        "You are Ghostwriter in this organisation's workspace. Today is 2026-11-16. Answer in the language of the "
        "user's last message, in Markdown; write tables as Markdown tables. You have no tools in this chat, and "
        "files cannot be read."
    )


def test_the_presentation_sentence_comes_with_the_tool() -> None:
    """The outline-first rule is in the prompt only when write_presentation is offered and tools are.

    The outline names each layout's places in order, which the deck is then built in.
    """
    assert PRESENTATIONS == (
        "To build a presentation in a slide master (.pptx or .potx): call inspect_template on it and read the "
        "sources, then show the user an outline (each slide's title, the layout's number and name with its places "
        "in order, key points, figures and sources) and stop. Call write_presentation only in a later message, "
        "after the user has agreed. Images for slides are made with run_python; prefer native charts."
    )
    with_tool = system_prompt(today=date(2026, 11, 16), folder=FOLDER, file_days=7, expired=False, presentations=True)
    assert with_tool == f"{SYSTEM} {PRESENTATIONS}"
    assert PRESENTATIONS not in SYSTEM
    bare = system_prompt(today=date(2026, 11, 16), folder=None, file_days=7, expired=False, presentations=True)
    assert bare == system_prompt(today=date(2026, 11, 16), folder=None, file_days=7, expired=False)
