"""Ghostwriter's chats in SQLite.

Every method runs one short transaction and returns schema objects, never ORM
rows. A lookup a user can reach names the owner. A chat is due under the chat
period (:mod:`ghostwriter.retention_rule`). Every attachment, tool step and result,
and every hand-over and tool call before it goes out, leaves a file mark: a time
from which the chat's workbench folder may hold a file, also once the row that
named it is gone. Content made at ``t`` (a step's arguments and result, a
result's preview) expires with the earliest file that can still exist then, the
earliest mark after ``t`` minus the file period, or a file period after ``t``:
nothing derived from a file outlives it (spec §5.6). An attachment or a result
itself lives as long as its own file, and so does its manifest (the counts and
sizes workbench reported of it, kept in ``render``): derived from the file, it is
not served, and is emptied, once the file's period has passed. A chat without
marks never used workbench. The retention sweep (:mod:`ghostwriter.retention`) reads what is due by the same
deadlines (:meth:`ChatStore.due_chats`, :meth:`ChatStore.due_contents`).
"""

import json
from bisect import bisect_right
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast
from uuid import uuid4

from loguru import logger
from sqlalchemy import CursorResult, Engine, delete, func, or_, select, update
from sqlalchemy.orm import Session

from ghostwriter.agent.context import PastCall, PastTurn, attachment_facts, user_content
from ghostwriter.agent.images import AttachedImage, rendition_size
from ghostwriter.agent.tools import parse_arguments, step_detail
from ghostwriter.db import utcnow
from ghostwriter.models import Attachment, Chat, FileMark, Message, Result, RetentionPeriod, ToolStep
from ghostwriter.retention_rule import RetentionPeriodState, deadline_under, next_period_state
from ghostwriter.schemas import (
    AttachmentKind,
    AttachmentOut,
    AttachmentStatus,
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
#: The file period's row in ``retention_periods``: the period the content deadlines were set under.
FILES_STORE: Final = "files"
#: The longest title the first message gives a chat.
TITLE_CHARS: Final = 60
#: What a manifest keeps of what workbench reports: counts, kinds and sizes, never a path, a hint or bytes.
STORED_MANIFEST_KEYS: Final = ("kind", "total", "pages_without_text", "renditions", "frames")
#: What a stored rendition keeps: its place, its size and its type.
_RENDITION_KEYS: Final = ("n", "width", "height", "mime")


class ChatNotFound(LookupError):
    """No chat of this owner has this id."""


class AttachmentNotPending(ValueError):
    """An attachment is not waiting in this chat for a message."""


class TooManyPending(Exception):
    """The chat already holds as many attachments waiting for a message as it may."""


@dataclass(frozen=True)
class FileRef:
    """Where a chat's file is in workbench.

    Attributes:
        path: Its path in the chat's folder.
        created_at: When it was attached or written.
    """

    path: str
    created_at: datetime


@dataclass(frozen=True)
class AttachmentRef:
    """An attachment of an owner's chat, as the processor and the turn read it.

    Attributes:
        id: The attachment's id.
        chat_id: Its chat.
        owner: The chat's owner.
        file_id: workbench's id of the file.
        path: Its path in the chat's folder.
        name: The file name.
        kind: ``document``, ``image`` or ``media``.
        status: ``processing``, ``ready`` or ``failed``.
        manifest: What workbench reported of it (:data:`STORED_MANIFEST_KEYS`); ``None``
            without one, and once the file's period has passed.
        created_at: When it was handed to workbench.
        expired: Whether workbench's period for it has passed.
    """

    id: str
    chat_id: str
    owner: str
    file_id: str
    path: str
    name: str
    kind: AttachmentKind
    status: AttachmentStatus
    manifest: Mapping[str, Any] | None
    created_at: datetime
    expired: bool


@dataclass(frozen=True)
class UserTurn:
    """A user's message as the model reads it.

    Attributes:
        text: The text, each attachment named with its facts
            (:func:`~ghostwriter.agent.context.user_content`).
        images: The pictures it attached that are ready to be seen, in their order.
        position: The message's place in the chat.
    """

    text: str
    images: tuple[AttachedImage, ...]
    position: int


def _loaded_manifest(render: str | None) -> Mapping[str, Any] | None:
    """Read a stored manifest.

    Args:
        render: The row's ``render``.

    Returns:
        The manifest, or ``None`` without one or for one that is not a JSON object.
    """
    if render is None:
        return None
    try:
        manifest = json.loads(render)
    except ValueError:
        return None
    return cast(Mapping[str, Any], manifest) if isinstance(manifest, dict) else None


def _stored_manifest(manifest: Mapping[str, Any]) -> str:
    """Reduce what workbench reported of a file to what the chat keeps, as JSON.

    Args:
        manifest: The report.

    Returns:
        The keys of :data:`STORED_MANIFEST_KEYS` it has, its renditions cut to their place,
        size and type.
    """
    kept = {key: manifest[key] for key in STORED_MANIFEST_KEYS if key in manifest}
    renditions = kept.get("renditions")
    if isinstance(renditions, list):
        kept["renditions"] = [
            {key: rendition[key] for key in _RENDITION_KEYS if key in rendition}
            for rendition in cast(list[Mapping[str, Any]], renditions)
        ]
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))


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
        The step; its detail read from its arguments while they are kept.
    """
    expired = step.content_expires_at <= now
    arguments = None if expired or step.arguments is None else parse_arguments(step.arguments)
    return StepOut(
        id=step.id,
        tool=step.tool,
        status=cast(StepStatus, step.status),
        detail=None if arguments is None else step_detail(step.tool, arguments),
        arguments=None if expired else step.arguments,
        result=None if expired else step.result,
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

    def _gone(self, created_at: datetime, now: datetime) -> bool:
        """Tell whether workbench's period for a file made at ``created_at`` has passed at ``now``.

        Args:
            created_at: When the file was attached or written.
            now: The time of the read.

        Returns:
            Whether it has passed.
        """
        return created_at + self._file_period <= now

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
            kind=cast(AttachmentKind, attachment.kind),
            status=cast(AttachmentStatus, attachment.status),
            created_at=attachment.created_at,
            expired=self._gone(attachment.created_at, now),
        )

    def _attachment_ref(self, attachment: Attachment, owner: str, now: datetime) -> AttachmentRef:
        """Read an attachment row for the processor and the turn.

        Args:
            attachment: The row.
            owner: The owner of its chat.
            now: The time of the read.

        Returns:
            The attachment; its manifest is ``None`` once the file's period has passed,
            though the sweep may not have emptied it yet.
        """
        expired = self._gone(attachment.created_at, now)
        return AttachmentRef(
            id=attachment.id,
            chat_id=attachment.chat_id,
            owner=owner,
            file_id=attachment.workbench_file_id,
            path=attachment.path,
            name=attachment.name,
            kind=cast(AttachmentKind, attachment.kind),
            status=cast(AttachmentStatus, attachment.status),
            manifest=None if expired or attachment.render is None else json.loads(attachment.render),
            created_at=attachment.created_at,
            expired=expired,
        )

    def _result_out(self, result: Result, now: datetime) -> ResultOut:
        """Serve a result row.

        Args:
            result: The row.
            now: The time of the read.

        Returns:
            The result, expired once workbench's period for its file has
            passed; its preview goes earlier, at ``preview_expires_at``.
        """
        expired = self._gone(result.created_at, now)
        preview: TablePreviewOut | None = None
        if not expired and result.preview_expires_at > now and result.preview is not None:
            preview = TablePreviewOut.model_validate_json(result.preview)
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
    def _cap_start(marks: Sequence[datetime], made: datetime, period: timedelta) -> datetime:
        """Return the earliest of ``made`` and the files that can still exist then.

        Args:
            marks: The chat's file marks, oldest first.
            made: When the content was made.
            period: How long a file lives.

        Returns:
            ``made``, or the earliest mark later than ``made - period`` when that is earlier.
        """
        index = bisect_right(marks, made - period)
        return made if index == len(marks) else min(made, marks[index])

    def _content_deadline(self, s: Session, chat_id: str, made: datetime) -> datetime:
        """Return when content made now expires: with the earliest file that can still exist.

        Args:
            s: The session.
            chat_id: The chat.
            made: When the step ran or the result was written.

        Returns:
            The deadline.
        """
        marks = s.scalars(
            select(FileMark.created_at)
            .where(FileMark.chat_id == chat_id, FileMark.created_at > made - self._file_period)
            .order_by(FileMark.created_at)
            .limit(1)
        ).all()
        return self._cap_start(marks, made, self._file_period) + self._file_period

    @staticmethod
    def _mark(s: Session, chat_id: str, now: datetime) -> None:
        """Remember that the chat's workbench folder may hold a file from ``now`` on.

        Args:
            s: The session.
            chat_id: The chat.
            now: The time of the attachment, step, result, hand-over or call.
        """
        s.add(FileMark(chat_id=chat_id, created_at=now))

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
        passed get theirs again under the current file period: capped by the
        files that could still exist when they were made, under the period
        then in force (recorded here too), so a shorter period never moves a
        deadline later. One already passed keeps it, so a longer period
        brings back nothing a read has shown as expired, and nothing the
        sweep has emptied.

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
            files = s.get(RetentionPeriod, FILES_STORE)
            before = self._file_period if files is None or files.days is None else timedelta(days=files.days)
            if files is None:
                s.add(RetentionPeriod(store=FILES_STORE, days=self._file_period.days, set_at=now))
            elif files.days != self._file_period.days:
                files.days, files.set_at = self._file_period.days, now
            marks: dict[str, list[datetime]] = {}
            for chat_id, at in s.execute(select(FileMark.chat_id, FileMark.created_at).order_by(FileMark.created_at)):
                marks.setdefault(chat_id, []).append(at)

            def deadline(chat_id: str, made: datetime) -> datetime:
                return self._cap_start(marks.get(chat_id, []), made, before) + self._file_period

            steps = s.execute(
                select(ToolStep.id, ToolStep.created_at, Message.chat_id)
                .join(Message, ToolStep.message_id == Message.id)
                .where(ToolStep.content_expires_at > now)
            ).all()
            if steps:
                s.execute(
                    update(ToolStep),
                    [
                        {"id": step_id, "content_expires_at": deadline(chat_id, made)}
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
                        {"id": result_id, "preview_expires_at": deadline(chat_id, made)}
                        for result_id, made, chat_id in results
                    ],
                )
        return state

    def create_chat(self, owner: str, *, id: str | None = None) -> ChatOut:
        """Start an untitled chat, due under the chat period in force.

        Args:
            owner: Its owner.
            id: The chat's id, when the caller minted it first; a fresh uuid otherwise.

        Returns:
            The chat.
        """
        now = self._now()
        with self._transaction() as s:
            state = self._period(s)
            chat = Chat(
                id=str(uuid4()) if id is None else id,
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
                .order_by(Result.message_id, Result.position)
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
                        results=[self._result_out(r, now) for r in results if r.message_id == message.id],
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

    def due_chats(self, now: datetime, limit: int) -> list[tuple[str, str]]:
        """Return the chats whose deadline has come, the earliest first.

        Args:
            now: The time of the sweep.
            limit: The most chats returned.

        Returns:
            Each chat's id and owner.
        """
        with self._transaction() as s:
            rows = s.execute(
                select(Chat.id, Chat.owner)
                .where(Chat.expires_at.is_not(None), Chat.expires_at <= now)
                .order_by(Chat.expires_at, Chat.id)
                .limit(limit)
            ).all()
            return [(chat_id, owner) for chat_id, owner in rows]

    def due_contents(self, now: datetime, limit: int) -> tuple[list[str], list[str], list[str]]:
        """Return the tool steps, results and attachments whose content has expired and is not yet emptied.

        An attachment's content is its manifest, derived from its file: it is due once the
        file's period has passed.

        Args:
            now: The time of the sweep.
            limit: The most steps, the most results and the most attachments returned.

        Returns:
            The steps' ids, the results' ids and the ids of the attachments whose manifest is
            due, the earliest deadline first.
        """
        with self._transaction() as s:
            steps = s.scalars(
                select(ToolStep.id)
                .where(
                    ToolStep.content_expires_at <= now,
                    or_(ToolStep.arguments.is_not(None), ToolStep.result.is_not(None)),
                )
                .order_by(ToolStep.content_expires_at, ToolStep.id)
                .limit(limit)
            ).all()
            results = s.scalars(
                select(Result.id)
                .where(Result.preview_expires_at <= now, Result.preview.is_not(None))
                .order_by(Result.preview_expires_at, Result.id)
                .limit(limit)
            ).all()
            attachments = s.scalars(
                select(Attachment.id)
                .where(Attachment.created_at <= now - self._file_period, Attachment.render.is_not(None))
                .order_by(Attachment.created_at, Attachment.id)
                .limit(limit)
            ).all()
            return list(steps), list(results), list(attachments)

    def empty_contents(
        self, step_ids: Sequence[str], result_ids: Sequence[str], attachment_ids: Sequence[str] = ()
    ) -> None:
        """Empty steps' arguments and results, results' previews and attachments' manifests.

        The tool, status, name, size and type stay; so does the attachment itself.

        Args:
            step_ids: The steps.
            result_ids: The results.
            attachment_ids: The attachments.
        """
        with self._transaction() as s:
            if step_ids:
                s.execute(update(ToolStep).where(ToolStep.id.in_(step_ids)).values(arguments=None, result=None))
            if result_ids:
                s.execute(update(Result).where(Result.id.in_(result_ids)).values(preview=None))
            if attachment_ids:
                s.execute(update(Attachment).where(Attachment.id.in_(attachment_ids)).values(render=None))

    def checkpoint(self) -> None:
        """Copy the write-ahead log into the database file and empty it, outside any transaction.

        ``secure_delete`` overwrites deleted rows in the database file, but their earlier
        versions can still sit in the log until it is checkpointed.
        """
        with self._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            busy, _, _ = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
        if busy:
            logger.warning("The database was busy; the write-ahead log is checkpointed by a later sweep.")

    def add_user_message(
        self, owner: str, chat_id: str, content: str, attachment_ids: Sequence[str], *, id: str | None = None
    ) -> MessageOut:
        """Store a user's message and bind the attachments it sends.

        An untitled chat takes the message's first line as its title, and the
        chat moves to the top of its owner's list.

        Args:
            owner: Who writes.
            chat_id: The chat.
            content: The message.
            attachment_ids: Pending attachments of this chat; a repeated id counts once.
            id: The message's id, when it was recorded before the insert; minted otherwise.

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
                id=str(uuid4()) if id is None else id,
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
        """Open an empty answer after the chat's last message; the chat moves to the top of its owner's list.

        Args:
            chat_id: The chat.

        Returns:
            The answer's id.
        """
        now = self._now()
        with self._transaction() as s:
            s.execute(update(Chat).where(Chat.id == chat_id).values(updated_at=now))
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

        Its content expires with the earliest file that can still exist now,
        or after the file period without one; it leaves a file mark.

        Args:
            message_id: The answer it belongs to.
            id: The step's id, minted before the ``running`` frame.
            position: Its place in the answer.
            round: The model round that called it.
            call_id: The call's id in the turn: the model's, unless an earlier call of the turn had it.
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
                content_expires_at=self._content_deadline(s, chat_id, now),
            )
            s.add(step)
            self._mark(s, chat_id, now)
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
        """Store a file a tool wrote, by reference, with its table preview, after the answer's earlier ones.

        The preview expires after the file period, or earlier with the earliest
        file that can still exist now; the result leaves a file mark.

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
            last = s.execute(select(func.max(Result.position)).where(Result.message_id == message_id)).scalar_one()
            row = Result(
                id=str(uuid4()),
                message_id=message_id,
                position=0 if last is None else last + 1,
                kind=kind,
                workbench_file_id=file_id,
                path=path,
                name=name,
                mime=mime,
                size=size,
                preview=None if preview is None else json.dumps(preview, ensure_ascii=False, separators=(",", ":")),
                created_at=now,
                preview_expires_at=self._content_deadline(s, chat_id, now),
            )
            s.add(row)
            self._mark(s, chat_id, now)
            return self._result_out(row, now)

    def add_attachment(
        self,
        chat_id: str,
        *,
        file_id: str,
        path: str,
        name: str,
        size: int,
        mime: str,
        kind: str = "document",
        status: str = "ready",
        id: str | None = None,
        max_pending: int | None = None,
    ) -> AttachmentOut:
        """Store a file handed to workbench, by reference, pending until a message sends it; it leaves a file mark.

        Args:
            chat_id: The chat.
            file_id: workbench's id of the file.
            path: Its path in the chat's folder.
            name: The file name.
            size: Its size in bytes.
            mime: Its MIME type.
            kind: ``document``, ``image`` or ``media``.
            status: ``ready``, or ``processing`` for a file workbench is still preparing.
            id: The attachment's id, when it was recorded before the hand-over; minted otherwise.
            max_pending: The most attachments the chat may hold waiting, counted under the write
                lock, so parallel uploads cannot pass it, and expired ones not counted; ``None``
                counts nothing.

        Returns:
            The attachment.

        Raises:
            ChatNotFound: If the chat is gone, as one deleted while its file was handed over.
            TooManyPending: If the chat already holds ``max_pending`` waiting attachments.
        """
        now = self._now()
        with self._transaction() as s:
            if s.get(Chat, chat_id) is None:
                raise ChatNotFound("chat not found")
            if max_pending is not None and self._pending(s, chat_id, now) >= max_pending:
                raise TooManyPending("too many pending attachments")
            attachment = Attachment(
                id=str(uuid4()) if id is None else id,
                chat_id=chat_id,
                message_id=None,
                workbench_file_id=file_id,
                path=path,
                name=name,
                size=size,
                mime=mime,
                kind=kind,
                status=status,
                created_at=now,
            )
            s.add(attachment)
            self._mark(s, chat_id, now)
            return self._attachment_out(attachment, now)

    def mark_files(self, chat_id: str) -> None:
        """Remember that the chat's workbench folder may hold files from now on, before a hand-over or a call goes out.

        Args:
            chat_id: The chat.

        Raises:
            ChatNotFound: If the chat is gone.
        """
        now = self._now()
        with self._transaction() as s:
            if s.get(Chat, chat_id) is None:
                raise ChatNotFound("chat not found")
            self._mark(s, chat_id, now)

    def workbench_chats(self, chat_ids: Sequence[str]) -> set[str]:
        """Return those of the chats that used workbench: the ones with a file mark.

        Args:
            chat_ids: The chats.

        Returns:
            Their ids; a chat left out never handed workbench a file or called a tool.
        """
        with self._transaction() as s:
            return set(s.scalars(select(FileMark.chat_id).where(FileMark.chat_id.in_(chat_ids)).distinct()))

    def _pending(self, s: Session, chat_id: str, now: datetime) -> int:
        """Count a chat's attachments not yet sent with a message whose file has not expired, in an open session.

        Args:
            s: The session.
            chat_id: The chat.
            now: The time of the count.

        Returns:
            The count.
        """
        return s.execute(
            select(func.count())
            .select_from(Attachment)
            .where(
                Attachment.chat_id == chat_id,
                Attachment.message_id.is_(None),
                Attachment.created_at > now - self._file_period,
            )
        ).scalar_one()

    def pending_count(self, chat_id: str) -> int:
        """Count a chat's attachments not yet sent with a message whose file has not expired.

        Args:
            chat_id: The chat.

        Returns:
            The count.
        """
        now = self._now()
        with self._transaction() as s:
            return self._pending(s, chat_id, now)

    def pending_ids(self, chat_id: str) -> set[str]:
        """Return the ids of a chat's attachments not yet sent with a message.

        Args:
            chat_id: The chat.

        Returns:
            The ids.
        """
        with self._transaction() as s:
            return set(
                s.scalars(select(Attachment.id).where(Attachment.chat_id == chat_id, Attachment.message_id.is_(None)))
            )

    def pending_statuses(self, chat_id: str) -> dict[str, AttachmentStatus]:
        """Return the status of each of a chat's attachments not yet sent with a message.

        Args:
            chat_id: The chat.

        Returns:
            Each one's id and status.
        """
        with self._transaction() as s:
            rows = s.execute(
                select(Attachment.id, Attachment.status)
                .where(Attachment.chat_id == chat_id, Attachment.message_id.is_(None))
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            return {attachment_id: cast(AttachmentStatus, status) for attachment_id, status in rows}

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

    def file_expired(self, ref: FileRef) -> bool:
        """Tell whether a file is gone by now: the rule its attachment or result reads ``expired`` by.

        Args:
            ref: The file, as :meth:`find_file` found it.

        Returns:
            Whether workbench's period for it has passed.
        """
        return self._gone(ref.created_at, self._now())

    def find_attachment(self, owner: str, chat_id: str, attachment_id: str) -> AttachmentRef | None:
        """Find an attachment through its owner's chat only.

        Args:
            owner: Who asks.
            chat_id: The chat.
            attachment_id: The attachment.

        Returns:
            The attachment, or ``None`` when this owner's chat holds no such one.
        """
        now = self._now()
        with self._transaction() as s:
            attachment = s.scalars(
                select(Attachment)
                .join(Chat, Attachment.chat_id == Chat.id)
                .where(Attachment.id == attachment_id, Attachment.chat_id == chat_id, Chat.owner == owner)
            ).one_or_none()
            return None if attachment is None else self._attachment_ref(attachment, owner, now)

    def attachment_out(self, owner: str, chat_id: str, attachment_id: str) -> AttachmentOut | None:
        """Serve an attachment through its owner's chat only.

        Args:
            owner: Who asks.
            chat_id: The chat.
            attachment_id: The attachment.

        Returns:
            The attachment, or ``None`` when this owner's chat holds no such one.
        """
        now = self._now()
        with self._transaction() as s:
            attachment = s.scalars(
                select(Attachment)
                .join(Chat, Attachment.chat_id == Chat.id)
                .where(Attachment.id == attachment_id, Attachment.chat_id == chat_id, Chat.owner == owner)
            ).one_or_none()
            return None if attachment is None else self._attachment_out(attachment, now)

    def processing_attachments(self) -> list[AttachmentRef]:
        """Return every attachment still being prepared, across chats.

        Returns:
            The attachments, each with the owner of its chat, the oldest first.
        """
        now = self._now()
        with self._transaction() as s:
            rows = s.execute(
                select(Attachment, Chat.owner)
                .join(Chat, Attachment.chat_id == Chat.id)
                .where(Attachment.status == "processing")
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            return [self._attachment_ref(attachment, owner, now) for attachment, owner in rows]

    def settle_attachment(
        self,
        attachment_id: str,
        *,
        status: Literal["ready", "failed"],
        manifest: Mapping[str, Any] | None,
        failure: str | None,
    ) -> bool:
        """Settle an attachment that was being prepared, once.

        Only a ``processing`` row changes, so a second settle, a settle of a row that never was
        prepared and a settle of one deleted meanwhile change nothing.

        Args:
            attachment_id: The attachment.
            status: ``ready`` or ``failed``.
            manifest: What workbench reported of the file; only :data:`STORED_MANIFEST_KEYS`
                are kept, its renditions cut to their place, size and type.
            failure: A token for why it failed, for a ``failed`` row.

        Returns:
            Whether the row changed: ``False`` when it is gone or already settled.
        """
        with self._transaction() as s:
            settled = cast(
                "CursorResult[Any]",
                s.execute(
                    update(Attachment)
                    .where(Attachment.id == attachment_id, Attachment.status == "processing")
                    .values(
                        status=status,
                        render=None if manifest is None else _stored_manifest(manifest),
                        failure=failure,
                    )
                ),
            )
            return settled.rowcount == 1

    def settle_processing(self, failure: str) -> int:
        """Settle every attachment left ``processing``, which nothing will settle now.

        A picture cannot be seen unprepared and goes ``failed`` with the token; any other
        file is read as it was before preparing, and goes ``ready``.

        Args:
            failure: The token a picture fails with.

        Returns:
            How many rows were settled.
        """
        with self._transaction() as s:
            pictures = cast(
                "CursorResult[Any]",
                s.execute(
                    update(Attachment)
                    .where(Attachment.status == "processing", Attachment.kind == "image")
                    .values(status="failed", failure=failure)
                ),
            )
            others = cast(
                "CursorResult[Any]",
                s.execute(update(Attachment).where(Attachment.status == "processing").values(status="ready")),
            )
            return pictures.rowcount + others.rowcount

    def _prompt_text(self, message: Message, attachments: Sequence[Attachment], now: datetime, *, seen: bool) -> str:
        """Return a user's message as the model reads it.

        Args:
            message: The message.
            attachments: The files it sent.
            now: The time of the read.
            seen: Whether the model sees images, which a picture's line says.

        Returns:
            The text, then each attachment with its path, size and facts
            (:func:`~ghostwriter.agent.context.attachment_facts`), or as expired or unreadable.
        """
        paths = {attachment.id: attachment.path for attachment in attachments}
        facts = {
            attachment.id: attachment_facts(attachment.kind, _loaded_manifest(attachment.render), seen=seen)
            for attachment in attachments
        }
        return user_content(
            message.content,
            [self._attachment_out(attachment, now) for attachment in attachments],
            path_of=lambda attachment: paths[attachment.id],
            facts_of=lambda attachment: facts[attachment.id],
        )

    def _images(self, attachments: Sequence[Attachment], now: datetime) -> tuple[AttachedImage, ...]:
        """Return the pictures among a message's attachments that are ready to be seen.

        Args:
            attachments: The files it sent, in order.
            now: The time of the read.

        Returns:
            Each ``image`` attachment that is ``ready``, has not expired and has a first
            rendition, with that rendition's size.
        """
        images: list[AttachedImage] = []
        for attachment in attachments:
            if attachment.kind != "image" or attachment.status != "ready" or self._gone(attachment.created_at, now):
                continue
            size = rendition_size(_loaded_manifest(attachment.render))
            if size is not None:
                images.append(
                    AttachedImage(attachment.workbench_file_id, attachment.path, attachment.name, size[0], size[1])
                )
        return tuple(images)

    def user_turn(self, chat_id: str, message_id: str, *, seen: bool = False) -> UserTurn:
        """Return a user's message as the model reads it, its pictures, and its place in the chat.

        Args:
            chat_id: The chat.
            message_id: The user's message.
            seen: Whether the model sees images, which a picture's line says.

        Returns:
            The text with its attachments named (:func:`~ghostwriter.agent.context.user_content`),
            the pictures that are ready to be seen, and the message's position.

        Raises:
            sqlalchemy.exc.NoResultFound: If the chat has no such user's message.
        """
        now = self._now()
        with self._transaction() as s:
            message = s.scalars(
                select(Message).where(Message.id == message_id, Message.chat_id == chat_id, Message.role == "user")
            ).one()
            attachments = s.scalars(
                select(Attachment)
                .where(Attachment.message_id == message_id)
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            return UserTurn(
                self._prompt_text(message, attachments, now, seen=seen),
                self._images(attachments, now),
                message.position,
            )

    def past_turns(self, chat_id: str, *, before_position: int, seen: bool = False) -> tuple[list[PastTurn], bool]:
        """Return the turns before a message, for a follow-up: questions, live tool contents, answers.

        A question is followed by its answer, if it has one. A step whose content has
        expired is not replayed, and a round left without a step goes with it. A question
        keeps the pictures it attached that are still ready to be seen.

        Args:
            chat_id: The chat.
            before_position: The position of the message being answered.
            seen: Whether the model sees images, which a picture's line says.

        Returns:
            The turns, oldest first, and whether any of their files or tool contents have expired.
        """
        now = self._now()
        earlier = (Message.chat_id == chat_id, Message.position < before_position)
        with self._transaction() as s:
            messages = s.scalars(select(Message).where(*earlier).order_by(Message.position)).all()
            attachments = s.scalars(
                select(Attachment)
                .join(Message, Attachment.message_id == Message.id)
                .where(*earlier)
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            steps = s.scalars(
                select(ToolStep)
                .join(Message, ToolStep.message_id == Message.id)
                .where(*earlier)
                .order_by(ToolStep.round, ToolStep.position)
            ).all()
            results = s.execute(
                select(Result.message_id, Result.created_at)
                .join(Message, Result.message_id == Message.id)
                .where(*earlier)
            ).all()
            gone_results = {message_id for message_id, made in results if made + self._file_period <= now}
            turns: list[PastTurn] = []
            question: tuple[str, bool, tuple[AttachedImage, ...]] | None = None
            for message in messages:
                if message.role == "user":
                    if question is not None:
                        turns.append(PastTurn(question[0], (), "", question[1], question[2]))
                    sent = [a for a in attachments if a.message_id == message.id]
                    lost = any(a.created_at + self._file_period <= now for a in sent)
                    question = (self._prompt_text(message, sent, now, seen=seen), lost, self._images(sent, now))
                    continue
                if question is None:
                    continue
                rounds, lost = self._live_rounds([step for step in steps if step.message_id == message.id], now)
                lost = lost or question[1] or message.id in gone_results
                turns.append(PastTurn(question[0], rounds, message.content, lost, question[2]))
                question = None
            if question is not None:
                turns.append(PastTurn(question[0], (), "", question[1], question[2]))
            return turns, any(turn.expired for turn in turns)

    @staticmethod
    def _live_rounds(steps: Sequence[ToolStep], now: datetime) -> tuple[tuple[tuple[PastCall, ...], ...], bool]:
        """Group an answer's steps into rounds, leaving out those whose content has expired.

        Args:
            steps: The answer's steps, by round and position.
            now: The time of the read.

        Returns:
            The live calls of each round that kept one, and whether any step had expired.
        """
        rounds: dict[int, list[PastCall]] = {}
        lost = False
        for step in steps:
            if step.content_expires_at <= now or step.arguments is None or step.result is None:
                lost = True
                continue
            rounds.setdefault(step.round, []).append(PastCall(step.call_id, step.tool, step.arguments, step.result))
        return tuple(tuple(calls) for calls in rounds.values()), lost

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

    def question_of(self, chat_id: str, answer_id: str) -> str | None:
        """Return the user's message an answer replies to: the last one before it.

        Args:
            chat_id: The chat.
            answer_id: The answer.

        Returns:
            The message's id, or ``None`` when the chat has no such answer or no message before it.
        """
        with self._transaction() as s:
            position = s.execute(
                select(Message.position).where(
                    Message.id == answer_id, Message.chat_id == chat_id, Message.role == "assistant"
                )
            ).scalar_one_or_none()
            if position is None:
                return None
            return s.execute(
                select(Message.id)
                .where(Message.chat_id == chat_id, Message.role == "user", Message.position < position)
                .order_by(Message.position.desc())
                .limit(1)
            ).scalar_one_or_none()

    def drop_message(self, message_id: str) -> None:
        """Delete a message with its steps and results.

        Args:
            message_id: The message.
        """
        with self._transaction() as s:
            s.execute(delete(Message).where(Message.id == message_id))
