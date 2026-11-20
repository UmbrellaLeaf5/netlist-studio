"""Проверки выбора блоков и передачи элементов на Canvas."""

import json

import pytest
from PyQt5.QtCore import Qt

from netlist.editor.block_list import ELEMENT_MIME, BlockList


@pytest.fixture
def blocks(qtbot):
    widget = BlockList()
    qtbot.addWidget(widget)
    return widget


def test_set_blocks_preserves_document_order(blocks):
    blocks.set_blocks(["zeta", "main", "alpha"])

    assert [blocks.blocks.item(i).text() for i in range(3)] == [
        "zeta",
        "main",
        "alpha",
    ]


def test_refresh_keeps_selected_block_when_its_index_changes(blocks):
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    blocks.set_blocks(["amp", "main"])

    assert blocks.current() == "amp"


def test_refresh_clears_selection_when_selected_block_is_removed(blocks):
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    blocks.set_blocks(["main"])

    assert blocks.current() is None


def test_programmatic_selection_does_not_request_navigation(blocks, qtbot):
    blocks.set_blocks(["main", "amp"])

    with qtbot.assertNotEmitted(blocks.blockActivated):
        blocks.select("amp")

    assert blocks.current() == "amp"


def test_clicking_block_requests_navigation_to_that_block(blocks, qtbot):
    blocks.set_blocks(["main", "amp"])
    blocks.show()
    row = blocks.blocks.item(1)
    position = blocks.blocks.visualItemRect(row).center()

    with qtbot.waitSignal(blocks.blockActivated) as activation:
        qtbot.mouseClick(blocks.blocks.viewport(), Qt.LeftButton, pos=position)

    assert activation.args == ["amp"]


def test_move_button_requests_destination_without_reordering_list(
    blocks, qtbot
):
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    with qtbot.waitSignal(blocks.moveRequested) as request:
        qtbot.mouseClick(blocks.up_button, Qt.LeftButton)

    assert request.args == ["amp", 0]
    assert blocks.blocks.item(0).text() == "main"


def test_first_block_cannot_move_up(blocks, qtbot):
    blocks.set_blocks(["main", "amp"])
    blocks.select("main")

    with qtbot.assertNotEmitted(blocks.moveRequested):
        blocks.up_button.click()


def test_delete_requests_selected_block_without_removing_it(blocks, qtbot):
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    with qtbot.waitSignal(blocks.deleteRequested) as request:
        blocks.remove_button.click()

    assert request.args == ["amp"]
    assert blocks.blocks.count() == 2


@pytest.mark.parametrize(
    ("section", "kind"),
    [("blocks", "sub"), ("primitives", "prim")],
    ids=["subcircuit", "primitive"],
)
def test_drag_payload_identifies_element_type(blocks, section, kind):
    blocks.set_blocks(["amp"])
    blocks.set_primitives(["amp"])
    items = getattr(blocks, section)

    payload = items.mimeData([items.item(0)])

    assert payload.hasFormat(ELEMENT_MIME)
    assert json.loads(bytes(payload.data(ELEMENT_MIME))) == {
        "kind": kind,
        "type": "amp",
    }
