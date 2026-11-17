"""Dictation: a recording goes to the router's speech-to-text and comes back as text, held in memory only.

The route exists only while ``GHOSTWRITER_SPEECH_INPUT`` is on (:mod:`ghostwriter.main`). The
multipart field ``file`` is read as it arrives (:func:`~ghostwriter.upload.read_file_part`) into one
``bytearray``, never Starlette's ``UploadFile``, which spools to disk, and is sent on under a name of
its type, never the browser's. Neither the recording nor its transcript is logged or stored. The
call runs on the app's speech threads (``app.state.speech_threads``), so stalled dictations never
hold the pool that serves every other request.

The call is recorded once it has ended, as ``ai_call`` / ``speech.transcribe`` (``inference:asr``):
the recording's size and type, the model, and the transcript by its hash only; a failed call with
``error``. A refusal before the call records nothing. A record that cannot be written withholds
the transcript.
"""

from functools import partial
from typing import Annotated, Final

import anyio.to_thread
import openai
from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from ghostwriter.audit import Operation, audited
from ghostwriter.body_limit import RECORDING_TOO_LONG
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.inference import MAX_RECORDING_BYTES, RECORDING_TYPES, ModelClient, inference_headers
from ghostwriter.upload import BadUpload, FilePart, TooLarge, read_file_part
from ghostwriter.utils.audit_event import AiCall, sha256_text

router = APIRouter(prefix="/api", tags=["Speech"])

TRANSCRIBE: Final = "speech.transcribe"
ASR_RECIPIENT: Final = "inference:asr"

UNSUPPORTED: Final = "This recording format is not supported."
UNREADABLE: Final = "The recording could not be read."
EMPTY: Final = "The recording is empty."
NOT_TRANSCRIBED: Final = "Speech could not be transcribed."
#: The extension a recording is sent under, where it is not its subtype.
_EXTENSIONS: Final = {"audio/mpeg": "mp3", "audio/x-wav": "wav"}

User = Annotated[Principal, Depends(resolve_principal)]


class TranscriptOut(BaseModel):
    """A recording's transcript.

    Attributes:
        text: What was said.
    """

    text: str


def _refused(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def base_type(content_type: str) -> str:
    """Return a type without its parameters, in lower case.

    Args:
        content_type: The type as sent, such as ``audio/webm;codecs=opus``.

    Returns:
        The base type, such as ``audio/webm``.
    """
    return content_type.partition(";")[0].strip().lower()


async def _read(part: FilePart) -> bytearray:
    audio = bytearray()
    async for chunk in part.chunks:
        audio += chunk
    return audio


@router.post("/transcribe", response_model=TranscriptOut, summary="Transcribe a recording")
async def transcribe(
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("ai_call", TRANSCRIBE))],
) -> TranscriptOut | JSONResponse:
    """Transcribe the multipart field ``file`` through the router's speech-to-text.

    A ``Content-Length`` past the route's cap is refused before this runs
    (:mod:`ghostwriter.body_limit`).

    Args:
        request: The request, its body still unread.
        user: Who dictates.
        op: ``ai_call`` / ``speech.transcribe``, recorded once the router answered or failed.

    Returns:
        The transcript; or 415 for a type not taken, 413 past :data:`MAX_RECORDING_BYTES`, 400 for
        an empty or unreadable recording, 502 when the router could not transcribe it.
    """
    try:
        part = await read_file_part(request, max_bytes=MAX_RECORDING_BYTES)
    except TooLarge:
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, RECORDING_TOO_LONG)
    except BadUpload:
        return _refused(status.HTTP_400_BAD_REQUEST, UNREADABLE)
    try:
        mime = base_type(part.content_type)
        if mime not in RECORDING_TYPES:
            return _refused(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, UNSUPPORTED)
        audio = await _read(part)
    except TooLarge:
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, RECORDING_TOO_LONG)
    except BadUpload:
        return _refused(status.HTTP_400_BAD_REQUEST, UNREADABLE)
    finally:
        await part.aclose()
    if not audio:
        return _refused(status.HTTP_400_BAD_REQUEST, EMPTY)
    model: ModelClient = request.app.state.model
    criteria = {"bytes": len(audio), "mime": mime}
    try:
        transcript = await anyio.to_thread.run_sync(
            partial(
                model.transcribe,
                audio=bytes(audio),
                filename=f"recording.{_EXTENSIONS.get(mime, mime.partition('/')[2])}",
                mime=mime,
                headers=inference_headers(user.name, op.context.request_id),
            ),
            limiter=request.app.state.speech_threads,
        )
    except openai.OpenAIError as exc:
        logger.warning("A transcription failed: {}", type(exc).__name__)
        failed = AiCall(model_alias=model.whisper_model)
        await run_in_threadpool(op.record, criteria=criteria, recipient=ASR_RECIPIENT, ai=failed, outcome="error")
        return _refused(status.HTTP_502_BAD_GATEWAY, NOT_TRANSCRIBED)
    ai = AiCall(
        model_alias=model.whisper_model, model_id=transcript.model_id, output_sha256=sha256_text(transcript.text)
    )
    await run_in_threadpool(op.record, criteria=criteria, recipient=ASR_RECIPIENT, ai=ai)
    return TranscriptOut(text=transcript.text)
