"""Ghostwriter's chats in SQLite.

Every method runs one short transaction and returns schema objects, never ORM
rows. A lookup a user can reach names the owner. A chat is due under the chat
period (:mod:`ghostwriter.retention_rule`). A tool step's content and a result's
preview expire with the chat's earliest file: nothing derived from a file
outlives it (spec §5.6).
"""

import json
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast
from uuid import uuid4

from sqlalchemy import Engine, delete, func, select, union_all, update
from sqlalchemy.orm import Session

from ghostwriter.db import utcnow
from ghostwriter.models import Attachment, Chat, Message, Result, RetentionPeriod, ToolStep
from ghostwriter.retention_rule import RetentionPeriodState, deadline_under, next_period_state
from ghostwriter.schemas import (
    AttachmentOut,
    ChatDetail,
    ChatOut,
    MessageOut,
    ResultKind,
    ResultOut,
    Role,
    StepOut,
    StepStatus,
    TablePreviewOut,
)

#: The chat period's row in ``retention_periods``.
CHATS_STORE: Final = "chats"
#: The longest title the first message gives a chat.
TITLE_CHARS: Final = 60


class ChatNotFound(LookupError):
    """No chat of this owner has this id."""


class AttachmentNotPending(ValueError):
    """An attachment is not waiting in this chat for a message."""


@dataclass(frozen=True)
class FileRef:
    """Where a chat's file is in workbench.

    Attributes:
        path: Its path in the chat's folder.
        created_at: When it was attached or written.
    """

    path: str
    created_at: datetime


def _title(content: str) -> str | None:
    """Name a chat by the first line of its first message.

    Args:
        content: The message.

    Returns:
        The line, cut to :data:`TITLE_CHARS` with ``…``; ``None`` for an empty message.
    """
    lines = content.strip().splitlines()
    line = lines[0].strip() if lines else ""
    if not line:
        return None
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1] + "…"


def _chat_out(chat: Chat) -> ChatOut:
    """Serve a chat row.

    Args:
        chat: The row.

    Returns:
        The chat.
    """
    return ChatOut(
        id=chat.id,
        title=chat.title,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        expires_at=chat.expires_at,
    )


def _step_out(step: ToolStep, now: datetime) -> StepOut:
    """Serve a step row, without its content once that has expired.

    Args:
        step: The row.
        now: The time of the read.

    Returns:
        The step.
    """
    expired = step.content_expires_at <= now
    return StepOut(
        id=step.id,
        tool=step.tool,
        status=cast(StepStatus, step.status),
        detail=None,
        arguments=None if expired else step.arguments,
        result=None if expired else step.result,
        expired=expired,
    )


def _result_out(result: Result, now: datetime) -> ResultOut:
    """Serve a result row, without its preview once that has expired.

    Args:
        result: The row.
        now: The time of the read.

    Returns:
        The result.
    """
    expired = result.preview_expires_at <= now
    preview = None if expired or result.preview is None else TablePreviewOut.model_validate_json(result.preview)
    return ResultOut(
        id=result.id,
        kind=cast(ResultKind, result.kind),
        file_id=result.workbench_file_id,
        name=result.name,
        mime=result.mime,
        size=result.size,
        preview=preview,
        expired=expired,
    )


class ChatStore:
    """The chats, their messages, tool steps, attachments and results.

    Args:
        engine: The database, with its schema (:func:`ghostwriter.db.init_db`).
        file_days: How long workbench keeps a chat's files; tool contents and previews follow it.
        notice_days: How long a new or shorter chat period waits before it deletes.
        now: The clock.
    """

    def __init__(
        self, engine: Engine, *, file_days: int, notice_days: int, now: Callable[[], datetime] = utcnow
    ) -> None:
        """Keep the engine, the periods and the clock."""
        self._engine = engine
        self._file_period = timedelta(days=file_days)
        self._notice_days = notice_days
        self._now = now

    @contextmanager
    def _transaction(self) -> Generator[Session]:
        """Open one short transaction.

        Yields:
            The session, committed on success and rolled back on an error.
        """
        with Session(self._engine) as s, s.begin():
            yield s

    def _attachment_out(self, attachment: Attachment, now: datetime) -> AttachmentOut:
        """Serve an attachment row.

        Args:
            attachment: The row.
            now: The time of the read.

        Returns:
            The attachment, expired once workbench's period for it has passed.
        """
        return AttachmentOut(
            id=attachment.id,
            file_id=attachment.workbench_file_id,
            name=attachment.name,
            size=attachment.size,
            mime=attachment.mime,
            created_at=attachment.created_at,
            expired=attachment.created_at + self._file_period <= now,
        )

    @staticmethod
    def _owned(s: Session, owner: str, chat_id: str) -> Chat:
        """Return an owner's chat.

        Args:
            s: The session.
            owner: Who asks.
            chat_id: The chat.

        Returns:
            The row.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
        """
        chat = s.scalars(select(Chat).where(Chat.id == chat_id, Chat.owner == owner)).one_or_none()
        if chat is None:
            raise ChatNotFound("chat not found")
        return chat

    @staticmethod
    def _period(s: Session) -> RetentionPeriodState | None:
        """Read the chat period recorded so far.

        Args:
            s: The session.

        Returns:
            The state, ``None`` before the first start.
        """
        row = s.get(RetentionPeriod, CHATS_STORE)
        if row is None:
            return None
        return RetentionPeriodState(row.days, row.set_at, row.grace_until, row.in_force_days, row.in_force_grace)

    @staticmethod
    def _earliest_files(s: Session, chat_id: str | None = None) -> dict[str, datetime]:
        """Return when each chat's earliest attachment or result was made.

        Args:
            s: The session.
            chat_id: One chat; every chat when ``None``.

        Returns:
            The earliest time per chat id, for chats that hold a file.
        """
        attachments = select(Attachment.chat_id.label("chat_id"), Attachment.created_at.label("created_at"))
        results = select(Message.chat_id, Result.created_at).join(Message, Result.message_id == Message.id)
        if chat_id is not None:
            attachments = attachments.where(Attachment.chat_id == chat_id)
            results = results.where(Message.chat_id == chat_id)
        files = union_all(attachments, results).subquery()
        rows = s.execute(select(files.c.chat_id, func.min(files.c.created_at)).group_by(files.c.chat_id)).all()
        return {chat: earliest for chat, earliest in rows}

    def _content_deadline(self, made: datetime, earliest_file: datetime | None) -> datetime:
        """Return when content made at ``made`` expires: with the chat's earliest file, or after its own period.

        The earliest file counts only when it is older than the content, so a
        file that came later never moves the deadline.

        Args:
            made: When the step ran or the result was written.
            earliest_file: When the chat's earliest file was made, if it holds one.

        Returns:
            The deadline.
        """
        start = made if earliest_file is None else min(made, earliest_file)
        return start + self._file_period

    @staticmethod
    def _next_position(s: Session, chat_id: str) -> int:
        """Return the position after a chat's last message.

        Args:
            s: The session.
            chat_id: The chat.

        Returns:
            The position.
        """
        last = s.execute(select(func.max(Message.position)).where(Message.chat_id == chat_id)).scalar_one()
        return 0 if last is None else last + 1

    def apply_periods(self, chat_days: int | None) -> RetentionPeriodState:
        """Put the configured periods in force, at startup.

        Records the chat period's next state and gives every chat its
        deadline under it. Tool contents and previews whose deadline has not
        passed get theirs again under the current file period; one already
        passed keeps it, so a longer period brings back nothing a read has
        shown as expired, and nothing the sweep has emptied.

        Args:
            chat_days: The chat period; ``None`` is off.

        Returns:
            The chat period's state.
        """
        now = self._now()
        with self._transaction() as s:
            state = next_period_state(self._period(s), chat_days, self._notice_days, now)
            row = s.get(RetentionPeriod, CHATS_STORE) or RetentionPeriod(store=CHATS_STORE)
            row.days, row.set_at, row.grace_until = state.days, state.set_at, state.grace_until
            row.in_force_days, row.in_force_grace = state.in_force_days, state.in_force_grace
            s.add(row)
            chats = s.execute(select(Chat.id, Chat.created_at)).all()
            if chats:
                s.execute(
                    update(Chat),
                    [
                        {"id": chat_id, "expires_at": deadline_under(created, state, self._notice_days)}
                        for chat_id, created in chats
                    ],
                )
            earliest = self._earliest_files(s)
            steps = s.execute(
                select(ToolStep.id, ToolStep.created_at, Message.chat_id)
                .join(Message, ToolStep.message_id == Message.id)
                .where(ToolStep.content_expires_at > now)
            ).all()
            if steps:
                s.execute(
                    update(ToolStep),
                    [
                        {"id": step_id, "content_expires_at": self._content_deadline(made, earliest.get(chat_id))}
                        for step_id, made, chat_id in steps
                    ],
                )
            results = s.execute(
                select(Result.id, Result.created_at, Message.chat_id)
                .join(Message, Result.message_id == Message.id)
                .where(Result.preview_expires_at > now)
            ).all()
            if results:
                s.execute(
                    update(Result),
                    [
                        {"id": result_id, "preview_expires_at": self._content_deadline(made, earliest.get(chat_id))}
                        for result_id, made, chat_id in results
                    ],
                )
        return state

    def create_chat(self, owner: str) -> ChatOut:
        """Start an untitled chat, due under the chat period in force.

        Args:
            owner: Its owner.

        Returns:
            The chat.
        """
        now = self._now()
        with self._transaction() as s:
            state = self._period(s)
            chat = Chat(
                id=str(uuid4()),
                owner=owner,
                title=None,
                created_at=now,
                updated_at=now,
                expires_at=None if state is None else deadline_under(now, state, self._notice_days),
            )
            s.add(chat)
            return _chat_out(chat)

    def list_chats(self, owner: str) -> list[ChatOut]:
        """Return an owner's chats, the latest activity first.

        Args:
            owner: The owner.

        Returns:
            The chats.
        """
        with self._transaction() as s:
            chats = s.scalars(
                select(Chat).where(Chat.owner == owner).order_by(Chat.updated_at.desc(), Chat.created_at.desc())
            ).all()
            return [_chat_out(chat) for chat in chats]

    def get_chat(self, owner: str, chat_id: str) -> ChatOut:
        """Return an owner's chat.

        Args:
            owner: Who asks.
            chat_id: The chat.

        Returns:
            The chat.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
        """
        with self._transaction() as s:
            return _chat_out(self._owned(s, owner, chat_id))

    def chat_detail(self, owner: str, chat_id: str) -> ChatDetail:
        """Return an owner's chat with its messages, steps, results and attachments.

        Args:
            owner: Who asks.
            chat_id: The chat.

        Returns:
            The chat, its messages in order and its pending attachments.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
        """
        now = self._now()
        with self._transaction() as s:
            chat = self._owned(s, owner, chat_id)
            messages = s.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.position)).all()
            attachments = s.scalars(
                select(Attachment).where(Attachment.chat_id == chat_id).order_by(Attachment.created_at, Attachment.id)
            ).all()
            steps = s.scalars(
                select(ToolStep)
                .join(Message, ToolStep.message_id == Message.id)
                .where(Message.chat_id == chat_id)
                .order_by(ToolStep.round, ToolStep.position)
            ).all()
            results = s.scalars(
                select(Result)
                .join(Message, Result.message_id == Message.id)
                .where(Message.chat_id == chat_id)
                .order_by(Result.created_at, Result.id)
            ).all()
            return ChatDetail(
                chat=_chat_out(chat),
                messages=[
                    MessageOut(
                        id=message.id,
                        role=cast(Role, message.role),
                        content=message.content,
                        created_at=message.created_at,
                        attachments=[self._attachment_out(a, now) for a in attachments if a.message_id == message.id],
                        steps=[_step_out(step, now) for step in steps if step.message_id == message.id],
                        results=[_result_out(result, now) for result in results if result.message_id == message.id],
                    )
                    for message in messages
                ],
                pending_attachments=[self._attachment_out(a, now) for a in attachments if a.message_id is None],
            )

    def rename_chat(self, owner: str, chat_id: str, title: str) -> ChatOut:
        """Give an owner's chat a title.

        Args:
            owner: Who asks.
            chat_id: The chat.
            title: The new title.

        Returns:
            The chat.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
        """
        now = self._now()
        with self._transaction() as s:
            chat = self._owned(s, owner, chat_id)
            chat.title = title
            chat.updated_at = now
            return _chat_out(chat)

    def chat_counts(self, chat_id: str) -> dict[str, int]:
        """Count what a chat holds, for the audit trail.

        Args:
            chat_id: The chat.

        Returns:
            ``messages``, ``steps``, ``results`` and ``attachments``.
        """
        counted = select(func.count())
        with self._transaction() as s:
            return {
                name: s.execute(statement).scalar_one()
                for name, statement in (
                    ("messages", counted.select_from(Message).where(Message.chat_id == chat_id)),
                    ("steps", counted.select_from(ToolStep).join(Message).where(Message.chat_id == chat_id)),
                    ("results", counted.select_from(Result).join(Message).where(Message.chat_id == chat_id)),
                    ("attachments", counted.select_from(Attachment).where(Attachment.chat_id == chat_id)),
                )
            }

    def delete_chat_rows(self, chat_id: str) -> None:
        """Delete a chat and, by its foreign keys, every row that belongs to it.

        Args:
            chat_id: The chat.
        """
        with self._transaction() as s:
            s.execute(delete(Chat).where(Chat.id == chat_id))

    def add_user_message(self, owner: str, chat_id: str, content: str, attachment_ids: Sequence[str]) -> MessageOut:
        """Store a user's message and bind the attachments it sends.

        An untitled chat takes the message's first line as its title, and the
        chat moves to the top of its owner's list.

        Args:
            owner: Who writes.
            chat_id: The chat.
            content: The message.
            attachment_ids: Pending attachments of this chat; a repeated id counts once.

        Returns:
            The message with its attachments.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
            AttachmentNotPending: If an id is not a pending attachment of this
                chat; nothing is stored then.
        """
        now = self._now()
        ids = list(dict.fromkeys(attachment_ids))
        with self._transaction() as s:
            chat = self._owned(s, owner, chat_id)
            message = Message(
                id=str(uuid4()),
                chat_id=chat_id,
                position=self._next_position(s, chat_id),
                role="user",
                content=content,
                created_at=now,
            )
            s.add(message)
            s.flush()
            attachments = s.scalars(
                select(Attachment)
                .where(Attachment.id.in_(ids), Attachment.chat_id == chat_id, Attachment.message_id.is_(None))
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            if len(attachments) != len(ids):
                raise AttachmentNotPending("attachment is not pending in this chat")
            for attachment in attachments:
                attachment.message_id = message.id
            if chat.title is None:
                chat.title = _title(content)
            chat.updated_at = now
            return MessageOut(
                id=message.id,
                role="user",
                content=content,
                created_at=now,
                attachments=[self._attachment_out(a, now) for a in attachments],
                steps=[],
                results=[],
            )

    def start_assistant_message(self, chat_id: str) -> str:
        """Open an empty answer after the chat's last message.

        Args:
            chat_id: The chat.

        Returns:
            The answer's id.
        """
        now = self._now()
        with self._transaction() as s:
            message = Message(
                id=str(uuid4()),
                chat_id=chat_id,
                position=self._next_position(s, chat_id),
                role="assistant",
                content="",
                created_at=now,
            )
            s.add(message)
            return message.id

    def finish_assistant_message(self, message_id: str, content: str) -> None:
        """Store an answer's text.

        Args:
            message_id: The answer.
            content: Its text.
        """
        with self._transaction() as s:
            s.execute(
                update(Message).where(Message.id == message_id, Message.role == "assistant").values(content=content)
            )

    def add_step(
        self,
        message_id: str,
        *,
        id: str,
        position: int,
        round: int,
        call_id: str,
        tool: str,
        arguments: str,
        result: str,
        status: str,
    ) -> StepOut:
        """Store a tool step under the id its ``running`` frame carried.

        Its content expires with the chat's earliest file, or after the file
        period without one.

        Args:
            message_id: The answer it belongs to.
            id: The step's id, minted before the ``running`` frame.
            position: Its place in the answer.
            round: The model round that called it.
            call_id: The model's id for the call.
            tool: The tool's name.
            arguments: The arguments as the model wrote them.
            result: What the model read back.
            status: ``ok`` or ``error``.

        Returns:
            The step.
        """
        now = self._now()
        with self._transaction() as s:
            chat_id = s.execute(select(Message.chat_id).where(Message.id == message_id)).scalar_one()
            step = ToolStep(
                id=id,
                message_id=message_id,
                position=position,
                round=round,
                call_id=call_id,
                tool=tool,
                arguments=arguments,
                result=result,
                status=status,
                created_at=now,
                content_expires_at=self._content_deadline(now, self._earliest_files(s, chat_id).get(chat_id)),
            )
            s.add(step)
            return _step_out(step, now)

    def add_result(
        self,
        message_id: str,
        *,
        kind: str,
        file_id: str,
        path: str,
        name: str,
        mime: str,
        size: int,
        preview: Mapping[str, Any] | None,
    ) -> ResultOut:
        """Store a file a tool wrote, by reference, with its table preview.

        The preview expires after the file period, or earlier with the chat's earliest file.

        Args:
            message_id: The answer it belongs to.
            kind: ``image``, ``table`` or ``file``.
            file_id: workbench's id of the file.
            path: Its path in the chat's folder.
            name: The file name.
            mime: Its MIME type.
            size: Its size in bytes.
            preview: The table's head, or ``None``.

        Returns:
            The result.
        """
        now = self._now()
        with self._transaction() as s:
            chat_id = s.execute(select(Message.chat_id).where(Message.id == message_id)).scalar_one()
            row = Result(
                id=str(uuid4()),
                message_id=message_id,
                kind=kind,
                workbench_file_id=file_id,
                path=path,
                name=name,
                mime=mime,
                size=size,
                preview=None if preview is None else json.dumps(preview, ensure_ascii=False, separators=(",", ":")),
                created_at=now,
                preview_expires_at=self._content_deadline(now, self._earliest_files(s, chat_id).get(chat_id)),
            )
            s.add(row)
            return _result_out(row, now)

    def add_attachment(
        self, chat_id: str, *, file_id: str, path: str, name: str, size: int, mime: str
    ) -> AttachmentOut:
        """Store a file handed to workbench, by reference, pending until a message sends it.

        Args:
            chat_id: The chat.
            file_id: workbench's id of the file.
            path: Its path in the chat's folder.
            name: The file name.
            size: Its size in bytes.
            mime: Its MIME type.

        Returns:
            The attachment.
        """
        now = self._now()
        with self._transaction() as s:
            attachment = Attachment(
                id=str(uuid4()),
                chat_id=chat_id,
                message_id=None,
                workbench_file_id=file_id,
                path=path,
                name=name,
                size=size,
                mime=mime,
                created_at=now,
            )
            s.add(attachment)
            return self._attachment_out(attachment, now)

    def pending_count(self, chat_id: str) -> int:
        """Count a chat's attachments not yet sent with a message.

        Args:
            chat_id: The chat.

        Returns:
            The count.
        """
        with self._transaction() as s:
            return s.execute(
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.chat_id == chat_id, Attachment.message_id.is_(None))
            ).scalar_one()

    def find_file(self, owner: str, chat_id: str, file_id: str) -> FileRef | None:
        """Find a file through its own chat only: an attachment or a result of the owner's chat.

        Args:
            owner: Who asks.
            chat_id: The chat.
            file_id: workbench's id of the file.

        Returns:
            Where it is, or ``None`` when this owner's chat holds no such file.
        """
        with self._transaction() as s:
            owned = select(Chat.id).where(Chat.id == chat_id, Chat.owner == owner)
            found = s.execute(
                select(Attachment.path, Attachment.created_at).where(
                    Attachment.chat_id.in_(owned), Attachment.workbench_file_id == file_id
                )
            ).first()
            if found is None:
                found = s.execute(
                    select(Result.path, Result.created_at)
                    .join(Message, Result.message_id == Message.id)
                    .where(Message.chat_id.in_(owned), Result.workbench_file_id == file_id)
                ).first()
            return None if found is None else FileRef(path=found[0], created_at=found[1])

    def last_answer(self, owner: str, chat_id: str) -> str | None:
        """Return the answer that ends an owner's chat.

        Args:
            owner: Who asks.
            chat_id: The chat.

        Returns:
            The id of the last message when it is an answer, else ``None``.

        Raises:
            ChatNotFound: If the owner has no chat with that id.
        """
        with self._transaction() as s:
            self._owned(s, owner, chat_id)
            last = s.execute(
                select(Message.id, Message.role)
                .where(Message.chat_id == chat_id)
                .order_by(Message.position.desc())
                .limit(1)
            ).first()
            return last[0] if last is not None and last[1] == "assistant" else None

    def drop_message(self, message_id: str) -> None:
        """Delete a message with its steps and results.

        Args:
            message_id: The message.
        """
        with self._transaction() as s:
            s.execute(delete(Message).where(Message.id == message_id))
