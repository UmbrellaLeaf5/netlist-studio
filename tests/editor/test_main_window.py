"""Тесты EditorMainWindow (src/editor/main_window.py, автор А. Пронин).

Контракт: docs/interfaces.md, п. 3.7. Тесты используют временные документы
и настройки Qt. SyntaxCheck, проверка иерархии и Autosave заменены
заглушками, чтобы проверить работу окна при открытии, сохранении
и закрытии документа.

Запуск из корня репозитория:
    python -m pytest tests/editor/test_main_window.py -v
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcutEvent
from PySide6.QtWidgets import QMessageBox

from src.core.model import Diagnostic, Severity, SourceLoc
from src.core.parser import NetlistParser, ParseResult, RawNetlist
from src.editor.canvas import (
    AddBlockCommand,
    AddInstanceCommand,
    MoveInstanceCommand,
)
from tests.core.fakes import make_syntax_module, replaced_modules


class AutosaveStub(QObject):
    """Граница окна: регистрирует вызовы без таймера."""

    saved = Signal(str)
    failed = Signal(str)

    def __init__(self, manager, get_state, interval_ms, parent):
        super().__init__(parent)
        self.enabled = False
        self.enable = Mock(side_effect=lambda: setattr(self, "enabled", True))
        self.disable = Mock(side_effect=lambda: setattr(self, "enabled", False))
        self.set_interval = Mock()
        self.mark_dirty = Mock()
        self.flush = Mock(return_value=True)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    # Подменяем только эту зависимость при загрузке тестируемого модуля.
    autosave = ModuleType("src.editor.autosave")
    autosave.Autosave = AutosaveStub
    autosave.DocumentState = Mock()
    source = (
        Path(__file__).resolve().parents[2] / "src/editor/main_window.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_tested_editor_window", source
    )
    module = importlib.util.module_from_spec(spec)
    with replaced_modules({autosave.__name__: autosave}):
        spec.loader.exec_module(module)

    # Синтаксис и иерархия принадлежат другим участникам.
    with replaced_modules(
        {"src.checker.syntax_check": make_syntax_module()}
    ):
        parser = NetlistParser()
    monkeypatch.setattr(module, "NetlistParser", lambda: parser)
    monkeypatch.setattr(module.HierarchyWalker, "validate", lambda self: [])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    widget = module.EditorMainWindow(settings)

    def close_without_dialog(closing_window):
        # pytest-qt закрывает виджеты до завершения обычных фикстур.
        monkeypatch.setattr(closing_window, "_maybe_save", lambda: True)

    qtbot.addWidget(widget, before_close_func=close_without_dialog)
    monkeypatch.setattr(widget, "_error", Mock())
    return widget


def test_save_on_existing_path_persists_changes_and_history(window, tmp_path):
    """Обычное сохранение обновляет открытый документ и сохраняет отмену."""
    path = tmp_path / "existing.net"
    path.write_text(
        "primitive resistor p1 p2\nblock main\nend\n", encoding="utf-8"
    )
    assert window.open_document(str(path))
    window.undo_stack.push(
        AddInstanceCommand("main", "r1", "prim", "resistor", (120, 160))
    )
    assert window.save_document()
    window.undo_stack.push(
        MoveInstanceCommand("main", "r1", (120, 160), (280, 200))
    )

    assert window.save_document()
    window.new_document()
    assert window.open_document(str(path))

    block = window.canvas.draft.block("main")
    assert block.instance("r1").type_name == "resistor"
    assert window.canvas.schematic()["blocks"]["main"]["instances"]["r1"][
        "pos"
    ] == [280, 200]
    window.canvas.undo()
    assert window.canvas.schematic()["blocks"]["main"]["instances"]["r1"][
        "pos"
    ] == [120, 160]


def test_double_clicking_diagnostic_opens_block_and_selects_pin(
    window, tmp_path
):
    """Двойной щелчок по диагностике показывает указанный в ней пин."""
    path = tmp_path / "blocks.net"
    path.write_text(
        "block main\nend\nblock amp\npin input -\nend\n",
        encoding="utf-8",
    )
    assert window.open_document(str(path))
    assert window.canvas.current_block == "main"
    warning = Diagnostic(
        "W604",
        Severity.WARNING,
        "editor",
        "Проверьте подключение",
        SourceLoc(str(path), 4),
        "amp",
        "input",
    )
    window.show_diagnostics([warning])

    row = window.diagnostics_list.item(0)
    window.diagnostics_list.itemDoubleClicked.emit(row)

    assert window.canvas.current_block == "amp"
    assert window.canvas.selected() == [("amp", "pin", "input")]


def test_grid_settings_are_restored_in_next_window(window, qtbot):
    """Размер сетки и привязка сохраняются между запусками окна."""
    window.canvas.set_grid_step(35)
    window.canvas.set_snap_enabled(False)
    filename = window.settings.fileName()
    assert window.close()

    settings = QSettings(filename, QSettings.IniFormat)
    reopened = type(window)(settings)
    qtbot.addWidget(reopened)
    try:
        assert reopened.canvas.grid_step == 35
        assert not reopened.canvas.snap_enabled
        assert not reopened.snap_action.isChecked()
    finally:
        reopened.close()


def _dispatch_shortcut(window, sequence):
    """Проверяет привязку и передаёт событие Qt напрямую действию."""
    key = QKeySequence(sequence)
    actions = [
        action
        for action in window.findChildren(QAction)
        if key in action.shortcuts()
    ]
    assert len(actions) == 1, f"Ожидалось одно действие для {sequence}"
    [action] = actions
    assert action.isEnabled(), f"Действие для {sequence} недоступно"
    # Фокус рабочего стола не нужен для проверки уже распознанной команды.
    assert QCoreApplication.sendEvent(action, QShortcutEvent(key))


def test_undo_and_redo_shortcuts_change_the_canvas(window):
    """Действия, привязанные к Ctrl+Z и Ctrl+Y, меняют черновик холста."""
    window.undo_stack.push(AddBlockCommand("amp", 1))
    assert window.canvas.draft.find_block("amp") is not None

    _dispatch_shortcut(window, "Ctrl+Z")
    assert window.canvas.draft.find_block("amp") is None
    _dispatch_shortcut(window, "Ctrl+Y")
    assert window.canvas.draft.find_block("amp") is not None


def test_copy_paste_shortcuts_preserve_original_and_undo_the_paste(
    window, tmp_path
):
    """Ctrl+C и Ctrl+V создают отдельную копию, которую можно убрать отменой."""
    path = tmp_path / "copy.net"
    path.write_text(
        "primitive resistor p1 p2\nblock main\ninst r1 prim resistor\nend\n",
        encoding="utf-8",
    )
    assert window.open_document(str(path))
    window.canvas.select([("main", "inst", "r1")])
    assert window.canvas.selected() == [("main", "inst", "r1")]

    _dispatch_shortcut(window, "Ctrl+C")
    _dispatch_shortcut(window, "Ctrl+V")

    block = window.canvas.draft.block("main")
    assert len(block.instances()) == 2
    [copy] = [inst for inst in block.instances() if inst.name != "r1"]
    assert copy.type_name == "resistor"
    instances = window.canvas.schematic()["blocks"]["main"]["instances"]
    x, y = instances["r1"]["pos"]
    assert instances[copy.name]["pos"] == [x + 40, y + 40]
    _dispatch_shortcut(window, "Ctrl+Z")
    assert [inst.name for inst in block.instances()] == ["r1"]


def test_new_document_resets_editor_and_turns_autosave_off(window, tmp_path):
    """Новый документ закрывает старый файл, сбрасывает историю и отключает
    автосохранение.
    """
    path = tmp_path / "old.net"
    path.write_text("block main\nend\nblock amp\nend\n", encoding="utf-8")
    assert window.open_document(str(path))

    window.new_document()

    assert window.fm.current_path is None
    assert window.canvas.current_block == "main"
    assert [block.name for block in window.canvas.draft.blocks()] == ["main"]
    assert window.undo_stack.count == 0
    assert not window.autosave.enabled


def test_open_document_selects_main_and_enables_autosave(window, tmp_path):
    """Открытие документа выбирает main, включает автосохранение и добавляет
    файл в список недавних.
    """
    path = tmp_path / "amp.net"
    path.write_text("block main\nend\n", encoding="utf-8")

    assert window.open_document(str(path))

    assert window.block_list.current() == "main"
    assert window.autosave.enabled
    assert str(path) in window.settings.value("files/recent")


def test_open_syntax_error_displays_diagnostic_without_accepting_file(
    window, tmp_path, monkeypatch
):
    """При синтаксической ошибке окно показывает диагностику, оставляет файл
    неоткрытым и не включает автосохранение.
    """
    diagnostic = Diagnostic(
        "E101",
        Severity.ERROR,
        "syntax",
        "Синтаксическая ошибка",
        SourceLoc(None, 1),
    )
    monkeypatch.setattr(
        window.fm._parser,
        "parse",
        lambda *args, **kwargs: ParseResult(RawNetlist(None), [diagnostic]),
    )
    path = tmp_path / "broken.net"
    path.write_text("invalid", encoding="utf-8")

    assert window.open_document(str(path)) is False

    assert window.fm.current_path is None
    assert "E101" in window.diagnostics_list.item(0).text()
    window.autosave.enable.assert_not_called()


def test_open_rejected_draft_releases_file_and_keeps_accepted_model(
    window, tmp_path
):
    """Если черновик не принят, окно освобождает блокировку и сохраняет
    последнее принятое состояние.
    """
    path = tmp_path / "broken.net"
    path.write_text("block main\npin p1 missing\nend\n", encoding="utf-8")
    window.model.add_block("accepted")

    assert window.open_document(str(path)) is False

    assert window.fm.current_path is None
    assert window.fm.check_lock(str(path)) is None
    assert [block.name for block in window.model.blocks()] == ["accepted"]
    assert "E601" in window.diagnostics_list.item(0).text()


def test_save_as_writes_logical_and_geometric_document(window, tmp_path):
    """При «Сохранить как» окно записывает нетлист, геометрию и историю,
    затем включает автосохранение.
    """
    path = tmp_path / "saved.net"

    assert window.save_document_as(str(path))

    assert window.fm.current_path == str(path)
    assert "block main" in path.read_text(encoding="utf-8")
    assert (tmp_path / "saved.sch.json").is_file()
    assert (tmp_path / "saved.history.json").is_file()
    assert window.autosave.enabled


def test_save_as_rejects_invalid_draft_before_creating_file(window, tmp_path):
    """Некорректный черновик останавливает «Сохранить как» до записи файла.
    Причина видна в диагностике.
    """
    window.canvas.draft.block("main").add_pin("p1", "missing")
    path = tmp_path / "refused.net"

    assert window.save_document_as(str(path)) is False

    assert not path.exists()
    assert "E601" in window.diagnostics_list.item(0).text()


def test_save_without_current_path_uses_save_as_dialog(window, monkeypatch):
    """Если у документа нет пути, обычное сохранение открывает диалог
    «Сохранить как» и учитывает его отмену.
    """
    dialog = Mock(return_value=False)
    monkeypatch.setattr(window, "_save_document_as_dialog", dialog)

    assert window.save_document() is False
    dialog.assert_called_once_with()


def test_cancel_close_preserves_document_and_does_not_flush(
    window, tmp_path, monkeypatch
):
    """При отмене закрытия документ остаётся открытым, а принудительное
    автосохранение не запускается.
    """
    path = tmp_path / "open.net"
    assert window.save_document_as(str(path))
    window.canvas.op_add_block("pending", 1)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel
    )

    assert window.close() is False

    assert window.fm.current_path == str(path)
    window.autosave.flush.assert_not_called()


def test_close_flushes_autosave_and_releases_file(window, tmp_path):
    """При закрытии окно вызывает автосохранение и освобождает блокировку
    документа.
    """
    path = tmp_path / "open.net"
    assert window.save_document_as(str(path))

    assert window.close()

    window.autosave.flush.assert_called_once_with()
    assert window.fm.current_path is None
    assert window.fm.check_lock(str(path)) is None


def test_rejected_open_does_not_replace_already_open_document(
    window, tmp_path, monkeypatch
):
    """Синтаксическая ошибка в другом файле не закрывает текущий документ и
    не отключает его автосохранение.
    """
    old = tmp_path / "open.net"
    assert window.save_document_as(str(old))
    broken = tmp_path / "broken.net"
    broken.write_text("invalid", encoding="utf-8")
    diag = Diagnostic(
        "E101", Severity.ERROR, "syntax", "bad", SourceLoc(None, 1)
    )
    monkeypatch.setattr(
        window.fm._parser,
        "parse",
        lambda *args, **kwargs: ParseResult(RawNetlist(None), [diag]),
    )

    assert window.open_document(str(broken)) is False

    assert window.fm.current_path == str(old)
    assert window.autosave.enabled


def test_diagnostics_panel_shows_error_and_warning_counts(window):
    """Панель выводит обе диагностики и отдельно считает ошибки и
    предупреждения.
    """
    diagnostics = [
        Diagnostic(
            "E601", Severity.ERROR, "editor", "no net", SourceLoc(None, 1)
        ),
        Diagnostic(
            "W604", Severity.WARNING, "net", "single pin", SourceLoc(None, 2)
        ),
    ]

    window.show_diagnostics(diagnostics)

    assert "ошибок: 1, предупреждений: 1" in window.diagnostics_label.text()
    assert [
        window.diagnostics_list.item(row).text()
        for row in range(window.diagnostics_list.count())
    ] == [diagnostic.format() for diagnostic in diagnostics]
