"""What the API serves of a chat. ``expired`` is computed against the time of the read."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Role = Literal["user", "assistant"]
StepStatus = Literal["running", "ok", "error"]
ResultKind = Literal["image", "table", "file"]
AttachmentKind = Literal["document", "image", "media"]
AttachmentStatus = Literal["processing", "ready", "failed"]
#: What an answer tells the user of itself: ``answer_cut``, ``document_partial``, ``document_unread`` and
#: ``images_dropped`` belong to the answer, ``context_trimmed`` to the chat.
NoticeCode = Literal["answer_cut", "document_partial", "document_unread", "images_dropped", "context_trimmed"]
#: What a document's length is counted in.
MeasureUnit = Literal["page", "slide", "paragraph", "row", "line"]


class ChatOut(BaseModel):
    """A chat in the list.

    Attributes:
        id: The chat's id.
        title: The first line of its first message, or the owner's name for it.
        created_at: When it started.
        updated_at: Its latest message or rename.
        expires_at: When retention deletes it; ``None`` while retention is off.
    """

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None


class AttachmentOut(BaseModel):
    """A file the user attached.

    Attributes:
        id: The attachment's id.
        file_id: workbench's id of the file.
        name: The file name.
        size: Its size in bytes.
        mime: Its MIME type.
        kind: ``document``, ``image`` or ``media`` (a video or audio clip).
        status: ``processing`` while workbench prepares it, then ``ready`` or ``failed``.
        created_at: When it was handed to workbench.
        expired: Whether workbench's period for it has passed.
    """

    id: str
    file_id: str
    name: str
    size: int
    mime: str
    kind: AttachmentKind
    status: AttachmentStatus
    created_at: datetime
    expired: bool


class StepOut(BaseModel):
    """A tool step of an answer.

    Attributes:
        id: The step's id, the same in its ``running`` and its final frame.
        tool: The tool's name.
        status: ``running`` while it runs, then ``ok`` or ``error``.
        detail: What it worked on, for the trail; ``None`` once expired.
        arguments: The arguments as the model wrote them; ``None`` once expired.
        result: What the tool answered; ``None`` once expired.
        expired: Whether its content has expired with the chat's files.
    """

    id: str
    tool: str
    status: StepStatus
    detail: str | None
    arguments: str | None
    result: str | None
    expired: bool


class TablePreviewOut(BaseModel):
    """The head of a table a tool wrote.

    Attributes:
        columns: The header.
        rows: At most 5 rows, cells cut short.
        row_count: The data rows below the header, when known.
        truncated: Whether rows or columns were left out.
    """

    columns: list[str]
    rows: list[list[str]]
    row_count: int | None
    truncated: bool


class ResultOut(BaseModel):
    """A file a tool wrote, as a card below the answer.

    Attributes:
        id: The result's id.
        kind: ``image``, ``table`` or ``file``.
        file_id: workbench's id of the file.
        name: The file name.
        mime: Its MIME type.
        size: Its size in bytes.
        preview: The table's head; ``None`` without one, or once it has expired
            with the files it may come from.
        expired: Whether workbench's period for its own file has passed.
    """

    id: str
    kind: ResultKind
    file_id: str
    name: str
    mime: str
    size: int
    preview: TablePreviewOut | None
    expired: bool


class NoticeOut(BaseModel):
    """What an answer tells the user of itself, as the chat serves it.

    The details of a notice about a file are served until the content deadline; after it, the code alone.

    Attributes:
        code: The notice (:data:`NoticeCode`).
        file_id: workbench's id of the file it is about.
        name: The file's name, read from its attachment.
        read: How many units of the file were read.
        total: How many units the file has.
        unit: What ``read`` and ``total`` count.
    """

    code: NoticeCode
    file_id: str | None = None
    name: str | None = None
    read: int | None = None
    total: int | None = None
    unit: MeasureUnit | None = None


class MessageOut(BaseModel):
    """A message with what belongs to it.

    Attributes:
        id: The message's id.
        role: ``user`` or ``assistant``.
        content: Its text.
        created_at: When it was written.
        attachments: The files a user's message bound.
        steps: An answer's tool steps, in order.
        results: An answer's result cards, in order.
        notices: An answer's notices, in order; ``None`` for a question, an answer still being written, and one
            stored before notices were.
        context_estimate: The estimated tokens of the prompt after this answer; ``None`` when none was stored.
    """

    id: str
    role: Role
    content: str
    created_at: datetime
    attachments: list[AttachmentOut]
    steps: list[StepOut]
    results: list[ResultOut]
    notices: list[NoticeOut] | None = None
    context_estimate: int | None = None


class ChatDetail(BaseModel):
    """A chat as its owner opens it.

    Attributes:
        chat: The chat.
        messages: Its messages, in order.
        pending_attachments: Files attached and not yet sent with a message.
    """

    chat: ChatOut
    messages: list[MessageOut]
    pending_attachments: list[AttachmentOut]
