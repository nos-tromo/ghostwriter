"""The system prompt: who answers, in which language, and what the chat's tools and files are."""

from datetime import date
from typing import Final

#: The rule for a presentation, given only when workbench offers ``write_presentation``: the user sees an
#: outline first and agrees to it before any deck is built, in the user's slide master or, with none in the chat,
#: in workbench's plain one.
PRESENTATIONS: Final = (
    "To build a presentation, use the user's slide master (.pptx or .potx) if the chat has one, else the plain "
    "16:9 master (leave path out of inspect_template and write_presentation). Call inspect_template and read the "
    "sources, then show the user an outline (each slide's title, the layout's number and name with its places in "
    "order, key points, figures and sources) and stop. Call write_presentation only in a later message, after the "
    "user has agreed. Images for slides are made with run_python; prefer native charts."
)
#: How the model looks at pictures and pages, given only while it sees and has the tools.
VISION: Final = (
    "Images the user attached are in their message: look at them. Read a document with read_document first; "
    "pages without text come back as images in the same answer. For how something looks (layout, design, "
    "charts, handwriting, photos), look with view_file, at most 4 pages a call. Images tools showed earlier are "
    "not sent again, and attached pictures only while there is room: use view_file to see one again. You can see "
    "the pictures, PDF, Word, OpenDocument text and PowerPoint files of this chat, also those you made yourself: "
    "when the user asks whether you can see one, or how it looks, look at it with view_file instead of saying you "
    "cannot."
)
#: How the model takes a video or audio clip the user attached, given only while it sees, has the tools and clips are
#: offered: the transcript is text, the frames are pictures, which only ``view_file`` shows.
MEDIA: Final = (
    "A video or audio file the user attached comes with a transcript: read it with read_document. A video's frames "
    "are pictures of it over time: look at them with view_file ('frame 3', 'frames 2-5', 'at 01:24')."
)
#: How the model checks and restyles a deck it built, given only while it sees and write_presentation is offered:
#: it looks at the slides it wrote and mends what is broken without changing what the user agreed to, and a better
#: look the user asks for is made with the tools it has, not declined.
DECK_CHECK: Final = (
    "After write_presentation, look at the new deck's first slides with view_file. Mend what is broken (text that "
    "overflows or overlaps, an empty place) with run_python or by writing the deck again from the agreed outline; "
    "change its content only when the user asks. When the user asks for a better look, pick layouts with pictures, "
    "native charts and tables, and set colours, fonts, backgrounds and shapes with python-pptx in run_python, which "
    "writes the restyled deck as a new file; look at it again, and name the file that is now the deck."
)


def system_prompt(
    *,
    today: date,
    folder: str | None,
    file_days: int,
    expired: bool,
    presentations: bool = False,
    vision: bool = False,
    media: bool = False,
) -> str:
    """Return the system prompt of a message.

    Args:
        today: The date the message is answered on.
        folder: The chat's folder in workbench; ``None`` when no tool is offered.
        file_days: How long workbench keeps a chat's files.
        expired: Whether files or tool results of earlier messages have expired.
        presentations: Whether workbench offers ``write_presentation``; the outline-first rule is given
            only then, and only when tools are offered.
        vision: Whether the model sees images and is offered ``view_file``; :data:`VISION` follows the
            sentences on the chat's folder only then, and only when tools are offered. With ``presentations``
            too, :data:`DECK_CHECK` follows the outline-first rule.
        media: Whether video and audio are offered; :data:`MEDIA` follows :data:`VISION` only then, and only
            with ``vision``, since a clip's frames are looked at with ``view_file``.

    Returns:
        The prompt, its sentences joined by single spaces.
    """
    parts = [
        f"You are Ghostwriter in this organisation's workspace. Today is {today:%Y-%m-%d}. Answer in the language "
        "of the user's last message, in Markdown; write tables as Markdown tables."
    ]
    if folder is not None:
        parts.append(
            f"This chat's files are in {folder}; files the user attached are named in their message. Read a file "
            "with the tools before you answer about it, and never guess its content. Python runs in a sandbox "
            "without network. To turn a file's data into a table or a document, process the file itself with "
            "run_python; use write_table only for a few rows you write yourself. Files a tool writes are shown to "
            "the user below your answer with a download button: name them, and do not repeat their content or write "
            f"links to them. Files and tool results are deleted {file_days} days after they were made."
        )
        if vision:
            parts.append(VISION)
            if media:
                parts.append(MEDIA)
        if presentations:
            parts.append(PRESENTATIONS)
            if vision:
                parts.append(DECK_CHECK)
    else:
        parts.append("You have no tools in this chat, and files cannot be read.")
    if expired:
        parts.append(
            "Some files and tool results of earlier messages in this chat have been deleted, as "
            f"{file_days} days had passed. Say so when the user asks about them, and ask them to attach the "
            "file again."
        )
    return " ".join(parts)
