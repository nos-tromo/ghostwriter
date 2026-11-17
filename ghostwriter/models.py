"""Ghostwriter's tables: chats, their messages, tool steps, attachments, results and file marks, and the periods.

A chat's rows go with it: every foreign key cascades, except an attachment's
message, which is set to ``NULL``. Files are kept by reference only (workbench
id, path, name, size, MIME type), never their bytes.
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
    """A user's message or an assistant's answer, in its chat's order."""

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
    """A file the user attached, held in workbench; pending until a message binds it."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    workbench_file_id: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int]
    mime: Mapped[str] = mapped_column(String(127))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Result(Base):
    """A file a tool wrote, held in workbench; its preview is emptied at ``preview_expires_at``."""

    __tablename__ = "results"
    __table_args__ = (CheckConstraint("kind IN ('image', 'table', 'file')", name="ck_results_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
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
