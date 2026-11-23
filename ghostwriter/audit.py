"""Data-protection audit events for Ghostwriter: one hash-chained trail, references only.

Translator's glue around the vendored :mod:`ghostwriter.utils.audit_event`
(event format, hash chain) and :mod:`ghostwriter.utils.audit_runtime` (output
stream, purpose header, request ids), with docint's :class:`Operation`,
:func:`begin` and :func:`audited`. The emitter is ``ghostwriter`` / ``backend``:
one per process and so one chain, opened in the lifespan (:func:`open_emitter`,
:func:`start_chain`), never at import. ``AUDIT_OUTPUT`` selects where it writes:
unset or ``stdout`` for the process's stdout, ``container`` for the container's
main stdout under a container-wide lock (what ``docker/compose.yaml`` sets).

Events name what they touch by reference: ids, counts, MIME types and sizes,
never a title, message, file name, code or transcript. Fail closed: an event
that cannot be written raises :class:`AuditWriteError`, and ``ghostwriter.main``
answers the request with the JSON 500 ``Request failed.`` instead of its data.
After shutdown the closed chain raises a plain ``RuntimeError``, which the
catch-all answers the same way. A request that ends in a server error settles
every operation it began and did not record as ``error``
(:func:`settle_failed_operations`).
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, TextIO
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from loguru import logger

from ghostwriter.config import Settings
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.utils.audit_event import Actor, AiCall, AuditEmitter, Purpose, Target
from ghostwriter.utils.audit_runtime import (
    PURPOSE_HEADER,
    PURPOSE_REQUIRED_STATUS,
    REQUEST_ID_HEADER,
    PurposeError,
    clean_request_id,
    encode_purpose,
    open_audit_stream,
    parse_purpose_header,
)

#: The compose project name; the audit store binds each chain to it.
SERVICE: Final = "ghostwriter"
#: The reason workbench gets for a request that states no purpose.
CHAT_REASON: Final = "ghostwriter chat"
OUTPUT_ENV: Final = "AUDIT_OUTPUT"
#: The app-state attribute holding the process's emitter.
EMITTER_STATE_KEY: Final = "audit_emitter"
#: The request-state attribute listing the operations a request began.
OPERATIONS_STATE_KEY: Final = "audit_operations"
#: The system itself, for startup and the retention sweep.
SYSTEM_ACTOR: Final = Actor(user=None, via="system")
#: The actions Ghostwriter's routes record. :func:`audited` accepts no other,
#: so this stays the complete set :func:`required_endpoint_actions` reads.
ENDPOINT_ACTIONS: Final = frozenset({"query", "collect", "modify", "delete", "disclose", "ai_call"})

_PURPOSE_REQUIRED_DETAIL: Final = "A stated purpose is required for this request."


@dataclass(frozen=True)
class RequestContext:
    """Who an operation runs for and why, carried into the calls it makes.

    Attributes:
        actor: Who caused the work.
        purpose: The stated purpose, ``None`` when none was stated.
        request_id: The gateway's correlation id, or a fresh one.
    """

    actor: Actor
    purpose: Purpose | None
    request_id: str


@dataclass
class Operation:
    """One audited operation and the events it has written.

    The primary event (:meth:`record`) is written once, when the operation
    completes; related events (:meth:`child`) point at it.

    Attributes:
        emitter: The chain to write to.
        context: Actor, purpose and request id.
        action: The primary event's action.
        operation: The primary event's operation name.
        container: The enclosing object, put on every target that names none.
        event_id: The primary event's id, once written.
        settled: Whether the primary event was attempted.
    """

    emitter: AuditEmitter
    context: RequestContext
    action: str
    operation: str
    container: str | None = None
    event_id: str | None = None
    settled: bool = False

    def _target(self, target: Target | None) -> Target | None:
        """Give a target the operation's container when it names none.

        Args:
            target: The target as the caller built it.

        Returns:
            The target to write.
        """
        if target is None or target.container is not None or self.container is None:
            return target
        return Target(kind=target.kind, ids=target.ids, count=target.count, container=self.container)

    def _emit(
        self,
        action: str,
        operation: str,
        *,
        outcome: str,
        parent_event_id: str | None,
        target: Target | None,
        criteria: Mapping[str, object] | None,
        recipient: str | None,
        ai: AiCall | None,
    ) -> str:
        """Write one event of this operation.

        Args:
            action: The action.
            operation: The operation name.
            outcome: The outcome.
            parent_event_id: The event it belongs to.
            target: What was touched.
            criteria: The parameters, never content.
            recipient: Where the data went.
            ai: Model provenance.

        Returns:
            The event's id.

        Raises:
            AuditWriteError: When the line could not be written.
            RuntimeError: When the chain is not open.
        """
        event = self.emitter.emit(
            action,
            operation,
            self.context.actor,
            outcome=outcome,
            request_id=self.context.request_id,
            parent_event_id=parent_event_id,
            purpose=self.context.purpose,
            target=self._target(target),
            criteria=criteria,
            recipient=recipient,
            ai=ai,
        )
        return str(event["event_id"])

    def record(
        self,
        *,
        target: Target | None = None,
        criteria: Mapping[str, object] | None = None,
        recipient: str | None = None,
        ai: AiCall | None = None,
        outcome: str = "ok",
    ) -> str:
        """Write the operation's primary event, once.

        Marked settled before the write, so a failed write is never followed
        by an ``error`` event the broken stream could not take either.

        Args:
            target: What was touched, by reference.
            criteria: The parameters, never content.
            recipient: Where the data went.
            ai: Model provenance.
            outcome: ``ok``, ``denied`` or ``error``.

        Returns:
            The primary event's id.

        Raises:
            RuntimeError: When the primary event was already written, or the chain is not open.
            AuditWriteError: When the line could not be written.
        """
        if self.settled:
            raise RuntimeError(f"audit operation {self.operation} already recorded")
        self.settled = True
        self.event_id = self._emit(
            self.action,
            self.operation,
            outcome=outcome,
            parent_event_id=None,
            target=target,
            criteria=criteria,
            recipient=recipient,
            ai=ai,
        )
        return self.event_id

    def child(
        self,
        action: str,
        operation: str,
        *,
        target: Target | None = None,
        criteria: Mapping[str, object] | None = None,
        recipient: str | None = None,
        ai: AiCall | None = None,
        outcome: str = "ok",
    ) -> str:
        """Write an event of this operation that points at its primary event.

        Args:
            action: The action.
            operation: The operation name.
            target: What was touched.
            criteria: The parameters, never content.
            recipient: Where the data went.
            ai: Model provenance.
            outcome: The outcome.

        Returns:
            The event's id.

        Raises:
            RuntimeError: When the primary event has not been written yet, or the chain is not open.
            AuditWriteError: When the line could not be written.
        """
        if self.event_id is None:
            raise RuntimeError(f"audit operation {self.operation} has no primary event yet")
        return self._emit(
            action,
            operation,
            outcome=outcome,
            parent_event_id=self.event_id,
            target=target,
            criteria=criteria,
            recipient=recipient,
            ai=ai,
        )

    def fail(self, *, target: Target | None = None, criteria: Mapping[str, object] | None = None) -> None:
        """Record the operation as failed, unless an outcome was already written.

        Args:
            target: What it touched before failing, if known.
            criteria: The parameters, never content.
        """
        if not self.settled:
            self.record(target=target, criteria=criteria, outcome="error")


def required_endpoint_actions(settings: Settings) -> list[str]:
    """Return the actions of Ghostwriter's routes that need a stated purpose.

    What ``/api/config`` reports, so the SPA's purpose bar shows a purpose as
    required exactly when a request needs one. An action in
    ``AUDIT_REQUIRE_PURPOSE`` that no route records, as in a list shared with
    other services, refuses nothing here and is left out.

    Args:
        settings: The settings in force.

    Returns:
        The actions, sorted.
    """
    return sorted(settings.require_purpose & ENDPOINT_ACTIONS)


def _operating_mode(settings: Settings) -> dict[str, object]:
    """Describe how far this process trusts its callers and what it keeps, for the start-up record.

    Args:
        settings: The settings in force.

    Returns:
        The criteria of the ``admin`` / ``config.audit`` event. Never a secret, a key or an account.
    """
    return {
        "edge_secret_enforced": settings.edge_secret is not None,
        "default_identity_enabled": settings.default_identity is not None and settings.edge_secret is None,
        "require_purpose": sorted(settings.require_purpose),
        "chat_retention_days": settings.chat_days,
        "file_retention_days": settings.file_days,
        "notice_days": settings.notice_days,
        "speech_input": settings.speech_input,
        "vision": settings.vision,
        "workbench": settings.workbench_enabled,
        "media": settings.nextext_enabled,
    }


def open_emitter(version: str, stream: TextIO | None = None) -> AuditEmitter:
    """Create this process's emitter, not yet started.

    Args:
        version: The running release, for every event.
        stream: Where the chain goes; where ``AUDIT_OUTPUT`` selects when omitted.

    Returns:
        The emitter.

    Raises:
        ValueError: If ``AUDIT_OUTPUT`` names an unknown mode.
        OSError: If the container stream cannot be opened.
    """
    if stream is None:
        stream = open_audit_stream(os.getenv(OUTPUT_ENV))
    return AuditEmitter(SERVICE, version, stream=stream)


def start_chain(app: FastAPI, emitter: AuditEmitter) -> None:
    """Open the emitter's chain, record the operating mode and make it the app's emitter.

    Args:
        app: The application, with its settings on ``app.state.settings``.
        emitter: The process's emitter.

    Raises:
        AuditWriteError: If an event cannot be written; startup must fail then.
    """
    settings: Settings = app.state.settings
    emitter.start()
    emitter.emit("admin", "config.audit", SYSTEM_ACTOR, criteria=_operating_mode(settings))
    setattr(app.state, EMITTER_STATE_KEY, emitter)


def get_emitter(request: Request) -> AuditEmitter:
    """Return the process's emitter, or refuse audited work without one.

    Args:
        request: The incoming request.

    Returns:
        The emitter the lifespan installed.

    Raises:
        HTTPException: 500 when no emitter is installed.
    """
    emitter = getattr(request.app.state, EMITTER_STATE_KEY, None)
    if not isinstance(emitter, AuditEmitter):
        logger.error("No audit emitter in this process; refusing audited work.")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed.")
    return emitter


def system_operation(emitter: AuditEmitter, action: str, operation: str) -> Operation:
    """Open an operation no request started, such as a retention sweep.

    Args:
        emitter: The process's emitter.
        action: The primary event's action.
        operation: The operation name.

    Returns:
        The operation, acting as the system under a fresh request id.
    """
    return Operation(emitter, RequestContext(SYSTEM_ACTOR, None, uuid4().hex), action, operation)


def forward_headers(context: RequestContext) -> dict[str, str]:
    """Return the headers that carry an operation's purpose and request id to workbench and to Nextext.

    Args:
        context: The operation's context.

    Returns:
        ``X-Audit-Purpose`` (the stated purpose, else the reason ``ghostwriter chat``) and ``X-Request-ID``.
    """
    purpose = context.purpose
    stated = encode_purpose(purpose.case_ref, purpose.reason) if purpose is not None else ""
    return {PURPOSE_HEADER: stated or encode_purpose(None, CHAT_REASON), REQUEST_ID_HEADER: context.request_id}


def _request_purpose(request: Request) -> Purpose | None:
    """Read the request's stated purpose.

    The purpose is personal data in its own right: it goes into audit events
    and to workbench and Nextext only, and is neither logged nor echoed.

    Args:
        request: The incoming request.

    Returns:
        The purpose, ``None`` when none is stated.

    Raises:
        HTTPException: 400 for a malformed, oversized or ambiguous header. No
            event: there is no valid operation yet.
    """
    try:
        case_ref, reason = parse_purpose_header(request.headers.get(PURPOSE_HEADER))
    except PurposeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request.") from exc
    return Purpose(case_ref=case_ref, reason=reason) if case_ref or reason else None


def begin(
    request: Request,
    principal: Principal,
    action: str,
    operation: str,
    *,
    also: Sequence[str] = (),
    container: str | None = None,
) -> Operation:
    """Open an audited operation for a request, refusing it when a required purpose is missing.

    Args:
        request: The incoming request.
        principal: Who it acts as.
        action: The operation's primary action.
        operation: The operation name.
        also: Further actions the operation may record (``ai_call`` for a chat
            message), checked against the purpose rule too.
        container: The enclosing object, when known.

    Returns:
        The open operation, registered on the request so a server error
        settles it (:func:`settle_failed_operations`).

    Raises:
        HTTPException: 400 for a malformed purpose header; 428 when a required
            purpose is missing, after recording ``denied``; 500 without an emitter.
        AuditWriteError: When the refusal could not be recorded.
    """
    emitter = get_emitter(request)
    context = RequestContext(
        actor=Actor(principal.name, principal.via),
        purpose=_request_purpose(request),
        request_id=clean_request_id(request.headers.get(REQUEST_ID_HEADER)) or uuid4().hex,
    )
    op = Operation(emitter, context, action, operation, container=container)
    operations: list[Operation] = getattr(request.state, OPERATIONS_STATE_KEY, [])
    setattr(request.state, OPERATIONS_STATE_KEY, [*operations, op])
    settings: Settings = request.app.state.settings
    if context.purpose is None and settings.require_purpose.intersection((action, *also)):
        # A refusal has no target, so the container it was about goes in its criteria.
        op.record(outcome="denied", criteria={"container": container} if container else None)
        raise HTTPException(status_code=PURPOSE_REQUIRED_STATUS, detail=_PURPOSE_REQUIRED_DETAIL)
    return op


def audited(action: str, operation: str, *, also: Sequence[str] = ()) -> Callable[..., Operation]:
    """Build the FastAPI dependency that opens a route's audited operation.

    It resolves, in this order, who acts (401, nothing recorded), the emitter
    (500, nothing recorded), the stated purpose (400, nothing recorded) and the
    purpose rule (428, recorded as ``denied``).

    Args:
        action: The operation's primary action.
        operation: The operation name.
        also: Further actions it may record.

    Returns:
        The dependency, which returns the request's :class:`Operation`.

    Raises:
        ValueError: If an action is not in :data:`ENDPOINT_ACTIONS`; a route
            with a new action adds it there first.
    """
    unknown = sorted({action, *also} - ENDPOINT_ACTIONS)
    if unknown:
        raise ValueError(f"not an endpoint audit action: {', '.join(unknown)} (add it to ENDPOINT_ACTIONS)")

    def dependency(request: Request, principal: Annotated[Principal, Depends(resolve_principal)]) -> Operation:
        """Open the operation for this request.

        Args:
            request: The incoming request.
            principal: Who it acts as.

        Returns:
            The open operation.
        """
        return begin(request, principal, action, operation, also=also)

    return dependency


def settle_quietly(
    op: Operation, *, target: Target | None = None, criteria: Mapping[str, object] | None = None
) -> None:
    """Record an interrupted operation as failed, unless it has an outcome already.

    For work that is failing anyway: a write that fails here is logged by its
    exception type only, and the failure the caller is handling goes on.

    Args:
        op: The interrupted operation.
        target: What it touched before failing, if known.
        criteria: What is known about how far it got.
    """
    if op.settled:
        return
    try:
        op.fail(target=target, criteria=criteria)
    except Exception as exc:
        logger.error("Could not record the failure of audited operation '{}': {}", op.operation, type(exc).__name__)


def settle_failed_operations(request: Request) -> None:
    """Record every operation a request began and did not record, as it ends in a server error.

    Args:
        request: The failing request.
    """
    operations: list[Operation] = getattr(request.state, OPERATIONS_STATE_KEY, [])
    for op in operations:
        settle_quietly(op)
