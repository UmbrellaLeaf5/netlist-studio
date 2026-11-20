"""Проверки координации документа в главном окне Пронина."""

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QMessageBox

from src.core.model import Diagnostic, Severity, SourceLoc
from src.core.parser import NetlistParser, ParseResult, RawNetlist
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
    qtbot.addWidget(widget)
    monkeypatch.setattr(widget, "_error", Mock())

    yield widget

    # Уборка не должна открывать диалог о несохранённых изменениях.
    monkeypatch.setattr(widget, "_maybe_save", lambda: True)
    widget.close()


def test_new_document_resets_editor_and_turns_autosave_off(window, tmp_path):
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
    path = tmp_path / "amp.net"
    path.write_text("block main\nend\n", encoding="utf-8")

    assert window.open_document(str(path))

    assert window.block_list.current() == "main"
    assert window.autosave.enabled
    assert str(path) in window.settings.value("files/recent")


def test_open_syntax_error_displays_diagnostic_without_accepting_file(
    window, tmp_path, monkeypatch
):
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
    path = tmp_path / "broken.net"
    path.write_text("block main\npin p1 missing\nend\n", encoding="utf-8")
    window.model.add_block("accepted")

    assert window.open_document(str(path)) is False

    assert window.fm.current_path is None
    assert window.fm.check_lock(str(path)) is None
    assert [block.name for block in window.model.blocks()] == ["accepted"]
    assert "E601" in window.diagnostics_list.item(0).text()


def test_save_as_writes_logical_and_geometric_document(window, tmp_path):
    path = tmp_path / "saved.net"

    assert window.save_document_as(str(path))

    assert window.fm.current_path == str(path)
    assert "block main" in path.read_text(encoding="utf-8")
    assert (tmp_path / "saved.sch.json").is_file()
    assert (tmp_path / "saved.history.json").is_file()
    assert window.autosave.enabled


def test_save_as_rejects_invalid_draft_before_creating_file(window, tmp_path):
    window.canvas.draft.block("main").add_pin("p1", "missing")
    path = tmp_path / "refused.net"

    assert window.save_document_as(str(path)) is False

    assert not path.exists()
    assert "E601" in window.diagnostics_list.item(0).text()


def test_save_without_current_path_uses_save_as_dialog(window, monkeypatch):
    dialog = Mock(return_value=False)
    monkeypatch.setattr(window, "_save_document_as_dialog", dialog)

    assert window.save_document() is False
    dialog.assert_called_once_with()


def test_cancel_close_preserves_document_and_does_not_flush(
    window, tmp_path, monkeypatch
):
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
    path = tmp_path / "open.net"
    assert window.save_document_as(str(path))

    assert window.close()

    window.autosave.flush.assert_called_once_with()
    assert window.fm.current_path is None
    assert window.fm.check_lock(str(path)) is None


def test_rejected_open_does_not_replace_already_open_document(
    window, tmp_path, monkeypatch
):
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
