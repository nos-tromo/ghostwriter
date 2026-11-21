"""What the model reads: tool schemas, arguments as models write them, the budget and images, follow-ups, the prompt.

Every name, id and value here is invented.
"""

import copy
import dataclasses
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from ghostwriter.agent.context import (
    CLEARED,
    PastCall,
    PastTurn,
    attachment_facts,
    estimate_tokens,
    fit,
    user_content,
)
from ghostwriter.agent.images import (
    LABEL,
    NOT_RESENT,
    OVER_CAP,
    RETIRED,
    UNSEEN,
    AttachedImage,
    ImageBudget,
    attachment_parts,
    note,
    refs,
    tool_image_message,
)
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
from ghostwriter.store import ChatStore, UserTurn
from ghostwriter.workbench import McpTool, OutputFile, TablePreview, ToolFailure, ToolImage, ToolResult
from tests.fakes import TINY_JPEG, TINY_PNG, stock_manifest

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
        id="att-1",
        file_id="1" * 32,
        name="sales.xlsx",
        size=14,
        mime=XLSX,
        kind="document",
        status="ready",
        created_at=T0,
        expired=False,
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
    assert store.user_turn(chat.id, first.id) == UserTurn(named, (), 0)
    assert store.user_turn(chat.id, second.id) == UserTurn("Und Spalte C?", (), 2)
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

    The outline names each layout's places in order, which the deck is then built in. Without a slide master of the
    user's, the deck is built in workbench's plain master, both tools called without a path.
    """
    assert PRESENTATIONS == (
        "To build a presentation, use the user's slide master (.pptx or .potx) if the chat has one, else the plain "
        "16:9 master (leave path out of inspect_template and write_presentation). Call inspect_template and read "
        "the sources, then show the user an outline (each slide's title, the layout's number and name with its "
        "places in order, key points, figures and sources) and stop. Call write_presentation only in a later "
        "message, after the user has agreed. Images for slides are made with run_python; prefer native charts."
    )
    with_tool = system_prompt(today=date(2026, 11, 16), folder=FOLDER, file_days=7, expired=False, presentations=True)
    assert with_tool == f"{SYSTEM} {PRESENTATIONS}"
    assert PRESENTATIONS not in SYSTEM
    bare = system_prompt(today=date(2026, 11, 16), folder=None, file_days=7, expired=False, presentations=True)
    assert bare == system_prompt(today=date(2026, 11, 16), folder=None, file_days=7, expired=False)


PAGE = ToolImage("quay-scan.pdf, page 2", 1131, 1600, "image/jpeg", TINY_JPEG)
CHART = ToolImage("plot.png", 640, 480, "image/png", TINY_PNG)


def _photo(n: int, name: str, *, width: int = 64, height: int = 48) -> AttachedImage:
    return AttachedImage(file_id=str(n) * 32, path=f"{FOLDER}/{name}", name=name, width=width, height=height)


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _labelled(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the messages with each image ref as its label: what the budget counts of them in characters."""
    return [
        {**m, "content": [note(p, LABEL) if p.get("type") == "image_ref" else p for p in m["content"]]}
        if isinstance(m.get("content"), list)
        else m
        for m in messages
    ]


def _text_tokens(current: list[dict[str, Any]]) -> int:
    """Return the tokens of the system prompt and the current turn without their images' own cost."""
    return estimate_tokens(_labelled([{"role": "system", "content": SYSTEM}, *current]))


def _image_manifest(width: int, height: int) -> dict[str, Any]:
    rendition = {"n": 1, "width": width, "height": height, "mime": "image/jpeg"}
    return {"kind": "image", "total": 1, "pages_without_text": [], "renditions": [rendition], "frames": None}


def _pdf_manifest(total: int, blank: list[int]) -> dict[str, Any]:
    return {"kind": "pdf", "total": total, "pages_without_text": blank, "renditions": [], "frames": None}


def test_the_attachment_line_carries_facts() -> None:
    """The line names an image's size, a PDF's pages and those without text; a failed or expired file says so."""
    times = "\N{MULTIPLICATION SIGN}"
    assert attachment_facts("image", _image_manifest(2048, 1536), seen=True) == f"image 2048{times}1536"
    assert attachment_facts("image", _image_manifest(2048, 1536), seen=False) == (
        f"image 2048{times}1536; not shown: this model does not take images"
    )
    assert (
        attachment_facts("document", _pdf_manifest(12, [3, 7]), seen=True) == "PDF, 12 pages; pages 3, 7 have no text"
    )
    assert attachment_facts("document", _pdf_manifest(12, []), seen=False) == "PDF, 12 pages"
    assert attachment_facts("document", _pdf_manifest(5, [3]), seen=True) == "PDF, 5 pages; page 3 has no text"
    assert attachment_facts("document", _pdf_manifest(1, []), seen=True) == "PDF, 1 page"
    assert attachment_facts("document", {"kind": "none", "total": 0}, seen=True) is None
    assert attachment_facts("document", None, seen=True) is None

    def attached(name: str, size: int, **changes: Any) -> AttachmentOut:
        fields: dict[str, Any] = {"kind": "document", "status": "ready", "expired": False, **changes}
        return AttachmentOut(
            id=f"att-{name}", file_id="1" * 32, name=name, size=size, mime="image/jpeg", created_at=T0, **fields
        )

    photo = attached("harbour-photo.jpg", 48_000, kind="image")
    files = [
        photo,
        attached("quay-scan.pdf", 900_000),
        attached("quay-letter.pdf", 70_000),
        attached("letter.pdf", 2_000),
        attached("sales.xlsx", 14),
        attached("blurred-photo.jpg", 300, kind="image", status="failed"),
        attached("old-scan.pdf", 5_000, expired=True),
    ]
    manifests: dict[str, Mapping[str, Any]] = {
        "att-harbour-photo.jpg": _image_manifest(2048, 1536),
        "att-quay-scan.pdf": _pdf_manifest(12, [3, 7]),
        "att-quay-letter.pdf": _pdf_manifest(5, [3]),
        "att-letter.pdf": _pdf_manifest(1, []),
        "att-old-scan.pdf": _pdf_manifest(2, []),
    }

    def path_of(a: AttachmentOut) -> str:
        return f"{FOLDER}/{a.name}"

    text = user_content(
        "Was steht darin?",
        files,
        path_of=path_of,
        facts_of=lambda a: attachment_facts(a.kind, manifests.get(a.id), seen=True),
    )
    assert text.split("\n") == [
        "Was steht darin?",
        f"[Attached: harbour-photo.jpg ({FOLDER}/harbour-photo.jpg, 48000 bytes; image 2048{times}1536)]",
        f"[Attached: quay-scan.pdf ({FOLDER}/quay-scan.pdf, 900000 bytes; PDF, 12 pages; pages 3, 7 have no text)]",
        f"[Attached: quay-letter.pdf ({FOLDER}/quay-letter.pdf, 70000 bytes; PDF, 5 pages; page 3 has no text)]",
        f"[Attached: letter.pdf ({FOLDER}/letter.pdf, 2000 bytes; PDF, 1 page)]",
        f"[Attached: sales.xlsx ({FOLDER}/sales.xlsx, 14 bytes)]",
        "[Attached: blurred-photo.jpg; could not be read]",
        "[Attached: old-scan.pdf; expired]",
    ]
    unseen = user_content(
        "", [photo], path_of=path_of, facts_of=lambda a: attachment_facts(a.kind, manifests.get(a.id), seen=False)
    )
    assert unseen == (
        f"[Attached: harbour-photo.jpg ({FOLDER}/harbour-photo.jpg, 48000 bytes; "
        f"image 2048{times}1536; not shown: this model does not take images)]"
    )


def _viewed(call_id: str, images: list[ToolImage], *, chars: int = 100) -> list[dict[str, Any]]:
    """Return one round of the current turn: a ``view_file`` call, its result, and the message with its images."""
    call = {"id": call_id, "type": "function", "function": {"name": "view_file", "arguments": "{}"}}
    shown, _ = tool_image_message([("view_file", call_id, images)])
    return [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call_id, "content": call_id[-1] * chars},
        shown,
    ]


def test_an_image_counts_its_estimate_against_the_budget() -> None:
    """At 280 tokens an image, room for one of two rounds' images retires the older; room for both retires none."""
    images = ImageBudget(fixed=280, max_images=8)
    current = [_user("Was zeigen die Seiten?"), *_viewed("call_0", [PAGE]), *_viewed("call_1", [CHART])]
    before = copy.deepcopy(current)
    text = _text_tokens(current)
    both = fit(SYSTEM, [], [], current, budget=text + 560, images=images)
    assert refs(both) == refs(current) and both[1:] == current
    one = fit(SYSTEM, [], [], current, budget=text + 559, images=images)
    assert one[4]["content"][1] == note(current[3]["content"][1], RETIRED)
    assert one[4]["content"][1]["text"] == (
        "[Image: quay-scan.pdf, page 2; not shown again to make room, view it again with view_file]"
    )
    assert one[-1] == current[-1] and len(refs(one)) == 1
    assert estimate_tokens(_labelled(one)) + 280 <= text + 559
    assert [m["content"] for m in one if m["role"] == "tool"] == ["0" * 100, "1" * 100]
    assert current == before


def test_tool_images_retire_oldest_first_before_results_are_cleared() -> None:
    """Short by one image, the earlier round's image retires; short by more, the newest is unseen, then results go."""
    images = ImageBudget(fixed=280, max_images=8)
    current = [
        _user("Was zeigen die Seiten?"),
        *_viewed("call_0", [PAGE], chars=3_000),
        *_viewed("call_1", [CHART], chars=3_000),
    ]
    text = _text_tokens(current)
    short_by_one = fit(SYSTEM, [], [], current, budget=text + 560 - 1, images=images)
    assert short_by_one[4]["content"][1] == note(current[3]["content"][1], RETIRED)
    assert short_by_one[-1] == current[-1]
    assert [m["content"] for m in short_by_one if m["role"] == "tool"] == ["0" * 3_000, "1" * 3_000]
    short_by_more = fit(SYSTEM, [], [], current, budget=text - 100, images=images)
    assert refs(short_by_more) == []
    assert short_by_more[4]["content"][1] == note(current[3]["content"][1], RETIRED)
    assert short_by_more[-1]["content"][1] == note(current[-1]["content"][1], UNSEEN)
    assert short_by_more[-1]["content"][1]["text"] == (
        "[Image: plot.png; not shown, there is no room for it in this request]"
    )
    assert [m["content"] for m in short_by_more if m["role"] == "tool"] == [CLEARED, "1" * 3_000]
    assert estimate_tokens(short_by_more) <= text - 100


def test_the_image_cap_holds_across_the_request() -> None:
    """At most three: an earlier round's image retires, the question's last gives way, the newest round's two stay."""
    photos = (_photo(1, "harbour-photo.jpg"), _photo(2, "quay-photo.jpg"))
    question = {"role": "user", "content": [_text("Vergleiche die Bilder"), *attachment_parts(photos, fixed=None)]}
    current: list[dict[str, Any]] = [question, *_viewed("call_0", [PAGE]), *_viewed("call_1", [CHART, PAGE])]
    pier = _photo(3, "pier-photo.jpg")
    past = [PastTurn("Zeig das Foto", (), "Ein Hafen.", False, (pier,))]
    messages = fit(SYSTEM, [], past, current, images=ImageBudget(fixed=None, max_images=3))
    assert [p["key"] for p in refs(messages)] == ["att:" + "1" * 32 + ":1/1", "tool:call_1:1", "tool:call_1:2"]
    assert messages[-4]["content"][1] == note(current[3]["content"][1], RETIRED)
    assert messages[3]["content"][2] == note(question["content"][2], OVER_CAP, max=3)
    assert messages[1] == {"role": "user", "content": "Zeig das Foto\n" + NOT_RESENT.format(label="pier-photo.jpg")}
    assert NOT_RESENT.format(label="pier-photo.jpg") == "[Image: pier-photo.jpg; not shown again, view_file shows it]"
    crowded = {"role": "user", "content": [_text("Vergleiche"), *attachment_parts((*photos, pier), fixed=None)]}
    over = fit(SYSTEM, [], [], [crowded], images=ImageBudget(fixed=None, max_images=2))
    assert over[1]["content"] == [*crowded["content"][:3], note(crowded["content"][3], OVER_CAP, max=2)]
    assert over[1]["content"][3]["text"] == (
        "[Image: pier-photo.jpg; not shown, at most 2 images go to the model at once; view_file shows it]"
    )
    assert len(crowded["content"]) == 4 and len(refs([crowded])) == 3


def test_the_newest_tool_images_stay_when_the_question_fills_the_cap() -> None:
    """Four large pictures at 280 fill the cap of 8 with strips: a view_file image stays, the last picture gives way.

    Short of tokens after that, the newest tool image gives way as unseen, before any result is cleared.
    """
    images = ImageBudget(fixed=280, max_images=8)
    photos = tuple(_photo(n, f"pier-{n}.jpg", width=1600, height=1200) for n in range(1, 5))
    question = {"role": "user", "content": [_text("Vergleiche die Fotos"), *attachment_parts(photos, fixed=280)]}
    current: list[dict[str, Any]] = [question, *_viewed("call_0", [PAGE])]
    assert len(refs(current)) == 9
    capped = fit(SYSTEM, [], [], current, images=images)
    assert [p["key"] for p in refs(capped)] == [
        *(f"att:{n}{n * 31}:{strip}/2" for n in "123" for strip in (1, 2)),
        "tool:call_0:1",
    ]
    assert capped[1]["content"][7:] == [note(part, OVER_CAP, max=8) for part in question["content"][7:]]
    assert capped[1]["content"][8]["text"] == (
        "[Image: pier-4.jpg, strip 2 of 2; not shown, at most 8 images go to the model at once; view_file shows it]"
    )
    budget = estimate_tokens(_labelled(capped)) + 7 * 280 - 1
    short = fit(SYSTEM, [], [], current, budget=budget, images=images)
    assert short[-1]["content"][1] == note(current[-1]["content"][1], UNSEEN)
    assert short[1] == capped[1] and len(refs(short)) == 6
    assert [m["content"] for m in short if m["role"] == "tool"] == ["0" * 100]
    assert estimate_tokens(_labelled(short)) + 6 * 280 <= budget


def test_a_picture_gives_way_whole() -> None:
    """A two-strip attachment past the cap gives way with both strips; so does an earlier round's page in strips."""
    small, large = _photo(1, "harbour-photo.jpg"), _photo(2, "site-plan.png", width=2048, height=1536)
    question = {"role": "user", "content": [_text("Vergleiche"), *attachment_parts((small, large), fixed=280)]}
    capped = fit(SYSTEM, [], [], [question], images=ImageBudget(fixed=280, max_images=2))
    assert capped[1]["content"] == [
        *question["content"][:2],
        *(note(part, OVER_CAP, max=2) for part in question["content"][2:]),
    ]
    assert [p["key"] for p in refs(capped)] == ["att:" + "1" * 32 + ":1/1"]
    top = ToolImage("page 3, strip 1 of 2", 1131, 820, "image/jpeg", TINY_JPEG)
    bottom = ToolImage("page 3, strip 2 of 2", 1131, 820, "image/jpeg", TINY_JPEG)
    current = [_user("Und Seite 3?"), *_viewed("call_0", [top, bottom]), *_viewed("call_1", [CHART])]
    fitted = fit(SYSTEM, [], [], current, images=ImageBudget(fixed=280, max_images=2))
    assert fitted[4]["content"][1:] == [note(part, RETIRED) for part in current[3]["content"][1:]]
    assert [p["key"] for p in refs(fitted)] == ["tool:call_1:1"]


def test_fit_refuses_images_without_an_image_budget() -> None:
    """Refs in the current turn need an image budget: without one they would neither count nor give way."""
    question = {
        "role": "user",
        "content": [_text("Was zeigt das?"), *attachment_parts([_photo(1, "a.jpg")], fixed=None)],
    }
    with pytest.raises(ValueError, match="no image budget"):
        fit(SYSTEM, [], [], [question])
    assert len(refs(fit(SYSTEM, [], [], [question], images=ImageBudget(None, max_images=8)))) == 1


def test_a_long_list_of_pages_without_text_is_cut() -> None:
    """At most ten pages are named, then how many more; a scan whose pages all lack text says so."""
    assert attachment_facts("document", _pdf_manifest(40, list(range(3, 18))), seen=True) == (
        "PDF, 40 pages; pages 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, and 5 more have no text"
    )
    assert attachment_facts("document", _pdf_manifest(12, list(range(1, 11))), seen=True) == (
        "PDF, 12 pages; pages 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 have no text"
    )
    assert attachment_facts("document", _pdf_manifest(300, list(range(1, 301))), seen=True) == (
        "PDF, 300 pages; no page has text"
    )
    assert attachment_facts("document", _pdf_manifest(2, [1, 2]), seen=True) == "PDF, 2 pages; no page has text"
    assert attachment_facts("document", _pdf_manifest(1, [1]), seen=True) == "PDF, 1 page; page 1 has no text"


def test_earlier_turns_replay_with_their_images_while_budget_and_cap_allow() -> None:
    """Newest first, a turn goes with its images, else with notes; a turn that fits neither way ends the replay."""
    oldest = PastTurn("alt: " + "Größe " * 3_000, (), "kurz", False, (_photo(4, "old-photo.jpg"),))
    newest = PastTurn("Zeig die Fotos", (), "Ein Hafen.", False, (_photo(1, "harbour-photo.jpg"), _photo(2, "kai.jpg")))
    middle = PastTurn("Und das?", (), "Ein Kai.", False, (_photo(3, "pier-photo.jpg"),))
    by_cap = fit(
        SYSTEM, [], [oldest, middle, newest], [_user("Weiter")], budget=4_000, images=ImageBudget(280, max_images=2)
    )
    assert [m["content"] for m in by_cap if m["role"] == "user"] == [
        "Und das?\n[Image: pier-photo.jpg; not shown again, view_file shows it]",
        [_text("Zeig die Fotos"), *attachment_parts(newest.images, fixed=280)],
        "Weiter",
    ]
    plan = PastTurn("Und der Plan?", (), "Ein Plan.", False, (_photo(5, "site-plan.png", width=2048, height=1536),))
    small = PastTurn("Zeig das Foto", (), "Ein Hafen.", False, (_photo(1, "harbour-photo.jpg"),))
    by_budget = fit(
        SYSTEM, [], [oldest, plan, small], [_user("Weiter")], budget=3_000, images=ImageBudget(None, max_images=8)
    )
    assert [m["content"] for m in by_budget if m["role"] == "user"] == [
        "Und der Plan?\n[Image: site-plan.png; not shown again, view_file shows it]",
        [_text("Zeig das Foto"), *attachment_parts(small.images, fixed=None)],
        "Weiter",
    ]
    assert estimate_tokens(_labelled(by_budget)) + 4 <= 3_000


def test_without_an_image_budget_nothing_changes() -> None:
    """Without ``images`` an earlier turn's images are not replayed; with it, messages without images fit as before."""
    past = [_past_turn(user=f"{n}: " + "Größe " * 700, answer="Übersicht " * 400) for n in range(30)]
    current = [_user("Fasse zusammen"), *_round_with_results(3, chars=12_000)]
    plain = fit(SYSTEM, TOOLS, past, current, budget=9_000)
    assert fit(SYSTEM, TOOLS, past, current, budget=9_000, images=ImageBudget(None, max_images=8)) == plain
    pictured = [dataclasses.replace(turn, images=(_photo(1, "harbour-photo.jpg"),)) for turn in past]
    assert fit(SYSTEM, TOOLS, pictured, current, budget=9_000) == plain
    assert refs(plain) == [] and CLEARED in json.dumps(plain)


def test_the_store_lists_ready_images_of_a_turn(store: ChatStore, clock: Clock) -> None:
    """Only a ready, unexpired picture with a rendition is listed; not one processing, failed or expired, nor a PDF."""

    def attach(name: str, kind: str, status: str, manifest: dict[str, Any] | None = None) -> str:
        clock.now += timedelta(seconds=1)
        path = f"{FOLDER}/{name}"
        out = store.add_attachment(
            chat.id,
            file_id=name[0] * 32,
            path=path,
            name=name,
            size=64,
            mime="image/jpeg",
            kind=kind,
            status="processing",
        )
        if status != "processing":
            failure = "unreadable_file" if status == "failed" else None
            store.settle_attachment(out.id, status="failed" if failure else "ready", manifest=manifest, failure=failure)
        return out.id

    chat = store.create_chat("user-a")
    clock.now = T0 - 7 * DAY
    old = attach("old-photo.jpg", "image", "ready", stock_manifest(f"{FOLDER}/old-photo.jpg"))
    clock.now = T0
    ready = attach("harbour-photo.jpg", "image", "ready", stock_manifest(f"{FOLDER}/harbour-photo.jpg"))
    waiting = attach("quay-photo.jpg", "image", "processing")
    broken = attach("blurred-photo.jpg", "image", "failed")
    scan = attach("scan.pdf", "document", "ready", stock_manifest(f"{FOLDER}/scan.pdf"))
    first = store.add_user_message("user-a", chat.id, "Was zeigen sie?", [old, ready, waiting, broken, scan])
    times = "\N{MULTIPLICATION SIGN}"
    photo = AttachedImage("h" * 32, f"{FOLDER}/harbour-photo.jpg", "harbour-photo.jpg", 64, 48)
    seen = store.user_turn(chat.id, first.id, seen=True)
    assert seen == UserTurn(
        "\n".join(
            [
                "Was zeigen sie?",
                "[Attached: old-photo.jpg; expired]",
                f"[Attached: harbour-photo.jpg ({FOLDER}/harbour-photo.jpg, 64 bytes; image 64{times}48)]",
                f"[Attached: quay-photo.jpg ({FOLDER}/quay-photo.jpg, 64 bytes; image)]",
                "[Attached: blurred-photo.jpg; could not be read]",
                f"[Attached: scan.pdf ({FOLDER}/scan.pdf, 64 bytes; PDF, 3 pages; page 2 has no text)]",
            ]
        ),
        (photo,),
        0,
    )
    unseen = store.user_turn(chat.id, first.id)
    assert unseen.images == (photo,) and f"image 64{times}48; not shown: this model does not take images" in unseen.text
    store.finish_assistant_message(store.start_assistant_message(chat.id), "Einen Hafen.")
    store.add_user_message("user-a", chat.id, "Und weiter?", [])
    [turn], expired = store.past_turns(chat.id, before_position=2, seen=True)
    assert (turn, expired) == (PastTurn(seen.text, (), "Einen Hafen.", True, (photo,)), True)
    clock.now = T0 + 8 * DAY
    [gone], _ = store.past_turns(chat.id, before_position=2, seen=True)
    assert gone.images == () and "[Attached: harbour-photo.jpg; expired]" in gone.user
