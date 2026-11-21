"""Images in a round's messages: refs while the round is fitted, data URLs only as it goes out.

An image enters a turn's messages as a ref part, ``{"type": "image_ref", ...}``: an attached
picture's rendition, or one of its strips, in the user's message (:func:`attachment_parts`), or an
image a tool showed, in the one user message that follows a round's tool messages
(:func:`tool_image_message`). A ref never leaves the process.
:func:`~ghostwriter.agent.context.fit` counts it by :func:`estimate_image_tokens`, never by
characters, and puts a note in its place where it gives way (:data:`RETIRED`, :data:`OVER_CAP`,
:data:`UNSEEN`, :data:`NO_ROOM`, :data:`NOT_RESENT`). In a round that does not offer ``view_file``, a
note never sends the model to it: each note that would has a form without that clause
(:data:`WITHOUT_VIEW`, :func:`told`). A picture gives way whole: each ref names the ``picture`` it
shows part of, and the strips of one picture go together (:func:`siblings`). Only
:func:`materialise`, just before the model call, swaps each ref for its label and a data URL, in a
new list, from the bytes of the message's :data:`ImageBank`: a data URL exists in that list alone,
never in a log, the store or the audit.

An image costs a token per started :data:`TOKEN_SIDE`-pixel square, or, for a model that takes every
image at one size, the fixed cost ``GHOSTWRITER_IMAGE_CONTEXT_TOKENS``. At a fixed cost, a picture or
page whose long side is over :data:`STRIP_LONG_SIDE` goes as two strips, so that its detail is not lost
to the model's one size.
"""

import base64
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final, TypeAlias, TypeGuard

from ghostwriter.upload import IMAGE_TYPES, OFFICE_SUFFIXES
from ghostwriter.workbench import ToolImage

#: The long side, in pixels, above which a picture or page goes as two strips at a fixed cost.
STRIP_LONG_SIDE: Final = 1024
#: The side, in pixels, of the square an image costs one token per, without a fixed cost.
TOKEN_SIDE: Final = 32
#: The type of a ref part.
REF: Final = "image_ref"
#: A tool image's label that names one strip of a picture or page, as workbench writes it.
_STRIP_LABEL: Final = re.compile(r"(?P<whole>.+), strip [1-9][0-9]* of [1-9][0-9]*")

#: What precedes a call's images in the message after the round's tool messages.
LEGEND: Final = "[Images returned by {tool} for call {call_id}: {labels}. Not a message from the user.]"
#: What precedes an image sent to the model.
LABEL: Final = "[Image: {label}]"
#: A tool image of the current turn that gave way to the budget or the cap.
RETIRED: Final = "[Image: {label}; not shown again to make room, view it again with view_file]"
#: An image of the question past the cap.
OVER_CAP: Final = "[Image: {label}; not shown, at most {max} images go to the model at once; view_file shows it]"
#: An image of the newest tool images, never shown, for which the request has no room.
UNSEEN: Final = "[Image: {label}; not shown, there is no room for it in this request]"
#: A picture of the question for which the request has no room.
NO_ROOM: Final = "[Image: {label}; not shown, there is no room for it in this request; view_file shows it]"
#: An image of an earlier turn that is not sent again.
NOT_RESENT: Final = "[Image: {label}; not shown again, view_file shows it]"
#: An image whose bytes could not be fetched.
NOT_LOADED: Final = "[Image: {label}; could not be loaded, view_file shows it]"
#: An image of a round the model refused with images, sent again without them.
LEFT_OUT: Final = "[Image: {label}; left out, the model did not take images]"
#: Each note that sends the model to ``view_file``, and its form for a round that does not offer the tool.
WITHOUT_VIEW: Final[Mapping[str, str]] = {
    RETIRED: "[Image: {label}; not shown again to make room]",
    OVER_CAP: "[Image: {label}; not shown, at most {max} images go to the model at once]",
    NO_ROOM: "[Image: {label}; not shown, there is no room for it in this request]",
    NOT_RESENT: "[Image: {label}; not shown again]",
    NOT_LOADED: "[Image: {label}; could not be loaded]",
}
#: Each of those notes as it reads once written, with its label and count as groups.
_VIEW_NOTES: Final = tuple(
    (
        re.compile(
            re.escape(template)
            .replace(re.escape("{label}"), "(?P<label>.*)")
            .replace(re.escape("{max}"), "(?P<max>[0-9]+)"),
            re.DOTALL,
        ),
        blind,
    )
    for template, blind in WITHOUT_VIEW.items()
)

#: The bytes of a message's images, by ref key: their media type and bytes. It lives for one message.
ImageBank: TypeAlias = dict[str, tuple[str, bytes]]


@dataclass(frozen=True)
class AttachedImage:
    """A picture the user attached that is ready to be seen.

    Attributes:
        file_id: workbench's id of the file.
        path: Its path in the chat's folder.
        name: The file name; for the model, never for a log.
        width: The width of its first rendition, in pixels.
        height: The height of its first rendition, in pixels.
    """

    file_id: str
    path: str
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageBudget:
    """What images cost a round, and how many it may carry.

    Attributes:
        fixed: The tokens one image costs, or ``None`` to count it by its pixels.
        max_images: The most images one request carries.
    """

    fixed: int | None
    max_images: int


def estimate_image_tokens(width: int, height: int, *, fixed: int | None) -> int:
    """Estimate the tokens an image takes of the context.

    Args:
        width: Its width in pixels.
        height: Its height in pixels.
        fixed: The fixed cost of an image, or ``None``.

    Returns:
        ``fixed``, else a token per started :data:`TOKEN_SIDE` x :data:`TOKEN_SIDE` square.
    """
    if fixed is not None:
        return fixed
    return math.ceil(width / TOKEN_SIDE) * math.ceil(height / TOKEN_SIDE)


def strips_for_image(width: int, height: int, *, fixed: int | None) -> int:
    """Say into how many strips a picture or page goes to the model.

    Args:
        width: Its width in pixels.
        height: Its height in pixels.
        fixed: The fixed cost of an image, or ``None``.

    Returns:
        2 at a fixed cost when the long side is over :data:`STRIP_LONG_SIDE`, else 1.
    """
    return 2 if fixed is not None and max(width, height) > STRIP_LONG_SIDE else 1


def strips_for_path(path: str, *, fixed: int | None, known: Mapping[str, tuple[int, int]]) -> int | None:
    """Say how many strips a ``view_file`` or ``read_document`` call asks for, by the file it reads.

    Args:
        path: The file's path, as the model wrote it.
        fixed: The fixed cost of an image, or ``None``.
        known: The sizes of the pictures the chat has attached, by path.

    Returns:
        ``None`` without a fixed cost, and for a file that is neither a PDF, an Office file nor a
        picture: the call then carries no ``strips``. 2 for a PDF or an Office file
        (:data:`~ghostwriter.upload.OFFICE_SUFFIXES`), whose pages are rendered larger than
        :data:`STRIP_LONG_SIDE`; for a picture, :func:`strips_for_image` on its known size, or 1.
    """
    if fixed is None:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".pdf" or suffix in OFFICE_SUFFIXES:
        return 2
    if suffix not in IMAGE_TYPES:
        return None
    size = known.get(path)
    return 1 if size is None else strips_for_image(size[0], size[1], fixed=fixed)


def rendition_size(manifest: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """Return the size of a stored manifest's first rendition.

    Args:
        manifest: The manifest as a chat keeps it, or ``None``.

    Returns:
        Its width and height in pixels, or ``None`` without a first rendition of a positive size.
    """
    renditions = None if manifest is None else manifest.get("renditions")
    if not isinstance(renditions, list):
        return None
    for rendition in renditions:
        if not isinstance(rendition, Mapping) or rendition.get("n") != 1:
            continue
        width, height = rendition.get("width"), rendition.get("height")
        if _pixels(width) and _pixels(height):
            return width, height
    return None


def _pixels(value: object) -> TypeGuard[int]:
    """Tell whether a manifest's value is a size in pixels.

    Args:
        value: The value.

    Returns:
        Whether it is a positive integer, not a bool.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def attachment_parts(images: Sequence[AttachedImage], *, fixed: int | None) -> list[dict[str, Any]]:
    """Return the refs of the pictures a user attached, one per strip.

    Args:
        images: The pictures.
        fixed: The fixed cost of an image, or ``None``: it sets the strips.

    Returns:
        The refs, each keyed ``att:{file_id}:{strip}/{strips}`` and labelled by the file's name,
        with ``, strip i of k`` past one strip. Each carries its first rendition's size, and the
        picture ``att:{file_id}`` that its strips share.
    """
    parts: list[dict[str, Any]] = []
    for image in images:
        strips = strips_for_image(image.width, image.height, fixed=fixed)
        for strip in range(1, strips + 1):
            parts.append(
                {
                    "type": REF,
                    "key": f"att:{image.file_id}:{strip}/{strips}",
                    "origin": "attachment",
                    "picture": f"att:{image.file_id}",
                    "label": image.name if strips == 1 else f"{image.name}, strip {strip} of {strips}",
                    "width": image.width,
                    "height": image.height,
                    "path": image.path,
                    "n": 1,
                    "strip": strip,
                    "strips": strips,
                }
            )
    return parts


def tool_image_message(groups: Sequence[tuple[str, str, Sequence[ToolImage]]]) -> tuple[dict[str, Any], ImageBank]:
    """Return the one user message that carries a round's tool images, and their bytes.

    Args:
        groups: Per call that showed images, in the round's order: the tool, the call's id and
            the images. A call without images is left out.

    Returns:
        The message: per call a :data:`LEGEND` text part, then its refs, keyed
        ``tool:{call_id}:{i}`` from 1; and the bank entries of those keys. A ref's picture is
        ``tool:{call_id}:{whole}`` for a label ``{whole}, strip i of k``, so the call's strips of
        one page share it, and else its own key.
    """
    content: list[dict[str, Any]] = []
    bank: ImageBank = {}
    for tool, call_id, images in groups:
        if not images:
            continue
        labels = "; ".join(image.label for image in images)
        content.append({"type": "text", "text": LEGEND.format(tool=tool, call_id=call_id, labels=labels)})
        for i, image in enumerate(images, start=1):
            key = f"tool:{call_id}:{i}"
            strip = _STRIP_LABEL.fullmatch(image.label)
            content.append(
                {
                    "type": REF,
                    "key": key,
                    "origin": "tool",
                    "picture": key if strip is None else f"tool:{call_id}:{strip['whole']}",
                    "label": image.label,
                    "width": image.width,
                    "height": image.height,
                }
            )
            bank[key] = (image.mime, image.data)
    return {"role": "user", "content": content}, bank


def told(template: str, *, view: bool) -> str:
    """Return a note's template as a round tells it.

    Args:
        template: One of the texts of this module.
        view: Whether the round offers ``view_file``.

    Returns:
        ``template``; without ``view``, its form that names no ``view_file`` (:data:`WITHOUT_VIEW`), when it has one.
    """
    return template if view else WITHOUT_VIEW.get(template, template)


def _blind_note(part: object) -> dict[str, Any] | None:
    """Return a written note that sends the model to ``view_file`` in its form without the tool.

    Args:
        part: A content part.

    Returns:
        The text part :data:`WITHOUT_VIEW` has for it, with its label and count; ``None`` for any other part.
    """
    text = part.get("text") if isinstance(part, Mapping) and part.get("type") == "text" else None
    if not isinstance(text, str):
        return None
    for pattern, form in _VIEW_NOTES:
        found = pattern.fullmatch(text)
        if found is not None:
            return {"type": "text", "text": form.format(**found.groupdict())}
    return None


def forget_view_file(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return messages whose written notes no longer send the model to ``view_file``.

    For a turn that goes on without the tool, as after a refusal: the notes a round wrote into its messages
    (a retired tool image above all) are told again as :data:`WITHOUT_VIEW` has them. Pass only messages whose
    text parts are this module's own, never the user's question.

    Args:
        messages: The messages; never changed.

    Returns:
        A new list; a message with such a note gets a new content list.
    """
    copied: list[dict[str, Any]] = []
    for message in messages:
        parts = parts_of(message)
        blinded = [_blind_note(part) for part in parts]
        if all(blind is None for blind in blinded):
            copied.append(dict(message))
        else:
            content = [dict(part) if blind is None else blind for part, blind in zip(parts, blinded, strict=True)]
            copied.append({**message, "content": content})
    return copied


def is_ref(part: object) -> bool:
    """Tell whether a content part is an image ref.

    Args:
        part: The part.

    Returns:
        Whether it is.
    """
    return isinstance(part, Mapping) and part.get("type") == REF


def parts_of(message: Mapping[str, Any]) -> list[Any]:
    """Return a message's content parts.

    Args:
        message: The message.

    Returns:
        Its content when that is a list of parts, else no parts.
    """
    content = message.get("content")
    return content if isinstance(content, list) else []


def refs(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the image refs of messages, in order.

    Args:
        messages: The messages.

    Returns:
        The refs.
    """
    return [part for message in messages for part in parts_of(message) if is_ref(part)]


def ref_places(messages: Sequence[dict[str, Any]], *, origin: str) -> list[tuple[dict[str, Any], int]]:
    """Return where the image refs of one origin are, in order.

    Args:
        messages: The messages.
        origin: ``attachment`` or ``tool``.

    Returns:
        Each ref's message and its index in the message's content.
    """
    return [
        (message, index)
        for message in messages
        for index, part in enumerate(parts_of(message))
        if is_ref(part) and part.get("origin") == origin
    ]


def newest_tool_images(messages: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the message that carries the latest round's tool images, which the model has not seen yet.

    A round's tool images follow its tool messages as the turn's last message, until the next
    round. Once a round whose results hold no image follows, no tool image is the newest.

    Args:
        messages: The current turn's messages, in order (a system message before them changes nothing).

    Returns:
        The last message, when it holds a tool image's ref; else ``None``.
    """
    if not messages:
        return None
    last = messages[-1]
    return last if any(is_ref(part) and part.get("origin") == "tool" for part in parts_of(last)) else None


def siblings(message: Mapping[str, Any], index: int) -> list[int]:
    """Return the refs of a message that show the same picture as one of them: its strips.

    Args:
        message: The message.
        index: The ref's index in its content.

    Returns:
        The indexes of the refs whose ``picture`` is the ref's, itself included, in order; a ref
        without a picture is its own.
    """
    parts = parts_of(message)
    picture = parts[index].get("picture", parts[index]["key"])
    return [i for i, part in enumerate(parts) if is_ref(part) and part.get("picture", part["key"]) == picture]


def note(ref: Mapping[str, Any], template: str, **values: Any) -> dict[str, Any]:
    """Return the text part that names an image in its place.

    Args:
        ref: The image's ref.
        template: One of the texts of this module, with ``{label}``.
        **values: The template's other fields, such as ``max``.

    Returns:
        The text part.
    """
    return {"type": "text", "text": template.format(label=ref["label"], **values)}


def materialise(
    messages: Sequence[Mapping[str, Any]], bank: Mapping[str, tuple[str, bytes]], *, view: bool = True
) -> tuple[list[dict[str, Any]], int]:
    """Return the messages as the model is sent them: each ref as its label and a data URL.

    Args:
        messages: The fitted messages; never changed.
        bank: The bytes of the message's images, by ref key.
        view: Whether the round offers ``view_file``, which the :data:`NOT_LOADED` note names.

    Returns:
        A new list, in which each ref is its :data:`LABEL` and an ``image_url`` part, or its
        :data:`NOT_LOADED` note (:func:`told`) when its key is not in the bank; and how many images it carries.
    """
    count = 0

    def sent(part: Any) -> list[dict[str, Any]]:
        nonlocal count
        if not is_ref(part):
            return [dict(part)]
        found = bank.get(part["key"])
        if found is None:
            return [note(part, told(NOT_LOADED, view=view))]
        mime, data = found
        count += 1
        url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return [note(part, LABEL), {"type": "image_url", "image_url": {"url": url}}]

    wire = [_with_parts(message, sent) for message in messages]
    return wire, count


def without_images(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the messages with every image ref as its :data:`LEFT_OUT` note.

    Args:
        messages: The fitted messages; never changed.

    Returns:
        A new list without refs.
    """
    return [
        _with_parts(message, lambda part: [note(part, LEFT_OUT) if is_ref(part) else dict(part)])
        for message in messages
    ]


def _with_parts(message: Mapping[str, Any], swap: Callable[[Any], list[dict[str, Any]]]) -> dict[str, Any]:
    """Return a copy of a message whose refs are swapped.

    Args:
        message: The message; never changed.
        swap: Takes one of its parts and returns the parts that take its place.

    Returns:
        The copy; its content a new list where it held a ref.
    """
    parts = parts_of(message)
    if not any(is_ref(part) for part in parts):
        return dict(message)
    return {**message, "content": [new for part in parts for new in swap(part)]}


def retire_tool_images(current: list[dict[str, Any]], bank: ImageBank, *, keep: int) -> None:
    """Retire the current turn's tool images but the newest ``keep``, oldest first and whole pictures, in place.

    Each becomes a note in its message and leaves the bank: :data:`RETIRED` for an image the model
    has been shown, and :data:`UNSEEN` for one of the newest tool images (:func:`newest_tool_images`),
    which it never was. A picture's strips go together, so a few more may go than ``keep`` asks.
    Attached images stay.

    Args:
        current: The current turn's messages.
        bank: The bytes of the message's images, by ref key.
        keep: How many of the newest tool images stay.
    """
    places = ref_places(current, origin="tool")
    newest = newest_tool_images(current)
    over = len(places) - keep
    for message, index in places:
        if over <= 0:
            break
        if not is_ref(message["content"][index]):
            continue
        template = UNSEEN if message is newest else RETIRED
        for i in siblings(message, index):
            ref = message["content"][i]
            message["content"][i] = note(ref, template)
            bank.pop(ref["key"], None)
            over -= 1
