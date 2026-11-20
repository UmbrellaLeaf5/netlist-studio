"""Главное окно редактора нетлиста.

Автор: А. Пронин. Контракт: docs/interfaces.md, раздел 3.6.

Окно связывает ``Canvas``, ``FileManager``, ``UndoStack``, ``Autosave``
и ``HierarchyWalker``: сверху меню и панель инструментов, слева список
блоков, в центре схема, снизу диагностика.
"""

from __future__ import annotations

import os
import sys

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QColor, QFont, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from netlist.core.hierarchy import HierarchyWalker
from netlist.core.model import (
    CheckOptions,
    Diagnostic,
    Netlist,
    Severity,
    SourceLoc,
)
from netlist.core.parser import NetlistParser
from netlist.core.serializer import NetlistSerializer
from netlist.editor.autosave import Autosave, DocumentState
from netlist.editor.block_list import BlockList
from netlist.editor.canvas import (
    AddBlockCommand,
    AddInstanceCommand,
    Canvas,
    CopyBlockCommand,
    EditorDraft,
    RemoveBlockCommand,
    RenameBlockCommand,
    Tool,
    MoveBlockCommand,
)
from netlist.editor.file_manager import (
    FileAccessError,
    FileLockedError,
    FileManager,
    file_sha256,
    history_path,
)
from netlist.editor.tool_palette import ToolPalette
from netlist.editor.undo_stack import UndoStack

AUTOSAVE_DEFAULT = 30_000


class EditorMainWindow(QMainWindow):
    """Окно редактора с документом, историей и автосохранением."""

    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or QSettings("Netlist", "Editor")
        self.model = Netlist()
        self.fm = FileManager(NetlistParser(), NetlistSerializer())
        self._dirty = False
        self._suppress_dirty = False
        self._diagnostics: list[Diagnostic] = []
        self._clipboard: list[dict] = []
        self._autosave_interval = self._int_setting(
            "autosave/interval_ms", AUTOSAVE_DEFAULT
        )
        if self._autosave_interval <= 0:
            self._autosave_interval = AUTOSAVE_DEFAULT

        self.block_list = BlockList(self)
        self.palette = ToolPalette(self)
        self.canvas = Canvas(
            self.model,
            None,
            self.block_list,
            self.palette,
            lambda view: HierarchyWalker(view).validate(),
            self,
        )
        self.undo_stack = UndoStack(self.canvas)
        self.canvas.set_undo_stack(self.undo_stack)

        self.autosave = Autosave(
            self.fm, self._document_state,
            self._autosave_interval, self,
        )

        self._build_ui()
        self._build_menu()
        self._connect()
        self._restore_settings()
        self._refresh_history_state()
        self._update_title()
        self._update_status()

    # ---- построение ---------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("Нетлист — редактор")
        self.resize(1280, 820)

        self.diagnostics_label = QLabel("Диагностика")
        self.diagnostics_label.setStyleSheet("font-weight: 600;")
        self.diagnostics_list = QListWidget()
        self.diagnostics_list.setFont(QFont("Monospace", 9))
        self.diagnostics_list.itemDoubleClicked.connect(
            self._activate_diagnostic
        )

        diagnostics = QWidget()
        layout = QVBoxLayout(diagnostics)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        layout.addWidget(self.diagnostics_label)
        layout.addWidget(self.diagnostics_list, 1)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.block_list)
        top.addWidget(self.canvas)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([360, 1040])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(diagnostics)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([650, 150])
        self.setCentralWidget(splitter)

        self.addToolBar(self.palette)
        self.statusBar().showMessage("Готово")

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("Файл")
        file_menu.addAction(self.palette.new_action)
        file_menu.addAction(self.palette.open_action)
        file_menu.addAction(self.palette.save_action)
        save_as = QAction("Сохранить как…", self)
        save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_as.triggered.connect(self._save_document_as_dialog)
        file_menu.addAction(save_as)
        self.recent_menu = file_menu.addMenu("Недавние")
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        exit_action = QAction("Выход", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = menu.addMenu("Правка")
        edit_menu.addAction(self.palette.undo_action)
        edit_menu.addAction(self.palette.redo_action)
        edit_menu.addSeparator()
        copy_action = QAction("Копировать", self)
        copy_action.setShortcut(QKeySequence("Ctrl+C"))
        copy_action.triggered.connect(self._copy_selected)
        edit_menu.addAction(copy_action)
        paste_action = QAction("Вставить", self)
        paste_action.setShortcut(QKeySequence("Ctrl+V"))
        paste_action.triggered.connect(self._paste_selected)
        edit_menu.addAction(paste_action)
        delete_action = QAction("Удалить", self)
        delete_action.setShortcut(QKeySequence("Del"))
        delete_action.triggered.connect(self.canvas.delete_selected)
        edit_menu.addAction(delete_action)
        rename_action = QAction("Переименовать", self)
        rename_action.setShortcut(QKeySequence("F2"))
        rename_action.triggered.connect(self.canvas.rename_selected)
        edit_menu.addAction(rename_action)
        properties_action = QAction("Свойства…", self)
        properties_action.setShortcut(QKeySequence("Alt+Return"))
        properties_action.triggered.connect(self.canvas.edit_selected)
        edit_menu.addAction(properties_action)

        block_menu = menu.addMenu("Блок")
        new_block = QAction("Новый блок…", self)
        new_block.triggered.connect(self._new_block)
        block_menu.addAction(new_block)
        delete_block = QAction("Удалить блок", self)
        delete_block.triggered.connect(self._delete_current_block)
        block_menu.addAction(delete_block)
        copy_block = QAction("Копировать блок…", self)
        copy_block.triggered.connect(self._copy_current_block)
        block_menu.addAction(copy_block)
        block_menu.addSeparator()
        up = QAction("Переместить вверх", self)
        up.setShortcut(QKeySequence("Ctrl+Up"))
        up.triggered.connect(lambda: self._move_current_block(-1))
        block_menu.addAction(up)
        down = QAction("Переместить вниз", self)
        down.setShortcut(QKeySequence("Ctrl+Down"))
        down.triggered.connect(lambda: self._move_current_block(1))
        block_menu.addAction(down)
        block_menu.addSeparator()
        block_props = QAction("Свойства блока…", self)
        block_props.triggered.connect(self._block_properties)
        block_menu.addAction(block_props)

        tools_menu = menu.addMenu("Инструменты")
        tools_menu.addAction(self.palette.select_action)
        tools_menu.addAction(self.palette.place_action)
        tools_menu.addAction(self.palette.wire_action)

        check_menu = menu.addMenu("Проверка")
        check_menu.addAction(self.palette.check_action)

        settings_menu = menu.addMenu("Настройки")
        autosave = QAction("Интервал автосохранения…", self)
        autosave.triggered.connect(self._configure_autosave)
        settings_menu.addAction(autosave)
        grid = QAction("Шаг сетки…", self)
        grid.triggered.connect(self._configure_grid)
        settings_menu.addAction(grid)
        snap = QAction("Привязка к сетке", self)
        snap.setCheckable(True)
        snap.setChecked(bool(self.settings.value("canvas/snap", True,
                                                  type=bool)))
        snap.toggled.connect(self.canvas.set_snap_enabled)
        settings_menu.addAction(snap)
        self.snap_action = snap

        help_menu = menu.addMenu("Справка")
        guide = QAction("Руководство", self)
        guide.triggered.connect(self._show_guide)
        help_menu.addAction(guide)
        about = QAction("О программе", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _connect(self) -> None:
        self.palette.newRequested.connect(self.new_document)
        self.palette.openRequested.connect(self._open_document_dialog)
        self.palette.saveRequested.connect(self.save_document)
        self.palette.checkRequested.connect(self.check_document)
        self.palette.undoRequested.connect(self.canvas.undo)
        self.palette.redoRequested.connect(self.canvas.redo)
        self.palette.toolChanged.connect(self.canvas.set_tool)

        self.block_list.blockActivated.connect(self.canvas.open_block)
        self.block_list.createRequested.connect(self._new_block)
        self.block_list.deleteRequested.connect(self._delete_block)
        self.block_list.copyRequested.connect(self._copy_block)
        self.block_list.moveRequested.connect(self._move_block)
        self.block_list.propertiesRequested.connect(
            self._block_properties_named
        )

        self.canvas.draftChanged.connect(self._on_draft_changed)
        self.canvas.currentBlockChanged.connect(self._on_block_changed)
        self.canvas.selectionChanged.connect(
            lambda _ids: self._update_status()
        )
        self.canvas.committed.connect(self._on_committed)
        self.canvas.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 5000)
        )

        self.undo_stack.add_listener(self._refresh_history_state)
        self.autosave.saved.connect(self._on_autosaved)
        self.autosave.failed.connect(self._on_autosave_failed)

    # ---- настройки -----------------------------------------------------
    def _int_setting(self, key: str, default: int) -> int:
        value = self.settings.value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _restore_settings(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/state")
        if state is not None:
            self.restoreState(state)
        grid = self._int_setting("canvas/grid_step", 20)
        self.canvas.set_grid_step(float(grid))
        snap = self.settings.value("canvas/snap", True, type=bool)
        self.canvas.set_snap_enabled(bool(snap))
        self.snap_action.setChecked(bool(snap))
        self._autosave_interval = self._int_setting(
            "autosave/interval_ms", AUTOSAVE_DEFAULT
        )
        if self._autosave_interval <= 0:
            self._autosave_interval = AUTOSAVE_DEFAULT
        self.autosave.set_interval(self._autosave_interval)

    def _save_settings(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        self.settings.setValue(
            "autosave/interval_ms", self._autosave_interval
        )
        self.settings.setValue("canvas/grid_step", int(self.canvas.grid_step))
        self.settings.setValue("canvas/snap", self.canvas.snap_enabled)
        self.settings.setValue("files/recent", self._recent_files())
        self.settings.sync()

    def _recent_files(self) -> list[str]:
        value = self.settings.value("files/recent", [])
        if isinstance(value, str):
            return [item for item in value.split("\n") if item]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return []

    def _add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        recent = [item for item in self._recent_files() if item != path]
        recent.insert(0, path)
        self.settings.setValue("files/recent", recent[:10])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        files = self._recent_files()
        if not files:
            empty = self.recent_menu.addAction("(пусто)")
            empty.setEnabled(False)
            return
        for path in files:
            action = self.recent_menu.addAction(path)
            action.triggered.connect(
                lambda _checked=False, value=path: self.open_document(value)
            )

    # ---- состояние окна ------------------------------------------------
    def _document_state(self) -> DocumentState:
        return DocumentState(
            self.model, self.canvas.schematic(), self.undo_stack
        )

    def _on_draft_changed(self) -> None:
        if self._suppress_dirty:
            return
        self._dirty = True
        self._update_title()
        # Принятое состояние обновляем сразу; ошибки не мешают правке.
        self.canvas.commit()

    def _on_committed(self, ok: bool) -> None:
        if self._suppress_dirty:
            return
        if ok:
            self.autosave.mark_dirty()
            self._dirty = True
            self._update_title()
        else:
            self.statusBar().showMessage(
                "Изменения не приняты: есть ошибки", 4000
            )

    def _on_block_changed(self, name: str) -> None:
        self.block_list.select(name)
        self._update_status()

    def _on_autosaved(self, path: str) -> None:
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(
            f"Автосохранение: {os.path.basename(path)}", 4000
        )

    def _on_autosave_failed(self, message: str) -> None:
        # Новый документ без пути не может автосохраняться: это не ошибка
        # пользователя, особенно при закрытии с отказом от сохранения.
        if self.fm.current_path is None:
            return
        self.statusBar().showMessage(
            f"Автосохранение не удалось: {message}", 6000
        )

    def _refresh_history_state(self) -> None:
        self.palette.set_undo_state(
            self.undo_stack.can_undo(), self.undo_stack.undo_text()
        )
        self.palette.set_redo_state(
            self.undo_stack.can_redo(), self.undo_stack.redo_text()
        )

    def _update_title(self) -> None:
        path = self.fm.current_path
        name = os.path.basename(path) if path else "Без имени"
        mark = " *" if self._dirty else ""
        auto = " · автосохранение вкл" if self.autosave.enabled else \
            " · автосохранение выкл"
        self.setWindowTitle(f"{name}{mark}{auto} — Нетлист")
        self.palette.set_save_enabled(
            self.fm.current_path is not None or self._dirty
        )

    def _update_status(self) -> None:
        selected = []
        for item_id in self.canvas.selected():
            if item_id[1] == "inst":
                selected.append(item_id[2])
            elif item_id[1] == "pin":
                selected.append(item_id[2])
            elif item_id[1] == "ipin":
                selected.append(f"{item_id[2]}.{item_id[3]}")
            elif item_id[1] == "net":
                selected.append(item_id[2])
        tool = {
            Tool.SELECT: "Выделение",
            Tool.PLACE: "Размещение",
            Tool.WIRE: "Соединение",
        }.get(self.canvas._tool, "Выделение")
        text = f"Блок {self.canvas.current_block}"
        if selected:
            text += " | выделено: " + ", ".join(selected)
        text += f" | инструмент: {tool}"
        self.statusBar().showMessage(text)

    # ---- документ ------------------------------------------------------
    def new_document(self) -> None:
        if not self._maybe_save():
            return
        try:
            self.fm.new()
        except FileAccessError as exc:
            self._error("Не удалось закрыть документ", str(exc))
            return
        self._suppress_dirty = True
        try:
            self.model.replace_with(EditorDraft.new_document())
            self.canvas.set_model(self.model)
            self.canvas.new_document()
            self.undo_stack.clear()
            self.autosave.disable()
        finally:
            self._suppress_dirty = False
        self._dirty = False
        self._diagnostics = []
        self.show_diagnostics([])
        self._update_title()
        self._update_status()

    def _open_document_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть нетлист", "", "Нетлист (*.net);;Все файлы (*)"
        )
        if path:
            self.open_document(path)

    def open_document(self, path: str) -> bool:
        if not self._maybe_save():
            return False
        result = None
        try:
            result = self.fm.open(path)
        except FileLockedError as exc:
            if exc.info.stale:
                answer = QMessageBox.question(
                    self,
                    "Файл заблокирован",
                    "Файл оставлен завершившимся процессом. Снять "
                    "устаревшую блокировку?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer == QMessageBox.Yes:
                    try:
                        result = self.fm.open(path, force_unlock=True)
                    except (FileAccessError, OSError) as inner:
                        self._error("Не удалось открыть файл", str(inner))
                        return False
                else:
                    return False
            else:
                self._error(
                    "Файл заблокирован",
                    f"pid={exc.info.pid}, host={exc.info.host}",
                )
                return False
        except (FileAccessError, OSError) as exc:
            self._error("Не удалось открыть файл", str(exc))
            return False
        if result is None:
            return False
        if not result.ok:
            self.show_diagnostics(result.parse.diagnostics)
            self.statusBar().showMessage(
                "Файл содержит синтаксические ошибки", 6000
            )
            return False

        self._suppress_dirty = True
        try:
            self.canvas.load_draft(result.parse.raw, result.schematic)
            diagnostics = self.canvas.commit()
        finally:
            self._suppress_dirty = False
        errors = [
            d for d in diagnostics if d.severity is Severity.ERROR
        ]
        if errors:
            self.show_diagnostics(diagnostics)
            try:
                self.fm.close()
            except FileAccessError:
                pass
            self.canvas.reset_to_model()
            self.autosave.disable()
            self._dirty = False
            self._update_title()
            self._error(
                "Документ не принят",
                "Иерархия или черновик содержат ошибки. Исправьте их и "
                "откройте файл снова.",
            )
            return False

        history_loaded = self.undo_stack.load(
            history_path(path), file_sha256(path)
        )
        diagnostics = list(result.parse.diagnostics)
        if not history_loaded and os.path.exists(history_path(path)):
            diagnostics.append(Diagnostic(
                code="W702",
                severity=Severity.WARNING,
                check="editor",
                message=(
                    "История редактирования не соответствует файлу и "
                    "отброшена"
                ),
                loc=SourceLoc(path, None),
            ))
        self.autosave.enable()
        self._add_recent(path)
        self._dirty = False
        self.show_diagnostics(diagnostics)
        self._update_title()
        self._update_status()
        self.block_list.select(self.canvas.current_block)
        return True

    def save_document(self) -> bool:
        if self.fm.current_path is None:
            return self._save_document_as_dialog()
        diagnostics = self.canvas.commit()
        if any(d.severity is Severity.ERROR for d in diagnostics):
            self.show_diagnostics(diagnostics)
            return False
        try:
            result = self.fm.save(
                self.model, self.canvas.schematic()
            )
            self.undo_stack.save(
                history_path(result.path), result.sha256
            )
        except (FileAccessError, OSError, ValueError) as exc:
            self._error("Не удалось сохранить файл", str(exc))
            return False
        self.autosave.enable()
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(
            f"Сохранено: {os.path.basename(result.path)}", 4000
        )
        return True

    def _save_document_as_dialog(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить нетлист", "", "Нетлист (*.net);;Все файлы (*)"
        )
        if not path:
            return False
        if not path.lower().endswith(".net"):
            path += ".net"
        return self.save_document_as(path)

    def save_document_as(self, path: str) -> bool:
        diagnostics = self.canvas.commit()
        if any(d.severity is Severity.ERROR for d in diagnostics):
            self.show_diagnostics(diagnostics)
            return False
        try:
            result = self.fm.save(
                self.model, self.canvas.schematic(), path=path
            )
            self.undo_stack.save(
                history_path(result.path), result.sha256
            )
        except (FileAccessError, OSError, ValueError) as exc:
            self._error("Не удалось сохранить файл", str(exc))
            return False
        self.autosave.enable()
        self._add_recent(result.path)
        self._dirty = False
        self._update_title()
        self.statusBar().showMessage(
            f"Сохранено: {os.path.basename(result.path)}", 4000
        )
        return True

    def check_document(self) -> list[Diagnostic]:
        diagnostics = self.canvas.validate()
        draft = self.canvas.draft
        options = CheckOptions()
        # Полные проверки подсхем/пинов/цепей подключаются, если
        # соответствующие модули есть в сборке. Редактор не должен
        # зависеть от конкретного набора чужих модулей.
        import importlib

        for name in ("subcircuit_check", "pin_check", "net_check"):
            try:
                module = importlib.import_module(
                    f"netlist.checker.{name}"
                )
            except ImportError:
                continue
            diagnostics.extend(module.check(draft, options))
        seen: set[tuple] = set()
        unique: list[Diagnostic] = []
        for diag in diagnostics:
            key = (
                diag.code, diag.severity, diag.block, diag.obj,
                diag.message,
            )
            if key not in seen:
                seen.add(key)
                unique.append(diag)
        self.show_diagnostics(unique)
        return unique

    def show_diagnostics(self, diags: list[Diagnostic]) -> None:
        self._diagnostics = list(diags)
        self.diagnostics_list.clear()
        errors = sum(d.severity is Severity.ERROR for d in diags)
        warnings = len(diags) - errors
        self.diagnostics_label.setText(
            f"Диагностика — ошибок: {errors}, предупреждений: {warnings}"
        )
        if not diags:
            item = QListWidgetItem("Ошибок и предупреждений нет.")
            item.setForeground(QColor("#667085"))
            self.diagnostics_list.addItem(item)
            return
        for diag in diags:
            item = QListWidgetItem(diag.format())
            item.setData(Qt.UserRole, diag)
            item.setForeground(
                QColor("#b42318")
                if diag.severity is Severity.ERROR
                else QColor("#8a6116")
            )
            self.diagnostics_list.addItem(item)

    def _activate_diagnostic(self, item: QListWidgetItem) -> None:
        diag = item.data(Qt.UserRole)
        if not isinstance(diag, Diagnostic):
            return
        block_name = diag.block
        if block_name and self.canvas.draft.find_block(block_name):
            self.canvas.open_block(block_name)
        obj = diag.obj
        if not obj or not block_name:
            return
        candidates: list[tuple[str, ...]] = []
        if "." in obj:
            instance, pin = obj.split(".", 1)
            candidates.append((block_name, "ipin", instance, pin))
            candidates.append((block_name, "inst", instance))
        else:
            candidates.extend([
                (block_name, "inst", obj),
                (block_name, "pin", obj),
                (block_name, "net", obj),
            ])
        for candidate in candidates:
            if candidate in self.canvas._id_items:
                self.canvas.select([candidate])
                break

    # ---- блоки ----------------------------------------------------------
    def _new_block(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Новый блок", "Имя блока:"
        )
        name = name.strip()
        if not ok or not name:
            return
        if self.canvas.draft.find_block(name) is not None:
            self._error("Блок уже существует", f"Блок '{name}' уже есть.")
            return
        self.undo_stack.push(
            AddBlockCommand(name, len(self.canvas.draft.blocks()))
        )
        self.canvas.open_block(name)

    def _delete_current_block(self) -> None:
        name = self.block_list.current()
        if name:
            self._delete_block(name)

    def _delete_block(self, name: str) -> None:
        if len(self.canvas.draft.blocks()) <= 1:
            self._error(
                "Нельзя удалить блок",
                "В документе должен остаться хотя бы один блок.",
            )
            return
        if name == "main":
            answer = QMessageBox.question(
                self, "Удалить main?",
                "Корневой блок main нужен для проверки. Всё равно "
                "удалить?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        snapshot = self.canvas.block_snapshot(name)
        if not snapshot:
            return
        self.undo_stack.push(RemoveBlockCommand(snapshot))

    def _copy_current_block(self) -> None:
        name = self.block_list.current()
        if name:
            self._copy_block(name)

    def _copy_block(self, name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Копировать блок", "Имя копии:", text=f"{name}_copy"
        )
        new_name = new_name.strip()
        if not ok or not new_name:
            return
        if self.canvas.draft.find_block(new_name) is not None:
            self._error("Блок уже существует", f"Блок '{new_name}' уже есть.")
            return
        self.undo_stack.push(
            CopyBlockCommand(name, new_name,
                             len(self.canvas.draft.blocks()))
        )
        self.canvas.open_block(new_name)

    def _move_current_block(self, delta: int) -> None:
        name = self.block_list.current()
        if not name:
            return
        names = [block.name for block in self.canvas.draft.blocks()]
        old = names.index(name)
        new = max(0, min(len(names) - 1, old + delta))
        if new != old:
            self.undo_stack.push(MoveBlockCommand(name, old, new))

    def _move_block(self, name: str, index: int) -> None:
        names = [block.name for block in self.canvas.draft.blocks()]
        if name not in names:
            return
        old = names.index(name)
        if old != index:
            self.undo_stack.push(MoveBlockCommand(name, old, index))

    def _block_properties(self) -> None:
        name = self.block_list.current()
        if name:
            self._block_properties_named(name)

    def _block_properties_named(self, name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Свойства блока", "Имя блока:", text=name
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == name:
            return
        if self.canvas.draft.find_block(new_name) is not None:
            self._error("Блок уже существует", f"Блок '{new_name}' уже есть.")
            return
        self.undo_stack.push(RenameBlockCommand(name, new_name))

    # ---- правка ---------------------------------------------------------
    def _copy_selected(self) -> None:
        snapshots = []
        for item_id in self.canvas.selected():
            if item_id[1] == "inst":
                snapshot = self.canvas.instance_snapshot(
                    item_id[0], item_id[2]
                )
                if snapshot:
                    snapshots.append(snapshot)
        self._clipboard = snapshots
        if snapshots:
            self.statusBar().showMessage(
                f"Скопировано: {len(snapshots)}", 3000
            )

    def _paste_selected(self) -> None:
        if not self._clipboard:
            self.canvas.copy_selected()
            return
        if self.undo_stack.can_undo() or self._clipboard:
            self.undo_stack.begin_macro("Вставить")
        for snapshot in self._clipboard:
            block = self.canvas.current_block
            existing = {
                inst.name for inst in
                self.canvas.draft.block(block).instances()
            }
            index = 1
            while f"t{index}" in existing:
                index += 1
            name = f"t{index}"
            pos = snapshot.get("pos", [0.0, 0.0])
            copy = dict(snapshot)
            copy["name"] = name
            copy["pos"] = [float(pos[0]) + 40.0, float(pos[1]) + 40.0]
            copy["block"] = block
            self.undo_stack.push(AddInstanceCommand(
                block, name, snapshot["kind"], snapshot["type_name"],
                tuple(copy["pos"]), copy,
            ))
        self.undo_stack.end_macro()

    # ---- настройки ------------------------------------------------------
    def _configure_autosave(self) -> None:
        value, ok = QInputDialog.getInt(
            self, "Автосохранение", "Интервал, мс:",
            self._autosave_interval, 1000, 3_600_000, 1000,
        )
        if not ok:
            return
        try:
            self.autosave.set_interval(value)
        except ValueError as exc:
            self._error("Неверный интервал", str(exc))
            return
        self._autosave_interval = value
        self.settings.setValue("autosave/interval_ms", value)

    def _configure_grid(self) -> None:
        value, ok = QInputDialog.getInt(
            self, "Шаг сетки", "Шаг, пикселей:",
            int(self.canvas.grid_step), 4, 100, 2,
        )
        if ok:
            self.canvas.set_grid_step(float(value))
            self.settings.setValue("canvas/grid_step", value)

    # ---- сервис ---------------------------------------------------------
    def _error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def _maybe_save(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self, "Несохранённые изменения",
            "Сохранить изменения перед продолжением?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_document()
        return True

    def _show_guide(self) -> None:
        QMessageBox.information(
            self, "Руководство",
            "Перетащите блок или примитив из списка слева на схему.\n"
            "Инструмент «Соединение» соединяет пины цепью.\n"
            "F5 — проверить нетлист, Ctrl+Z — отменить.",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "О программе",
            "Нетлист — редактор иерархических нетлистов.\n"
            "PyQt5, формат .net и .sch.json.",
        )

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        try:
            self.autosave.flush()
        except Exception:
            pass
        try:
            self.fm.close()
        except FileAccessError:
            pass
        self._save_settings()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    """Точка входа ``python -m netlist.editor [FILE]``."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(
        ["netlist-editor", *arguments]
    )
    window = EditorMainWindow()
    window.show()
    if arguments:
        window.open_document(arguments[0])
    return app.exec_()


__all__ = ["EditorMainWindow", "main"]
