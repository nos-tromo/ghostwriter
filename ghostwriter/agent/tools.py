"""workbench's tools as the model sees them, and what it reads back.

The model is offered workbench's MCP tools as OpenAI functions. Its arguments are read as
smaller models write them: nothing is no arguments, a JSON string holding an object is decoded
once, anything else is refused to the model. What it reads back is bounded: a result's text
(which workbench writes without the table previews) cut to ``TOOL_RESULT_CHARS``, or a small
JSON object naming why there is none. A result that showed images says how many it returned,
or that they could not be shown; which of them went to the model, the note in each one's place
says. The texts here are the model-facing protocol.

Ghostwriter sets the arguments of :data:`HIDDEN_ARGUMENTS` itself: their schemas are left out of
what the model is offered, and whatever it sends for them is dropped.
"""

import json
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from ghostwriter.agent.limits import IMAGE_MIMES, INLINE_IMAGE_BYTES, TOOL_RESULT_CHARS
from ghostwriter.workbench import McpTool, OutputFile, ToolFailure, ToolResult

#: What the model reads when a call is not run, or workbench was busy.
REFUSALS: Final = {
    "unknown_tool": "there is no tool named {name!r}; the tools you can call are {names}.",
    "bad_arguments": "the arguments for {name} must be a JSON object.",
    "call_limit": "at most 4 tool calls run per round; call it again in your next round.",
    "no_time": "not run: the time for tools in this answer is used up; answer with what you have.",
    "busy": "workbench is busy; tell the user to try again in a minute.",
}
#: What the model reads when the length limit cut the call it was writing: the arguments are not whole.
CUT_ARGUMENTS: Final = (
    "the call to {tool} was cut off at the answer's length limit; make the arguments shorter "
    "(fewer or shorter slides, rows or text) and call it again. If you cannot call it again in this message, "
    "tell the user the deck was too long for one call and offer fewer or shorter slides."
)
#: workbench's tokens for a call it could not take on now; such a call is tried once more.
BUSY_TOKENS: Final = frozenset({"queue_full", "wait_timeout", "executor_busy"})
#: What ends a text that was cut.
CUT_MARK: Final = "…[cut]"
#: The deepest objects and lists may nest in arguments, as docint allows.
MAX_ARGUMENT_DEPTH: Final = 32
#: The longest detail a step shows in the trail.
DETAIL_CHARS: Final = 120
#: The most of a failure's text the model reads.
FAILURE_CHARS: Final = 500
#: Arguments Ghostwriter sets itself, never offered to the model.
HIDDEN_ARGUMENTS: Final = frozenset({"strips"})
#: The tools whose ``strips`` Ghostwriter sets, by the file a call reads.
STRIPPED_TOOLS: Final = frozenset({"view_file", "read_document"})
#: What ends a result whose images go to the model, as far as the request has room (the note of each says).
SHOWN: Final = "[{n} images were returned]"
#: What ends a result whose images could not go to the model.
NOT_SHOWN: Final = "[{n} images could not be shown: this model does not take images]"


@dataclass(frozen=True)
class Refusal:
    """A call the loop did not send.

    Attributes:
        token: Why, as a token: a key of :data:`REFUSALS`.
        text: What the model reads about it.
    """

    token: str
    text: str


def compact(value: Any) -> str:
    """Return a value as compact JSON, its text unescaped.

    Args:
        value: A JSON-compatible value.

    Returns:
        The JSON.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def cut(text: str, limit: int) -> str:
    """Cut a text to at most ``limit`` characters, marking the cut.

    Args:
        text: The text.
        limit: The most characters it may keep, the mark included.

    Returns:
        The text, or its start and :data:`CUT_MARK`.
    """
    return text if len(text) <= limit else text[: max(0, limit - len(CUT_MARK))] + CUT_MARK


def _without(schema: Mapping[str, Any], hidden: frozenset[str]) -> dict[str, Any]:
    """Return a copy of an input schema without some of its arguments.

    Args:
        schema: The schema; never changed, as discovery caches it.
        hidden: The arguments to leave out.

    Returns:
        The copy, without them in ``properties`` or in ``required``.
    """
    copy = dict(schema)
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        copy["properties"] = {key: value for key, value in properties.items() if key not in hidden}
    required = schema.get("required")
    if isinstance(required, list):
        copy["required"] = [name for name in required if not (isinstance(name, str) and name in hidden)]
    return copy


def function_tools(tools: Sequence[McpTool], *, hidden: frozenset[str] = HIDDEN_ARGUMENTS) -> list[dict[str, Any]]:
    """Offer workbench's tools as OpenAI functions.

    Args:
        tools: The tools discovery found.
        hidden: The arguments Ghostwriter sets itself, left out of every schema.

    Returns:
        One function per tool, in order, a copy of its input schema without ``hidden`` as the parameters.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _without(tool.input_schema, hidden),
            },
        }
        for tool in tools
    ]


def _nested_too_deeply(value: Any) -> bool:
    """Tell whether objects and lists nest deeper than :data:`MAX_ARGUMENT_DEPTH`, without recursion.

    Args:
        value: Parsed arguments.

    Returns:
        Whether they do.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_ARGUMENT_DEPTH:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def _read(raw: str) -> tuple[dict[str, Any] | None, bool]:
    """Read a call's arguments.

    Args:
        raw: The arguments as the model wrote them.

    Returns:
        The object, or ``None``; and whether ``raw`` itself is that object's JSON.
    """
    if not raw.strip():
        return {}, False
    try:
        value = json.loads(raw)
        direct = isinstance(value, dict)
        if isinstance(value, str):
            value = json.loads(value)
    except (ValueError, RecursionError):
        return None, False
    if not isinstance(value, dict) or _nested_too_deeply(value):
        return None, False
    try:
        compact(value).encode("utf-8")
    except UnicodeEncodeError:
        return None, False
    return value, direct


def parse_arguments(raw: str) -> dict[str, Any] | None:
    """Read a call's arguments as smaller models write them.

    Args:
        raw: The arguments as the model wrote them.

    Returns:
        ``{}`` for none, the object, or the object a JSON string holds (decoded once); ``None``
        for anything else, an object nested deeper than :data:`MAX_ARGUMENT_DEPTH` or one that
        holds an unpaired surrogate (it cannot be encoded to be sent, stored or shown) included.
    """
    return _read(raw)[0]


def echoed_arguments(raw: str) -> str:
    """Return a call's arguments as the conversation repeats them to the model.

    Chat templates parse an earlier call's arguments as JSON, so anything but an object would
    fail the next round; the call's own tool message says what was wrong.

    Args:
        raw: The arguments as the model wrote them.

    Returns:
        ``raw`` when it is an object, the object a string held as compact JSON, else ``{}``.
    """
    value, direct = _read(raw)
    if value is None:
        return "{}"
    return raw if direct else compact(value)


def step_detail(tool: str, arguments: Mapping[str, Any]) -> str | None:
    """Name what a step worked on, for the trail.

    Every tool names its subject by the same keys, whatever ``tool`` is; code is never shown.

    Args:
        tool: The tool.
        arguments: Its arguments.

    Returns:
        The base name of ``path``, else ``name``, else ``query``, cut to :data:`DETAIL_CHARS`;
        ``None`` when none is a non-empty string.
    """
    for key in ("path", "name", "query"):
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        shown = posixpath.basename(value.rstrip("/")) if key == "path" else value.strip()
        if shown:
            return shown[:DETAIL_CHARS]
    return None


def tool_message(outcome: ToolResult | ToolFailure | Refusal, *, images_seen: bool = True) -> str:
    """Return what the model reads back for a call; the store keeps exactly this text.

    Args:
        outcome: What the call returned, why it failed, or why it was not run.
        images_seen: Whether a result's images go to the model.

    Returns:
        A result's text, cut to :data:`TOOL_RESULT_CHARS`, and after a newline :data:`SHOWN`, or
        :data:`NOT_SHOWN` when not ``images_seen``, if it showed images; otherwise ``{"error", "detail"}``
        as compact JSON: a busy workbench's detail says to try later, a failure's is cut to
        :data:`FAILURE_CHARS`.
    """
    if isinstance(outcome, ToolResult):
        text = cut(outcome.text, TOOL_RESULT_CHARS)
        if not outcome.images:
            return text
        return f"{text}\n{(SHOWN if images_seen else NOT_SHOWN).format(n=len(outcome.images))}"
    if isinstance(outcome, Refusal):
        detail = outcome.text
    elif outcome.token in BUSY_TOKENS:
        detail = REFUSALS["busy"]
    else:
        detail = outcome.text[:FAILURE_CHARS]
    return compact({"error": outcome.token, "detail": detail})


def result_kind(output: OutputFile) -> Literal["image", "table", "file"]:
    """Decide how a file a tool wrote is shown.

    Args:
        output: The file.

    Returns:
        ``table`` with a preview; ``image`` for a raster image up to :data:`INLINE_IMAGE_BYTES`;
        otherwise ``file``, a csv or xlsx without a preview included.
    """
    if output.preview is not None:
        return "table"
    mime = output.mime.split(";", 1)[0].strip().lower()
    if mime in IMAGE_MIMES and output.size <= INLINE_IMAGE_BYTES:
        return "image"
    return "file"
