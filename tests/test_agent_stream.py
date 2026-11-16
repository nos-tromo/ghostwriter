"""One model round over the OpenAI SDK: text as it arrives, joined tool calls, no reasoning, a stop.

Every name, id and value here is invented.
"""

import dataclasses
import json
import re
import threading
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from typing_extensions import override

from ghostwriter.agent import limits
from ghostwriter.agent.stream import RoundDone, TextDelta, ToolCall, stream_round
from ghostwriter.config import Settings
from ghostwriter.inference import ModelClient, cache_scope_for, inference_headers
from tests.conftest import make_settings
from tests.fakes import (
    SERVED_MODEL,
    Chunk,
    FakeOpenAI,
    Round,
    call_chunk,
    reasoning_chunk,
    text_chunk,
    usage_chunk,
)

MSGS: list[dict[str, Any]] = [
    {"role": "system", "content": "You are Ghostwriter."},
    {"role": "user", "content": "Was steht in a.pdf?"},
]
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read_document", "description": "Read a document.", "parameters": SCHEMA}}
]
H = inference_headers("user-a", "req-0001")
ROUTER = "http://router.test/v1"
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
THINKING_ON = {"chat_template_kwargs": {"enable_thinking": True}}
#: The JSON body of a round offering TOOLS, before any provider's own field.
BODY = {
    "model": "chat-model",
    "messages": MSGS,
    "tools": TOOLS,
    "tool_choice": "auto",
    "stream": True,
    "stream_options": {"include_usage": True},
    "max_tokens": 4000,
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Return the test settings, with an invented chat model.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The settings.
    """
    return make_settings(tmp_path, text_model="chat-model")


def _model(settings: Settings, *rounds: Round) -> tuple[ModelClient, FakeOpenAI]:
    fake = FakeOpenAI(rounds)
    return ModelClient(settings, client=fake.as_client()), fake


def _round(
    model: ModelClient, *, tools: list[dict[str, Any]] | None = None, reasoning: bool = False
) -> list[TextDelta | RoundDone]:
    return list(stream_round(model, MSGS, tools=tools or [], headers=H, cancel=threading.Event(), reasoning=reasoning))


def _text(events: list[TextDelta | RoundDone]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def _together(*chunks: Chunk) -> Chunk:
    """Return one chunk carrying the tool-call fragments of several."""
    entries = [entry for chunk in chunks for entry in chunk["choices"][0]["delta"]["tool_calls"]]
    return {**chunks[0], "choices": [{"index": 0, "delta": {"tool_calls": entries}, "finish_reason": None}]}


def _router_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx2.Request("POST", f"{ROUTER}/chat/completions"))


def test_text_streams_as_it_arrives_and_reasoning_never_does(settings: Settings) -> None:
    """Each chunk's visible text goes out before the next chunk is read; the think block never does."""
    model, fake = _model(
        settings, [text_chunk("<think>plan</think>Hal"), text_chunk("lo"), usage_chunk(812, 40, "served-model")]
    )
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event())
    assert next(rounds) == TextDelta("Hal") and fake.streams[0].sent == 1
    events = [TextDelta("Hal"), *rounds]
    assert events[1:-1] == [TextDelta("lo")]
    assert _text(events) == "Hallo"
    assert events[-1] == RoundDone("Hallo", [], None, "served-model", 812, 40, False)
    assert fake.streams[0].closed


def test_reasoning_split_across_chunks_or_sent_apart_never_goes_out(settings: Settings) -> None:
    """A think block split across chunks is read out of the content; a separate reasoning field is never shown."""
    apart = text_chunk("")
    apart["choices"][0]["delta"] = {"reasoning_content": "Plan: erst lesen"}
    model, _ = _model(
        settings,
        [apart, text_chunk("<thi"), text_chunk("nk>Plan</th"), text_chunk("ink>Kosten: "), text_chunk("42")],
    )
    events = _round(model)
    assert events[:-1] == [TextDelta("Kosten:"), TextDelta(" 42")]
    assert events[-1] == RoundDone("Kosten: 42", [], None, SERVED_MODEL, None, None, False)


def test_a_tail_that_never_became_a_tag_goes_out_at_the_end(settings: Settings) -> None:
    """A trailing ``<`` is held while it could open a think block, and released once the reply ends."""
    model, _ = _model(settings, [text_chunk("Kleiner als: <")])
    events = _round(model)
    assert events[:-1] == [TextDelta("Kleiner als:"), TextDelta(" <")]
    assert isinstance(events[-1], RoundDone) and events[-1].text == "Kleiner als: <"


def test_with_reasoning_the_text_before_the_close_tag_is_dropped(settings: Settings) -> None:
    """A reasoning round may open inside the template's think block: held until the close tag, then dropped."""
    model, fake = _model(
        settings,
        [text_chunk("Der Nutzer fragt nach B."), text_chunk("\n</th"), text_chunk("ink>\n\nSumme: "), text_chunk("42")],
    )
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event(), reasoning=True)
    assert next(rounds) == TextDelta("Summe:") and fake.streams[0].sent == 3
    events = [TextDelta("Summe:"), *rounds]
    assert events[:-1] == [TextDelta("Summe:"), TextDelta(" 42")]
    assert events[-1] == RoundDone("Summe: 42", [], None, SERVED_MODEL, None, None, False)
    assert fake.calls[0]["max_tokens"] == 12_000


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_with_reasoning_a_separate_reasoning_field_releases_the_content(settings: Settings, field: str) -> None:
    """A router that keeps reasoning apart sends clean content: it streams as it arrives, the reasoning never does."""
    model, fake = _model(
        settings,
        [
            reasoning_chunk("Erst lesen, ", field=field),
            reasoning_chunk("dann rechnen.", field=field),
            text_chunk("Summe: "),
            text_chunk("42"),
        ],
    )
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event(), reasoning=True)
    assert next(rounds) == TextDelta("Summe:") and fake.streams[0].sent == 3
    events = [TextDelta("Summe:"), *rounds]
    assert events[:-1] == [TextDelta("Summe:"), TextDelta(" 42")]
    assert events[-1] == RoundDone("Summe: 42", [], None, SERVED_MODEL, None, None, False)


def test_with_reasoning_content_held_before_a_reasoning_field_is_the_answer(settings: Settings) -> None:
    """Content held while no close tag came is released once a reasoning field shows the router keeps them apart."""
    model, _ = _model(settings, [text_chunk("Summe: "), reasoning_chunk("nachrechnen"), text_chunk("42")])
    events = _round(model, reasoning=True)
    assert events[:-1] == [TextDelta("Summe:"), TextDelta(" 42")]


def test_with_reasoning_a_reply_without_a_close_tag_is_released_at_the_end(settings: Settings) -> None:
    """A reasoning round that never closed a think block and sent no reasoning field was the answer."""
    model, fake = _model(settings, [text_chunk("Summe: "), text_chunk("42"), usage_chunk(812, 40, SERVED_MODEL)])
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event(), reasoning=True)
    assert next(rounds) == TextDelta("Summe: 42") and fake.streams[0].sent == 3
    assert list(rounds) == [RoundDone("Summe: 42", [], None, SERVED_MODEL, 812, 40, False)]


class _SetAfter(threading.Event):
    """A stop that arrives mid-round: it reads as set from its ``checks + 1``-th check on."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self._left = checks

    @override
    def is_set(self) -> bool:
        self._left -= 1
        return self._left < 0 or super().is_set()


def test_a_stopped_reasoning_round_shows_nothing_it_held(settings: Settings) -> None:
    """Held content may be reasoning: a stop before the close tag drops it."""
    model, fake = _model(settings, [text_chunk("Der Nutzer fragt"), text_chunk(" nach B."), text_chunk("\n")])
    events = list(stream_round(model, MSGS, tools=[], headers=H, cancel=_SetAfter(2), reasoning=True))
    assert events == [RoundDone("", [], None, SERVED_MODEL, None, None, True)] and fake.streams[0].sent == 3


def test_tool_calls_are_joined_from_their_fragments(settings: Settings) -> None:
    """Two calls in one chunk both survive, and a call's argument split over chunks is joined."""
    both = _together(
        call_chunk(0, "call_a", "read_document", '{"path": "/w/a.pdf"}'),
        call_chunk(1, "call_b", "search_files", '{"query": '),
    )
    model, _ = _model(
        settings,
        [text_chunk("Ich lese."), both, call_chunk(1, None, None, '"Umsatz"}'), usage_chunk(900, 30, SERVED_MODEL)],
    )
    done = _round(model, tools=TOOLS)[-1]
    assert isinstance(done, RoundDone)
    assert done.calls == [
        ToolCall("call_a", "read_document", '{"path": "/w/a.pdf"}'),
        ToolCall("call_b", "search_files", '{"query": "Umsatz"}'),
    ]
    assert (done.text, done.input_tokens, done.output_tokens, done.stopped) == ("Ich lese.", 900, 30, False)


def test_a_round_without_tools_sends_neither_tools_nor_tool_choice(settings: Settings) -> None:
    """With no tool offered the request carries neither key: an endpoint may refuse an empty list."""
    model, fake = _model(settings, [text_chunk("Hallo")])
    _round(model, tools=[])
    assert "tools" not in fake.calls[0] and "tool_choice" not in fake.calls[0]


def test_a_round_with_tools_asks_for_usage_and_bounds_the_answer(settings: Settings) -> None:
    """The request streams, asks for the usage, offers the tools and is bounded in tokens and time."""
    model, fake = _model(settings, [text_chunk("Hallo")])
    _round(model, tools=TOOLS)
    assert fake.calls == [
        {
            "model": "chat-model",
            "messages": MSGS,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 4000,
            "timeout": 120.0,
            "extra_headers": H,
            "extra_body": THINKING_OFF,
        }
    ]


def test_a_stop_closes_the_stream(settings: Settings) -> None:
    """Once cancel is set no further chunk is read or shown, no half-sent call is asked for, the stream closes."""
    model, fake = _model(
        settings,
        [
            call_chunk(0, "call_a", "read_document", '{"pa'),
            text_chunk("Ich lese"),
            text_chunk(" weiter"),
            call_chunk(0, None, None, 'th": "/w/a.pdf"}'),
            usage_chunk(812, 40, SERVED_MODEL),
        ],
    )
    cancel = threading.Event()
    rounds = stream_round(model, MSGS, tools=TOOLS, headers=H, cancel=cancel)
    assert next(rounds) == TextDelta("Ich lese")
    cancel.set()
    assert list(rounds) == [RoundDone("Ich lese", [], None, SERVED_MODEL, None, None, True)]
    assert fake.streams[0].closed and fake.streams[0].sent == 3


def test_a_round_left_unread_closes_its_stream(settings: Settings) -> None:
    """A consumer that drops the round, as a closed tab does, still releases the reply."""
    model, fake = _model(settings, [text_chunk("Hal"), text_chunk("lo")])
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event())
    assert isinstance(rounds, Generator) and next(rounds) == TextDelta("Hal")
    rounds.close()
    assert fake.streams[0].closed and fake.streams[0].sent == 1


def test_the_models_errors_propagate(settings: Settings) -> None:
    """A refused request raises before anything goes out; a reply cut off raises mid-round and is closed."""
    timeout = openai.APITimeoutError(request=httpx2.Request("POST", f"{ROUTER}/chat/completions"))
    model, fake = _model(settings, _router_error(), [text_chunk("Hal"), timeout])
    with pytest.raises(openai.APIConnectionError):
        next(stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event()))
    rounds = stream_round(model, MSGS, tools=[], headers=H, cancel=threading.Event())
    assert next(rounds) == TextDelta("Hal")
    with pytest.raises(openai.APITimeoutError):
        next(rounds)
    assert len(fake.calls) == 2 and fake.streams[0].closed


def test_every_round_carries_the_cache_scope_and_request_id(settings: Settings) -> None:
    """The round names the user's cache scope, a keyed pseudonym, and the request id; nothing else."""
    model, fake = _model(settings, [text_chunk("Hallo")])
    _round(model)
    assert fake.calls[0]["extra_headers"] == {"X-Cache-Scope": cache_scope_for("user-a"), "X-Request-ID": "req-0001"}
    assert cache_scope_for("user-a") != cache_scope_for("user-b") and re.fullmatch(
        r"[0-9a-f]{64}", cache_scope_for("user-a")
    )
    assert cache_scope_for("user-a") == cache_scope_for("user-a") and "user-a" not in cache_scope_for("user-a")


def _over_the_wire(
    settings: Settings, *, reasoning: bool = False, reply: Sequence[Chunk] = (text_chunk("Hal"), text_chunk("lo"))
) -> tuple[list[TextDelta | RoundDone], httpx2.Request]:
    """Run one round offering TOOLS through the real SDK over a mock transport."""
    seen: list[httpx2.Request] = []
    finish = text_chunk("")
    finish["choices"][0] |= {"delta": {}, "finish_reason": "stop"}
    chunks = [*reply, finish, usage_chunk(812, 40, SERVED_MODEL)]
    sse = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    client = openai.OpenAI(
        base_url=ROUTER,
        api_key="router-key",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(answer)),
    )
    events = _round(ModelClient(settings, client=client), tools=TOOLS, reasoning=reasoning)
    [request] = seen
    return events, request


def test_the_sdk_sends_the_round_to_the_router_as_built(settings: Settings) -> None:
    """Through the real SDK: one POST to chat/completions with the headers, body and timeout, its SSE read back."""
    events, request = _over_the_wire(settings)
    assert events == [TextDelta("Hal"), TextDelta("lo"), RoundDone("Hallo", [], "stop", SERVED_MODEL, 812, 40, False)]
    assert (request.method, str(request.url)) == ("POST", f"{ROUTER}/chat/completions")
    assert (
        request.headers["x-cache-scope"] == cache_scope_for("user-a") and request.headers["x-request-id"] == "req-0001"
    )
    assert request.headers["authorization"] == "Bearer router-key"
    assert request.extensions["timeout"]["read"] == 120.0
    assert json.loads(request.content) == {**BODY, **THINKING_OFF}


@pytest.mark.parametrize(
    ("provider", "reasoning", "extra", "max_tokens"),
    [
        ("vllm", False, THINKING_OFF, 4000),
        ("vllm", True, THINKING_ON, 12000),
        ("ollama", False, {}, 4000),
        ("ollama", True, {}, 12000),
        ("openai", True, {}, 12000),
    ],
)
def test_on_vllm_the_reasoning_switch_reaches_the_chat_template(
    settings: Settings, provider: str, reasoning: bool, extra: dict[str, Any], max_tokens: int
) -> None:
    """On vllm the body sets the template's thinking to the switch, as docint does; other providers get nothing.

    A reasoning round may answer longer, whatever the provider.
    """
    _, request = _over_the_wire(dataclasses.replace(settings, inference_provider=provider), reasoning=reasoning)
    assert json.loads(request.content) == {**BODY, "max_tokens": max_tokens, **extra}


def test_the_sdk_hands_over_a_separate_reasoning_field_and_it_is_dropped(settings: Settings) -> None:
    """Through the real SDK: a ``reasoning_content`` delta is read as the sign of clean content, and never shown."""
    reply = (reasoning_chunk("Erst lesen."), text_chunk("Hal"), text_chunk("lo"))
    events, request = _over_the_wire(settings, reasoning=True, reply=reply)
    assert events == [TextDelta("Hal"), TextDelta("lo"), RoundDone("Hallo", [], "stop", SERVED_MODEL, 812, 40, False)]
    assert json.loads(request.content)["chat_template_kwargs"] == {"enable_thinking": True}


def test_stray_surrogates_become_replacement_characters_and_valid_text_is_unchanged(settings: Settings) -> None:
    """A lone surrogate in the content, a call's id or name, or the served model's name is U+FFFD."""
    reply = [
        text_chunk("Größe ✓ 😀 \ud83d"),
        call_chunk(0, "call_\udfff", "read\ud800", "{}"),
        usage_chunk(812, 40, "served\udc00"),
    ]
    model, _ = _model(settings, reply)
    events = _round(model, tools=TOOLS)
    assert _text(events) == "Größe ✓ 😀 \ufffd"
    done = events[-1]
    assert isinstance(done, RoundDone)
    assert done.calls == [ToolCall("call_\ufffd", "read\ufffd", "{}")] and done.model_id == "served\ufffd"


def test_an_escaped_surrogate_from_the_router_is_replaced(settings: Settings) -> None:
    """Through the real SDK: the router's escaped U+D800 arrives as a lone surrogate and goes out as U+FFFD."""
    reply = (text_chunk("Hal\ud800lo"), call_chunk(0, "call_\ud800", "read_document", "{}"))
    events, _ = _over_the_wire(settings, reply=reply)
    assert _text(events) == "Hal\ufffdlo"
    done = events[-1]
    assert isinstance(done, RoundDone) and done.calls == [ToolCall("call_\ufffd", "read_document", "{}")]


def test_the_client_goes_to_the_router_once_per_round(settings: Settings) -> None:
    """Built from the settings, the client calls OPENAI_API_BASE with its key and never retries a round."""
    client = ModelClient(settings).client
    assert str(client.base_url) == "http://vllm-router:4000/v1/" and client.api_key == settings.openai_api_key
    assert client.max_retries == 0


def test_the_prompt_and_the_answer_fit_the_context() -> None:
    """The caps are the plan's; a full prompt plus a full answer fit the 40k context."""
    assert (limits.CONTEXT_TOKENS, limits.PROMPT_TOKENS, limits.MAX_ANSWER_TOKENS) == (40_000, 34_000, 4_000)
    assert limits.PROMPT_TOKENS + limits.MAX_ANSWER_TOKENS <= limits.CONTEXT_TOKENS
    assert (limits.REASONING_PROMPT_TOKENS, limits.REASONING_ANSWER_TOKENS) == (26_000, 12_000)
    assert limits.REASONING_PROMPT_TOKENS + limits.REASONING_ANSWER_TOKENS <= limits.CONTEXT_TOKENS
    assert (limits.CHARS_PER_TOKEN, limits.MODEL_TIMEOUT_S) == (3, 120.0)
    assert (limits.MAX_TOOL_ROUNDS, limits.MAX_CALLS_PER_ROUND) == (8, 4)
    assert (limits.MESSAGE_WALL_S, limits.TOOL_CUTOFF_S, limits.BUSY_RETRY_S) == (300.0, 10.0, 2.0)
    assert (limits.TOOL_RESULT_CHARS, limits.HISTORY_ARGUMENT_CHARS, limits.HISTORY_RESULT_CHARS) == (
        12_000,
        2_000,
        2_000,
    )
    assert (limits.MAX_RESULTS_PER_MESSAGE, limits.INLINE_IMAGE_BYTES) == (20, 10 * 1024 * 1024)
    assert limits.IMAGE_MIMES == {"image/png", "image/jpeg", "image/gif", "image/webp"}
