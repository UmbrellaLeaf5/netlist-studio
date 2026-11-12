"""Список блоков и примитивов редактора.

Автор: А. Пронин. Контракт: docs/interfaces.md, раздел 3.4.

Виджет не меняет черновик: он только сообщает о запросах сигналами.
``Canvas`` превращает их в команды ``UndoStack``. Текущий открытый блок
всегда подсвечен, но ``select`` не испускает ``blockActivated``.
"""

from __future__ import annotations

import json

from PyQt5.QtCore import QMimeData, Qt, pyqtSignal as Signal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ELEMENT_MIME = "application/x-netlist-element"


class _DragList(QListWidget):
    """Список, элементы которого можно перетащить на холст."""

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(False)

    def mimeData(self, items) -> QMimeData:  # type: ignore[override]
        data = QMimeData()
        if items:
            item = items[0]
            kind = item.data(Qt.UserRole) or self.kind
            type_name = item.data(Qt.UserRole + 1) or item.text()
            payload = {"kind": str(kind), "type": str(type_name)}
            data.setData(
                ELEMENT_MIME,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        return data

class BlockList(QWidget):
    """Левая панель: определения блоков и примитивы файла."""

    blockActivated = Signal(str)
    createRequested = Signal()
    deleteRequested = Signal(str)
    copyRequested = Signal(str)
    moveRequested = Signal(str, int)
    propertiesRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self.setMaximumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("Список блоков")
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(title)

        blocks_label = QLabel("Блоки")
        blocks_label.setStyleSheet("font-weight: 500;")
        layout.addWidget(blocks_label)

        self.blocks = _DragList("sub", self)
        self.blocks.setMinimumHeight(160)
        layout.addWidget(self.blocks, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.add_button = QPushButton("+")
        self.remove_button = QPushButton("−")
        self.up_button = QPushButton("↑")
        self.down_button = QPushButton("↓")
        for button in (
            self.add_button, self.remove_button,
            self.up_button, self.down_button,
        ):
            button.setFixedWidth(42)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        primitives_label = QLabel("Примитивы")
        primitives_label.setStyleSheet("font-weight: 500;")
        layout.addWidget(primitives_label)

        self.primitives = _DragList("prim", self)
        self.primitives.setMinimumHeight(120)
        layout.addWidget(self.primitives, 1)

        elements_label = QLabel("Элементы")
        elements_label.setStyleSheet("font-weight: 500;")
        layout.addWidget(elements_label)

        self.elements = _DragList("element", self)
        self.elements.setMaximumHeight(76)
        for kind, text in (
            ("pin", "Внешний пин"),
            ("ipin", "Пин инстанса"),
        ):
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, kind)
            item.setData(Qt.UserRole + 1, text)
            self.elements.addItem(item)
        layout.addWidget(self.elements)

        hint = QLabel(
            "Перетащите блок, примитив или элемент на схему"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085; font-size: 11px;")
        layout.addWidget(hint)

        self.blocks.itemClicked.connect(self._on_block_clicked)
        self.blocks.setContextMenuPolicy(Qt.CustomContextMenu)
        self.blocks.customContextMenuRequested.connect(
            self._block_menu
        )
        self.add_button.clicked.connect(self.createRequested.emit)
        self.remove_button.clicked.connect(self._delete_current)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))

    # ---- данные ------------------------------------------------------
    def set_blocks(self, names: list[str]) -> None:
        current = self.current()
        self.blocks.blockSignals(True)
        self.blocks.clear()
        for name in names:
            self.blocks.addItem(QListWidgetItem(name))
        if current in names:
            self.blocks.setCurrentRow(names.index(current))
        self.blocks.blockSignals(False)

    def set_primitives(self, names: list[str]) -> None:
        self.primitives.blockSignals(True)
        self.primitives.clear()
        for name in names:
            self.primitives.addItem(QListWidgetItem(name))
        self.primitives.blockSignals(False)

    def select(self, name: str) -> None:
        for row in range(self.blocks.count()):
            if self.blocks.item(row).text() == name:
                self.blocks.blockSignals(True)
                self.blocks.setCurrentRow(row)
                self.blocks.blockSignals(False)
                return

    def current(self) -> str | None:
        item = self.blocks.currentItem()
        return item.text() if item is not None else None

    # ---- обработчики -------------------------------------------------
    def _on_block_clicked(self, item: QListWidgetItem) -> None:
        self.blockActivated.emit(item.text())

    def _delete_current(self) -> None:
        name = self.current()
        if name is not None:
            self.deleteRequested.emit(name)

    def _move(self, delta: int) -> None:
        name = self.current()
        if name is None:
            return
        row = self.blocks.currentRow()
        target = max(0, min(self.blocks.count() - 1, row + delta))
        if target != row:
            self.moveRequested.emit(name, target)

    def _block_menu(self, position) -> None:
        item = self.blocks.itemAt(position)
        if item is None:
            self.createRequested.emit()
            return
        self.blocks.setCurrentItem(item)
        name = item.text()
        menu = QMenu(self)
        create = menu.addAction("Новый блок…")
        delete = menu.addAction("Удалить")
        copy = menu.addAction("Копировать…")
        menu.addSeparator()
        up = menu.addAction("Вверх")
        down = menu.addAction("Вниз")
        properties = menu.addAction("Свойства…")
        action = menu.exec_(self.blocks.mapToGlobal(position))
        if action is create:
            self.createRequested.emit()
        elif action is delete:
            self.deleteRequested.emit(name)
        elif action is copy:
            self.copyRequested.emit(name)
        elif action is up:
            row = self.blocks.currentRow()
            if row > 0:
                self.moveRequested.emit(name, row - 1)
        elif action is down:
            row = self.blocks.currentRow()
            if row < self.blocks.count() - 1:
                self.moveRequested.emit(name, row + 1)
        elif action is properties:
            self.propertiesRequested.emit(name)


__all__ = ["BlockList", "ELEMENT_MIME"]
