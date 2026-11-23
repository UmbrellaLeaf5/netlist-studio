"""Тесты BlockList (src/editor/block_list.py, автор А. Пронин).

Контракт: docs/interfaces.md, п. 3.5. Тесты проверяют порядок блоков,
выбор при обновлении списка и сигналы при действиях пользователя.
Данные перетаскивания проверяются по формату ELEMENT_MIME.

Запуск из корня репозитория:
    python -m pytest tests/editor/test_block_list.py -v
"""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu

from src.editor import block_list as block_list_module
from src.editor.block_list import ELEMENT_MIME, BlockList


@pytest.fixture
def blocks(qtbot):
    widget = BlockList()
    qtbot.addWidget(widget)
    return widget


@pytest.mark.parametrize(
    ("choice", "signal_name"),
    [("Копировать…", "copyRequested"), ("Свойства…", "propertiesRequested")],
    ids=["copy", "properties"],
)
def test_context_menu_action_targets_right_clicked_block(
    blocks, qtbot, monkeypatch, choice, signal_name
):
    """Действие меню относится к блоку под курсором, даже если выбран другой."""
    blocks.set_blocks(["main", "amp"])
    blocks.select("main")
    item = blocks.blocks.item(1)
    position = blocks.blocks.visualItemRect(item).center()

    class MenuWithSelection(QMenu):
        def exec(self, _position):
            return next(
                action for action in self.actions() if action.text() == choice
            )

    monkeypatch.setattr(block_list_module, "QMenu", MenuWithSelection)
    with qtbot.waitSignal(getattr(blocks, signal_name)) as request:
        blocks.blocks.customContextMenuRequested.emit(position)

    assert request.args == ["amp"]


def test_set_blocks_preserves_document_order(blocks):
    """Блоки показываются в порядке документа, без сортировки по имени."""
    blocks.set_blocks(["zeta", "main", "alpha"])

    assert [blocks.blocks.item(i).text() for i in range(3)] == [
        "zeta",
        "main",
        "alpha",
    ]


def test_refresh_keeps_selected_block_when_its_index_changes(blocks):
    """Выбор привязан к имени блока и сохраняется при изменении порядка."""
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    blocks.set_blocks(["amp", "main"])

    assert blocks.current() == "amp"


def test_refresh_clears_selection_when_selected_block_is_removed(blocks):
    """Если выбранный блок исчез из списка, выбор сбрасывается."""
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    blocks.set_blocks(["main"])

    assert blocks.current() is None


def test_programmatic_selection_does_not_request_navigation(blocks, qtbot):
    """Когда холст синхронизирует выбор панели, панель не отправляет
    встречный запрос на открытие блока.
    """
    blocks.set_blocks(["main", "amp"])

    with qtbot.assertNotEmitted(blocks.blockActivated):
        blocks.select("amp")

    assert blocks.current() == "amp"


def test_clicking_block_requests_navigation_to_that_block(blocks, qtbot):
    """Щелчок пользователя передаёт имя блока, который нужно открыть."""
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
    """Кнопка «Вверх» передаёт целевую позицию, а список ждёт обновления
    документа.
    """
    blocks.set_blocks(["main", "amp"])
    blocks.select("amp")

    with qtbot.waitSignal(blocks.moveRequested) as request:
        qtbot.mouseClick(blocks.up_button, Qt.LeftButton)

    assert request.args == ["amp", 0]
    assert blocks.blocks.item(0).text() == "main"


def test_first_block_cannot_move_up(blocks, qtbot):
    """У первого блока нет позиции выше, поэтому запроса на перемещение нет."""
    blocks.set_blocks(["main", "amp"])
    blocks.select("main")

    with qtbot.assertNotEmitted(blocks.moveRequested):
        blocks.up_button.click()


def test_delete_requests_selected_block_without_removing_it(blocks, qtbot):
    """Кнопка сообщает, какой блок удалить. Строка остаётся в панели до
    обновления списка.
    """
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
    """Данные перетаскивания позволяют холсту различить одноимённые подсхему
    и примитив.
    """
    blocks.set_blocks(["amp"])
    blocks.set_primitives(["amp"])
    items = getattr(blocks, section)

    payload = items.mimeData([items.item(0)])

    assert payload.hasFormat(ELEMENT_MIME)
    assert json.loads(bytes(payload.data(ELEMENT_MIME))) == {
        "kind": kind,
        "type": "amp",
    }
