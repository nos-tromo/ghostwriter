"""Metadata routes: who the SPA's user is, and what this deployment switches on."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ghostwriter import __version__
from ghostwriter.audit import required_endpoint_actions
from ghostwriter.config import Settings
from ghostwriter.identity import Principal, trusted_identity
from ghostwriter.inference import MAX_RECORDING_BYTES, MAX_RECORDING_S
from ghostwriter.nextext import NextextHealth
from ghostwriter.upload import ATTACHMENT_TYPES, IMAGE_TYPES, MEDIA_TYPES
from ghostwriter.vision import Vision
from ghostwriter.workbench import MAX_ATTACHMENT_BYTES

router = APIRouter(prefix="/api", tags=["Metadata"])


class WhoamiOut(BaseModel):
    """The account a request acts as, for display.

    Attributes:
        username: The account, or ``None`` without a trusted identity.
        display_name: The gateway's display name, or ``None``.
    """

    username: str | None
    display_name: str | None


@router.get("/whoami", summary="The signed-in account")
def whoami(principal: Annotated[Principal | None, Depends(trusted_identity)]) -> WhoamiOut:
    """Return who the request acts as, as far as it is trusted; never refused.

    Args:
        principal: The identity, or ``None`` without a trusted one.

    Returns:
        The account and display name, both ``None`` without a trusted identity.
    """
    if principal is None:
        return WhoamiOut(username=None, display_name=None)
    return WhoamiOut(username=principal.name, display_name=principal.display_name)


class ConfigOut(BaseModel):
    """What the SPA needs to know of this deployment.

    Attributes:
        ui_language: The SPA's language, ``RESPONSE_LANGUAGE``.
        require_purpose: The actions of these routes that need a stated purpose, sorted.
        speech_input: Whether dictation is offered.
        attachments: Whether files can be attached, that is whether workbench is configured.
        attachment_types: The extensions an attachment may have, sorted; the picture types too while
            pictures are offered, and the clip types while clips are.
        image_types: The picture extensions, sorted; empty unless pictures are offered.
        media_types: The video and audio extensions, sorted; empty unless clips are offered.
        max_images: The most images one request carries.
        media: Whether video and audio are offered: Nextext is configured and healthy, and workbench is 0.9.0
            or later.
        vision: Whether pictures are offered: ``GHOSTWRITER_VISION`` is on, the chat model can see and
            workbench lists ``view_file``.
        max_attachment_bytes: The largest attachment.
        max_recording_s: The longest recording; the recorder stops there.
        max_recording_bytes: The largest recording.
        chat_retention_days: How long a chat is kept after it starts; ``None`` while retention is off.
        file_retention_days: How long workbench keeps a chat's files.
        version: The running release.
    """

    ui_language: Literal["en", "de"]
    require_purpose: list[str]
    speech_input: bool
    attachments: bool
    attachment_types: list[str]
    image_types: list[str]
    media_types: list[str]
    media: bool
    max_images: int
    vision: bool
    max_attachment_bytes: int
    max_recording_s: int
    max_recording_bytes: int
    chat_retention_days: int | None
    file_retention_days: int
    version: str


@router.get("/config", summary="What the SPA needs to know of this deployment")
def config(request: Request) -> ConfigOut:
    """Return the deployment's switches, limits and periods.

    Unauthenticated and not audited, as translator's and docint's: it describes the deployment,
    never a caller or their data, and the SPA reads it first. Whether pictures or clips are offered reads
    workbench's cached discovery, so a request waits for one at most once per cache window; the route
    runs on a pool thread for that. Whether Nextext is up is the background probe's last answer
    (:class:`~ghostwriter.nextext.NextextHealth`), never asked here: a Nextext that hangs holds nothing.

    Args:
        request: The request.

    Returns:
        The configuration in force.
    """
    settings: Settings = request.app.state.settings
    vision: Vision = request.app.state.vision
    workbench = request.app.state.workbench
    health: NextextHealth | None = request.app.state.media
    offered = vision.images_offered(workbench)
    clips = health is not None and health.offered(workbench)
    types = {**ATTACHMENT_TYPES, **(IMAGE_TYPES if offered else {}), **(MEDIA_TYPES if clips else {})}
    return ConfigOut(
        ui_language=settings.ui_language,
        require_purpose=required_endpoint_actions(settings),
        speech_input=settings.speech_input,
        attachments=settings.workbench_enabled,
        attachment_types=sorted(types),
        image_types=sorted(IMAGE_TYPES) if offered else [],
        media_types=sorted(MEDIA_TYPES) if clips else [],
        media=clips,
        max_images=settings.max_images,
        vision=offered,
        max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        max_recording_s=MAX_RECORDING_S,
        max_recording_bytes=MAX_RECORDING_BYTES,
        chat_retention_days=settings.chat_days,
        file_retention_days=settings.file_days,
        version=__version__,
    )
