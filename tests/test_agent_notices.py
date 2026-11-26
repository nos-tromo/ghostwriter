"""What one message's calls opened and read: ``document_partial`` and ``document_unread``.

Every name, id and value here is invented.
"""

from collections.abc import Sequence
from typing import Any

import pytest

from ghostwriter.agent.limits import TOOL_RESULT_CHARS
from ghostwriter.agent.notices import NoticeEvent, Reading, SentFile, covered
from ghostwriter.agent.tools import REFUSALS, Refusal, compact, tool_message
from ghostwriter.schemas import AttachmentKind, AttachmentStatus, MeasureUnit
from ghostwriter.workbench import ToolFailure, ToolResult

FOLDER = "/workspace/5e7a1c00b2d94f0e8a6b3c2d1e0f9a8b"
FILE = "a1" * 16
NOTES_ID = "b2" * 16
PHOTO_ID = "c3" * 16
REPORT = f"{FOLDER}/long-report.pdf"
NOTES = f"{FOLDER}/tide-notes.txt"
PHOTO = f"{FOLDER}/harbour-photo.jpg"


def _sent(
    path: str = REPORT,
    *,
    file_id: str = FILE,
    kind: AttachmentKind = "document",
    status: AttachmentStatus = "ready",
    total: int | None = 500,
    unit: MeasureUnit | None = "page",
) -> SentFile:
    """Return an attachment of the chat, named as its path ends, measured at ``total`` ``unit`` by default."""
    return SentFile(file_id, path, path.rsplit("/", 1)[-1], kind, status, total, unit)


def _part(first: int, last: int, *, cut: bool = False, text: str = "Gezeitentabelle") -> dict[str, Any]:
    """Return a part of a read, as workbench 0.10 gives it: its units and whether it was cut."""
    return {"label": f"pages {first}-{last}", "text": text, "units": [first, last], "cut": cut}


def _answer(
    *parts: dict[str, Any],
    path: str = REPORT,
    kind: str = "pdf",
    total_parts: int = 500,
    pages_without_text: Sequence[int] = (),
) -> str:
    """Return ``read_document``'s text: compact JSON, as workbench writes it."""
    return compact(
        {
            "path": path,
            "kind": kind,
            "parts": list(parts),
            "total_parts": total_parts,
            "pages_without_text": list(pages_without_text),
            "shown": [],
            "truncated": False,
            "next_part": None,
            "hint": None,
        }
    )


def _read(reading: Reading, text: str, path: str = REPORT) -> None:
    """Record a ``read_document`` call answered with ``text``, the model reading what the loop gives it."""
    outcome = ToolResult(text, ())
    reading.call("read_document", {"path": path}, outcome, tool_message(outcome))


def _files(*sent: SentFile) -> dict[str, SentFile]:
    return {file.path: file for file in sent}


@pytest.mark.parametrize(
    ("spans", "count"),
    [([(1, 9)], 9), ([(1, 9), (5, 12)], 12), ([(1, 3), (7, 7)], 4), ([(3, 3), (3, 3)], 1), ([], 0)],
)
def test_covered_counts_the_union(spans: list[tuple[int, int]], count: int) -> None:
    """Spans that overlap or repeat count each unit once."""
    assert covered(spans) == count


def test_a_document_read_in_part_says_how_much() -> None:
    """Two parts of a 500-page report: 9 of 500 pages, with the file's id and name."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4), _part(5, 9)))
    assert reading.partial(_files(_sent())) == [
        NoticeEvent("document_partial", FILE, "long-report.pdf", 9, 500, "page")
    ]


def test_a_part_workbench_cut_gives_a_notice_without_numbers() -> None:
    """A part workbench cut was read in part, by an amount the units do not say: the notice carries no number."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4), _part(5, 9, cut=True)))
    assert reading.partial(_files(_sent())) == [NoticeEvent("document_partial", FILE, "long-report.pdf")]


def test_a_part_ghostwriter_trimmed_gives_a_notice_without_numbers() -> None:
    """A lone part trimmed to fit the model's window (``cut_result``) was read in part: no "0 of 3000 lines".

    The part's units say lines 1-1200, and the file has 3000; the model was given about a sixth of the part's text.
    """
    notes = _sent(NOTES, file_id=NOTES_ID, total=3_000, unit="line")
    lone = _answer(_part(1, 1_200, text="Hochwasser " * 2_000), path=NOTES, kind="text", total_parts=3_000)
    assert len(lone) > TOOL_RESULT_CHARS and '"cut":true' not in lone
    reading = Reading()
    _read(reading, lone, NOTES)
    assert reading.partial(_files(notes)) == [NoticeEvent("document_partial", NOTES_ID, "tide-notes.txt")]


def test_a_part_without_units_gives_a_notice_without_numbers() -> None:
    """A part with ``units: null`` (a run of a table's rows) says nothing of what it covers: no number is given."""
    reading = Reading()
    answer = _answer({"label": "table 1", "text": "Gezeitentabelle", "units": None, "cut": False}, total_parts=12)
    _read(reading, answer)
    assert reading.partial(_files(_sent(total=12))) == [NoticeEvent("document_partial", FILE, "long-report.pdf")]


def test_a_part_without_units_beside_counted_ones_gives_no_numbers() -> None:
    """One part of the read that cannot be counted takes the numbers from the notice, though the others can."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4), {"label": "table 1", "text": "Pegel", "units": None, "cut": False}))
    assert reading.partial(_files(_sent())) == [NoticeEvent("document_partial", FILE, "long-report.pdf")]


def test_a_cut_part_read_again_whole_has_no_notice() -> None:
    """A part cut, then every unit read again uncut, leaves the file read whole: no notice."""
    reading = Reading()
    _read(reading, _answer(_part(1, 9, cut=True)))
    _read(reading, _answer(_part(1, 4), _part(5, 9)))
    assert reading.partial(_files(_sent(total=9))) == []


def test_a_cut_part_reads_again_in_part_still_gives_no_numbers() -> None:
    """A part cut, then some of its units read again uncut: still read only in part, still without a number."""
    reading = Reading()
    _read(reading, _answer(_part(1, 9, cut=True)))
    _read(reading, _answer(_part(1, 4)))
    assert reading.partial(_files(_sent(total=9))) == [NoticeEvent("document_partial", FILE, "long-report.pdf")]


def test_only_the_path_with_a_part_cut_loses_its_numbers() -> None:
    """Of two documents read in one message, the one read cleanly keeps its numbers; the one cut does not."""
    notes = _sent(NOTES, file_id=NOTES_ID, total=40, unit="line")
    reading = Reading()
    _read(reading, _answer(_part(1, 4), _part(5, 9)))
    _read(reading, _answer(_part(1, 8, cut=True), path=NOTES, kind="text", total_parts=40), NOTES)
    assert reading.partial(_files(_sent(), notes)) == [
        NoticeEvent("document_partial", FILE, "long-report.pdf", 9, 500, "page"),
        NoticeEvent("document_partial", NOTES_ID, "tide-notes.txt"),
    ]


def test_a_part_that_does_not_count_gives_no_notice_without_a_length() -> None:
    """Without a known length, a cut part still gives no notice: the notice's other conditions stand."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4, cut=True), kind="xlsx", total_parts=3))
    assert reading.partial(_files(_sent(total=None, unit=None))) == []


def test_pages_without_text_count_as_read() -> None:
    """Pages 1-2 and 4 read, page 3 without text: a 4-page scan was read whole."""
    reading = Reading()
    _read(reading, _answer(_part(1, 2), _part(4, 4), total_parts=4, pages_without_text=[3]))
    assert reading.partial(_files(_sent(total=4))) == []


def test_without_units_there_is_no_partial_notice() -> None:
    """Workbench 0.9's parts carry neither ``units`` nor ``cut``: how much was read is unknown."""
    reading = Reading()
    _read(reading, _answer({"label": "pages 1-9", "text": "Gezeitentabelle"}))
    assert reading.partial(_files(_sent())) == []


def test_total_parts_stands_in_where_a_part_is_one_unit() -> None:
    """Without a measure, a PDF's ``total_parts`` counts its pages; a workbook's counts sheets, so it says nothing."""
    reading = Reading()
    _read(reading, _answer(_part(1, 3), total_parts=12))
    unmeasured = _sent(total=None, unit=None)
    assert reading.partial(_files(unmeasured)) == [
        NoticeEvent("document_partial", FILE, "long-report.pdf", 3, 12, "page")
    ]
    ledger = f"{FOLDER}/quay-ledger.xlsx"
    sheets = Reading()
    _read(sheets, _answer(_part(1, 3), path=ledger, kind="xlsx", total_parts=12), path=ledger)
    assert sheets.partial(_files(_sent(ledger, total=None, unit=None))) == []


def test_a_whole_read_has_no_notice() -> None:
    """Every page of a 9-page report was read."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4), _part(5, 9)))
    assert reading.partial(_files(_sent(total=9))) == []


def test_units_past_the_total_do_not_count() -> None:
    """A unit past the measured total is not one of the file's: what is read never passes the total."""
    reading = Reading()
    _read(reading, _answer(_part(1, 4), _part(9, 12)))
    assert reading.partial(_files(_sent(total=10))) == [
        NoticeEvent("document_partial", FILE, "long-report.pdf", 6, 10, "page")
    ]


def test_only_the_chats_attachments_are_named() -> None:
    """A file a tool wrote, or another chat's, is not an attachment of the chat: no notice names it."""
    reading = Reading()
    written = f"{FOLDER}/out/summary.pdf"
    _read(reading, _answer(_part(1, 2), path=written), path=written)
    assert reading.partial(_files(_sent())) == []


@pytest.mark.parametrize("tool", ["run_python", "search_files"])
@pytest.mark.parametrize(
    "outcome",
    [ToolResult('{"stdout": ""}', ()), ToolFailure("not_completed", "the code did not finish", True)],
    ids=["answered", "failed"],
)
def test_a_blind_tool_silences_both(tool: str, outcome: ToolResult | ToolFailure) -> None:
    """A tool that reads files without naming them ran: what was read is unknown, so neither notice is given.

    A refused call never ran, and silences nothing.
    """
    notes = _sent(NOTES, file_id=NOTES_ID, total=2, unit="line")
    refused = Refusal("call_limit", REFUSALS["call_limit"])
    reading = Reading()
    _read(reading, _answer(_part(1, 4)))
    reading.call(tool, {"code": "print(1)"}, refused, tool_message(refused))
    assert reading.partial(_files(_sent(), notes)) != []
    assert reading.unread([notes], sees=False) != []
    reading.call(tool, {"code": "print(1)", "query": "Pegel"}, outcome, tool_message(outcome))
    assert reading.partial(_files(_sent(), notes)) == []
    assert reading.unread([notes], sees=False) == []


def test_an_unopened_attachment_is_unread() -> None:
    """Of the question's two documents, the one never opened is named; the one read in part is not unread."""
    notes = _sent(NOTES, file_id=NOTES_ID, total=2, unit="line")
    reading = Reading()
    _read(reading, _answer(_part(1, 4)))
    assert reading.unread([_sent(), notes], sees=False) == [
        NoticeEvent("document_unread", file_id=NOTES_ID, name="tide-notes.txt")
    ]


def test_a_failed_read_does_not_open() -> None:
    """A read workbench refused, or one the loop never sent, opened nothing."""
    notes = _sent(NOTES, file_id=NOTES_ID, total=2, unit="line")
    reading = Reading()
    failed = ToolFailure("unreadable_file", "the file could not be read", True)
    reading.call("read_document", {"path": NOTES}, failed, tool_message(failed))
    refused = Refusal("no_time", REFUSALS["no_time"])
    reading.call("view_file", {"path": NOTES}, refused, tool_message(refused))
    assert reading.unread([notes], sees=False) == [
        NoticeEvent("document_unread", file_id=NOTES_ID, name="tide-notes.txt")
    ]
    assert reading.partial(_files(notes)) == []


def test_view_file_opens() -> None:
    """``view_file`` opens the file at its path, written as the model may: the path is normalised."""
    reading = Reading()
    shown = ToolResult(compact({"path": REPORT, "kind": "pdf", "unit": "page", "total": 500, "shown": []}), ())
    reading.call("view_file", {"path": f"{FOLDER}/./long-report.pdf"}, shown, tool_message(shown))
    assert reading.unread([_sent()], sees=True) == []
    assert reading.partial(_files(_sent())) == []


def test_an_attachment_a_tool_used_is_not_unread() -> None:
    """A template filled, and a master and a picture a deck was built with, were used: none is unread.

    The picture is named deep in the slides, and written as the model may: the path is normalised. A tool other than
    ``read_document`` opens a file but reads none of it, so no ``document_partial`` comes of it.
    """
    template = _sent(f"{FOLDER}/harbour-template.docx", file_id="d4" * 16, total=12, unit="paragraph")
    master = _sent(f"{FOLDER}/harbour-master.pptx", file_id="e5" * 16, total=3, unit="slide")
    photo = _sent(PHOTO, file_id=PHOTO_ID, kind="image", total=None, unit=None)
    filled = ToolResult(compact({"path": f"{FOLDER}/out/harbour-letter.docx", "hint": None}), ())
    built = ToolResult(compact({"path": f"{FOLDER}/out/quay-deck.pptx", "hint": None}), ())
    reading = Reading()
    reading.call(
        "fill_template", {"path": template.path, "values": {"Kunde": "Hafenamt"}}, filled, tool_message(filled)
    )
    slides = [{"title": "Pegel", "content": [{"image": f"{FOLDER}//harbour-photo.jpg"}]}]
    deck = {"path": master.path, "name": "quay-deck", "slides": slides}
    reading.call("write_presentation", deck, built, tool_message(built))
    assert reading.unread([template, master, photo], sees=False) == []
    assert reading.partial(_files(template, master, photo)) == []


def test_a_path_inside_a_tools_text_counts_as_opened() -> None:
    """A picture written as ``{image: <path>}``, and a master inside a block's JSON, were used: neither is unread.

    The full path is matched, its chat folder included: notes named in a text by their file name alone stay unread.
    """
    master = _sent(f"{FOLDER}/harbour-master.pptx", file_id="e5" * 16, total=3, unit="slide")
    photo = _sent(PHOTO, file_id=PHOTO_ID, kind="image", total=None, unit=None)
    notes = _sent(NOTES, file_id=NOTES_ID, total=2, unit="line")
    built = ToolResult(compact({"path": f"{FOLDER}/out/quay-deck.pptx", "hint": None}), ())
    slides = [
        {"title": "Pegel", "content": [f"{{image: {PHOTO}}}"]},
        {"title": "Vorlage", "content": [compact({"image": master.path})], "notes": "aus tide-notes.txt"},
    ]
    reading = Reading()
    reading.call("write_presentation", {"name": "quay-deck", "slides": slides}, built, tool_message(built))
    assert reading.unread([master, photo, notes], sees=False) == [
        NoticeEvent("document_unread", file_id=NOTES_ID, name="tide-notes.txt")
    ]


def test_a_failed_call_naming_the_path_leaves_it_unread() -> None:
    """A fill workbench refused, and one the loop never sent, used nothing."""
    template = _sent(f"{FOLDER}/harbour-template.docx", file_id="d4" * 16, total=12, unit="paragraph")
    failed = ToolFailure("unreadable_file", "the template could not be read", True)
    refused = Refusal("call_limit", REFUSALS["call_limit"])
    reading = Reading()
    for outcome in (failed, refused):
        reading.call("fill_template", {"path": template.path, "values": {}}, outcome, tool_message(outcome))
    assert reading.unread([template], sees=False) == [
        NoticeEvent("document_unread", file_id="d4" * 16, name="harbour-template.docx")
    ]


def test_a_picture_counts_only_without_sight() -> None:
    """A picture unopened is unread only in a turn that does not see; one that sees was sent it as an image."""
    photo = _sent(PHOTO, file_id=PHOTO_ID, kind="image", total=None, unit=None)
    reading = Reading()
    assert reading.unread([photo], sees=False) == [
        NoticeEvent("document_unread", file_id=PHOTO_ID, name="harbour-photo.jpg")
    ]
    assert reading.unread([photo], sees=True) == []


def test_a_clip_unopened_is_unread() -> None:
    """A clip is read through ``read_document`` or ``view_file``; one never opened is unread, sight or not."""
    clip = _sent(f"{FOLDER}/harbour-tour.mp4", file_id=PHOTO_ID, kind="media", total=None, unit=None)
    assert Reading().unread([clip], sees=True) == [
        NoticeEvent("document_unread", file_id=PHOTO_ID, name="harbour-tour.mp4")
    ]


def test_a_failed_attachment_is_not_reported() -> None:
    """A failed attachment already says it could not be read: it is not reported unread."""
    reading = Reading()
    assert reading.unread([_sent(NOTES, file_id=NOTES_ID, status="failed")], sees=False) == []
