"""Автосохранение принятой модели по § 3.3 без ожидания таймера."""

import gc
import hashlib
import json
import sys
import unittest
import weakref
from contextlib import contextmanager, nullcontext
from copy import copy, deepcopy
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

from netlist.core.model import Netlist
from netlist.editor import file_manager
from netlist.editor.undo_stack import UndoStack


HAS_QT = find_spec("PySide6") is not None
DOCUMENT = str(Path("design", "example.net"))
HISTORY = str(Path("design", "example.history.json"))
SHA = "a" * 64


class Event:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class TestSignal:
    def __init__(self, *_types):
        pass

    def __set_name__(self, _owner, name):
        self.name = name

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        return instance.__dict__.setdefault(self.name, Event())


class TimerOwner:
    def __init__(self, parent=None):
        self._parent = weakref.ref(parent) if parent is not None else None

    def parent(self):
        return self._parent() if self._parent else None


class ManualTimer(TimerOwner):
    """Минимальный Qt-таймер, которому тест сам посылает timeout."""

    def __init__(self, parent):
        super().__init__(parent)
        self.timeout = Event()
        self.active = False
        self.interval_ms = 0

    def setInterval(self, ms):
        self.interval_ms = ms

    def interval(self):
        return self.interval_ms

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active


def load_without_qt():
    """Подменить Qt при импорте реального Autosave под тестовым именем."""
    qt = ModuleType("PySide6")
    qt.__path__ = []
    qtcore = ModuleType("PySide6.QtCore")
    qt.QtCore = qtcore
    qtcore.QObject, qtcore.QTimer, qtcore.Signal = (
        TimerOwner, ManualTimer, TestSignal,
    )
    path = Path(file_manager.__file__).with_name("autosave.py")
    spec = spec_from_file_location("netlist.editor._autosave_unit_tests", path)
    module = module_from_spec(spec)
    with patch.dict(sys.modules, {
        "PySide6": qt, "PySide6.QtCore": qtcore, spec.name: module,
    }):
        spec.loader.exec_module(module)
    return module


if HAS_QT:
    from PySide6.QtCore import QCoreApplication, QEvent, QObject
    from shiboken6 import isValid
    from netlist.editor import autosave
else:
    autosave = load_without_qt()
    QObject = TimerOwner


class DocumentStateTest(unittest.TestCase):
    def test_construct_copy_and_release_document_state(self):
        model = Netlist()
        model.add_block("main")
        schematic = {"version": 1, "blocks": {"main": {}}}
        stack = UndoStack(SimpleNamespace())
        state = autosave.DocumentState(model, schematic, stack)
        self.assertIs(state.model, model)
        self.assertIs(state.schematic, schematic)
        self.assertIs(state.undo_stack, stack)

        duplicate = copy(state)
        self.assertEqual(duplicate, state)
        self.assertIsNot(duplicate, state)
        independent = deepcopy(state)
        independent.schematic["blocks"]["other"] = {}
        self.assertEqual(state.schematic["blocks"], {"main": {}})
        ref = weakref.ref(duplicate)
        del duplicate
        gc.collect()
        self.assertIsNone(ref())


class AutosaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if HAS_QT:
            cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.model = Netlist()
        self.model.add_block("main")
        self.schematic = {"version": 1, "blocks": {"main": {}}}
        self.manager = Mock()
        self.manager.current_path = DOCUMENT
        self.manager.save.return_value = file_manager.SaveResult(DOCUMENT, SHA)
        self.manager.transaction.side_effect = nullcontext
        self.stack = Mock(spec_set=["save"])
        self.get_state = Mock(return_value=autosave.DocumentState(
            self.model, self.schematic, self.stack,
        ))
        self.service = autosave.Autosave(self.manager, self.get_state)
        self.addCleanup(self.service.disable)
        self.calls = Mock()
        self.calls.attach_mock(self.get_state, "get_state")
        self.calls.attach_mock(self.manager.save, "document")
        self.calls.attach_mock(self.stack.save, "history")
        self.service.saved.connect(self.calls.saved)
        self.service.failed.connect(self.calls.failed)

    def test_initial_state_default_interval_and_enable_disable(self):
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.service._timer.interval(), 30_000)
        self.service.enable()
        self.assertTrue(self.service.enabled)
        self.service.disable()
        self.service.disable()
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.calls.mock_calls, [])

    def test_enable_requires_previous_open_or_save_as(self):
        self.manager.current_path = None
        with self.assertRaises(RuntimeError):
            self.service.enable()
        self.assertFalse(self.service.enabled)
        self.manager.current_path = DOCUMENT
        self.service.enable()
        self.assertTrue(self.service.enabled)

    def test_set_interval_changes_active_timer_and_rejects_invalid_values(self):
        self.service.enable()
        self.service.set_interval(500)
        self.assertTrue(self.service.enabled)
        self.assertEqual(self.service._timer.interval(), 500)
        for ms in (0, -1, 2_147_483_648, True, 1.5):
            with self.subTest(ms=ms):
                with self.assertRaises(ValueError):
                    self.service.set_interval(ms)
                self.assertEqual(self.service._timer.interval(), 500)
        with self.assertRaises(ValueError):
            autosave.Autosave(self.manager, self.get_state, interval_ms=0)

    def test_clean_flush_returns_success_without_io_or_signals(self):
        self.assertTrue(self.service.flush())
        self.assertEqual(self.calls.mock_calls, [])

    def test_mark_dirty_defers_write_until_timer_and_uses_current_state(self):
        self.service.enable()
        self.service.mark_dirty()
        self.assertEqual(self.calls.mock_calls, [])
        latest_schematic = {"version": 1, "blocks": {"latest": {}}}
        self.get_state.return_value = autosave.DocumentState(
            self.model, latest_schematic, self.stack,
        )
        self.service._timer.timeout.emit()
        self.service._timer.timeout.emit()  # Второй тик после успеха чистый.
        self.assertEqual(self.calls.mock_calls, [
            call.get_state(),
            call.document(self.model, latest_schematic),
            call.history(HISTORY, SHA),
            call.saved(DOCUMENT),
        ])

    def test_manual_flush_after_disable_saves_and_clears_dirty_state(self):
        self.service.enable()
        self.service.mark_dirty()
        self.service.disable()
        self.assertFalse(self.service.enabled)
        self.assertTrue(self.service.flush())
        self.assertTrue(self.service.flush())
        self.assertEqual(self.calls.mock_calls, [
            call.get_state(),
            call.document(self.model, self.schematic),
            call.history(HISTORY, SHA),
            call.saved(DOCUMENT),
        ])

    def test_transaction_wraps_both_writes_and_commits_before_success_signal(self):
        events = []

        @contextmanager
        def transaction():
            events.append("begin")
            yield
            events.append("commit")

        self.manager.transaction.side_effect = transaction
        self.manager.save.side_effect = lambda *_: (
            events.append("document") or file_manager.SaveResult(DOCUMENT, SHA)
        )
        self.stack.save.side_effect = lambda *_: events.append("history")
        self.service.saved.connect(lambda _path: events.append("saved"))
        self.service.mark_dirty()

        self.assertTrue(self.service.flush())
        self.assertEqual(events, ["begin", "document", "history", "commit", "saved"])

    def test_real_autosave_restores_all_files_if_history_write_fails(self):
        folder = Path(self.enterContext(TemporaryDirectory())).resolve()
        path = folder / "demo.net"
        schematic = folder / "demo.sch.json"
        history = folder / "demo.history.json"
        serializer = Mock()
        serializer.serialize.return_value = "block main\nend\n"
        manager = file_manager.FileManager(Mock(), serializer)
        self.addCleanup(manager.close)
        initial = manager.save(self.model, self.schematic, str(path))
        stack = UndoStack(SimpleNamespace())
        stack.save(str(history), initial.sha256)
        paths = (path, schematic, history)
        old = tuple(item.read_bytes() for item in paths)
        next_schematic = {"version": 1, "blocks": {}}
        service = autosave.Autosave(manager, lambda: autosave.DocumentState(
            self.model, next_schematic, stack,
        ))
        self.addCleanup(service.disable)
        saved = []
        service.saved.connect(saved.append)
        serializer.serialize.return_value = "block main\nend\n\n"
        service.mark_dirty()

        with patch.object(stack, "save", side_effect=OSError("disk full")):
            self.assertFalse(service.flush())
        self.assertEqual(tuple(item.read_bytes() for item in paths), old)
        self.assertEqual(saved, [])

        self.assertTrue(service.flush())
        self.assertEqual(saved, [str(path)])
        updated = json.loads(history.read_bytes())
        self.assertEqual(updated["netlist_sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(updated["schematic_sha256"],
                         hashlib.sha256(schematic.read_bytes()).hexdigest())
        self.assertFalse((folder / "demo.net.transaction.json").exists())

    def test_document_write_error_signals_failure_and_retries(self):
        self.service.enable()
        self.manager.save.side_effect = OSError("нет места")
        self.service.mark_dirty()
        self.assertFalse(self.service.flush())
        self.assertTrue(self.service.enabled)
        self.assertEqual(self.calls.mock_calls, [
            call.get_state(), call.document(self.model, self.schematic),
            call.failed("нет места"),
        ])
        self.manager.save.side_effect = None
        self.assertTrue(self.service.flush())
        self.assertEqual(self.calls.mock_calls[-2:],
                         [call.history(HISTORY, SHA), call.saved(DOCUMENT)])

    def test_history_write_error_prevents_success_and_retries_both_writes(self):
        self.stack.save.side_effect = OSError("история недоступна")
        self.service.mark_dirty()
        self.assertFalse(self.service.flush())
        self.assertEqual(self.calls.mock_calls, [
            call.get_state(),
            call.document(self.model, self.schematic),
            call.history(HISTORY, SHA),
            call.failed("история недоступна"),
        ])
        self.calls.reset_mock()
        self.stack.save.side_effect = None
        self.assertTrue(self.service.flush())
        self.assertEqual(self.calls.mock_calls, [
            call.get_state(),
            call.document(self.model, self.schematic),
            call.history(HISTORY, SHA),
            call.saved(DOCUMENT),
        ])

    def test_dirty_flush_if_path_disappears_reports_failure_and_can_retry(self):
        self.service.mark_dirty()
        self.manager.current_path = None
        with self.assertLogs(autosave.__name__, level="ERROR"):
            self.assertFalse(self.service.flush())
        self.get_state.assert_not_called()
        self.manager.save.assert_not_called()
        self.assertEqual(len(self.calls.mock_calls), 1)
        self.assertEqual(self.calls.mock_calls[0][0], "failed")
        self.assertTrue(self.calls.mock_calls[0].args[0])
        self.manager.current_path = DOCUMENT
        self.assertTrue(self.service.flush())

    def test_unexpected_failure_disables_timer_and_preserves_pending_change(self):
        self.service.enable()
        self.service.mark_dirty()
        self.manager.save.side_effect = RuntimeError("ошибка кода")
        with self.assertLogs(autosave.__name__, level="ERROR"):
            self.assertFalse(self.service.flush())
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.calls.mock_calls[-1], call.failed("ошибка кода"))
        self.manager.save.side_effect = None
        self.assertTrue(self.service.flush())
        self.assertEqual(self.calls.mock_calls[-1], call.saved(DOCUMENT))

    def test_new_change_during_flush_requires_another_save(self):
        def save_new_revision(*_args):
            self.service.mark_dirty()
            return file_manager.SaveResult(DOCUMENT, SHA)

        self.manager.save.side_effect = save_new_revision
        self.service.mark_dirty()
        self.assertTrue(self.service.flush())
        self.manager.save.side_effect = None
        self.assertTrue(self.service.flush())
        self.assertTrue(self.service.flush())
        self.assertEqual(self.stack.save.call_args_list,
                         [call(HISTORY, SHA), call(HISTORY, SHA)])

    def test_saved_callback_cannot_trigger_recursive_flush(self):
        nested = []

        def on_saved(_path):
            # Слот меняет документ только при первом сигнале.
            if self.manager.save.call_count == 1:
                self.service.mark_dirty()
                nested.append(self.service.flush())

        self.service.saved.connect(on_saved)
        self.service.mark_dirty()
        self.assertTrue(self.service.flush())
        self.assertEqual(nested, [False])
        self.manager.save.assert_called_once_with(self.model, self.schematic)
        self.assertTrue(self.service.flush())
        self.assertEqual(self.manager.save.call_count, 2)

    def test_failed_callback_cannot_trigger_recursive_retry(self):
        nested = []

        def on_failed(_message):
            if self.manager.save.call_count == 1:
                nested.append(self.service.flush())

        self.service.failed.connect(on_failed)
        self.manager.save.side_effect = OSError("нет места")
        self.service.mark_dirty()
        self.assertFalse(self.service.flush())
        self.assertEqual(nested, [False])
        self.manager.save.assert_called_once_with(self.model, self.schematic)

    def test_timer_belongs_to_service_and_service_to_parent(self):
        parent = QObject()
        service = autosave.Autosave(self.manager, self.get_state, parent=parent)
        self.assertIs(service.parent(), parent)
        self.assertIs(service._timer.parent(), service)
        service.enable()
        service.disable()
        self.assertFalse(service.enabled)

    @unittest.skipUnless(HAS_QT, "Для уничтожения QObject нужен PySide6")
    def test_destroying_qt_parent_destroys_service_and_active_timer(self):
        parent = QObject()
        service = autosave.Autosave(self.manager, self.get_state, parent=parent)
        timer = service._timer
        service.enable()
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(service))
        self.assertFalse(isValid(timer))
