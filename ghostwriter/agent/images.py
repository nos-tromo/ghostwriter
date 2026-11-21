"""Images in a round's messages: refs while the round is fitted, data URLs only as it goes out.

An image enters a turn's messages as a ref part, ``{"type": "image_ref", ...}``: an attached
picture's rendition, or one of its strips, in the user's message (:func:`attachment_parts`), or an
image a tool showed, in the one user message that follows a round's tool messages
(:func:`tool_image_message`). A ref never leaves the process.
:func:`~ghostwriter.agent.context.fit` counts it by :func:`estimate_image_tokens`, never by
characters, and puts a note in its place where it gives way (:data:`RETIRED`, :data:`OVER_CAP`,
:data:`NOT_RESENT`). Only :func:`materialise`, just before the model call, swaps each ref for its
label and a data URL, in a new list, from the bytes of the message's :data:`ImageBank`: a data URL
exists in that list alone, never in a log, the store or the audit.

An image costs a token per started :data:`TOKEN_SIDE`-pixel square, or, for a model that takes every
image at one size, the fixed cost ``GHOSTWRITER_IMAGE_CONTEXT_TOKENS``. At a fixed cost, a picture or
page whose long side is over :data:`STRIP_LONG_SIDE` goes as two strips, so that its detail is not lost
to the model's one size.
"""

import base64
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final, TypeAlias, TypeGuard

from ghostwriter.upload import IMAGE_TYPES
from ghostwriter.workbench import ToolImage

#: The long side, in pixels, above which a picture or page goes as two strips at a fixed cost.
STRIP_LONG_SIDE: Final = 1024
#: The side, in pixels, of the square an image costs one token per, without a fixed cost.
TOKEN_SIDE: Final = 32
#: The type of a ref part.
REF: Final = "image_ref"

#: What precedes a call's images in the message after the round's tool messages.
LEGEND: Final = "[Images returned by {tool} for call {call_id}: {labels}. Not a message from the user.]"
#: What precedes an image sent to the model.
LABEL: Final = "[Image: {label}]"
#: A tool image of the current turn that gave way to the budget or the cap.
RETIRED: Final = "[Image: {label}; not shown again to make room, view it again with view_file]"
#: An image of the question past the cap.
OVER_CAP: Final = "[Image: {label}; not shown, at most {max} images go to the model at once; view_file shows it]"
#: An image of an earlier turn that is not sent again.
NOT_RESENT: Final = "[Image: {label}; not shown again, view_file shows it]"
#: An image whose bytes could not be fetched.
NOT_LOADED: Final = "[Image: {label}; could not be loaded, view_file shows it]"
#: An image of a round the model refused with images, sent again without them.
LEFT_OUT: Final = "[Image: {label}; left out, the model did not take images]"

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
        ``None`` without a fixed cost, and for a file that is neither a PDF nor a picture: the
        call then carries no ``strips``. 2 for a PDF, whose pages are rendered larger than
        :data:`STRIP_LONG_SIDE`; for a picture, :func:`strips_for_image` on its known size, or 1.
    """
    if fixed is None:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".pdf":
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
        with ``, strip i of k`` past one strip. Each carries its first rendition's size.
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
        ``tool:{call_id}:{i}`` from 1; and the bank entries of those keys.
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
            content.append(
                {
                    "type": REF,
                    "key": key,
                    "origin": "tool",
                    "label": image.label,
                    "width": image.width,
                    "height": image.height,
                }
            )
            bank[key] = (image.mime, image.data)
    return {"role": "user", "content": content}, bank


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
    messages: Sequence[Mapping[str, Any]], bank: Mapping[str, tuple[str, bytes]]
) -> tuple[list[dict[str, Any]], int]:
    """Return the messages as the model is sent them: each ref as its label and a data URL.

    Args:
        messages: The fitted messages; never changed.
        bank: The bytes of the message's images, by ref key.

    Returns:
        A new list, in which each ref is its :data:`LABEL` and an ``image_url`` part, or its
        :data:`NOT_LOADED` note when its key is not in the bank; and how many images it carries.
    """
    count = 0

    def sent(part: Any) -> list[dict[str, Any]]:
        nonlocal count
        if not is_ref(part):
            return [dict(part)]
        found = bank.get(part["key"])
        if found is None:
            return [note(part, NOT_LOADED)]
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
    """Retire the current turn's tool images but the newest ``keep``, in place.

    Each becomes its :data:`RETIRED` note in its message and leaves the bank; attached images stay.

    Args:
        current: The current turn's messages.
        bank: The bytes of the message's images, by ref key.
        keep: How many of the newest tool images stay.
    """
    found = [
        (message, index)
        for message in current
        for index, part in enumerate(parts_of(message))
        if is_ref(part) and part.get("origin") == "tool"
    ]
    for message, index in found[: max(0, len(found) - keep)]:
        ref = message["content"][index]
        message["content"][index] = note(ref, RETIRED)
        bank.pop(ref["key"], None)
