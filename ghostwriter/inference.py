"""The model client: chat rounds and transcriptions through the router's OpenAI-compatible API, and nothing else.

Inference goes only to ``OPENAI_API_BASE``, the federation's LiteLLM router. Every call
carries ``X-Cache-Scope``, a pseudonym of the user keyed in this process, so the router keeps
each user's cached prompt prefixes apart from other users', and ``X-Request-ID``. A round is
one attempt: the SDK retries nothing. :data:`~ghostwriter.agent.limits.MODEL_TIMEOUT_S`
bounds each wait on the router (connecting, and each read of the stream), not the round: a
long answer streams for longer.

On vllm, the federation's provider, every round sets the chat template's thinking
(``chat_template_kwargs.enable_thinking``) to the message's reasoning switch, in both states,
as docint does: a template that opens a think block in the prompt would otherwise begin the
reply inside it unasked. Other providers get no such field, as OpenAI proper refuses unknown ones.

A transcription sends one recording, held in memory, to the router's ``/audio/transcriptions``
with ``WHISPER_MODEL``; :data:`TRANSCRIBE_TIMEOUT_S` bounds each wait on it. The limits a
recording has are here too, beside the client that sends it, as :mod:`ghostwriter.body_limit`
caps the route's body by them.
"""

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, TypedDict, cast

import openai
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionStreamOptionsParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolUnionParam,
)

from ghostwriter.config import Settings
from ghostwriter.utils.audit_runtime import REQUEST_ID_HEADER

CACHE_SCOPE_HEADER = "X-Cache-Scope"

#: The longest recording, in seconds; the SPA stops its recorder there.
MAX_RECORDING_S: Final = 120
#: The largest recording, in bytes; the server refuses one past it.
MAX_RECORDING_BYTES: Final = 10 * 1024 * 1024
#: The recordings taken, by base type: what Firefox, Chrome and Safari record, and mp3 and wav.
RECORDING_TYPES: Final = frozenset({"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav"})
#: Seconds each wait on the router may take while it transcribes.
TRANSCRIBE_TIMEOUT_S: Final = 60.0

# Keys the cache scopes; it never leaves this process, so a scope cannot be
# mapped back to a user anywhere else. A restart gives every user a new scope.
_CACHE_SCOPE_KEY = secrets.token_bytes(32)


def cache_scope_for(name: str) -> str:
    """Return the cache scope of a user: a pseudonym stable within this process.

    The inference router gives calls with the same scope one prompt-prefix
    cache and calls with different scopes separate ones, so one user cannot
    time whether another user's text is cached.

    Args:
        name: The authenticated user's name.

    Returns:
        64 lowercase hex characters.
    """
    return hmac.new(_CACHE_SCOPE_KEY, b"cache-scope/v1\0" + name.encode("utf-8"), hashlib.sha256).hexdigest()


def inference_headers(owner: str, request_id: str) -> dict[str, str]:
    """Return the headers of every model call made for a request.

    Args:
        owner: The user the call is made for.
        request_id: The request's correlation id.

    Returns:
        The user's cache scope and the request id; never the user's name.
    """
    return {CACHE_SCOPE_HEADER: cache_scope_for(owner), REQUEST_ID_HEADER: request_id}


@dataclass(frozen=True)
class Transcript:
    """What the router's speech-to-text made of a recording.

    Attributes:
        text: The transcript.
        model_id: The model that served it, when the router names one; the SDK's result has no such field.
    """

    text: str
    model_id: str | None


class BadTranscript(openai.OpenAIError):
    """The router answered a transcription without its text."""


class _Options(TypedDict, total=False):
    """A request's keys sent only where they apply: the tools while any are offered, the thinking switch on vllm."""

    tools: list[ChatCompletionToolUnionParam]
    tool_choice: ChatCompletionToolChoiceOptionParam
    extra_body: dict[str, Any]


class ModelClient:
    """The chat and speech-to-text models behind the router.

    Attributes:
        client: The SDK's client.
        model: The chat model every round asks for, ``TEXT_MODEL``.
        whisper_model: The model every transcription asks for, ``WHISPER_MODEL``.
        provider: The provider behind the endpoint, ``INFERENCE_PROVIDER``.
    """

    def __init__(self, settings: Settings, *, client: openai.OpenAI | None = None) -> None:
        """Build the client.

        Args:
            settings: The router's base URL and key, and the models.
            client: The SDK's client (tests pass a fake); built from ``settings`` when omitted,
                making one attempt per call.
        """
        self.model = settings.text_model
        self.whisper_model = settings.whisper_model
        self.provider = settings.inference_provider
        self.client = (
            client
            if client is not None
            else openai.OpenAI(base_url=settings.openai_api_base, api_key=settings.openai_api_key, max_retries=0)
        )

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        headers: Mapping[str, str],
        max_tokens: int,
        timeout_s: float,
        reasoning: bool = False,
    ) -> openai.Stream[Any]:
        """Start one streamed round.

        Args:
            messages: The conversation, as the endpoint receives it.
            tools: The tool schemas offered. With none, the request carries neither ``tools``
                nor ``tool_choice``: an OpenAI-compatible endpoint may refuse an empty list.
            headers: The extra headers, :func:`inference_headers`.
            max_tokens: The longest answer.
            timeout_s: Seconds each wait on the router may take.
            reasoning: Whether the model may think first; on vllm it sets the template's
                ``enable_thinking``, elsewhere nothing is sent for it.

        Returns:
            The stream of chunks; the last one carries the usage.

        Raises:
            openai.OpenAIError: When the router refuses the request or cannot be reached.
        """
        options: _Options = {}
        if tools:
            options["tools"] = cast("list[ChatCompletionToolUnionParam]", tools)
            options["tool_choice"] = "auto"
        if self.provider == "vllm":
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": reasoning}}
        usage: ChatCompletionStreamOptionsParam = {"include_usage": True}
        return self.client.chat.completions.create(
            model=self.model,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            stream=True,
            stream_options=usage,
            max_tokens=max_tokens,
            timeout=timeout_s,
            extra_headers=dict(headers),
            **options,
        )

    def transcribe(self, *, audio: bytes, filename: str, mime: str, headers: Mapping[str, str]) -> Transcript:
        """Transcribe one recording.

        Args:
            audio: The recording.
            filename: The name it is sent under.
            mime: Its type.
            headers: The extra headers, :func:`inference_headers`.

        Returns:
            The transcript, and the served model when the router names one.

        Raises:
            openai.OpenAIError: When the router refuses the request, cannot be reached, or answers
                without a transcript (:class:`BadTranscript`).
        """
        result = self.client.audio.transcriptions.create(
            model=self.whisper_model,
            file=(filename, audio, mime),
            response_format="json",
            timeout=TRANSCRIBE_TIMEOUT_S,
            extra_headers=dict(headers),
        )
        text = getattr(result, "text", None)
        if not isinstance(text, str):
            raise BadTranscript("the transcription has no text")
        served = getattr(result, "model", None)
        return Transcript(text=text, model_id=served if isinstance(served, str) and served else None)
