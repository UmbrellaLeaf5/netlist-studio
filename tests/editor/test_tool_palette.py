"""Проверки сигналов и доступности действий панели."""

import pytest

from src.editor.canvas import Tool
from src.editor.tool_palette import ToolPalette


@pytest.fixture
def palette(qtbot):
    widget = ToolPalette()
    qtbot.addWidget(widget)
    return widget


@pytest.mark.parametrize("action", ["new", "open", "save", "check"])
def test_document_action_emits_its_request(palette, qtbot, action):
    signal = getattr(palette, action + "Requested")

    with qtbot.waitSignal(signal) as request:
        getattr(palette, action + "_action").trigger()

    assert request.args == []


@pytest.mark.parametrize("action", ["undo", "redo"])
def test_history_button_shows_command_name_and_emits_request(
    palette, qtbot, action
):
    button = getattr(palette, action + "_action")
    signal = getattr(palette, action + "Requested")
    assert not button.isEnabled()

    getattr(palette, "set_" + action + "_state")(True, "Добавить блок")

    assert "Добавить блок" in button.toolTip()
    with qtbot.waitSignal(signal):
        button.trigger()


def test_disabled_save_does_not_emit_request(palette, qtbot):
    palette.set_save_enabled(False)

    with qtbot.assertNotEmitted(palette.saveRequested):
        palette.widgetForAction(palette.save_action).click()


def test_switching_tool_emits_tool_and_leaves_only_it_selected(palette, qtbot):
    with qtbot.waitSignal(palette.toolChanged) as change:
        palette.wire_action.trigger()

    assert change.args == [Tool.WIRE]
    assert palette.wire_action.isChecked()
    assert not palette.select_action.isChecked()


def test_syncing_tool_selection_does_not_emit_change(palette, qtbot):
    with qtbot.assertNotEmitted(palette.toolChanged):
        palette.set_tool(Tool.WIRE)

    assert palette.wire_action.isChecked()
