"""The agent loop: rounds, tool calls within the caps, the wall time, the audit order, stops and failures.

A hand clock stands for the monotonic clock; the stop's waits pass on it. Every name, id and value
here is invented.
"""

import base64
import dataclasses
import itertools
import json
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import openai
import pytest
from typing_extensions import override

from ghostwriter.agent.context import estimate_tokens
from ghostwriter.agent.images import LEFT_OUT, LEGEND, NOT_LOADED, RETIRED, UNSEEN
from ghostwriter.agent.limits import (
    BUSY_RETRY_S,
    MAX_ANSWER_TOKENS,
    PROMPT_TOKENS,
    REASONING_ANSWER_TOKENS,
    REASONING_PROMPT_TOKENS,
    prompt_tokens,
)
from ghostwriter.agent.loop import (
    AgentLoop,
    ErrorEvent,
    NoticeEvent,
    ResultEvent,
    StepEvent,
    TokenEvent,
    Turn,
    TurnEvent,
)
from ghostwriter.agent.prompt import PRESENTATIONS, VISION
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.inference import ModelClient
from ghostwriter.store import ChatStore
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.vision import Vision
from ghostwriter.workbench import (
    CallFor,
    OutputFile,
    RenditionRequest,
    TablePreview,
    ToolFailure,
    ToolImage,
    ToolResult,
    chat_folder,
)
from tests.conftest import KEY, make_settings
from tests.fakes import (
    TINY_JPEG,
    Chunk,
    FakeOpenAI,
    FakeWorkbench,
    RecordingAudit,
    Round,
    call_chunk,
    contract_tools,
    finish_chunk,
    stock_manifest,
    text_chunk,
    usage_chunk,
)

T0 = datetime(2026, 11, 16, tzinfo=UTC)
CHAT = "c0ffee00-0000-4000-8000-000000000001"
FOLDER = chat_folder(KEY, CHAT)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
READ_SALES = json.dumps({"path": f"{FOLDER}/sales.xlsx"})
RESULT = ToolResult('{"text": "Monat;Summe\\n2026-01;1200"}', ())
BUSY_TEXT = "workbench is busy; tell the user to try again in a minute."
CUT_TEXT = (
    "the call to write_presentation was cut off at the answer's length limit; make the arguments shorter "
    "(fewer or shorter slides, rows or text) and call it again. If you cannot call it again in this message, "
    "tell the user the deck was too long for one call and offer fewer or shorter slides."
)
HALF_DECK = '{"name": "deck", "slides": [{"title": "Q1", "bullets": ["a'
#: How a round ends at its cap: as OpenAI says, and as vLLM does once a call's name streamed.
AT_THE_CAP = {
    "length": [finish_chunk("length")],
    "vllm": [finish_chunk("tool_calls"), usage_chunk(900, MAX_ANSWER_TOKENS, "served-model")],
}
PHOTO = "harbour-photo.jpg"
PHOTO_PATH = f"{FOLDER}/{PHOTO}"
SCAN_PATH = f"{FOLDER}/quay-scan.pdf"
VIEW_SCAN = json.dumps({"path": SCAN_PATH})
TIMES = "\N{MULTIPLICATION SIGN}"
JPEG_URL = "data:image/jpeg;base64," + base64.b64encode(TINY_JPEG).decode("ascii")


class StoreClock:
    """The store's time of day, set by hand.

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


class HandClock:
    """The monotonic clock, set by hand.

    Attributes:
        t: The seconds it reads.
    """

    def __init__(self) -> None:
        """Start at 0."""
        self.t = 0.0

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The seconds it is set to.
        """
        return self.t


class HandStop(threading.Event):
    """A turn's stop whose waits pass on the hand clock instead of in real time."""

    def __init__(self, clock: HandClock) -> None:
        """Bind the stop to its clock.

        Args:
            clock: The clock a wait moves on.
        """
        super().__init__()
        self._clock = clock

    @override
    def wait(self, timeout: float | None = None) -> bool:
        """Wait on the hand clock: a wait the stop does not end moves the clock by its timeout.

        Args:
            timeout: The seconds to wait.

        Returns:
            Whether the stop is set.
        """
        if not self.is_set() and timeout is not None:
            self._clock.t += timeout
        return self.is_set()


@dataclass
class LoopEnv:
    """A chat with a scripted router, workbench and audit, and the loop over them.

    Attributes:
        settings: The settings.
        store: The chat store.
        fake: The router.
        workbench: workbench.
        audit: The turn's audit.
        clock: The monotonic clock.
        cancel: The turn's stop.
        today: The store's time of day.
        answer_id: The answer the last run wrote.
    """

    settings: Settings
    store: ChatStore
    fake: FakeOpenAI
    workbench: FakeWorkbench
    audit: RecordingAudit
    clock: HandClock
    cancel: HandStop
    today: StoreClock
    answer_id: str = ""

    def run(
        self,
        *rounds: Round,
        content: str = "Summe der Spalte B",
        workbench: FakeWorkbench | None | str = "default",
        reasoning: bool = False,
        vision: bool = False,
        attach: Sequence[tuple[str, dict[str, Any]]] = (),
    ) -> list[TurnEvent]:
        """Send a message and run its turn to the end.

        Args:
            *rounds: The router's rounds, in order.
            content: The user's message.
            workbench: workbench, the env's own by default; ``None`` for none at all.
            reasoning: The message's reasoning switch.
            vision: Whether the model sees images.
            attach: The files the message sends, by name and manifest, each settled ``ready`` first:
                a manifest of kind ``image`` makes a picture, any other a document.

        Returns:
            The turn's events.
        """
        self.fake.script = list(rounds)
        chosen = self.workbench if workbench == "default" else workbench
        ids: list[str] = []
        for name, manifest in attach:
            picture = manifest.get("kind") == "image"
            attachment = self.store.add_attachment(
                CHAT,
                file_id=uuid4().hex,
                path=f"{FOLDER}/{name}",
                name=name,
                size=4_096,
                mime="image/jpeg" if picture else "application/pdf",
                kind="image" if picture else "document",
                status="processing",
            )
            self.store.settle_attachment(attachment.id, status="ready", manifest=manifest, failure=None)
            ids.append(attachment.id)
        user = self.store.add_user_message("user-a", CHAT, content, ids)
        self.answer_id = self.store.start_assistant_message(CHAT)
        loop = AgentLoop(
            model=ModelClient(self.settings, client=self.fake.as_client()),
            workbench=chosen.as_client() if isinstance(chosen, FakeWorkbench) else None,
            store=self.store,
            settings=self.settings,
            clock=self.clock,
            now=lambda: T0,
            vision=Vision(dataclasses.replace(self.settings, vision=True)) if vision else None,
        )
        headers = {"X-Request-ID": "req-0001"}
        turn = Turn(
            chat_id=CHAT,
            owner="user-a",
            user_message_id=user.id,
            assistant_message_id=self.answer_id,
            call=CallFor("user-a", CHAT, headers),
            request_id="req-0001",
            cancel=self.cancel,
            reasoning=reasoning,
        )
        return list(loop.run(turn, self.audit))

    def stored(self) -> str:
        """Return the stored text of the last answer."""
        return self.store.chat_detail("user-a", CHAT).messages[-1].content

    def sent(self, round: int) -> list[dict[str, Any]]:
        """Return the messages a round sent to the model, 1-based."""
        return self.fake.calls[round - 1]["messages"]


@pytest.fixture
def loop_env(tmp_path: Path) -> Iterator[LoopEnv]:
    """Yield a fresh chat of ``user-a`` with the loop's neighbours scripted."""
    settings = make_settings(tmp_path, text_model="chat-model")
    engine = make_engine(settings.db_path)
    init_db(engine)
    today = StoreClock()
    store = ChatStore(engine, file_days=settings.file_days, notice_days=30, now=today)
    store.create_chat("user-a", id=CHAT)
    audit = RecordingAudit()
    clock = HandClock()
    yield LoopEnv(settings, store, FakeOpenAI(), FakeWorkbench(audit=audit), audit, clock, HandStop(clock), today)
    engine.dispose()


def _calls(*arguments: str, name: str = "read_document") -> list[dict[str, Any]]:
    """Return one round's chunks calling a tool once per arguments."""
    return [call_chunk(i, f"call_{i}", name, raw) for i, raw in enumerate(arguments)]


def _steps(events: list[TurnEvent]) -> list[StepEvent]:
    return [e for e in events if isinstance(e, StepEvent)]


def _file(name: str, mime: str, size: int, *, id: str, preview: TablePreview | None = None) -> OutputFile:
    return OutputFile(id, f"{FOLDER}/out/{name}", name, mime, size, preview)


def _router_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://router.test/v1/chat/completions"))


def _then(effect: Callable[[], object], answer: ToolResult | ToolFailure) -> Callable[[], ToolResult | ToolFailure]:
    """Return a call's answer that first has an effect, as time passing or a stop while it runs."""

    def answered() -> ToolResult | ToolFailure:
        effect()
        return answer

    return answered


def _at(clock: HandClock, t: float) -> Callable[[], None]:
    def move() -> None:
        clock.t = t

    return move


def test_a_turn_reads_calls_and_answers(loop_env: LoopEnv) -> None:
    """A round asks for a tool, workbench runs it, the next round answers; the step's frames share one id."""
    loop_env.workbench.answers = [RESULT]
    events = loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42"), usage_chunk(900, 12, "served-model")])
    assert [type(e).__name__ for e in events] == ["StepEvent", "StepEvent", "TokenEvent"]
    running, done = _steps(events)
    assert (running.step.status, running.step.detail, running.step.tool) == ("running", "sales.xlsx", "read_document")
    assert (done.step.status, done.step.detail) == ("ok", "sales.xlsx")
    assert running.step.id == done.step.id and done.step.result == RESULT.text
    assert events[2] == TokenEvent("Summe: 42")
    assert loop_env.stored() == "Summe: 42"
    [sent] = loop_env.workbench.calls
    assert (sent.name, sent.arguments, sent.timeout_s) == ("read_document", {"path": f"{FOLDER}/sales.xlsx"}, 150.0)
    assert sent.call == CallFor("user-a", CHAT, {"X-Request-ID": "req-0001"})
    assert [m["role"] for m in loop_env.sent(2)] == ["system", "user", "assistant", "tool"]
    assert loop_env.sent(2)[2]["tool_calls"] == [
        {"id": "call_0", "type": "function", "function": {"name": "read_document", "arguments": READ_SALES}}
    ]
    stored = loop_env.store.chat_detail("user-a", CHAT).messages[-1].steps
    assert [(s.id, s.status, s.result) for s in stored] == [(done.step.id, "ok", RESULT.text)]


def test_a_call_marks_the_chat_before_it_is_sent(loop_env: LoopEnv) -> None:
    """While a call runs, the chat is already one that used workbench, so its delete asks workbench."""
    seen: list[set[str]] = []
    loop_env.workbench.answers = [_then(lambda: seen.append(loop_env.store.workbench_chats([CHAT])), RESULT)]
    assert loop_env.store.workbench_chats([CHAT]) == set()
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")])
    assert seen == [{CHAT}]


def test_outputs_become_result_cards(loop_env: LoopEnv) -> None:
    """Each new output is a card: a small raster image inline, a table with its preview, the rest as files."""
    preview = TablePreview(("Monat", "Summe"), (("2026-01", "1200"),), 12, True)
    png = _file("plot.png", "image/png", 2048, id="a" * 32)
    outputs = (
        png,
        _file("sums.xlsx", XLSX, 48_213, id="b" * 32, preview=preview),
        _file("bericht.docx", DOCX, 9_000, id="c" * 32),
        _file("gross.png", "image/png", 11 * 1024 * 1024, id="d" * 32),
        png,
        _file("roh.csv", "text/csv", 300, id="e" * 32),
    )
    loop_env.workbench.answers = [ToolResult('{"stdout": ""}', outputs)]
    events = loop_env.run(_calls('{"code": "plot()"}', name="run_python"), [text_chunk("Fertig.")])
    cards = [e.result for e in events if isinstance(e, ResultEvent)]
    assert [c.kind for c in cards] == ["image", "table", "file", "file", "file"]
    assert [c.file_id for c in cards] == ["a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32]
    assert cards[1].preview is not None and cards[1].preview.rows == [["2026-01", "1200"]]
    assert [type(e).__name__ for e in events[:2]] == ["StepEvent", "StepEvent"]
    stored = loop_env.store.chat_detail("user-a", CHAT).messages[-1].results
    assert {r.id for r in stored} == {c.id for c in cards}


def test_the_cards_of_a_message_are_capped(loop_env: LoopEnv) -> None:
    """At most 20 cards per message, counted over all its calls; the model still reads every call's text."""
    outputs = tuple(_file(f"teil-{n}.docx", DOCX, 100, id=f"{n:032x}") for n in range(15))
    more = tuple(_file(f"mehr-{n}.docx", DOCX, 100, id=f"{n + 100:032x}") for n in range(15))
    loop_env.workbench.answers = [ToolResult("erste", outputs), ToolResult("zweite", more)]
    events = loop_env.run(_calls('{"code": "a()"}', '{"code": "b()"}', name="run_python"), [text_chunk("Fertig.")])
    assert len([e for e in events if isinstance(e, ResultEvent)]) == 20
    assert [m["content"] for m in loop_env.sent(2) if m["role"] == "tool"] == ["erste", "zweite"]


def test_the_model_reads_workbenchs_text_and_never_a_preview(loop_env: LoopEnv) -> None:
    """The tool message is workbench's text; the preview goes to the card only."""
    preview = TablePreview(("month", "sum"), (("2026-01", "1200"),), 12, True)
    text = json.dumps({"path": f"{FOLDER}/out/sums.xlsx", "rows": 12, "outputs": [{"name": "sums.xlsx"}]})
    loop_env.workbench.answers = [ToolResult(text, (_file("sums.xlsx", XLSX, 48_213, id="b" * 32, preview=preview),))]
    loop_env.run(_calls('{"name": "sums.xlsx", "rows": []}', name="write_table"), [text_chunk("Fertig.")])
    assert loop_env.sent(2)[-1] == {"role": "tool", "tool_call_id": "call_0", "content": text}
    assert all("2026-01" not in json.dumps(call["messages"]) for call in loop_env.fake.calls)


def test_a_refused_call_is_an_error_step_the_model_reads(loop_env: LoopEnv) -> None:
    """Workbench's refusal is a step in error, and the model reads its token and text."""
    loop_env.workbench.answers = [ToolFailure("not_completed", "the code did not finish", True)]
    events = loop_env.run(_calls('{"code": "while True: pass"}', name="run_python"), [text_chunk("Das ging nicht.")])
    assert [s.step.status for s in _steps(events)] == ["running", "error"]
    assert loop_env.sent(2)[-1]["content"] == '{"error":"not_completed","detail":"the code did not finish"}'


def test_a_busy_workbench_is_tried_once_more(loop_env: LoopEnv) -> None:
    """A busy answer is disclosed and sent again after the pause; the second answer counts."""
    loop_env.workbench.answers = [ToolFailure("queue_full", "busy, retry later", True), RESULT]
    events = loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")])
    assert [s.step.status for s in _steps(events)] == ["running", "ok"]
    assert len(loop_env.workbench.calls) == 2 and loop_env.clock.t == BUSY_RETRY_S
    assert loop_env.audit.log == [
        "model:1:ok",
        "disclose:read_document:1:1",
        "disclose:read_document:1:2",
        "model:2:ok",
    ]
    assert loop_env.workbench.sent_after == ["disclose:read_document:1:1", "disclose:read_document:1:2"]


def test_a_workbench_busy_twice_tells_the_model_to_say_so(loop_env: LoopEnv) -> None:
    """A second busy answer ends the call: an error step, and the model reads the busy text."""
    loop_env.workbench.answers = [
        ToolFailure("wait_timeout", "busy, retry later", True),
        ToolFailure("executor_busy", "busy, retry later", True),
    ]
    events = loop_env.run(_calls(READ_SALES), [text_chunk("workbench ist ausgelastet.")])
    assert [s.step.status for s in _steps(events)] == ["running", "error"]
    assert len(loop_env.workbench.calls) == 2
    assert loop_env.sent(2)[-1]["content"] == json.dumps(
        {"error": "executor_busy", "detail": BUSY_TEXT}, separators=(",", ":")
    )


def test_a_busy_workbench_is_not_tried_again_without_the_time_for_it(loop_env: LoopEnv) -> None:
    """With 151 s of the wall left, 149 after the pause, a retry could outlast its 150 s: none is sent."""
    busy = ToolFailure("queue_full", "busy, retry later", True)
    loop_env.workbench.answers = [_then(_at(loop_env.clock, 149.0), busy)]
    events = loop_env.run(_calls(READ_SALES), [text_chunk("Später.")])
    assert len(loop_env.workbench.calls) == 1 and loop_env.clock.t == 149.0
    assert [s.step.status for s in _steps(events)] == ["running", "error"]
    assert loop_env.audit.log == ["model:1:ok", "disclose:read_document:1:1", "model:2:ok"]


def test_a_stop_during_the_busy_pause_sends_nothing_more(loop_env: LoopEnv) -> None:
    """The pause ends at a stop: no retry, the step is kept, and no round follows."""
    busy = ToolFailure("queue_full", "busy, retry later", True)
    loop_env.workbench.answers = [_then(loop_env.cancel.set, busy)]
    events = loop_env.run(_calls(READ_SALES), [text_chunk("nie")])
    assert len(loop_env.workbench.calls) == 1 and len(loop_env.fake.calls) == 1
    assert [s.step.status for s in _steps(events)] == ["running", "error"]


def test_unknown_tools_and_bad_arguments_never_reach_workbench(loop_env: LoopEnv) -> None:
    """A tool not offered, or arguments that are not an object, are refused to the model; workbench hears nothing."""
    chunks = [call_chunk(0, "call_a", "delete_everything", "{}"), call_chunk(1, "call_b", "read_document", "[1]")]
    events = loop_env.run(chunks, [text_chunk("Das kann ich nicht.")])
    assert loop_env.workbench.calls == [] and _steps(events) == []
    assistant, unknown, bad = loop_env.sent(2)[-3:]
    names = "fill_template, inspect_template, list_files, read_document, run_python, search_files, "
    names += "write_document, write_presentation, write_table"
    assert json.loads(unknown["content"]) == {
        "error": "unknown_tool",
        "detail": f"there is no tool named 'delete_everything'; the tools you can call are {names}.",
    }
    assert json.loads(bad["content"]) == {
        "error": "bad_arguments",
        "detail": "the arguments for read_document must be a JSON object.",
    }
    assert [c["function"]["arguments"] for c in assistant["tool_calls"]] == ["{}", "{}"]
    assert loop_env.audit.log == ["model:1:ok", "model:2:ok"]


def test_a_call_cut_at_the_length_limit_says_so(loop_env: LoopEnv) -> None:
    """A call the stream cut at the answer's length limit is not sent; the model reads that it was cut."""
    cut = [call_chunk(0, "call_0", "write_presentation", HALF_DECK), finish_chunk("length")]
    events = loop_env.run(cut, [text_chunk("Ich fasse mich kürzer.")])
    assert loop_env.workbench.calls == [] and _steps(events) == []
    assistant, answer = loop_env.sent(2)[-2:]
    assert json.loads(answer["content"]) == {"error": "cut_arguments", "detail": CUT_TEXT}
    assert [c["function"]["arguments"] for c in assistant["tool_calls"]] == ["{}"]
    assert loop_env.audit.log == ["model:1:ok", "model:2:ok"]
    first = loop_env.audit.rounds[0]
    assert first is not None and first.finish_reason == "length" and len(first.calls) == 1


@pytest.mark.parametrize(
    ("reasoning", "completion", "error"),
    [
        (False, MAX_ANSWER_TOKENS, "cut_arguments"),
        (True, REASONING_ANSWER_TOKENS, "cut_arguments"),
        (False, MAX_ANSWER_TOKENS - 1, "bad_arguments"),
        (True, MAX_ANSWER_TOKENS, "bad_arguments"),
    ],
    ids=["at-the-cap", "reasoning-at-its-cap", "below-the-cap", "reasoning-below-its-cap"],
)
def test_a_call_cut_at_the_cap_is_named_cut_when_vllm_says_tool_calls(
    loop_env: LoopEnv, reasoning: bool, completion: int, error: str
) -> None:
    """VLLM ends a round whose call's name streamed with ``tool_calls``, at the cap too: the usage tells the cut.

    The round's own cap counts: a reasoning round's is the larger, its thinking included.
    """
    chunks = [
        call_chunk(0, "call_0", "write_presentation", HALF_DECK),
        finish_chunk("tool_calls"),
        usage_chunk(900, completion, "served-model"),
    ]
    loop_env.run(chunks, [text_chunk("Ich fasse mich kürzer.")], reasoning=reasoning)
    assert loop_env.workbench.calls == []
    assert json.loads(loop_env.sent(2)[-1]["content"])["error"] == error


@pytest.mark.parametrize("ending", AT_THE_CAP.values(), ids=AT_THE_CAP.keys())
def test_a_whole_last_call_of_a_round_at_the_cap_still_runs(loop_env: LoopEnv, ending: list[Chunk]) -> None:
    """The cap reached right after a call's last brace leaves it whole: it is sent."""
    loop_env.workbench.answers = [RESULT]
    loop_env.run([call_chunk(0, "call_0", "read_document", READ_SALES), *ending], [text_chunk("Summe: 42")])
    assert [(c.name, c.arguments) for c in loop_env.workbench.calls] == [("read_document", json.loads(READ_SALES))]
    assert loop_env.audit.log == ["model:1:ok", "disclose:read_document:1:1", "model:2:ok"]


@pytest.mark.parametrize("raw", ["", " \n"], ids=["empty", "blank"])
@pytest.mark.parametrize("ending", AT_THE_CAP.values(), ids=AT_THE_CAP.keys())
def test_a_last_call_cut_right_after_its_name_is_named_cut(loop_env: LoopEnv, ending: list[Chunk], raw: str) -> None:
    """At the cap, a last call without arguments was cut after its name: it is refused as cut, never sent as ``{}``."""
    loop_env.run([call_chunk(0, "call_0", "write_presentation", raw), *ending], [text_chunk("Ich fasse mich kürzer.")])
    assert loop_env.workbench.calls == []
    assert json.loads(loop_env.sent(2)[-1]["content"]) == {"error": "cut_arguments", "detail": CUT_TEXT}


def test_a_call_without_arguments_below_the_cap_is_sent(loop_env: LoopEnv) -> None:
    """Below the cap, no arguments are no arguments: the call goes out with ``{}``."""
    loop_env.workbench.answers = [RESULT]
    chunks = [
        call_chunk(0, "call_0", "list_files", ""),
        finish_chunk("tool_calls"),
        usage_chunk(900, 40, "served-model"),
    ]
    loop_env.run(chunks, [text_chunk("Ok")])
    assert [(c.name, c.arguments) for c in loop_env.workbench.calls] == [("list_files", {})]


def test_only_the_last_call_of_a_round_cut_at_the_limit_is_named_cut(loop_env: LoopEnv) -> None:
    """An earlier call of that round was written whole and runs; one with bad arguments there is not called cut."""
    chunks = [
        call_chunk(0, "call_0", "read_document", READ_SALES),
        call_chunk(1, "call_1", "read_document", "[1]"),
        call_chunk(2, "call_2", "run_python", '{"code": "x = 1'),
        finish_chunk("length"),
    ]
    loop_env.workbench.answers = [RESULT]
    loop_env.run(chunks, [text_chunk("Ich fasse mich kürzer.")])
    assert [c.name for c in loop_env.workbench.calls] == ["read_document"]
    answers = loop_env.sent(2)[-3:]
    assert [json.loads(a["content"]).get("error") for a in answers] == [None, "bad_arguments", "cut_arguments"]


def test_a_round_that_ends_normally_keeps_bad_arguments_bad(loop_env: LoopEnv) -> None:
    """Without the length limit, and with no usage to show the cap, half-written arguments are the generic refusal."""
    loop_env.run(
        [call_chunk(0, "call_0", "run_python", '{"code": "x = 1'), finish_chunk("tool_calls")], [text_chunk("Ok")]
    )
    assert json.loads(loop_env.sent(2)[-1]["content"])["error"] == "bad_arguments"


def test_the_loop_sets_presentations_from_the_offered_tools(loop_env: LoopEnv) -> None:
    """Only a workbench that offers write_presentation gets the outline-first rule in the prompt."""
    loop_env.run([text_chunk("Hallo!")])
    system = loop_env.sent(1)[0]["content"]
    assert PRESENTATIONS in system and f"This chat's files are in {FOLDER};" in system
    without = tuple(tool for tool in contract_tools() if tool.name != "write_presentation")
    loop_env.run([text_chunk("Hallo!")], workbench=FakeWorkbench(tools=without, audit=loop_env.audit))
    assert "write_presentation" not in loop_env.fake.calls[-1]["messages"][0]["content"]


def test_a_call_id_the_record_cannot_hold_is_refused(loop_env: LoopEnv) -> None:
    """An id past 64 characters, or with other characters, never reaches workbench, the store or the audit.

    The call is refused as bad arguments, and the round is answered under an id the loop mints.
    """
    long_id, odd_id = "x" * 65, "call 1\nAnmerkung"
    chunks = [
        call_chunk(0, long_id, "read_document", READ_SALES),
        call_chunk(1, odd_id, "read_document", READ_SALES),
        call_chunk(2, "call_ok-1", "read_document", READ_SALES),
    ]
    loop_env.workbench.answers = [RESULT]
    events = loop_env.run(chunks, [text_chunk("Summe: 42")])
    assert len(loop_env.workbench.calls) == 1 and len(_steps(events)) == 2
    assert loop_env.audit.log == ["model:1:ok", "disclose:read_document:1:1", "model:2:ok"]
    assistant, *answers = loop_env.sent(2)[-4:]
    ids = [call["id"] for call in assistant["tool_calls"]]
    assert ids[2] == "call_ok-1" and len(set(ids)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", i) for i in ids)
    assert [answer["tool_call_id"] for answer in answers] == ids
    assert [json.loads(answer["content"]).get("error") for answer in answers[:2]] == ["bad_arguments"] * 2
    [turn], _ = loop_env.store.past_turns(CHAT, before_position=99)
    assert [call.call_id for calls in turn.rounds for call in calls] == ["call_ok-1"]


def test_a_call_id_with_dots_and_colons_is_kept(loop_env: LoopEnv) -> None:
    """Some models name a call ``functions.<tool>:<n>``: it is sent, stored and answered under that id."""
    loop_env.workbench.answers = [RESULT]
    events = loop_env.run(
        [call_chunk(0, "functions.read_document:0", "read_document", READ_SALES)], [text_chunk("Summe: 42")]
    )
    assert len(loop_env.workbench.calls) == 1 and len(_steps(events)) == 2
    assistant, answer = loop_env.sent(2)[-2:]
    assert [c["id"] for c in assistant["tool_calls"]] == ["functions.read_document:0"]
    assert answer["tool_call_id"] == "functions.read_document:0"
    [turn], _ = loop_env.store.past_turns(CHAT, before_position=99)
    assert [call.call_id for calls in turn.rounds for call in calls] == ["functions.read_document:0"]


def test_at_most_four_calls_run_per_round(loop_env: LoopEnv) -> None:
    """The first four calls of a round run; the rest are refused, to be asked again in the next round."""
    loop_env.run(_calls(*[json.dumps({"path": f"{FOLDER}/teil-{n}.pdf"}) for n in range(6)]), [text_chunk("Fertig.")])
    assert [c.arguments["path"] for c in loop_env.workbench.calls] == [f"{FOLDER}/teil-{n}.pdf" for n in range(4)]
    refused = [json.loads(m["content"]) for m in loop_env.sent(2) if m["role"] == "tool"][4:]
    limit = "at most 4 tool calls run per round; call it again in your next round."
    assert refused == [{"error": "call_limit", "detail": limit}] * 2


def test_after_eight_rounds_the_model_must_answer(loop_env: LoopEnv) -> None:
    """Eight rounds may call tools; the ninth is offered none."""
    rounds: list[Round] = [_calls(READ_SALES) for _ in range(8)]
    loop_env.run(*rounds, [text_chunk("Summe: 42")])
    assert ["tools" in call for call in loop_env.fake.calls] == [True] * 8 + [False]
    assert len(loop_env.workbench.calls) == 8 and loop_env.stored() == "Summe: 42"


def test_a_round_offered_no_tools_ends_the_turn(loop_env: LoopEnv) -> None:
    """A round offered no tools is the last, even if it asks for a call anyway."""
    rounds: list[Round] = [_calls(READ_SALES) for _ in range(9)]
    loop_env.run(*rounds)
    assert len(loop_env.fake.calls) == 9 and len(loop_env.workbench.calls) == 8


def test_tools_stop_at_the_wall_time(loop_env: LoopEnv) -> None:
    """At 295 s a call is refused unsent, and the next round is offered no tools."""
    loop_env.audit.on_model = lambda round: _at(loop_env.clock, 295.0)() if round == 1 else None
    events = loop_env.run(_calls(READ_SALES), [text_chunk("Keine Zeit mehr.")])
    assert loop_env.workbench.calls == [] and _steps(events) == []
    no_time = "not run: the time for tools in this answer is used up; answer with what you have."
    assert json.loads(loop_env.sent(2)[-1]["content"]) == {"error": "no_time", "detail": no_time}
    assert "tools" not in loop_env.fake.calls[1]


@pytest.mark.parametrize(("elapsed", "offered"), [(285.0, True), (295.0, False)])
def test_tools_are_offered_until_ten_seconds_before_the_wall(loop_env: LoopEnv, elapsed: float, offered: bool) -> None:
    """A round is offered tools while at least 10 s of the 300 s wall remain."""
    loop_env.workbench.answers = [_then(_at(loop_env.clock, elapsed), RESULT)]
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")])
    assert ("tools" in loop_env.fake.calls[1]) is offered


@pytest.mark.parametrize(("elapsed", "sent"), [(150.0, True), (151.0, False)])
def test_a_call_starts_only_with_its_timeout_left(loop_env: LoopEnv, elapsed: float, sent: bool) -> None:
    """A call is sent only while its 150 s fit in the wall; otherwise tools are withdrawn for the message."""
    loop_env.audit.on_model = lambda round: _at(loop_env.clock, elapsed)() if round == 2 else None
    loop_env.run(_calls(READ_SALES), _calls(READ_SALES), [text_chunk("Summe: 42")])
    assert len(loop_env.workbench.calls) == (2 if sent else 1)
    assert ("tools" in loop_env.fake.calls[2]) is sent
    if not sent:
        assert json.loads(loop_env.sent(3)[-1]["content"])["error"] == "no_time"


def test_each_call_is_disclosed_before_it_is_sent(loop_env: LoopEnv) -> None:
    """The disclosure is written before workbench hears the call, and each round is recorded after it."""
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")])
    assert loop_env.audit.log == ["model:1:ok", "disclose:read_document:1:1", "model:2:ok"]
    assert loop_env.workbench.sent_after == ["disclose:read_document:1:1"]
    first, second = loop_env.audit.rounds
    assert first is not None and [c.name for c in first.calls] == ["read_document"]
    assert second is not None and second.text == "Summe: 42"


def test_a_failed_disclosure_sends_nothing(loop_env: LoopEnv) -> None:
    """A disclosure that cannot be written stops the turn before workbench hears it; the text so far is kept."""
    loop_env.audit.failing.add("disclose")
    with pytest.raises(AuditWriteError):
        loop_env.run([text_chunk("Ich lese."), *_calls(READ_SALES)])
    assert loop_env.workbench.calls == [] and loop_env.stored() == "Ich lese."


def test_a_failed_model_record_ends_the_turn_and_keeps_the_text(loop_env: LoopEnv) -> None:
    """A round that cannot be recorded raises after the text is stored; no call follows."""
    loop_env.audit.failing.add("model")
    with pytest.raises(AuditWriteError):
        loop_env.run([text_chunk("Ich lese."), *_calls(READ_SALES)])
    assert loop_env.workbench.calls == [] and loop_env.stored() == "Ich lese."


@pytest.mark.parametrize("workbench", [None, FakeWorkbench(tools=())])
def test_without_workbench_no_tool_is_offered(loop_env: LoopEnv, workbench: FakeWorkbench | None) -> None:
    """With no workbench, or none usable, a chat still answers: no tools, and the prompt says so."""
    events = loop_env.run([text_chunk("Hallo!")], workbench=workbench)
    assert events == [TokenEvent("Hallo!")]
    assert "tools" not in loop_env.fake.calls[0]
    assert "You have no tools in this chat" in loop_env.sent(1)[0]["content"]
    assert "/workspace/" not in loop_env.sent(1)[0]["content"]


def test_with_workbench_the_prompt_names_the_chats_folder(loop_env: LoopEnv) -> None:
    """The system prompt names this chat's folder, and discovery asks for this chat."""
    loop_env.run([text_chunk("Hallo!")])
    assert f"This chat's files are in {FOLDER};" in loop_env.sent(1)[0]["content"]
    assert loop_env.workbench.discoveries == [CallFor("user-a", CHAT, {"X-Request-ID": "req-0001"})]


@pytest.mark.parametrize(
    ("second", "tokens"),
    [
        (_router_error(), []),
        ([text_chunk("Ich rechne."), _router_error()], [TokenEvent("\n\n"), TokenEvent("Ich rechne.")]),
    ],
)
def test_a_model_failure_ends_with_an_error_event(loop_env: LoopEnv, second: Round, tokens: list[TokenEvent]) -> None:
    """A refused or broken round ends the turn with the model error; what was shown is kept."""
    events = loop_env.run([text_chunk("Ich lese."), *_calls(READ_SALES)], second)
    assert events[-1] == ErrorEvent("model", "The model did not answer.")
    assert [e for e in events if isinstance(e, TokenEvent)] == [TokenEvent("Ich lese."), *tokens]
    assert loop_env.audit.log[-1] == "model:2:error" and loop_env.audit.rounds[-1] is None
    assert loop_env.stored() == "".join(t.text for t in [TokenEvent("Ich lese."), *tokens])


def test_an_unexpected_failure_ends_with_an_internal_error_and_keeps_the_text(loop_env: LoopEnv) -> None:
    """Anything else that breaks a turn is an internal error event; the loop raises nothing but the audit's error."""

    def broken() -> ToolResult:
        raise RuntimeError("synthetic fault")

    loop_env.workbench.answers = [broken]
    events = loop_env.run([text_chunk("Ich lese."), *_calls(READ_SALES)])
    assert isinstance(events[-1], ErrorEvent) and events[-1].code == "internal"
    assert "synthetic" not in events[-1].message and loop_env.stored() == "Ich lese."


def test_a_stop_ends_the_turn_after_the_call_in_flight(loop_env: LoopEnv) -> None:
    """A stop while a call runs keeps that call's step; no further call and no further round runs."""
    loop_env.workbench.answers = [_then(loop_env.cancel.set, RESULT)]
    events = loop_env.run(_calls(READ_SALES, json.dumps({"path": f"{FOLDER}/b.pdf"})), [text_chunk("nie")])
    assert len(loop_env.workbench.calls) == 1 and len(loop_env.fake.calls) == 1
    assert [s.step.status for s in _steps(events)] == ["running", "ok"]
    assert [s.status for s in loop_env.store.chat_detail("user-a", CHAT).messages[-1].steps] == ["ok"]


def test_a_round_that_breaks_after_a_stop_ends_the_turn_as_stopped(loop_env: LoopEnv) -> None:
    """A stalled round that fails once the stop is set is no model error; it is recorded as a failed round."""

    def stalled() -> Chunk:
        loop_env.cancel.set()
        raise _router_error()

    events = loop_env.run([text_chunk("Ich lese."), stalled])
    assert events == [TokenEvent("Ich lese.")]
    assert loop_env.audit.log == ["model:1:error"] and loop_env.stored() == "Ich lese."


def test_no_round_starts_once_the_turn_is_stopped(loop_env: LoopEnv) -> None:
    """A stop that came first sends nothing to the model; the answer stays empty."""
    loop_env.cancel.set()
    assert loop_env.run([text_chunk("nie")]) == []
    assert loop_env.fake.calls == [] and loop_env.stored() == ""
    assert loop_env.workbench.discoveries == []


def test_the_stored_answer_is_what_was_streamed(loop_env: LoopEnv) -> None:
    """Texts of two rounds are joined by a blank line, as streamed; the stored answer is exactly that."""
    events = loop_env.run([text_chunk("Ich lese."), *_calls(READ_SALES)], [text_chunk("Fertig.")])
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert tokens == ["Ich lese.", "\n\n", "Fertig."]
    assert loop_env.stored() == "Ich lese.\n\nFertig."
    assert loop_env.sent(2)[2] == {
        "role": "assistant",
        "content": "Ich lese.",
        "tool_calls": [
            {"id": "call_0", "type": "function", "function": {"name": "read_document", "arguments": READ_SALES}}
        ],
    }


def test_a_follow_up_reads_the_earlier_turn_with_its_tool_results(loop_env: LoopEnv) -> None:
    """The next message replays the earlier question, its call and result, and its answer."""
    loop_env.workbench.answers = [RESULT]
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")])
    loop_env.run([text_chunk("Im Vorjahr: 40")], content="Und im Vorjahr?")
    sent = loop_env.sent(3)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sent[3] == {"role": "tool", "tool_call_id": "call_0", "content": RESULT.text}
    assert (sent[4]["content"], sent[5]["content"]) == ("Summe: 42", "Und im Vorjahr?")


def test_a_reasoning_message_writing_long_documents_stays_within_its_budget(loop_env: LoopEnv) -> None:
    """Three rounds each writing a document of 30 000 characters: every round's prompt fits 26 000."""
    rounds: list[Round] = []
    for n in range(3):
        markdown = f"# Fassung {n}\n" + "Absatz. " * 3_750
        rounds.append(_calls(json.dumps({"name": f"bericht-{n}.docx", "markdown": markdown}), name="write_document"))
    loop_env.run(*rounds, [text_chunk("Der Bericht steht.")], reasoning=True)
    assert len(loop_env.fake.calls) == 4 and len(loop_env.workbench.calls) == 3
    for call in loop_env.fake.calls:
        assert estimate_tokens(call["messages"]) + estimate_tokens(call["tools"]) <= REASONING_PROMPT_TOKENS
    written = [m["tool_calls"][0]["function"]["arguments"] for m in loop_env.sent(4) if m.get("tool_calls")]
    assert [json.loads(arguments)["name"] for arguments in written] == [f"bericht-{n}.docx" for n in range(3)]
    assert len(written[0]) <= 2_000 and len(written[2]) > 30_000


def test_arguments_that_cannot_be_encoded_are_refused(loop_env: LoopEnv) -> None:
    """An unpaired surrogate in the arguments is refused unsent and unstored; the chat still opens and goes on."""
    events = loop_env.run(_calls('{"path": "a\\ud800.pdf"}'), [text_chunk("Das ging nicht.")])
    assert loop_env.workbench.calls == [] and _steps(events) == []
    assistant, refused = loop_env.sent(2)[-2:]
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert json.loads(refused["content"])["error"] == "bad_arguments"
    loop_env.store.chat_detail("user-a", CHAT).model_dump_json()
    loop_env.run([text_chunk("Gern.")], content="Dann lies sales.xlsx")
    assert len(loop_env.fake.calls) == 3
    for call in loop_env.fake.calls:
        json.dumps(call["messages"], ensure_ascii=False).encode("utf-8")


def test_lone_surrogates_from_the_stream_are_replaced(loop_env: LoopEnv) -> None:
    """A stray surrogate in a text delta, a call's id or its name becomes U+FFFD; the next round still goes out.

    An id holding U+FFFD is no id the record takes: that call is refused and answered under one the loop mints.
    """
    loop_env.workbench.answers = [RESULT]
    first = [
        text_chunk("Ich lese\ud800."),
        call_chunk(0, "call_\udc00", "read_document", READ_SALES),
        call_chunk(1, "call_1", "read\ud800", "{}"),
    ]
    events = loop_env.run(first, [text_chunk("Summe: 42, Größe ✓ 😀")])
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert tokens == ["Ich lese\ufffd.", "\n\n", "Summe: 42, Größe ✓ 😀"]
    assert loop_env.stored() == "".join(tokens)
    assert len(loop_env.fake.calls) == 2 and loop_env.workbench.calls == []
    sent = loop_env.sent(2)
    minted, kept = (c["id"] for c in sent[2]["tool_calls"])
    assert re.fullmatch(r"call_[0-9a-f]{32}", minted) and kept == "call_1"
    assert [m["tool_call_id"] for m in sent[3:]] == [minted, kept]
    assert sent[2]["tool_calls"][1]["function"]["name"] == "read\ufffd"
    json.dumps(sent, ensure_ascii=False).encode("utf-8")
    loop_env.store.chat_detail("user-a", CHAT).model_dump_json()


def test_a_fault_while_building_the_context_is_an_internal_error(
    loop_env: LoopEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round whose messages cannot be built never reaches the router and is no model error."""

    def broken(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        raise ValueError("synthetic fault")

    monkeypatch.setattr("ghostwriter.agent.loop.fit", broken)
    events = loop_env.run([text_chunk("nie")])
    assert events == [ErrorEvent("internal", "The answer could not be finished.")]
    assert loop_env.fake.calls == [] and loop_env.audit.log == []


def test_a_reasoning_message_reasons_in_every_round_within_its_budget(loop_env: LoopEnv) -> None:
    """Every round of a reasoning message asks for thinking and a longer answer, and its prompt fits 26_000."""
    for n in range(30):
        loop_env.store.add_user_message("user-a", CHAT, f"{n}: " + "Größe " * 700, [])
        loop_env.store.finish_assistant_message(loop_env.store.start_assistant_message(CHAT), "Übersicht " * 400)
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")], reasoning=True)
    for call in loop_env.fake.calls:
        assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
        assert call["max_tokens"] == 12_000
        assert estimate_tokens(call["messages"]) + estimate_tokens(call["tools"]) <= REASONING_PROMPT_TOKENS
    reasoned = len(loop_env.sent(1))
    loop_env.run([text_chunk("Gut.")], content="Danke")
    plain = loop_env.fake.calls[-1]
    assert plain["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}} and plain["max_tokens"] == 4000
    assert REASONING_PROMPT_TOKENS < estimate_tokens(plain["messages"]) + estimate_tokens(plain["tools"])
    assert estimate_tokens(plain["messages"]) + estimate_tokens(plain["tools"]) <= PROMPT_TOKENS
    assert len(plain["messages"]) > reasoned


@pytest.mark.parametrize("reasoning", [False, True])
def test_a_smaller_context_fits_every_round_to_what_it_leaves(loop_env: LoopEnv, reasoning: bool) -> None:
    """At a context of 24 576 a long chat's rounds still replay earlier turns, within that context's budget."""
    loop_env.settings = dataclasses.replace(loop_env.settings, context_tokens=24_576)
    for n in range(30):
        loop_env.store.add_user_message("user-a", CHAT, f"{n}: " + "Größe " * 700, [])
        loop_env.store.finish_assistant_message(loop_env.store.start_assistant_message(CHAT), "Übersicht " * 400)
    loop_env.run(_calls(READ_SALES), [text_chunk("Summe: 42")], reasoning=reasoning)
    budget = prompt_tokens(24_576, reasoning=reasoning)
    for call in loop_env.fake.calls:
        assert estimate_tokens(call["messages"]) + estimate_tokens(call["tools"]) <= budget
    assert len(loop_env.sent(1)) > 3


# --- images: attached pictures, tool images, strips, the cap and a refusal -----


def _page(n: int, *, width: int = 1131, height: int = 1600, data: bytes = TINY_JPEG) -> ToolImage:
    """Return page ``n`` of the scan, as a tool shows it."""
    return ToolImage(f"quay-scan.pdf, page {n}", width, height, "image/jpeg", data)


def _shown(*pages: ToolImage) -> ToolResult:
    """Return a tool's answer that shows ``pages``."""
    return ToolResult(json.dumps({"path": SCAN_PATH, "shown": len(pages)}), (), pages)


def _url(data: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _photo(width: int = 64, height: int = 48) -> tuple[str, dict[str, Any]]:
    """Return the harbour photo as it is attached: its name, and its manifest of one rendition of that size."""
    manifest = stock_manifest(PHOTO_PATH)
    return PHOTO, {**manifest, "renditions": [{**manifest["renditions"][0], "width": width, "height": height}]}


def _content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message["content"]
    return content if isinstance(content, list) else []


def _image_urls(messages: list[dict[str, Any]]) -> list[str]:
    """Return the data URLs of messages, in order."""
    return [p["image_url"]["url"] for m in messages for p in _content(m) if p.get("type") == "image_url"]


def _labelled(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return each image's label text and its data URL, in order."""
    parts = [part for message in messages for part in _content(message)]
    return [(a["text"], b["image_url"]["url"]) for a, b in itertools.pairwise(parts) if b.get("type") == "image_url"]


def _refused() -> openai.BadRequestError:
    """Return the router's refusal of a request with images."""
    request = httpx2.Request("POST", "http://router.test/v1/chat/completions")
    return openai.BadRequestError("image refused", response=httpx2.Response(400, request=request), body=None)


def _failed_otherwise() -> dict[str, Callable[[], Round]]:
    request = httpx2.Request("POST", "http://router.test/v1/chat/completions")
    return {
        "server-error": lambda: openai.InternalServerError(
            "router down", response=httpx2.Response(500, request=request), body=None
        ),
        "after-text": lambda: [text_chunk("Ich sehe"), _refused()],
        "unreachable": _router_error,
    }


def test_an_attached_image_goes_into_the_users_message(loop_env: LoopEnv) -> None:
    """The question carries its text, then the picture's label and its data URL; its rendition is fetched once.

    view_file is offered, and no tool's schema shows ``strips``; the cached discovery keeps it.
    """
    events = loop_env.run([text_chunk("Ein Hafen.")], content="Was zeigt das Bild?", vision=True, attach=[_photo()])
    assert events == [TokenEvent("Ein Hafen.")]
    text, label, image = loop_env.sent(1)[-1]["content"]
    assert text["type"] == "text" and text["text"].startswith("Was zeigt das Bild?\n")
    assert f"[Attached: {PHOTO} ({PHOTO_PATH}, 4096 bytes; image 64{TIMES}48)]" in text["text"]
    assert label == {"type": "text", "text": f"[Image: {PHOTO}]"}
    assert image == {"type": "image_url", "image_url": {"url": JPEG_URL}}
    assert loop_env.workbench.fetched == [(RenditionRequest(PHOTO_PATH, n=1, strips=1, strip=1),)]
    assert loop_env.audit.images == [1]
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in loop_env.fake.calls[0]["tools"]}
    assert "view_file" in schemas
    for name in ("view_file", "read_document"):
        assert "strips" not in schemas[name]["properties"] and "strips" not in schemas[name]["required"]
    discovery = loop_env.workbench.discovery
    assert discovery is not None
    cached = {tool.name: tool.input_schema for tool in discovery.tools}
    assert "strips" in cached["read_document"]["properties"] and "strips" in cached["read_document"]["required"]


def test_a_fixed_image_cost_sends_two_strips_of_a_large_image(loop_env: LoopEnv) -> None:
    """At a fixed cost, a picture longer than 1024 pixels goes as two strips, each labelled and fetched."""
    loop_env.settings = dataclasses.replace(loop_env.settings, image_tokens=280)
    loop_env.run([text_chunk("Ein Hafen.")], vision=True, attach=[_photo(2048, 1536)])
    assert loop_env.sent(1)[-1]["content"][1:] == [
        {"type": "text", "text": f"[Image: {PHOTO}, strip 1 of 2]"},
        {"type": "image_url", "image_url": {"url": JPEG_URL}},
        {"type": "text", "text": f"[Image: {PHOTO}, strip 2 of 2]"},
        {"type": "image_url", "image_url": {"url": JPEG_URL}},
    ]
    assert loop_env.workbench.fetched == [
        (RenditionRequest(PHOTO_PATH, n=1, strips=2, strip=1), RenditionRequest(PHOTO_PATH, n=1, strips=2, strip=2))
    ]
    assert loop_env.audit.images == [2]


def test_tool_images_follow_the_tool_messages_in_one_user_message(loop_env: LoopEnv) -> None:
    """A round's images go in one user message after its tool messages: per call its legend, then its images.

    Each tool message names how many images were shown; the stored steps hold that same text, never the bytes.
    """
    loop_env.workbench.answers = [_shown(_page(1), _page(2)), _shown(_page(3))]
    first = [call_chunk(0, "call_0", "view_file", VIEW_SCAN), call_chunk(1, "call_1", "read_document", VIEW_SCAN)]
    loop_env.run(first, [text_chunk("Drei Seiten.")], vision=True)
    sent = loop_env.sent(2)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "tool", "user"]
    image = {"type": "image_url", "image_url": {"url": JPEG_URL}}

    def label(n: int) -> dict[str, str]:
        return {"type": "text", "text": f"[Image: quay-scan.pdf, page {n}]"}

    both = "quay-scan.pdf, page 1; quay-scan.pdf, page 2"
    assert sent[-1]["content"] == [
        {"type": "text", "text": LEGEND.format(tool="view_file", call_id="call_0", labels=both)},
        label(1),
        image,
        label(2),
        image,
        {"type": "text", "text": LEGEND.format(tool="read_document", call_id="call_1", labels="quay-scan.pdf, page 3")},
        label(3),
        image,
    ]
    view, read = sent[3]["content"], sent[4]["content"]
    assert view == json.dumps({"path": SCAN_PATH, "shown": 2}) + "\n[2 images were shown]"
    assert read == json.dumps({"path": SCAN_PATH, "shown": 1}) + "\n[1 images were shown]"
    steps = loop_env.store.chat_detail("user-a", CHAT).messages[-1].steps
    assert [step.result for step in steps] == [view, read]
    stored = json.dumps([step.model_dump(mode="json") for step in steps])
    assert base64.b64encode(TINY_JPEG).decode("ascii") not in stored and "data:image" not in stored
    assert [call.images for call in loop_env.workbench.calls] == [True, True]
    assert loop_env.audit.images == [0, 3]


def test_ghostwriter_sets_strips_and_the_model_cannot(loop_env: LoopEnv) -> None:
    """At a fixed cost a PDF is viewed in 2 strips and an attached picture by its size; the model's strips go.

    A file that is neither gets none, and without a fixed cost no call carries strips.
    """
    loop_env.settings = dataclasses.replace(loop_env.settings, image_tokens=280)
    sheet = f"{FOLDER}/sales.xlsx"
    calls = [
        call_chunk(0, "call_0", "view_file", json.dumps({"path": SCAN_PATH, "strips": 3})),
        call_chunk(1, "call_1", "view_file", json.dumps({"path": PHOTO_PATH, "strips": 3})),
        call_chunk(2, "call_2", "read_document", json.dumps({"path": sheet, "strips": 2})),
    ]
    loop_env.run(calls, [text_chunk("Ok.")], vision=True, attach=[_photo(2048, 1536)])
    assert [call.arguments for call in loop_env.workbench.calls] == [
        {"path": SCAN_PATH, "strips": 2},
        {"path": PHOTO_PATH, "strips": 2},
        {"path": sheet},
    ]
    loop_env.settings = dataclasses.replace(loop_env.settings, image_tokens=None)
    loop_env.run(calls, [text_chunk("Ok.")], vision=True)
    assert [call.arguments for call in loop_env.workbench.calls[3:]] == [
        {"path": SCAN_PATH},
        {"path": PHOTO_PATH},
        {"path": sheet},
    ]


def test_a_follow_up_replays_tool_steps_as_text_and_attached_images_again(loop_env: LoopEnv) -> None:
    """The next message sends the earlier question with its picture again, fetched anew, and its tool step as text.

    The earlier turn's tool images are not sent again: its legend message is gone.
    """
    loop_env.workbench.answers = [_shown(_page(1))]
    view = [call_chunk(0, "call_0", "view_file", VIEW_SCAN)]
    loop_env.run(view, [text_chunk("Ein Hafen.")], vision=True, attach=[_photo()])
    loop_env.run([text_chunk("Ja.")], content="Und das Wasser?", vision=True)
    sent = loop_env.sent(3)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sent[1]["content"][1:] == [
        {"type": "text", "text": f"[Image: {PHOTO}]"},
        {"type": "image_url", "image_url": {"url": JPEG_URL}},
    ]
    assert sent[3]["content"] == json.dumps({"path": SCAN_PATH, "shown": 1}) + "\n[1 images were shown]"
    assert "Images returned by" not in json.dumps(sent) and _image_urls(sent) == [JPEG_URL]
    assert len(loop_env.workbench.fetched) == 2


def test_without_vision_view_file_is_not_offered_and_tool_images_are_named(loop_env: LoopEnv) -> None:
    """A model that does not see gets no image anywhere: no view_file, no strips, a tool's images named as not shown.

    Its calls do not ask workbench for images, and an attached picture is named in the text alone, never fetched.
    """
    loop_env.settings = dataclasses.replace(loop_env.settings, image_tokens=280)
    loop_env.workbench.answers = [_shown(_page(1))]
    read = [call_chunk(0, "call_0", "read_document", json.dumps({"path": SCAN_PATH, "strips": 2}))]
    loop_env.run(read, [text_chunk("Seite 1 hat keinen Text.")], attach=[_photo()])
    assert _image_urls([m for call in loop_env.fake.calls for m in call["messages"]]) == []
    assert "image_url" not in json.dumps([call["messages"] for call in loop_env.fake.calls])
    assert loop_env.sent(2)[-1] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": json.dumps({"path": SCAN_PATH, "shown": 1})
        + "\n[1 images could not be shown: this model does not take images]",
    }
    assert "view_file" not in [tool["function"]["name"] for tool in loop_env.fake.calls[0]["tools"]]
    [sent] = loop_env.workbench.calls
    assert (sent.arguments, sent.images) == ({"path": SCAN_PATH}, False)
    assert loop_env.workbench.fetched == [] and loop_env.audit.images == [0, 0]
    question = loop_env.sent(1)[-1]["content"]
    assert isinstance(question, str) and f"image 64{TIMES}48; not shown: this model does not take images" in question
    assert VISION not in loop_env.sent(1)[0]["content"]


def test_an_expired_or_missing_image_is_noted_and_the_turn_goes_on(loop_env: LoopEnv) -> None:
    """A follow-up whose picture's rendition is gone sends a note that it could not be loaded, and answers.

    Once the picture has expired, its line says so, and nothing is fetched.
    """
    loop_env.run([text_chunk("Ein Hafen.")], vision=True, attach=[_photo()])
    loop_env.workbench.missing = {PHOTO_PATH}
    assert loop_env.run([text_chunk("Ein Kran.")], content="Und links?", vision=True) == [TokenEvent("Ein Kran.")]
    assert loop_env.sent(2)[1]["content"][1:] == [{"type": "text", "text": NOT_LOADED.format(label=PHOTO)}]
    assert len(loop_env.workbench.fetched) == 2 and loop_env.audit.images == [1, 0]
    loop_env.today.now = T0 + timedelta(days=loop_env.settings.file_days, seconds=1)
    assert loop_env.run([text_chunk("Nicht mehr.")], content="Und rechts?", vision=True) == [TokenEvent("Nicht mehr.")]
    assert loop_env.sent(3)[1]["content"] == f"Summe der Spalte B\n[Attached: {PHOTO}; expired]"
    assert len(loop_env.workbench.fetched) == 2 and loop_env.audit.images == [1, 0, 0]


def test_a_refused_image_round_is_tried_once_without_images(loop_env: LoopEnv) -> None:
    """A round refused with its images goes once more with a note in each image's place, after one notice.

    The rest of the turn goes without images: a tool's images are named as not shown, and none is sent.
    """
    loop_env.workbench.answers = [_shown(_page(1))]
    view = [call_chunk(0, "call_0", "view_file", VIEW_SCAN)]
    events = loop_env.run(_refused(), view, [text_chunk("Ohne Bild.")], vision=True, attach=[_photo()])
    assert [e for e in events if isinstance(e, NoticeEvent)] == [NoticeEvent("images_dropped")]
    assert (events[0], events[-1]) == (NoticeEvent("images_dropped"), TokenEvent("Ohne Bild."))
    second = loop_env.sent(2)
    assert second[1]["content"][1:] == [{"type": "text", "text": LEFT_OUT.format(label=PHOTO)}]
    assert "image_url" not in json.dumps(second)
    assert loop_env.audit.log[:2] == ["model:1:error", "model:1:ok"] and loop_env.audit.images[:2] == [1, 0]
    third = loop_env.sent(3)
    assert "image_url" not in json.dumps(third) and third[1]["content"] == second[1]["content"]
    assert third[-1]["role"] == "tool"
    assert third[-1]["content"].endswith("\n[1 images could not be shown: this model does not take images]")
    assert loop_env.audit.images == [1, 0, 0]


def test_a_second_refusal_is_the_model_error(loop_env: LoopEnv) -> None:
    """The round sent again without images and refused again ends the turn with the model error."""
    events = loop_env.run(_refused(), _refused(), vision=True, attach=[_photo()])
    assert events == [NoticeEvent("images_dropped"), ErrorEvent("model", "The model did not answer.")]
    assert loop_env.audit.log == ["model:1:error", "model:1:error"] and loop_env.audit.images == [1, 0]


def test_a_refusal_without_images_is_not_retried(loop_env: LoopEnv) -> None:
    """A refused round that carried no image is the model error at once: one call, no notice."""
    events = loop_env.run(_refused(), vision=True)
    assert events == [ErrorEvent("model", "The model did not answer.")]
    assert len(loop_env.fake.calls) == 1 and loop_env.audit.log == ["model:1:error"]


@pytest.mark.parametrize("failed", _failed_otherwise().values(), ids=_failed_otherwise().keys())
def test_a_round_with_images_that_failed_otherwise_is_not_retried(
    loop_env: LoopEnv, failed: Callable[[], Round]
) -> None:
    """A failure that is no refusal before any text, a server error or none from the router, is the model error."""
    events = loop_env.run(failed(), vision=True, attach=[_photo()])
    assert events[-1] == ErrorEvent("model", "The model did not answer.")
    assert not any(isinstance(e, NoticeEvent) for e in events) and len(loop_env.fake.calls) == 1


def test_the_prompt_tells_the_model_how_to_look_only_with_vision(loop_env: LoopEnv) -> None:
    """How to look at files is told right after the chat's folder while the model sees and has the tools; else not."""
    loop_env.run([text_chunk("Hallo!")], vision=True)
    loop_env.run([text_chunk("Hallo!")])
    loop_env.run([text_chunk("Hallo!")], vision=True, workbench=None)
    seeing, plain, toolless = (call["messages"][0]["content"] for call in loop_env.fake.calls)
    assert VISION in seeing and seeing.index("days after they were made.") < seeing.index(VISION)
    assert seeing.index(VISION) < seeing.index(PRESENTATIONS)
    assert VISION not in plain and VISION not in toolless


def test_tool_images_past_the_cap_are_retired(loop_env: LoopEnv) -> None:
    """With at most 2 images a request, three rounds of one image each leave the oldest as a note."""
    loop_env.settings = dataclasses.replace(loop_env.settings, max_images=2)
    loop_env.workbench.answers = [_shown(_page(n)) for n in (1, 2, 3)]
    views: list[Round] = [[call_chunk(0, f"call_{n}", "view_file", VIEW_SCAN)] for n in range(3)]
    loop_env.run(*views, [text_chunk("Drei Seiten.")], vision=True)
    fourth = loop_env.sent(4)
    assert [label for label, _ in _labelled(fourth)] == [f"[Image: quay-scan.pdf, page {n}]" for n in (2, 3)]
    assert json.dumps(fourth).count(RETIRED.format(label="quay-scan.pdf, page 1")) == 1
    assert loop_env.audit.images == [0, 1, 2, 2]


def test_a_reused_call_id_keeps_each_image_its_own(loop_env: LoopEnv) -> None:
    """A model that names a call ``call_0`` in two rounds gets two ids: each image keeps its own label and bytes.

    The id is the same in the call, its tool message, the legend and the stored step.
    """
    one, two = TINY_JPEG + b"\x01", TINY_JPEG + b"\x02"
    loop_env.workbench.answers = [_shown(_page(1, data=one)), _shown(_page(2, data=two))]
    view = [call_chunk(0, "call_0", "view_file", VIEW_SCAN)]
    loop_env.run(view, view, [text_chunk("Zwei Seiten.")], vision=True)
    third = loop_env.sent(3)
    ids = [m["tool_calls"][0]["id"] for m in third if m.get("tool_calls")]
    assert ids[0] == "call_0" and len(set(ids)) == 2 and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", ids[1])
    assert [m["tool_call_id"] for m in third if m["role"] == "tool"] == ids
    assert _labelled(third) == [
        ("[Image: quay-scan.pdf, page 1]", _url(one)),
        ("[Image: quay-scan.pdf, page 2]", _url(two)),
    ]
    legends = [p["text"] for m in third for p in _content(m) if p.get("text", "").startswith("[Images returned by")]
    assert legends == [
        LEGEND.format(tool="view_file", call_id=ids[0], labels="quay-scan.pdf, page 1"),
        LEGEND.format(tool="view_file", call_id=ids[1], labels="quay-scan.pdf, page 2"),
    ]
    [turn], _ = loop_env.store.past_turns(CHAT, before_position=99)
    assert [call.call_id for calls in turn.rounds for call in calls] == ids


def test_an_image_never_shown_is_never_said_to_be_shown_before(loop_env: LoopEnv) -> None:
    """A tool image the request had no room for stays named as never shown in later rounds, never as retired."""
    loop_env.settings = dataclasses.replace(loop_env.settings, context_tokens=24_576)
    loop_env.workbench.answers = [_shown(_page(1, width=4096, height=3072), _page(2, width=4096, height=3072)), RESULT]
    view = [call_chunk(0, "call_0", "view_file", VIEW_SCAN)]
    listing = [call_chunk(0, "call_1", "list_files", "{}")]
    loop_env.run(view, listing, [text_chunk("Seite 2.")], vision=True)
    unseen = UNSEEN.format(label="quay-scan.pdf, page 1")
    retired = RETIRED.format(label="quay-scan.pdf, page 1")
    for n in (2, 3):
        sent = loop_env.sent(n)
        assert unseen in json.dumps(sent) and retired not in json.dumps(sent)
        assert [label for label, _ in _labelled(sent)] == ["[Image: quay-scan.pdf, page 2]"]
