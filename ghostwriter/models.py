"""Ghostwriter's tables: chats, their messages, tool steps, attachments, results and file marks, and the periods.

A chat's rows go with it: every foreign key cascades, except an attachment's
message, which is set to ``NULL``. Files are kept by reference only (workbench
id, path, name, size, MIME type), never their bytes; an attachment also keeps
its manifest, the counts and sizes workbench reported of it, and its measure,
its length, both derived from the file and emptied with it. An answer keeps its
notices, codes with a file's id and counts, never content or a name.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ghostwriter.db import UtcDateTime


class Base(DeclarativeBase):
    """The declarative base of Ghostwriter's tables."""


class Chat(Base):
    """A chat, its owner and its deadline."""

    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), index=True)


class FileMark(Base):
    """A time from which a chat's workbench folder may hold a file, some named by no row.

    One is written with every attachment, tool step and result, and before every
    hand-over and tool call. A chat without one never used workbench. Marks go
    only with their chat.
    """

    __tablename__ = "file_marks"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Message(Base):
    """A user's message or an assistant's answer, in its chat's order.

    An answer stores its notices, a JSON list of ``{code, …}``, when it is written; a question, an answer still
    being written and one from before schema 3 have ``NULL``. The file ids and counts of a notice about a file go at
    ``notices_expire_at``, then only the codes stay. ``context_estimate`` is the estimated tokens of the next prompt.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "position"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    notices: Mapped[str | None] = mapped_column(Text)
    context_estimate: Mapped[int | None]
    notices_expire_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), index=True)


class ToolStep(Base):
    """One tool call of an answer; its arguments and result are emptied at ``content_expires_at``."""

    __tablename__ = "tool_steps"
    __table_args__ = (CheckConstraint("status IN ('ok', 'error')", name="ck_tool_steps_status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    round: Mapped[int]
    call_id: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    content_expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)


class Attachment(Base):
    """A file the user attached, held in workbench; pending until a message binds it.

    A picture or a PDF is ``processing`` while workbench prepares it, then ``ready``
    with its manifest in ``render``, or ``failed`` with a token in ``failure``. A
    document's length is in ``measure`` (``{total, unit, chars, capped}``) once
    ``measure_state`` is ``measured``; it is ``pending`` before, ``none`` when it
    cannot be measured, and ``NULL`` for one never measured. A manifest and a
    measure are derived from the file, so they are emptied when the file's period passes.
    """

    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("kind IN ('document', 'image', 'media')", name="ck_attachments_kind"),
        CheckConstraint("status IN ('processing', 'ready', 'failed')", name="ck_attachments_status"),
        CheckConstraint("measure_state IN ('pending', 'measured', 'none')", name="ck_attachments_measure_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), index=True)
    workbench_file_id: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int]
    mime: Mapped[str] = mapped_column(String(127))
    kind: Mapped[str] = mapped_column(String(8), server_default="document")
    status: Mapped[str] = mapped_column(String(10), server_default="ready", index=True)
    render: Mapped[str | None] = mapped_column(Text)
    failure: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    measure: Mapped[str | None] = mapped_column(Text)
    measure_state: Mapped[str | None] = mapped_column(String(8))


class Result(Base):
    """A file a tool wrote, held in workbench, in order in its answer; the preview ends at ``preview_expires_at``."""

    __tablename__ = "results"
    __table_args__ = (CheckConstraint("kind IN ('image', 'table', 'file')", name="ck_results_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    kind: Mapped[str] = mapped_column(String(8))
    workbench_file_id: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(127))
    size: Mapped[int]
    preview: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    preview_expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)


class RetentionPeriod(Base):
    """A store's period, as :class:`~ghostwriter.retention_rule.RetentionPeriodState` records it."""

    __tablename__ = "retention_periods"

    store: Mapped[str] = mapped_column(String(16), primary_key=True)
    days: Mapped[int | None]
    set_at: Mapped[datetime] = mapped_column(UtcDateTime())
    grace_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    in_force_days: Mapped[int | None]
    in_force_grace: Mapped[datetime | None] = mapped_column(UtcDateTime())
