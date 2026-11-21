"""The retention sweep, and its dry run.

A daily sweep (:class:`Sweeper`, run by :func:`~ghostwriter.retention_rule.run_retention_loop`
from the lifespan) deletes the chats whose deadline has come and empties the tool contents and
table previews whose deadline has passed (spec §6.2; owner decisions 4 and 6). Every deletion is
recorded first, by reference, as the system: a record that cannot be written raises before
anything of its batch is deleted, and the next sweep tries again. A batch names at most
:data:`SWEEP_BATCH` ids, the most one event holds.

Each batch of chats:

1. holds each chat's turn (:class:`~ghostwriter.turns.TurnRegistry`); a chat whose turn is held,
   by an answer or by its owner's delete, is left to the next sweep. Holding the turns, the sweep
   reads again what is due, so a chat deleted meanwhile is not named;
2. records ``delete`` / ``retention.chat``: the chats, the period and the cutoff, the sweep's time;
3. cancels the jobs that prepare the chats' attachments (:mod:`ghostwriter.processing`), then
   deletes each chat's folder in workbench, as the system, for the reason ``ghostwriter chat``,
   under the batch's request id; a chat that never used workbench (no file mark) is not sent.
   A batch that fails after the cancel keeps its chats, and their attachments are prepared again.
   After one timeout, workbench is not called again in this sweep, so a hung workbench holds
   it up once, not once per chat;
4. records the child ``delete`` / ``retention.chat.workbench``: the files workbench deleted, the
   chats it failed or was not asked for, ``error`` when there are any, and the chats that never
   used it;
5. deletes the chats' rows, also where workbench failed: the chat is due, and workbench's own
   retention (``WORKBENCH_RETENTION_DAYS``) removes its files.

Then the contents: ``delete`` / ``retention.tool_content`` names the steps, and again the
results and the attachments, before their arguments, results, previews and manifests are
emptied (a manifest is derived from its file, so it goes when the file's period passes; the
attachment stays). Last, the write-ahead log is
checkpointed, so no earlier version of what went stays in it.

``python -m ghostwriter.retention --dry-run [--chat-days N]`` (``make retention-dry-run``) prints
what a sweep would delete now, read through a read-only connection: counts, ages and the
period's dates only, never an id, a name or content. It records and changes nothing.
"""

import argparse
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Final

import anyio.to_thread
from loguru import logger

from ghostwriter.audit import Operation, forward_headers, system_operation
from ghostwriter.config import MAX_DAYS, Settings, load_settings
from ghostwriter.db import utcnow
from ghostwriter.processing import AttachmentProcessor
from ghostwriter.retention_rule import RetentionPeriodState, deadline_under, is_due, next_period_state
from ghostwriter.store import CHATS_STORE, ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import AuditEmitter, Target
from ghostwriter.workbench import CallFor, WorkbenchClient, WorkbenchUnavailable

#: Seconds between sweeps.
SWEEP_INTERVAL_S: Final = 86_400.0
#: Seconds after startup before the first sweep.
FIRST_SWEEP_DELAY_S: Final = 60.0
#: The most chats, steps or results one batch, and so one event, names.
SWEEP_BATCH: Final = 200

CHAT_DELETE: Final = "retention.chat"
WORKBENCH_DELETE: Final = "retention.chat.workbench"
CONTENT_DELETE: Final = "retention.tool_content"

DAY: Final = timedelta(days=1)


@dataclass
class SweepReport:
    """What one sweep did.

    Attributes:
        chats: Chats deleted.
        steps: Tool steps emptied.
        previews: Table previews emptied.
        manifests: Attachment manifests emptied.
        workbench_failed: Chats whose folder workbench could not delete; their rows went all the same.
    """

    chats: int = 0
    steps: int = 0
    previews: int = 0
    manifests: int = 0
    workbench_failed: int = 0


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Sweeper:
    """The daily sweep of due chats and expired tool contents.

    Args:
        store: The chats.
        workbench: The client; ``None`` while workbench is off.
        emitter: The process's audit chain.
        settings: The chat and file periods, for the records.
        turns: The process's turn registry, whose turns the sweep holds while it deletes.
        now: The clock.
        processor: The attachment processor, whose jobs of a due chat are cancelled before its
            folder goes; ``None`` while workbench is off.
    """

    def __init__(
        self,
        *,
        store: ChatStore,
        workbench: WorkbenchClient | None,
        emitter: AuditEmitter,
        settings: Settings,
        turns: TurnRegistry,
        now: Callable[[], datetime] = utcnow,
        processor: AttachmentProcessor | None = None,
    ) -> None:
        """Keep what the sweep works with."""
        self._store = store
        self._workbench = workbench
        self._emitter = emitter
        self._settings = settings
        self._turns = turns
        self._now = now
        self._processor = processor
        self._workbench_timed_out = False

    async def sweep(self) -> SweepReport:
        """Delete the due chats, empty the expired contents, then checkpoint the log.

        Returns:
            What was deleted and emptied.

        Raises:
            AuditWriteError: When a record cannot be written; nothing of its batch is deleted.
        """
        now = self._now()
        report = SweepReport()
        self._workbench_timed_out = False
        busy = await self._sweep_chats(now, report)
        await self._sweep_contents(now, report)
        await anyio.to_thread.run_sync(self._store.checkpoint)
        logger.info(
            "Retention sweep: {} chats deleted, {} left while busy; {} tool steps, {} previews and {} manifests "
            "emptied; workbench failed {}.",
            report.chats,
            busy,
            report.steps,
            report.previews,
            report.manifests,
            report.workbench_failed,
        )
        return report

    async def _sweep_chats(self, now: datetime, report: SweepReport) -> int:
        """Delete the due chats batch by batch, each while holding its chats' turns.

        A batch that brings no chat this sweep has not handled yet ends it with a warning, so
        rows that did not go cannot keep it turning.

        Args:
            now: The time of the sweep.
            report: What the sweep did, counted on.

        Returns:
            How many due chats were left because their turn was held.
        """
        busy: set[str] = set()
        handled: set[str] = set()
        while True:
            window = SWEEP_BATCH + len(busy)
            due = await anyio.to_thread.run_sync(self._store.due_chats, now, window)
            batch = [(chat_id, owner) for chat_id, owner in due if chat_id not in busy][:SWEEP_BATCH]
            if not batch:
                return len(busy)
            if all(chat_id in handled for chat_id, _ in batch):
                logger.warning("The retention sweep found no chat it had not handled; it stops here.")
                return len(busy)
            claims: dict[str, threading.Event] = {}
            try:
                for chat_id, _ in batch:
                    claim = self._turns.begin(chat_id)
                    if claim is None:
                        busy.add(chat_id)
                    else:
                        claims[chat_id] = claim
                if not claims:
                    continue
                still = {chat_id for chat_id, _ in await anyio.to_thread.run_sync(self._store.due_chats, now, window)}
                held = [(chat_id, owner) for chat_id, owner in batch if chat_id in claims and chat_id in still]
                handled.update(claims)
                if held:
                    await self._delete_chats(held, now, report)
            finally:
                for chat_id, claim in claims.items():
                    self._turns.end(chat_id, claim)

    async def _delete_chats(self, batch: Sequence[tuple[str, str]], now: datetime, report: SweepReport) -> None:
        """Record a batch of due chats, cancel their attachment jobs, delete their workbench folders, then their rows.

        Args:
            batch: Each chat's id and owner, its turn held.
            now: The time of the sweep.
            report: What the sweep did, counted on.
        """
        op = system_operation(self._emitter, "delete", CHAT_DELETE)
        ids = tuple(chat_id for chat_id, _ in batch)
        await anyio.to_thread.run_sync(
            partial(
                op.record,
                target=Target(kind="chat", ids=ids, count=len(ids)),
                criteria={"period_days": self._settings.chat_days, "cutoff": _iso(now)},
            )
        )
        cancelled = [] if self._processor is None else [c for c in ids if self._processor.cancel_chat(c)]
        try:
            await self._delete_folders_then_rows(op, batch, report)
        except Exception:
            if self._processor is not None:
                for chat_id in cancelled:
                    await self._processor.requeue_chat(chat_id)
            raise

    async def _delete_folders_then_rows(
        self, op: Operation, batch: Sequence[tuple[str, str]], report: SweepReport
    ) -> None:
        """Delete a recorded batch's folders in workbench, record how that went, then delete its rows.

        Args:
            op: The batch's ``retention.chat`` operation, its primary written.
            batch: Each chat's id and owner, its turn held.
            report: What the sweep did, counted on.
        """
        ids = tuple(chat_id for chat_id, _ in batch)
        used = set() if self._workbench is None else await anyio.to_thread.run_sync(self._store.workbench_chats, ids)
        if self._workbench is None:
            child = partial(op.child, "delete", WORKBENCH_DELETE, criteria={"workbench": "not_configured"})
        elif not used:
            child = partial(op.child, "delete", WORKBENCH_DELETE, criteria={"workbench": "not_used"})
        else:
            headers = forward_headers(op.context)
            deleted = failed = 0
            for chat_id, owner in batch:
                if chat_id not in used:
                    continue
                if self._workbench_timed_out:
                    failed += 1
                    continue
                try:
                    deleted += await self._workbench.delete_chat(call=CallFor(owner, chat_id, headers))
                except WorkbenchUnavailable as exc:
                    failed += 1
                    if exc.token == "timeout":
                        self._workbench_timed_out = True
                        logger.warning("workbench timed out; this sweep calls it no more.")
            report.workbench_failed += failed
            child = partial(
                op.child,
                "delete",
                WORKBENCH_DELETE,
                criteria={"files_deleted": deleted, "failed": failed, "not_used": len(ids) - len(used)},
                outcome="error" if failed else "ok",
            )
        await anyio.to_thread.run_sync(child)
        await anyio.to_thread.run_sync(self._delete_rows, ids)
        report.chats += len(ids)

    def _delete_rows(self, chat_ids: Sequence[str]) -> None:
        for chat_id in chat_ids:
            self._store.delete_chat_rows(chat_id)

    async def _sweep_contents(self, now: datetime, report: SweepReport) -> None:
        """Record, then empty, the expired tool contents, previews and manifests, batch by batch.

        A batch that brings no step, result or attachment this sweep has not handled yet ends it
        with a warning, so contents that were not emptied cannot keep it turning.

        Args:
            now: The time of the sweep.
            report: What the sweep did, counted on.
        """
        criteria = {"file_days": self._settings.file_days}
        handled: set[str] = set()
        while True:
            steps, results, attachments = await anyio.to_thread.run_sync(self._store.due_contents, now, SWEEP_BATCH)
            if not steps and not results and not attachments:
                return
            if handled.issuperset(steps) and handled.issuperset(results) and handled.issuperset(attachments):
                logger.warning("The retention sweep found no tool content it had not handled; it stops here.")
                return
            handled.update(steps, results, attachments)
            for kind, ids in (("tool_step", steps), ("result", results), ("attachment", attachments)):
                if ids:
                    op = system_operation(self._emitter, "delete", CONTENT_DELETE)
                    target = Target(kind=kind, ids=tuple(ids), count=len(ids))
                    await anyio.to_thread.run_sync(partial(op.record, target=target, criteria=criteria))
            await anyio.to_thread.run_sync(self._store.empty_contents, steps, results, attachments)
            report.steps += len(steps)
            report.previews += len(results)
            report.manifests += len(attachments)


# --- the dry run --------------------------------------------------------------


def _aware(stored: str) -> datetime:
    return datetime.fromisoformat(stored).replace(tzinfo=UTC)


def _recorded_period(conn: sqlite3.Connection) -> RetentionPeriodState | None:
    row = conn.execute(
        "SELECT days, set_at, grace_until, in_force_days, in_force_grace FROM retention_periods WHERE store = ?",
        (CHATS_STORE,),
    ).fetchone()
    if row is None:
        return None
    days, set_at, grace_until, in_force_days, in_force_grace = row
    return RetentionPeriodState(
        days,
        _aware(set_at),
        None if grace_until is None else _aware(grace_until),
        in_force_days,
        None if in_force_grace is None else _aware(in_force_grace),
    )


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _ages(verb: str, times: Sequence[datetime], now: datetime) -> str:
    """Say how long ago the oldest and the newest of ``times`` were.

    Args:
        verb: ``started`` or ``made``.
        times: The times.
        now: The time of the report.

    Returns:
        E.g. ``started 95 to 120 days ago; `` or ``made 1 day ago; ``; nothing without times.
    """
    if not times:
        return ""
    newest, oldest = (now - max(times)) // DAY, (now - min(times)) // DAY
    span = _counted(newest, "day") if newest == oldest else f"{newest} to {oldest} days"
    return f"{verb} {span} ago; "


def _chat_lines(
    chats: Mapping[str, datetime], recorded: RetentionPeriodState | None, days: int, notice_days: int, now: datetime
) -> tuple[list[str], set[str]]:
    """Report the chats a sweep with ``days`` in force would delete now, and those its notice holds back.

    Args:
        chats: Each chat's start, by id.
        recorded: The period the backend recorded, if any.
        days: The period to report on.
        notice_days: The notice.
        now: The time of the report.

    Returns:
        The lines, and the due chats' ids.
    """
    state = next_period_state(recorded, days, notice_days, now)
    due = {chat_id for chat_id, started in chats.items() if is_due(deadline_under(started, state, notice_days), now)}
    held = [started for chat_id, started in chats.items() if chat_id not in due and started + days * DAY <= now]
    since = f"in force since {state.set_at:%Y-%m-%d}" if recorded is not None and recorded.days == days else "previewed"
    ages = _ages("started", [chats[chat_id] for chat_id in due], now)
    lines = [f"chats due now: {len(due)} ({ages}period {_counted(days, 'day')}, {since})"]
    if held:
        end = state.grace_end(notice_days)
        lines.append(f"chats held back by the notice: {len(held)} ({_ages('started', held, now)}due by {end:%Y-%m-%d})")
    return lines, due


def dry_run(db_path: str, *, chat_days: int | None, file_days: int, notice_days: int, now: datetime) -> str:
    """Report what a sweep would delete now, without recording or changing anything.

    The chats are counted under ``chat_days`` as a start with it would put it in force: the
    period recorded, or another one previewed, with the notice a new or shorter period gives.
    The contents are counted by the deadlines the store holds, leaving out those of due chats:
    the steps, the previews and the attachments' manifests, which go at the file period. A file
    of the schema before 2 has no manifests, and counts none.

    Args:
        db_path: The SQLite file, opened read-only.
        chat_days: The chat period; ``None`` is off.
        file_days: The file period the contents' deadlines follow.
        notice_days: The notice.
        now: The time of the report.

    Returns:
        The report: counts, ages and the period's dates only.

    Raises:
        sqlite3.Error: When the database cannot be read.
    """
    uri = f"{Path(db_path).absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        recorded = _recorded_period(conn)
        chats = {chat_id: _aware(started) for chat_id, started in conn.execute("SELECT id, created_at FROM chats")}
        steps = conn.execute(
            "SELECT m.chat_id, s.created_at, s.content_expires_at FROM tool_steps AS s"
            " JOIN messages AS m ON m.id = s.message_id WHERE s.arguments IS NOT NULL OR s.result IS NOT NULL"
        ).fetchall()
        previews = conn.execute(
            "SELECT m.chat_id, r.created_at, r.preview_expires_at FROM results AS r"
            " JOIN messages AS m ON m.id = r.message_id WHERE r.preview IS NOT NULL"
        ).fetchall()
        manifests: list[tuple[str, str]] = []
        if any(column[1] == "render" for column in conn.execute("PRAGMA table_info(attachments)")):
            manifests = conn.execute("SELECT chat_id, created_at FROM attachments WHERE render IS NOT NULL").fetchall()
    if chat_days is None:
        lines, due = ["chats due now: 0 (chat retention is off)"], set()
    else:
        lines, due = _chat_lines(chats, recorded, chat_days, notice_days, now)

    def expired(rows: list[tuple[str, str, str]]) -> list[datetime]:
        return [_aware(made) for chat_id, made, deadline in rows if chat_id not in due and _aware(deadline) <= now]

    file_period = file_days * DAY
    made_steps, made_previews = expired(steps), expired(previews)
    made_manifests = [
        made for chat_id, stored in manifests if chat_id not in due and (made := _aware(stored)) + file_period <= now
    ]
    counts = (
        f"{_counted(len(made_steps), 'step')}, {_counted(len(made_previews), 'preview')}, "
        f"{_counted(len(made_manifests), 'manifest')}"
    )
    ages = _ages("made", made_steps + made_previews + made_manifests, now)
    lines.append(f"tool contents due now: {counts} ({ages}period {_counted(file_days, 'day')})")
    lines.append("This run deletes nothing.")
    return "\n".join(lines)


def _chat_days(raw: str) -> int:
    """Read ``--chat-days``.

    Args:
        raw: The value given.

    Returns:
        The days.

    Raises:
        argparse.ArgumentTypeError: Unless it is a whole number from 1 to :data:`~ghostwriter.config.MAX_DAYS`.
    """
    message = f"a whole number of days from 1 to {MAX_DAYS}"
    try:
        days = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if not 1 <= days <= MAX_DAYS:
        raise argparse.ArgumentTypeError(message)
    return days


def main(argv: Sequence[str] | None = None) -> int:
    """Run the dry run on the configured periods, or on another chat period.

    Args:
        argv: The arguments; the command line's when ``None``.

    Returns:
        0 once the report is printed; 1 when the database cannot be read.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ghostwriter.retention",
        description="Report what retention would delete now: counts and ages only. Nothing is recorded or deleted.",
    )
    parser.add_argument("--dry-run", action="store_true", required=True, help="report only, the one mode there is")
    parser.add_argument("--chat-days", type=_chat_days, metavar="N", help="preview another chat period, in days")
    args = parser.parse_args(argv)
    settings = load_settings()
    chat_days = settings.chat_days if args.chat_days is None else args.chat_days
    try:
        report = dry_run(
            settings.db_path,
            chat_days=chat_days,
            file_days=settings.file_days,
            notice_days=settings.notice_days,
            now=utcnow(),
        )
    except sqlite3.Error as exc:
        print(f"The database could not be read: {type(exc).__name__}.", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
