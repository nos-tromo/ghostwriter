"""The system prompt: who answers, in which language, and what the chat's tools and files are."""

from datetime import date


def system_prompt(*, today: date, folder: str | None, file_days: int, expired: bool) -> str:
    """Return the system prompt of a message.

    Args:
        today: The date the message is answered on.
        folder: The chat's folder in workbench; ``None`` when no tool is offered.
        file_days: How long workbench keeps a chat's files.
        expired: Whether files or tool results of earlier messages have expired.

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
            "without network. Files a tool writes are shown to the user below your answer with a download button: "
            "name them, and do not repeat their content or write links to them. Files and tool results are "
            f"deleted {file_days} days after they were made."
        )
    else:
        parts.append("You have no tools in this chat, and files cannot be read.")
    if expired:
        parts.append(
            "Some files and tool results of earlier messages in this chat have been deleted, as "
            f"{file_days} days had passed. Say so when the user asks about them, and ask them to attach the "
            "file again."
        )
    return " ".join(parts)
