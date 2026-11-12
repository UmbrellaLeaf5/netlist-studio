"""Панель инструментов редактора.

Автор: А. Пронин. Контракт: docs/interfaces.md, раздел 3.5.

Панель только передаёт намерения пользователя сигналами. Файловые
действия обрабатывает ``EditorMainWindow``, отмену, повтор и смену
инструмента — ``Canvas``.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal as Signal
from PyQt5.QtWidgets import QAction, QActionGroup, QToolBar, QWidget

from netlist.editor.canvas import Tool


class ToolPalette(QToolBar):
    """Кнопки «Файл», история и переключатель инструментов."""

    newRequested = Signal()
    openRequested = Signal()
    saveRequested = Signal()
    checkRequested = Signal()
    undoRequested = Signal()
    redoRequested = Signal()
    toolChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Инструменты", parent)
        self.setObjectName("ToolPalette")
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setMovable(False)
        self.setFloatable(False)

        self.new_action = self._action(
            "Новый", self.newRequested, "Ctrl+N"
        )
        self.open_action = self._action(
            "Открыть", self.openRequested, "Ctrl+O"
        )
        self.save_action = self._action(
            "Сохранить", self.saveRequested, "Ctrl+S"
        )
        self.check_action = self._action(
            "Проверить", self.checkRequested, "F5"
        )
        self.addSeparator()
        self.undo_action = self._action(
            "Отменить", self.undoRequested, "Ctrl+Z"
        )
        self.redo_action = self._action(
            "Повторить", self.redoRequested, "Ctrl+Y"
        )
        self.undo_action.setEnabled(False)
        self.redo_action.setEnabled(False)
        self.addSeparator()

        self._tools = QActionGroup(self)
        self._tools.setExclusive(True)
        self.select_action = self._tool_action("Выделение", Tool.SELECT, "S")
        self.place_action = self._tool_action("Размещение", Tool.PLACE, "P")
        self.wire_action = self._tool_action("Соединение", Tool.WIRE, "N")
        self.select_action.setChecked(True)
        self._tools.triggered.connect(self._on_tool)

    def _action(self, text: str, signal, shortcut: str | None = None) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(signal.emit)
        self.addAction(action)
        return action

    def _tool_action(
        self, text: str, tool: Tool, shortcut: str
    ) -> QAction:
        action = QAction(text, self)
        action.setCheckable(True)
        action.setShortcut(shortcut)
        action.setData(tool)
        self._tools.addAction(action)
        self.addAction(action)
        return action

    def _on_tool(self, action: QAction) -> None:
        tool = action.data()
        if isinstance(tool, Tool):
            self.toolChanged.emit(tool)

    # ---- состояние ---------------------------------------------------
    def set_undo_state(self, enabled: bool, text: str | None) -> None:
        self.undo_action.setEnabled(enabled)
        self.undo_action.setToolTip(
            f"Отменить: {text}" if text else "Отменить"
        )

    def set_redo_state(self, enabled: bool, text: str | None) -> None:
        self.redo_action.setEnabled(enabled)
        self.redo_action.setToolTip(
            f"Повторить: {text}" if text else "Повторить"
        )

    def set_save_enabled(self, enabled: bool) -> None:
        self.save_action.setEnabled(enabled)

    def set_tool(self, tool: Tool | str) -> None:
        if not isinstance(tool, Tool):
            try:
                tool = Tool(tool)
            except ValueError:
                return
        action = {
            Tool.SELECT: self.select_action,
            Tool.PLACE: self.place_action,
            Tool.WIRE: self.wire_action,
        }.get(tool)
        if action is not None and not action.isChecked():
            action.setChecked(True)


__all__ = ["ToolPalette"]
