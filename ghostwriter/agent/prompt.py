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


def system_prompt(
    *, today: date, folder: str | None, file_days: int, expired: bool, presentations: bool = False
) -> str:
    """Return the system prompt of a message.

    Args:
        today: The date the message is answered on.
        folder: The chat's folder in workbench; ``None`` when no tool is offered.
        file_days: How long workbench keeps a chat's files.
        expired: Whether files or tool results of earlier messages have expired.
        presentations: Whether workbench offers ``write_presentation``; the outline-first rule is given
            only then, and only when tools are offered.

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
        if presentations:
            parts.append(PRESENTATIONS)
    else:
        parts.append("You have no tools in this chat, and files cannot be read.")
    if expired:
        parts.append(
            "Some files and tool results of earlier messages in this chat have been deleted, as "
            f"{file_days} days had passed. Say so when the user asks about them, and ask them to attach the "
            "file again."
        )
    return " ".join(parts)
