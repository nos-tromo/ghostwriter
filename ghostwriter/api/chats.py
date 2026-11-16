"""The chats: list, new, read, rename and delete, each for its owner alone.

There is no admin path and no ``owner`` parameter: every lookup names the
request's own account, and another owner's chat answers as an unknown one does,
404 ``Chat not found.``. Events name a chat by its id, never by its title, and
each is written before the action takes effect, so an action whose event cannot
be written does not happen.

A delete (spec §6.2) is refused while an answer runs, and holds the chat's turn
while it runs itself; holding it, it looks the chat up again, so of two deletes
only the first acts. It records ``chat.delete``, then asks workbench to delete
the chat's folder, and deletes the chat's rows only after that worked. If
workbench fails, the rows stay and the user may try again.
"""

from functools import partial
from typing import Annotated, Final
from uuid import uuid4

import anyio.from_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, StringConstraints

from ghostwriter.audit import Operation, audited, forward_headers
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.schemas import ChatDetail, ChatOut
from ghostwriter.store import ChatNotFound, ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import Target
from ghostwriter.workbench import CallFor, WorkbenchClient, WorkbenchUnavailable

router = APIRouter(prefix="/api/chats", tags=["Chats"])

#: The answer for another owner's chat and an unknown one alike.
CHAT_NOT_FOUND: Final = "Chat not found."
ANSWER_RUNNING: Final = "Stop the answer before deleting the chat."
FILES_NOT_DELETED: Final = "The chat's files could not be deleted; try again."
#: The longest title an owner may give a chat, after stripping.
MAX_TITLE_CHARS: Final = 200

User = Annotated[Principal, Depends(resolve_principal)]


class RenameIn(BaseModel):
    """A chat's new title.

    Attributes:
        title: 1 to :data:`MAX_TITLE_CHARS` characters after stripping.
    """

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_CHARS)]


def _store(request: Request) -> ChatStore:
    store: ChatStore = request.app.state.store
    return store


def _chat(chat_id: str) -> Target:
    return Target(kind="chat", ids=(chat_id,))


def owned_chat(store: ChatStore, owner: str, chat_id: str) -> ChatOut:
    """Return the owner's chat, or answer 404 as for an unknown one.

    Args:
        store: The chats.
        owner: Who asks.
        chat_id: The chat.

    Returns:
        The chat.

    Raises:
        HTTPException: 404 ``Chat not found.`` when the owner has no chat with that id.
    """
    try:
        return store.get_chat(owner, chat_id)
    except ChatNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None


@router.get("", summary="The user's chats")
def list_chats(
    request: Request, user: User, op: Annotated[Operation, Depends(audited("query", "chat.list"))]
) -> list[ChatOut]:
    """List the user's chats, the latest activity first.

    Args:
        request: The request.
        user: Who asks.
        op: ``query`` / ``chat.list``, recorded with the count.

    Returns:
        The chats.
    """
    chats = _store(request).list_chats(user.name)
    op.record(target=Target(kind="chat", count=len(chats)))
    return chats


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start a chat")
def create_chat(
    request: Request, user: User, op: Annotated[Operation, Depends(audited("collect", "chat.create"))]
) -> ChatOut:
    """Start an untitled chat; its id is minted and recorded before the chat exists.

    Args:
        request: The request.
        user: Who starts it.
        op: ``collect`` / ``chat.create``.

    Returns:
        The chat.
    """
    chat_id = str(uuid4())
    op.record(target=_chat(chat_id))
    return _store(request).create_chat(user.name, id=chat_id)


@router.get("/{chat_id}", summary="A chat with its messages")
def read_chat(
    chat_id: str, request: Request, user: User, op: Annotated[Operation, Depends(audited("query", "chat.read"))]
) -> ChatDetail:
    """Return the user's chat with its messages, steps, results and pending attachments.

    Args:
        chat_id: The chat.
        request: The request.
        user: Who asks.
        op: ``query`` / ``chat.read``, recorded with what the chat holds.

    Returns:
        The chat.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one.
    """
    store = _store(request)
    try:
        detail = store.chat_detail(user.name, chat_id)
    except ChatNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None
    op.record(target=_chat(chat_id), criteria=store.chat_counts(chat_id))
    return detail


@router.patch("/{chat_id}", summary="Rename a chat")
def rename_chat(
    chat_id: str,
    body: RenameIn,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("modify", "chat.rename"))],
) -> ChatOut:
    """Give the user's chat a title; the event names the chat, never the title.

    Args:
        chat_id: The chat.
        body: The title.
        request: The request.
        user: Who asks.
        op: ``modify`` / ``chat.rename``.

    Returns:
        The chat.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one.
    """
    store = _store(request)
    owned_chat(store, user.name, chat_id)
    op.record(target=_chat(chat_id))
    try:
        return store.rename_chat(user.name, chat_id, body.title)
    except ChatNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a chat")
def delete_chat(
    chat_id: str, request: Request, user: User, op: Annotated[Operation, Depends(audited("delete", "chat.delete"))]
) -> None:
    """Delete the user's chat: record it, delete its folder in workbench, then its rows.

    Runs in a worker thread; only the call to workbench runs on the event loop.

    Args:
        chat_id: The chat.
        request: The request.
        user: Who asks.
        op: ``delete`` / ``chat.delete``, recorded first with what the chat holds; its child
            ``delete`` / ``chat.workbench`` records how many files workbench deleted, or why not.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one, also once another delete
            removed it; 409 while an answer runs in it; 502 when workbench could not delete its
            files, the chat then kept.
    """
    store = _store(request)
    owned_chat(store, user.name, chat_id)
    turns: TurnRegistry = request.app.state.turns
    if turns.begin(chat_id) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ANSWER_RUNNING)
    try:
        owned_chat(store, user.name, chat_id)
        op.record(target=_chat(chat_id), criteria=store.chat_counts(chat_id))
        workbench: WorkbenchClient | None = request.app.state.workbench
        if workbench is None:
            op.child("delete", "chat.workbench", criteria={"workbench": "not_configured"})
        else:
            call = CallFor(user.name, chat_id, forward_headers(op.context))
            try:
                deleted = anyio.from_thread.run(partial(workbench.delete_chat, call=call))
            except WorkbenchUnavailable as exc:
                op.child("delete", "chat.workbench", criteria={"reason": exc.token}, outcome="error")
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=FILES_NOT_DELETED) from None
            op.child("delete", "chat.workbench", criteria={"files_deleted": deleted})
        store.delete_chat_rows(chat_id)
    finally:
        turns.end(chat_id)
