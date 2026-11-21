"""Images in a round's messages: what they cost, their strips, the refs, the notes and the data URLs.

Every name, id and value here is invented.
"""

import base64
import copy
import json
from typing import Any

from ghostwriter.agent.images import (
    LEFT_OUT,
    NOT_LOADED,
    RETIRED,
    AttachedImage,
    ImageBank,
    attachment_parts,
    estimate_image_tokens,
    materialise,
    refs,
    retire_tool_images,
    strips_for_image,
    strips_for_path,
    tool_image_message,
    without_images,
)
from ghostwriter.workbench import ToolImage
from tests.fakes import TINY_JPEG, TINY_PNG

FOLDER = "/workspace/" + "c" * 64
PHOTO = AttachedImage(
    file_id="F", path=f"{FOLDER}/harbour-photo.jpg", name="harbour-photo.jpg", width=2048, height=1536
)
PAGE = ToolImage("quay-scan.pdf, page 2", 1131, 1600, "image/jpeg", TINY_JPEG)
CHART = ToolImage("plot.png", 640, 480, "image/png", TINY_PNG)


def _url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _question(*images: AttachedImage) -> dict[str, Any]:
    return {"role": "user", "content": [_text("Was zeigen die Bilder?"), *attachment_parts(images, fixed=None)]}


def test_an_image_costs_its_fixed_price_or_one_token_per_32_pixels() -> None:
    """Without a fixed cost, an image costs a token per started 32 x 32 tile; with one, that cost."""
    assert estimate_image_tokens(2048, 1536, fixed=None) == 3072
    assert estimate_image_tokens(1600, 1131, fixed=None) == 1800
    assert estimate_image_tokens(100, 10, fixed=None) == 4
    assert estimate_image_tokens(2048, 1536, fixed=280) == 280


def test_strips_only_for_a_fixed_cost() -> None:
    """With a fixed cost, a page and a picture whose long side is over 1024 go as two strips; nothing else is cut."""
    assert strips_for_image(2048, 1536, fixed=None) == 1
    assert strips_for_image(2048, 1536, fixed=280) == 2
    assert strips_for_image(1536, 2048, fixed=280) == 2
    assert strips_for_image(1024, 768, fixed=280) == 1
    assert strips_for_path("/workspace/c/quay-scan.PDF", fixed=280, known={}) == 2
    shot = "/workspace/c/screenshot-a.png"
    assert strips_for_path(shot, fixed=280, known={shot: (1600, 1200)}) == 2
    assert strips_for_path(shot, fixed=280, known={shot: (800, 600)}) == 1
    assert strips_for_path(shot, fixed=280, known={}) == 1
    assert strips_for_path(shot, fixed=None, known={shot: (1600, 1200)}) is None
    assert strips_for_path("/workspace/c/quay-scan.pdf", fixed=None, known={}) is None
    assert strips_for_path("/workspace/c/sums.xlsx", fixed=280, known={}) is None
    assert strips_for_path("/workspace/c/plot.svg", fixed=280, known={}) is None


def test_attachment_parts_label_each_strip() -> None:
    """A large picture at a fixed cost is two refs, one per strip; at a cost by pixels it is one, named."""
    parts = attachment_parts([PHOTO], fixed=280)
    assert [(p["key"], p["label"], p["strip"], p["strips"]) for p in parts] == [
        ("att:F:1/2", "harbour-photo.jpg, strip 1 of 2", 1, 2),
        ("att:F:2/2", "harbour-photo.jpg, strip 2 of 2", 2, 2),
    ]
    assert all(
        (p["type"], p["origin"], p["path"], p["n"], p["width"], p["height"])
        == ("image_ref", "attachment", PHOTO.path, 1, 2048, 1536)
        for p in parts
    )
    assert attachment_parts([PHOTO], fixed=None) == [
        {
            "type": "image_ref",
            "key": "att:F:1/1",
            "origin": "attachment",
            "label": "harbour-photo.jpg",
            "width": 2048,
            "height": 1536,
            "path": PHOTO.path,
            "n": 1,
            "strip": 1,
            "strips": 1,
        }
    ]


def test_tool_images_go_into_one_user_message_with_a_legend() -> None:
    """Each call's images follow a legend naming the tool and the call; their bytes go to the bank only."""
    message, bank = tool_image_message([("view_file", "call_0", [PAGE, CHART]), ("read_document", "call_1", [PAGE])])
    assert message["role"] == "user"
    content = message["content"]
    assert content[0] == _text(
        "[Images returned by view_file for call call_0: quay-scan.pdf, page 2; plot.png. Not a message from the user.]"
    )
    assert [(p["type"], p["key"], p["origin"], p["label"], p["width"], p["height"]) for p in content[1:3]] == [
        ("image_ref", "tool:call_0:1", "tool", "quay-scan.pdf, page 2", 1131, 1600),
        ("image_ref", "tool:call_0:2", "tool", "plot.png", 640, 480),
    ]
    assert content[3] == _text(
        "[Images returned by read_document for call call_1: quay-scan.pdf, page 2. Not a message from the user.]"
    )
    assert [p["key"] for p in content[4:]] == ["tool:call_1:1"]
    assert bank == {
        "tool:call_0:1": ("image/jpeg", TINY_JPEG),
        "tool:call_0:2": ("image/png", TINY_PNG),
        "tool:call_1:1": ("image/jpeg", TINY_JPEG),
    }
    assert "base64" not in json.dumps(message) and len(refs([message])) == 3


def test_materialise_swaps_refs_for_data_urls_and_notes_for_missing_ones() -> None:
    """Each ref becomes its label and a data URL, in a new list; a ref whose bytes are missing becomes a note."""
    shown, bank = tool_image_message([("view_file", "call_0", [PAGE, CHART])])
    del bank["tool:call_0:2"]
    bank["att:F:1/1"] = ("image/jpeg", TINY_JPEG)
    messages: list[dict[str, Any]] = [{"role": "system", "content": "Sei hilfreich."}, _question(PHOTO), shown]
    before = copy.deepcopy(messages)
    wire, count = materialise(messages, bank)
    assert count == 2 and copy.deepcopy(messages) == before == messages
    assert wire[0] == messages[0] and wire is not messages
    assert wire[1]["content"] == [
        _text("Was zeigen die Bilder?"),
        _text("[Image: harbour-photo.jpg]"),
        {"type": "image_url", "image_url": {"url": _url("image/jpeg", TINY_JPEG)}},
    ]
    assert wire[2]["content"][1:] == [
        _text("[Image: quay-scan.pdf, page 2]"),
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(TINY_JPEG).decode()}},
        _text(NOT_LOADED.format(label="plot.png")),
    ]
    assert NOT_LOADED.format(label="plot.png") == "[Image: plot.png; could not be loaded, view_file shows it]"
    assert refs(wire) == [] and len(refs(messages)) == 3
    assert "data:" not in json.dumps(messages)


def test_without_images_leaves_only_notes() -> None:
    """A round sent again without images names each one as left out; nothing is materialised from it."""
    shown, bank = tool_image_message([("view_file", "call_0", [PAGE])])
    bank["att:F:1/1"] = ("image/jpeg", TINY_JPEG)
    messages = [_question(PHOTO), shown]
    before = copy.deepcopy(messages)
    left = without_images(messages)
    assert messages == before and refs(left) == []
    assert left[0]["content"][1] == _text("[Image: harbour-photo.jpg; left out, the model did not take images]")
    assert left[1]["content"][1] == _text(LEFT_OUT.format(label="quay-scan.pdf, page 2"))
    assert materialise(left, bank) == (left, 0)


def test_retire_tool_images_keeps_the_newest() -> None:
    """Tool images older than the newest ``keep`` become notes in place and leave the bank; attached ones stay."""
    first, first_bank = tool_image_message([("view_file", "call_0", [PAGE, CHART])])
    second, second_bank = tool_image_message([("view_file", "call_1", [PAGE])])
    question = _question(PHOTO)
    current = [question, first, second]
    bank: ImageBank = {**first_bank, **second_bank, "att:F:1/1": ("image/jpeg", TINY_JPEG)}
    retire_tool_images(current, bank, keep=2)
    assert first["content"][1] == _text(
        "[Image: quay-scan.pdf, page 2; not shown again to make room, view it again with view_file]"
    )
    assert [p["key"] for p in refs(current)] == ["att:F:1/1", "tool:call_0:2", "tool:call_1:1"]
    assert set(bank) == {"att:F:1/1", "tool:call_0:2", "tool:call_1:1"}
    retire_tool_images(current, bank, keep=0)
    assert [p["key"] for p in refs(current)] == ["att:F:1/1"] and set(bank) == {"att:F:1/1"}
    assert second["content"][1] == _text(RETIRED.format(label="quay-scan.pdf, page 2"))
    assert current[0] is question and len(refs([question])) == 1
