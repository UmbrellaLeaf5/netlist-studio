"""FileManager, форматы файлов и блокировки по §§ 1.6–1.8 и 3.1."""

import base64
import errno
import gc
import hashlib
import json
import os
import socket
import unittest
import weakref
from copy import copy, deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from netlist.core.model import Diagnostic, Netlist, Severity, SourceLoc
from netlist.core.parser import ParseResult, RawNetlist
from netlist.editor import file_manager as fm


NETLIST = "block main\nend\n"
SCHEMATIC = {"version": 1, "blocks": {"main": {"frame": [0, 0, 50, 30]}}}
LOCK_TIME = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)


def parse_result(path, *diagnostics):
    return ParseResult(RawNetlist(path), list(diagnostics))


class FileFunctionsTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder, "demo.net")

    def test_companion_paths_replace_only_the_netlist_suffix(self):
        for name, expected in (
            ("demo.net", ("demo.sch.json", "demo.history.json", "demo.net.lock")),
            ("v2.demo.net", ("v2.demo.sch.json", "v2.demo.history.json",
                             "v2.demo.net.lock")),
        ):
            with self.subTest(name=name):
                path = str(self.path.with_name(name))
                for function, filename in zip(
                    (fm.schematic_path, fm.history_path, fm.lock_path), expected
                ):
                    self.assertEqual(function(path), str(self.path.with_name(filename)))

    def test_read_text_decodes_utf8_replaces_invalid_bytes_and_ignores_lock(self):
        self.path.write_bytes("схема\n".encode() + b"\xff\n")
        Path(str(self.path) + ".lock").write_text("occupied", encoding="ascii")
        self.assertEqual(fm.read_text(str(self.path)), "схема\n\ufffd\n")

    def test_read_text_reports_access_errors(self):
        with self.assertRaises(fm.FileAccessError):
            fm.read_text(str(self.path))
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(fm.FileAccessError):
                fm.read_text(str(self.path))

    def test_file_sha256_hashes_original_bytes(self):
        self.path.write_bytes(b"abc")
        expected = (
            "ba7816bf8f01cfea414140de5dae2223b"
            "00361a396177a9cb410ff61f20015ad"
        )
        self.assertEqual(fm.file_sha256(str(self.path)), expected)
        self.path.unlink()
        with self.assertRaises(fm.FileAccessError):
            fm.file_sha256(str(self.path))

    def test_write_text_atomic_creates_and_replaces_utf8_file(self):
        for text in ("abc", "новый файл\n"):
            with self.subTest(text=text):
                expected = text.encode("utf-8")
                digest = fm.write_text_atomic(str(self.path), text)
                self.assertEqual(self.path.read_bytes(), expected)
                self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_write_text_atomic_preserves_previous_bytes_when_replace_fails(self):
        self.path.write_bytes(b"original")
        with patch.object(fm.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaises(fm.FileAccessError):
                fm.write_text_atomic(str(self.path), "replacement")
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class FileManagerDataTest(unittest.TestCase):
    def test_lock_and_save_info_construct_copy_and_release(self):
        for value in (fm.LockInfo(41, "pc", LOCK_TIME, True),
                      fm.SaveResult("demo.net", "a" * 64)):
            with self.subTest(type=type(value).__name__):
                clone = copy(value)
                self.assertIsNot(clone, value)
                self.assertEqual(clone, value)
                field = "stale" if isinstance(value, fm.LockInfo) else "sha256"
                with self.assertRaises(FrozenInstanceError):
                    setattr(clone, field, None)
                self.assertEqual(deepcopy(value), value)
                reference = weakref.ref(clone)
                del clone
                gc.collect()
                self.assertIsNone(reference())

    def test_open_result_copies_data_and_exposes_parser_status(self):
        warning = Diagnostic("W701", Severity.WARNING, "editor", "bad JSON",
                             SourceLoc("demo.sch.json", None))
        error = Diagnostic("E101", Severity.ERROR, "syntax", "bad line",
                           SourceLoc("demo.net", 1))
        original = fm.OpenResult("demo.net", parse_result("demo.net", warning),
                                 {"version": 1, "blocks": {}})
        clone = deepcopy(original)
        self.assertIsNot(clone, original)
        self.assertEqual(clone, original)
        self.assertTrue(original.ok)
        clone.parse.diagnostics.append(error)
        clone.schematic["blocks"]["main"] = {}
        self.assertFalse(clone.ok)
        self.assertTrue(original.ok)
        self.assertEqual(original.schematic["blocks"], {})
        reference = weakref.ref(clone)
        del clone
        gc.collect()
        self.assertIsNone(reference())

    def test_error_types_expose_path_and_owner_and_can_be_released(self):
        error = fm.FileAccessError("missing")
        self.assertIsInstance(error, OSError)
        self.assertEqual(str(error), "missing")
        owner = fm.LockInfo(41, "pc", LOCK_TIME, False)
        locked = fm.FileLockedError("demo.net", owner)
        self.assertIsInstance(locked, fm.FileAccessError)
        self.assertEqual((locked.filename, locked.info, locked.errno),
                         ("demo.net", owner, errno.EBUSY))
        error_ref, locked_ref = weakref.ref(error), weakref.ref(locked)
        del error, locked
        gc.collect()
        self.assertIsNone(error_ref())
        self.assertIsNone(locked_ref())


class FileManagerTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.path = self.folder / "demo.net"
        self.parser = Mock(spec_set=["parse"])
        self.parser.parse.side_effect = lambda text, filename: parse_result(filename)
        self.serializer = Mock(spec_set=["serialize"])
        self.serializer.serialize.return_value = NETLIST
        self.manager = fm.FileManager(self.parser, self.serializer)
        self.addCleanup(self.manager.close)
        # Блокировки настоящие. Только состояние PID задаём детерминированно.
        self.enterContext(patch.object(
            fm, "_process_alive", side_effect=lambda pid: pid == os.getpid(),
        ))

    def marker(self, path=None):
        return Path(str(path or self.path) + ".lock")

    def write_marker(self, pid, host):
        self.marker().write_text(
            f"pid={pid} host={host} time={LOCK_TIME.isoformat()}\n",
            encoding="utf-8",
        )

    def test_new_manager_has_no_path_and_releases_open_file_on_close(self):
        self.assertIsNone(self.manager.current_path)
        self.assertFalse(self.manager.is_open)
        with self.assertRaises(AttributeError):
            self.manager.current_path = str(self.path)
        self.path.write_text(NETLIST, encoding="ascii")
        self.manager.open(str(self.path))
        self.assertTrue(self.manager.is_open)
        self.assertTrue(self.marker().is_file())
        self.manager.close()
        self.manager.close()
        self.assertIsNone(self.manager.current_path)
        self.assertFalse(self.manager.is_open)
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.path.read_bytes(), NETLIST.encode())

    def test_closed_manager_can_be_destroyed_and_document_reopened(self):
        self.path.write_text(NETLIST, encoding="ascii")
        manager = fm.FileManager(self.parser, self.serializer)
        try:
            manager.open(str(self.path))
        finally:
            manager.close()
        ref = weakref.ref(manager)
        del manager
        gc.collect()
        self.assertIsNone(ref())
        self.assertTrue(self.manager.open(str(self.path)).ok)

    def test_new_releases_previous_document_without_touching_its_bytes(self):
        self.path.write_text(NETLIST, encoding="ascii")
        self.manager.open(str(self.path))
        self.manager.new()
        self.assertFalse(self.manager.is_open)
        self.assertIsNone(self.manager.current_path)
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.path.read_text(encoding="ascii"), NETLIST)

    def test_open_reads_schematic_and_can_reopen_its_own_document(self):
        self.path.write_text(NETLIST, encoding="ascii")
        companion = self.folder / "demo.sch.json"
        companion.write_text(json.dumps(SCHEMATIC), encoding="utf-8")
        for _ in range(2):
            result = self.manager.open(str(self.path))
            self.assertTrue(result.ok)
            self.assertEqual((result.path, result.schematic),
                             (str(self.path), SCHEMATIC))
        self.parser.parse.assert_called_with(NETLIST, filename=str(self.path))
        self.assertEqual(self.manager.current_path, str(self.path))

    def test_open_without_schematic_is_successful(self):
        self.path.write_text(NETLIST, encoding="ascii")
        result = self.manager.open(str(self.path))
        self.assertTrue(result.ok)
        self.assertIsNone(result.schematic)
        self.assertEqual(result.parse.diagnostics, [])

    def test_damaged_schematic_yields_w701_and_keeps_document_open(self):
        self.path.write_text(NETLIST, encoding="ascii")
        companion = self.folder / "demo.sch.json"
        for data in ("{", '{"version":2,"blocks":{}}'):
            with self.subTest(data=data):
                companion.write_text(data, encoding="utf-8")
                result = self.manager.open(str(self.path))
                self.assertTrue(result.ok)
                self.assertIsNone(result.schematic)
                self.assertEqual([(d.code, d.severity, d.loc.file)
                                  for d in result.parse.diagnostics],
                                 [("W701", Severity.WARNING, str(companion))])
        self.assertTrue(self.manager.is_open)

    def test_failed_open_preserves_old_document_and_releases_new_marker(self):
        self.path.write_text(NETLIST, encoding="ascii")
        self.manager.open(str(self.path))
        other = self.folder / "bad.net"
        with self.assertRaises(fm.FileAccessError):
            self.manager.open(str(other))
        self.assertFalse(self.marker(other).exists())
        other.write_text("bad\n", encoding="ascii")
        diagnostic = Diagnostic("E101", Severity.ERROR, "syntax", "bad line",
                                SourceLoc(str(other), 1))
        self.parser.parse.side_effect = None
        self.parser.parse.return_value = parse_result(str(other), diagnostic)
        result = self.manager.open(str(other))
        self.assertFalse(result.ok)
        self.assertEqual(result.parse.diagnostics, [diagnostic])
        self.assertEqual(self.manager.current_path, str(self.path))
        self.assertFalse(self.marker(other).exists())
        self.assertTrue(self.marker().exists())

    def test_opening_another_valid_document_releases_first_marker(self):
        self.path.write_text(NETLIST, encoding="ascii")
        other = self.folder / "other.net"
        other.write_text(NETLIST, encoding="ascii")
        self.manager.open(str(self.path))
        self.manager.open(str(other))
        self.assertEqual(self.manager.current_path, str(other))
        self.assertFalse(self.marker().exists())
        self.assertTrue(self.marker(other).exists())

    def test_check_lock_handles_missing_live_stale_remote_and_corrupt_markers(self):
        self.assertIsNone(self.manager.check_lock(str(self.path)))
        hostname = socket.gethostname()
        for pid, host, stale in ((os.getpid(), hostname, False),
                                 (99999, hostname, True),
                                 (99999, hostname + "-remote", False)):
            with self.subTest(pid=pid, host=host):
                self.write_marker(pid, host)
                self.assertEqual(self.manager.check_lock(str(self.path)),
                                 fm.LockInfo(pid, host, LOCK_TIME, stale))
        for data in (b"not a marker", b"\xff"):
            with self.subTest(data=data):
                self.marker().write_bytes(data)
                self.assertFalse(self.manager.check_lock(str(self.path)).stale)
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(fm.FileAccessError):
                self.manager.check_lock(str(self.path))

    def test_open_can_force_only_a_stale_local_lock(self):
        self.path.write_text(NETLIST, encoding="ascii")
        hostname = socket.gethostname()
        self.write_marker(os.getpid(), hostname)
        with self.assertRaises(fm.FileLockedError) as live:
            self.manager.open(str(self.path), force_unlock=True)
        self.assertEqual(live.exception.info.pid, os.getpid())
        self.write_marker(99999, hostname + "-remote")
        with self.assertRaises(fm.FileLockedError):
            self.manager.open(str(self.path), force_unlock=True)
        self.write_marker(99999, hostname)
        with self.assertRaises(fm.FileLockedError) as stale:
            self.manager.open(str(self.path))
        self.assertTrue(stale.exception.info.stale)
        self.assertTrue(self.manager.open(str(self.path), force_unlock=True).ok)
        self.assertEqual(self.manager.check_lock(str(self.path)).pid, os.getpid())

    def test_save_without_path_requires_first_save_as(self):
        with self.assertRaises(ValueError):
            self.manager.save(Netlist(), SCHEMATIC)
        self.assertIsNone(self.manager.current_path)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_save_as_does_not_replace_another_managers_document(self):
        old = self.folder / "other.net"
        old.write_text("occupied\n", encoding="ascii")
        other_manager = fm.FileManager(self.parser, self.serializer)
        self.addCleanup(other_manager.close)
        other_manager.open(str(old))
        self.manager.save(Netlist(), SCHEMATIC, str(self.path))
        with self.assertRaises(fm.FileLockedError):
            self.manager.save(Netlist(), SCHEMATIC, str(old))
        self.assertEqual(self.manager.current_path, str(self.path))
        self.assertEqual(other_manager.current_path, str(old))
        self.assertEqual(old.read_bytes(), b"occupied\n")
        self.assertFalse(old.with_suffix(".sch.json").exists())

    def test_save_writes_two_files_updates_hash_and_supports_save_as(self):
        model = Netlist()
        model.add_block("main")
        first = self.manager.save(model, SCHEMATIC, str(self.path))
        self.assertEqual(first.path, str(self.path))
        self.assertEqual(first.sha256, hashlib.sha256(NETLIST.encode()).hexdigest())
        self.assertTrue(self.manager.is_open)
        self.assertEqual(self.manager.current_path, str(self.path))
        self.serializer.serialize.assert_called_once_with(model)

        self.serializer.serialize.return_value = NETLIST + "\n"
        self.assertNotEqual(self.manager.save(model, SCHEMATIC).sha256,
                            first.sha256)
        other = self.folder / "other.net"
        result = self.manager.save(model, SCHEMATIC, str(other))
        self.assertEqual(result.path, str(other))
        self.assertEqual(self.manager.current_path, str(other))
        self.assertEqual(other.read_bytes(), (NETLIST + "\n").encode())
        self.assertEqual(json.loads(other.with_suffix(".sch.json").read_text(
            encoding="utf-8")), SCHEMATIC)
        self.assertFalse(self.marker().exists())
        self.assertTrue(self.marker(other).exists())

    def test_save_failure_keeps_previous_files_and_path(self):
        model = Netlist()
        self.manager.save(model, SCHEMATIC, str(self.path))
        companion = self.path.with_suffix(".sch.json")
        before = companion.read_bytes()
        self.serializer.serialize.return_value = "changed\n"
        write = fm.write_text_atomic

        def fail_on_netlist(path, text):
            if path == str(self.path):
                raise fm.FileAccessError("write denied")
            return write(path, text)

        with patch.object(fm, "write_text_atomic", side_effect=fail_on_netlist):
            with self.assertRaises(fm.FileAccessError):
                self.manager.save(model, {"version": 1, "blocks": {}})
        self.assertEqual(self.path.read_bytes(), NETLIST.encode())
        self.assertEqual(companion.read_bytes(), before)
        self.assertEqual(self.manager.current_path, str(self.path))
        self.assertTrue(self.marker().exists())

    def test_transaction_requires_open_document(self):
        with self.assertRaises(ValueError):
            with self.manager.transaction():
                self.fail("Без открытого документа контекст недоступен")

    def test_transaction_commits_three_files_and_removes_journal(self):
        model = Netlist()
        self.manager.save(model, SCHEMATIC, str(self.path))
        history = self.folder / "demo.history.json"
        journal = self.folder / "demo.net.transaction.json"
        self.serializer.serialize.return_value = NETLIST + "\n"
        new_schematic = {"version": 1, "blocks": {}}

        with self.manager.transaction():
            self.assertTrue(journal.exists())
            self.manager.save(model, new_schematic)
            history.write_text("new history", encoding="utf-8")

        self.assertFalse(journal.exists())
        self.assertEqual(self.path.read_bytes(), (NETLIST + "\n").encode())
        companion = self.folder / "demo.sch.json"
        self.assertEqual(json.loads(companion.read_bytes()), new_schematic)
        self.assertEqual(history.read_bytes(), b"new history")
        self.assertTrue(self.marker().exists())

    def test_transaction_rolls_back_if_history_write_fails(self):
        model = Netlist()
        self.manager.save(model, SCHEMATIC, str(self.path))
        companion = self.folder / "demo.sch.json"
        history = self.folder / "demo.history.json"
        journal = self.folder / "demo.net.transaction.json"
        old_schematic = companion.read_bytes()
        self.serializer.serialize.return_value = NETLIST + "\n"

        with self.assertRaisesRegex(OSError, "history write failed"):
            with self.manager.transaction():
                self.manager.save(model, {"version": 1, "blocks": {}})
                history.write_bytes(b"incomplete")
                raise OSError("history write failed")

        self.assertEqual(self.path.read_bytes(), NETLIST.encode())
        self.assertEqual(companion.read_bytes(), old_schematic)
        self.assertFalse(history.exists())
        self.assertFalse(journal.exists())
        self.assertTrue(self.manager.is_open)

    def test_next_open_recovers_the_previous_generation_from_journal(self):
        companion = self.folder / "demo.sch.json"
        history = self.folder / "demo.history.json"
        self.path.write_bytes(b"interrupted write")
        companion.write_bytes(b"interrupted")
        history.write_bytes(b"partial history")
        journal = self.folder / "demo.net.transaction.json"
        old_schematic = json.dumps(SCHEMATIC).encode("utf-8")
        journal.write_text(json.dumps({
            str(self.path): base64.b64encode(NETLIST.encode()).decode("ascii"),
            str(companion): base64.b64encode(old_schematic).decode("ascii"),
            str(history): None,
        }), encoding="utf-8")

        opened = self.manager.open(str(self.path))
        self.assertTrue(opened.ok)
        self.assertEqual(opened.schematic, SCHEMATIC)
        self.parser.parse.assert_called_once_with(NETLIST, filename=str(self.path))
        self.assertFalse(history.exists())
        self.assertFalse(journal.exists())

    def test_recovery_rejects_journal_for_an_unrelated_path(self):
        self.path.write_bytes(NETLIST.encode())
        unrelated = self.folder / "another.net"
        unrelated.write_bytes(b"keep me")
        journal = self.folder / "demo.net.transaction.json"
        journal.write_text(json.dumps({str(unrelated): None}), encoding="utf-8")
        with self.assertRaises(fm.FileAccessError):
            self.manager.open(str(self.path))
        self.assertEqual(unrelated.read_bytes(), b"keep me")
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.manager.is_open)

    def test_corrupt_journal_cannot_partially_restore_document(self):
        self.path.write_bytes(b"current netlist")
        companion = self.folder / "demo.sch.json"
        companion.write_bytes(b"current schematic")
        history = self.folder / "demo.history.json"
        history.write_bytes(b"current history")
        journal = self.folder / "demo.net.transaction.json"
        journal.write_text(json.dumps({
            str(self.path): base64.b64encode(NETLIST.encode()).decode("ascii"),
            str(companion): "invalid base64!",
            str(history): None,
        }), encoding="utf-8")

        with self.assertRaises(fm.FileAccessError):
            self.manager.open(str(self.path))
        self.assertEqual(self.path.read_bytes(), b"current netlist")
        self.assertEqual(companion.read_bytes(), b"current schematic")
        self.assertEqual(history.read_bytes(), b"current history")
        self.assertTrue(journal.exists())
        self.assertFalse(self.marker().exists())


@unittest.skipUnless(os.name == "posix", "Нужна POSIX-блокировка")
class PosixFileManagerTest(unittest.TestCase):
    def test_native_lock_allows_reading_but_excludes_another_flock(self):
        import fcntl

        with TemporaryDirectory() as folder:
            path = Path(folder, "demo.net").resolve()
            path.write_text(NETLIST, encoding="ascii")
            parser = Mock(spec_set=["parse"])
            parser.parse.return_value = parse_result(str(path))
            manager = fm.FileManager(parser, Mock(spec_set=["serialize"]))
            try:
                self.assertTrue(manager.open(str(path)).ok)
                self.assertTrue(Path(str(path) + ".lock").is_file())
                self.assertEqual(fm.read_text(str(path)), NETLIST)
                with path.open("rb+") as another_handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(another_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                manager.close()
            self.assertFalse(Path(str(path) + ".lock").exists())
            with path.open("rb+") as another_handle:
                fcntl.flock(another_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
