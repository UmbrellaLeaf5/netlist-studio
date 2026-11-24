"""Реестр команд, история и Undo/Redo по §§ 1.7 и 3.2."""

import gc
import hashlib
import json
import unittest
import weakref
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.editor import undo_stack as history
from src.editor.file_manager import FileAccessError


@dataclass
class Append(history.CanvasCommand):
    """Одна обратимая операция тестового Canvas со списком элементов."""

    type_name = "test_append"
    value: str

    @property
    def text(self):
        return f"Добавить {self.value}"

    def redo(self, canvas):
        canvas.items.append(self.value)

    def undo(self, canvas):
        if not canvas.items or canvas.items[-1] != self.value:
            raise ValueError("Нельзя отменить добавление не последнего элемента")
        canvas.items.pop()

    def to_dict(self):
        return {"type": self.type_name, "value": self.value}

    @classmethod
    def from_dict(cls, data):
        return cls(data["value"])


@dataclass
class Move(history.CanvasCommand):
    """Два движения одной точки объединяются в одно."""

    type_name = "test_move"
    value: int
    old: int = 0

    @property
    def text(self):
        return "Переместить"

    def redo(self, canvas):
        canvas.position = self.value

    def undo(self, canvas):
        canvas.position = self.old

    def to_dict(self):
        return {"type": self.type_name, "old": self.old, "value": self.value}

    @classmethod
    def from_dict(cls, data):
        return cls(data["value"], old=data["old"])

    def merge_with(self, other):
        self.value = other.value
        return True


class CommandRegistryTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(history._REGISTRY))

    def test_abstract_command_cannot_be_instantiated(self):
        self.assertEqual(history.CanvasCommand.__abstractmethods__,
                         {"redo", "undo", "to_dict", "from_dict"})
        with self.assertRaises(TypeError):
            history.CanvasCommand()

    def test_command_uses_default_no_merge_and_can_be_released(self):
        command = Append("A")
        self.assertFalse(command.merge_with(Append("B")))
        ref = weakref.ref(command)
        del command
        gc.collect()
        self.assertIsNone(ref())

    def test_register_and_restore_a_command(self):
        self.assertIs(history.register_command(Append), Append)
        self.assertIs(history.register_command(Append), Append)
        loaded = history.command_from_dict({"type": "test_append", "value": "A"})
        self.assertIsInstance(loaded, Append)
        self.assertEqual(loaded.text, "Добавить A")
        self.assertEqual(loaded.to_dict(), {"type": "test_append", "value": "A"})
        canvas = SimpleNamespace(items=[])
        loaded.redo(canvas)
        self.assertEqual(canvas.items, ["A"])
        loaded.undo(canvas)
        self.assertEqual(canvas.items, [])

    def test_register_rejects_invalid_class_and_name_collision(self):
        class Duplicate(Append):
            pass

        class EmptyName(Append):
            type_name = ""

        history.register_command(Append)
        for invalid in (str, EmptyName, Duplicate):
            with self.subTest(command=invalid):
                with self.assertRaises(ValueError):
                    history.register_command(invalid)
        self.assertIsInstance(history.command_from_dict(
            {"type": "test_append", "value": "A"}), Append)

    def test_restore_rejects_missing_unknown_and_invalid_command_types(self):
        for invalid in ([], {}, {"type": "missing"}, {"type": []}):
            with self.subTest(data=invalid):
                with self.assertRaises(ValueError):
                    history.command_from_dict(invalid)

    def test_restore_rejects_if_factory_returns_a_different_type(self):
        class BadFactory(Append):
            type_name = "test_bad_factory"

            @classmethod
            def from_dict(cls, data):
                return Append("wrong type")

        history.register_command(BadFactory)
        with self.assertRaises(ValueError):
            history.command_from_dict({"type": "test_bad_factory"})


class UndoStackTest(unittest.TestCase):
    def setUp(self):
        self.canvas = SimpleNamespace(items=[], position=0)
        self.stack = history.UndoStack(self.canvas)
        self.enterContext(patch.dict(history._REGISTRY))
        history.register_command(Append)
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder, "demo.history.json")
        self.sha = "a" * 64

    def test_initial_stack_is_empty_and_head_properties_are_read_only(self):
        self.assertEqual((self.stack.index, self.stack.count), (0, 0))
        self.assertFalse(self.stack.can_undo())
        self.assertFalse(self.stack.can_redo())
        self.assertIsNone(self.stack.undo_text())
        self.assertIsNone(self.stack.redo_text())
        self.assertFalse(self.stack.undo())
        self.assertFalse(self.stack.redo())
        with self.assertRaises(AttributeError):
            self.stack.index = 1
        with self.assertRaises(AttributeError):
            self.stack.count = 1
        self.assertEqual(self.canvas.items, [])

    def test_destroying_stack_releases_history_without_mutating_canvas(self):
        stack = history.UndoStack(self.canvas)
        command = Append("A")
        stack.push(command)
        stack_ref, command_ref = weakref.ref(stack), weakref.ref(command)
        del stack, command
        gc.collect()
        self.assertIsNone(stack_ref())
        self.assertIsNone(command_ref())
        self.assertEqual(self.canvas.items, ["A"])

    def test_push_undo_redo_change_canvas_index_and_labels(self):
        self.stack.push(Append("A"))
        self.stack.push(Append("B"))
        self.assertEqual(self.canvas.items, ["A", "B"])
        self.assertEqual((self.stack.index, self.stack.count), (2, 2))
        self.assertTrue(self.stack.can_undo())
        self.assertFalse(self.stack.can_redo())
        self.assertEqual(self.stack.undo_text(), "Добавить B")
        self.assertTrue(self.stack.undo())
        self.assertEqual(self.canvas.items, ["A"])
        self.assertEqual((self.stack.index, self.stack.count), (1, 2))
        self.assertTrue(self.stack.can_redo())
        self.assertEqual(self.stack.redo_text(), "Добавить B")
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["A", "B"])
        self.assertEqual(self.stack.index, 2)
        self.assertIsNone(self.stack.redo_text())

    def test_push_after_undo_replaces_previous_redo_tail(self):
        self.stack.push(Append("A"))
        self.stack.push(Append("B"))
        self.assertTrue(self.stack.undo())
        self.stack.push(Append("C"))
        self.assertEqual(self.canvas.items, ["A", "C"])
        self.assertEqual((self.stack.index, self.stack.count), (2, 2))
        self.assertFalse(self.stack.can_redo())

    def test_push_merges_successive_moves_into_one_step(self):
        self.stack.push(Move(3, old=0))
        self.stack.push(Move(7, old=3))
        self.assertEqual((self.canvas.position, self.stack.index,
                          self.stack.count), (7, 1, 1))
        self.assertTrue(self.stack.undo())
        self.assertEqual(self.canvas.position, 0)
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.position, 7)

    def test_loaded_merged_move_undoes_to_the_initial_position(self):
        history.register_command(Move)
        self.stack.push(Move(3, old=0))
        self.stack.push(Move(7, old=3))
        self.stack.save(str(self.path), self.sha)
        restored = history.UndoStack(self.canvas)
        self.assertTrue(restored.load(str(self.path), self.sha))
        self.assertEqual((restored.count, self.canvas.position), (1, 7))
        self.assertTrue(restored.undo())
        self.assertEqual(self.canvas.position, 0)
        self.assertTrue(restored.redo())
        self.assertEqual(self.canvas.position, 7)

    def test_failed_push_keeps_redo_tail(self):
        self.stack.push(Append("A"))
        self.assertTrue(self.stack.undo())
        failing = Mock(spec_set=history.CanvasCommand)
        failing.redo.side_effect = RuntimeError("failed redo")
        with self.assertRaisesRegex(RuntimeError, "failed redo"):
            self.stack.push(failing)
        self.assertEqual((self.stack.index, self.stack.count), (0, 1))
        self.assertEqual(self.stack.redo_text(), "Добавить A")
        self.assertEqual(self.canvas.items, [])

    def test_command_errors_during_undo_and_redo_preserve_index(self):
        command = Append("A")
        self.stack.push(command)
        with patch.object(command, "undo", side_effect=RuntimeError("undo")):
            with self.assertRaisesRegex(RuntimeError, "undo"):
                self.stack.undo()
        self.assertEqual((self.stack.index, self.canvas.items), (1, ["A"]))
        self.assertTrue(self.stack.undo())
        with patch.object(command, "redo", side_effect=RuntimeError("redo")):
            with self.assertRaisesRegex(RuntimeError, "redo"):
                self.stack.redo()
        self.assertEqual((self.stack.index, self.canvas.items), (0, []))

    def test_nested_macros_undo_and_redo_as_one_step(self):
        self.stack.begin_macro("Группа")
        self.stack.push(Append("A"))
        self.stack.begin_macro("Вложенная группа")
        self.stack.push(Append("B"))
        self.stack.end_macro()
        self.stack.push(Append("C"))
        self.assertFalse(self.stack.can_undo())
        self.assertFalse(self.stack.can_redo())
        self.assertIsNone(self.stack.undo_text())
        self.assertFalse(self.stack.undo())
        self.assertFalse(self.stack.redo())
        self.stack.end_macro()
        self.assertEqual((self.stack.index, self.stack.count), (1, 1))
        self.assertEqual(self.stack.undo_text(), "Группа")
        self.assertEqual(self.canvas.items, ["A", "B", "C"])
        self.assertTrue(self.stack.undo())
        self.assertEqual(self.canvas.items, [])
        self.assertEqual(self.stack.redo_text(), "Группа")
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["A", "B", "C"])

    def test_empty_macro_preserves_existing_redo_tail(self):
        self.stack.push(Append("A"))
        self.stack.undo()
        self.stack.begin_macro("Пустая группа")
        self.stack.end_macro()
        self.assertEqual((self.stack.index, self.stack.count), (0, 1))
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["A"])

    def test_abort_macro_reverts_group_and_keeps_existing_redo(self):
        self.stack.push(Append("old"))
        self.stack.undo()
        self.stack.begin_macro("Временная группа")
        self.stack.push(Append("A"))
        self.stack.push(Append("B"))
        self.stack.abort_macro()
        self.assertEqual(self.canvas.items, [])
        self.assertEqual((self.stack.index, self.stack.count), (0, 1))
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["old"])
        with self.assertRaises(RuntimeError):
            self.stack.abort_macro()

    def test_macro_context_commits_on_success_and_rolls_back_on_exception(self):
        with self.stack.macro("Сохранённая группа"):
            self.stack.push(Append("A"))
            self.stack.push(Append("B"))
        self.assertEqual(self.stack.undo_text(), "Сохранённая группа")
        with self.assertRaisesRegex(ValueError, "cancel"):
            with self.stack.macro("Отменённая группа"):
                self.stack.push(Append("C"))
                raise ValueError("cancel")
        self.assertEqual(self.canvas.items, ["A", "B"])
        self.assertEqual((self.stack.index, self.stack.count), (1, 1))

    def test_failed_push_aborts_open_group_and_propagates_the_error(self):
        failing = Mock(spec_set=history.CanvasCommand)
        failing.redo.side_effect = RuntimeError("command failed")
        self.stack.begin_macro("Группа")
        self.stack.push(Append("A"))
        with self.assertRaisesRegex(RuntimeError, "command failed"):
            self.stack.push(failing)
        self.assertEqual(self.canvas.items, [])
        self.assertEqual((self.stack.index, self.stack.count), (0, 0))
        with self.assertRaises(RuntimeError):
            self.stack.end_macro()

    def test_failed_macro_undo_reapplies_commands_already_undone(self):
        first = Append("A")
        self.stack.begin_macro("Группа")
        self.stack.push(first)
        self.stack.push(Append("B"))
        self.stack.end_macro()
        with patch.object(first, "undo", side_effect=RuntimeError("undo A")):
            with self.assertRaisesRegex(RuntimeError, "undo A"):
                self.stack.undo()
        self.assertEqual((self.stack.index, self.canvas.items), (1, ["A", "B"]))

    def test_failed_macro_redo_undoes_commands_already_redone(self):
        second = Append("B")
        self.stack.begin_macro("Группа")
        self.stack.push(Append("A"))
        self.stack.push(second)
        self.stack.end_macro()
        self.assertTrue(self.stack.undo())
        with patch.object(second, "redo", side_effect=RuntimeError("redo B")):
            with self.assertRaisesRegex(RuntimeError, "redo B"):
                self.stack.redo()
        self.assertEqual((self.stack.index, self.canvas.items), (0, []))

    def test_macro_must_be_open_to_end_and_closed_to_clear_save_or_load(self):
        with self.assertRaises(RuntimeError):
            self.stack.end_macro()
        self.stack.begin_macro("Группа")
        for operation in (
            self.stack.clear,
            lambda: self.stack.save(str(self.path), self.sha),
            lambda: self.stack.load(str(self.path), self.sha),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError):
                    operation()
        self.assertFalse(self.path.exists())

    def test_clear_forgets_history_without_changing_canvas(self):
        self.stack.push(Append("A"))
        self.stack.clear()
        self.assertEqual((self.stack.index, self.stack.count), (0, 0))
        self.assertFalse(self.stack.can_undo())
        self.assertFalse(self.stack.can_redo())
        self.assertEqual(self.canvas.items, ["A"])

    def test_listener_observes_the_new_index_after_change(self):
        events = []

        def listener():
            events.append((self.stack.index, self.stack.count))

        self.stack.add_listener(listener)
        self.stack.add_listener(listener)
        self.assertEqual(events, [])
        self.stack.push(Append("A"))
        self.stack.undo()
        self.stack.redo()
        self.stack.clear()
        self.assertEqual(events, [(1, 1), (0, 1), (1, 1), (0, 0)])
        self.assertFalse(self.stack.undo())
        self.assertEqual(len(events), 4)

    def test_one_bad_listener_does_not_stop_later_listeners(self):
        bad_listener = Mock(side_effect=RuntimeError("broken listener"))
        next_listener = Mock()
        self.stack.add_listener(bad_listener)
        self.stack.add_listener(next_listener)
        with self.assertLogs(history.__name__, level="ERROR"):
            self.stack.push(Append("A"))
        next_listener.assert_called_once_with()
        self.assertEqual((self.stack.index, self.canvas.items), (1, ["A"]))

    def test_save_writes_version_hash_index_and_entire_command_list(self):
        self.stack.push(Append("A"))
        self.stack.push(Append("B"))
        self.stack.undo()
        self.stack.save(str(self.path), self.sha)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {
            "version": 1,
            "schematic_sha256": None,
            "netlist_sha256": self.sha,
            "index": 1,
            "commands": [
                {"type": "test_append", "value": "A"},
                {"type": "test_append", "value": "B"},
            ],
        })

    def test_history_matches_schematic_bytes_and_rejects_change_or_removal(self):
        schematic = self.path.with_name("demo.sch.json")
        original = b'{"version":1,"blocks":{}}'
        schematic.write_bytes(original)
        self.stack.push(Append("A"))
        self.stack.save(str(self.path), self.sha)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["schematic_sha256"], hashlib.sha256(original).hexdigest())
        self.assertTrue(self.stack.load(str(self.path), self.sha))
        schematic.write_bytes(b"changed")
        self.assertFalse(self.stack.load(str(self.path), self.sha))
        schematic.unlink()
        self.assertFalse(self.stack.load(str(self.path), self.sha))
        self.assertEqual((self.stack.index, self.stack.count), (0, 0))
        self.assertEqual(self.canvas.items, ["A"])

    def test_history_saved_without_schematic_rejects_new_schematic_file(self):
        self.stack.push(Append("A"))
        self.stack.save(str(self.path), self.sha)
        self.path.with_name("demo.sch.json").write_bytes(b"{}")
        self.assertFalse(self.stack.load(str(self.path), self.sha))
        self.assertEqual(self.canvas.items, ["A"])

    def test_loads_documented_version_one_history_without_schematic_hash(self):
        self.path.with_name("demo.sch.json").write_bytes(b"{}")
        self.path.write_text(json.dumps({
            "version": 1,
            "netlist_sha256": self.sha,
            "index": 0,
            "commands": [{"type": "test_append", "value": "A"}],
        }), encoding="utf-8")
        self.assertTrue(self.stack.load(str(self.path), self.sha))
        self.assertEqual((self.stack.index, self.stack.count), (0, 1))
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["A"])

    def test_save_reports_write_error_and_preserves_old_history_file(self):
        self.path.write_text("old history", encoding="utf-8")
        self.stack.push(Append("A"))
        with patch("src.editor.file_manager.os.replace",
                   side_effect=PermissionError("denied")):
            with self.assertRaises(FileAccessError):
                self.stack.save(str(self.path), self.sha)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old history")
        self.assertEqual((self.stack.index, self.stack.count), (1, 1))

    def test_load_restores_index_and_redo_without_replaying_canvas(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "netlist_sha256": self.sha,
            "index": 1,
            "commands": [
                {"type": "test_append", "value": "A"},
                {"type": "test_append", "value": "B"},
            ],
        }), encoding="utf-8")
        self.canvas.items[:] = ["A"]  # .net уже соответствует index=1
        listener = Mock()
        self.stack.add_listener(listener)

        self.assertTrue(self.stack.load(str(self.path), self.sha))
        listener.assert_called_once_with()
        self.assertEqual(self.canvas.items, ["A"])
        self.assertEqual((self.stack.index, self.stack.count), (1, 2))
        self.assertTrue(self.stack.redo())
        self.assertEqual(self.canvas.items, ["A", "B"])

    def test_saved_nested_macro_restores_as_single_command(self):
        self.stack.begin_macro("Группа")
        self.stack.push(Append("A"))
        self.stack.begin_macro("Вложенная")
        self.stack.push(Append("B"))
        self.stack.end_macro()
        self.stack.end_macro()
        self.stack.save(str(self.path), self.sha)
        restored = history.UndoStack(self.canvas)

        self.assertTrue(restored.load(str(self.path), self.sha))
        self.assertEqual((restored.index, restored.count), (1, 1))
        self.assertEqual(restored.undo_text(), "Группа")
        self.assertEqual(self.canvas.items, ["A", "B"])
        self.assertTrue(restored.undo())
        self.assertEqual(self.canvas.items, [])

    def test_load_rejects_invalid_history_and_clears_stack_only(self):
        valid = {"version": 1, "netlist_sha256": self.sha, "index": 0,
                 "commands": []}
        cases = [
            ("missing", None),
            ("malformed JSON", "{"),
            ("wrong hash", json.dumps(dict(valid, netlist_sha256="b" * 64))),
            ("unsupported version", json.dumps(dict(valid, version=2))),
            ("negative index", json.dumps(dict(valid, index=-1))),
            ("index beyond end", json.dumps(dict(valid, index=1))),
            ("invalid commands", json.dumps(dict(valid, commands={}))),
            ("unknown command", json.dumps(dict(valid, commands=[
                {"type": "test_append", "value": "A"}, {"type": "unknown"},
            ]))),
        ]
        for name, contents in cases:
            with self.subTest(case=name):
                self.canvas.items.clear()
                self.stack.push(Append("current"))
                if contents is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(contents, encoding="utf-8")
                self.assertFalse(self.stack.load(str(self.path), self.sha))
                self.assertEqual((self.stack.index, self.stack.count), (0, 0))
                self.assertEqual(self.canvas.items, ["current"])
