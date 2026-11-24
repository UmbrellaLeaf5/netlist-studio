"""Тесты ToolPalette (src/editor/tool_palette.py, автор А. Пронин).

Контракт: docs/interfaces.md, п. 3.6. Тесты проверяют сигналы действий,
доступность сохранения, подписи команд истории и переключение инструмента.
Программный выбор инструмента проверяется отдельно от действия пользователя.

Запуск из корня репозитория:
    python -m pytest tests/editor/test_tool_palette.py -v
"""

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
    """Каждая кнопка работы с документом отправляет свой запрос окну."""
    signal = getattr(palette, action + "Requested")

    with qtbot.waitSignal(signal) as request:
        getattr(palette, action + "_action").trigger()

    assert request.args == []


@pytest.mark.parametrize("action", ["undo", "redo"])
def test_history_button_shows_command_name_and_emits_request(
    palette, qtbot, action
):
    """Доступная кнопка истории показывает имя команды в подсказке и
    реагирует на нажатие.
    """
    button = getattr(palette, action + "_action")
    signal = getattr(palette, action + "Requested")
    assert not button.isEnabled()

    getattr(palette, "set_" + action + "_state")(True, "Добавить блок")

    assert "Добавить блок" in button.toolTip()
    with qtbot.waitSignal(signal):
        button.trigger()


def test_disabled_save_does_not_emit_request(palette, qtbot):
    """Щелчок по недоступной кнопке «Сохранить» не запускает сохранение."""
    palette.set_save_enabled(False)

    with qtbot.assertNotEmitted(palette.saveRequested):
        palette.widgetForAction(palette.save_action).click()


def test_switching_tool_emits_tool_and_leaves_only_it_selected(palette, qtbot):
    """Выбор инструмента сообщает холсту новый режим и снимает выделение с
    предыдущего.
    """
    with qtbot.waitSignal(palette.toolChanged) as change:
        palette.wire_action.trigger()

    assert change.args == [Tool.WIRE]
    assert palette.wire_action.isChecked()
    assert not palette.select_action.isChecked()


def test_syncing_tool_selection_does_not_emit_change(palette, qtbot):
    """При синхронизации с холстом панель меняет выбранный инструмент без
    повторного сигнала.
    """
    with qtbot.assertNotEmitted(palette.toolChanged):
        palette.set_tool(Tool.WIRE)

    assert palette.wire_action.isChecked()
