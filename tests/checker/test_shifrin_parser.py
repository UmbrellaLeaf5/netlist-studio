"""CheckerParser, § 4.2. Запуск: python -m unittest tests.checker.test_shifrin_parser"""
import copy
import gc
import unittest
import weakref
from types import SimpleNamespace as Row
from unittest.mock import patch

from src.checker import checker_parser as cp
from src.core.model import PinRef, SourceLoc


def raw():
    return Row(source_file="a.net", primitives=[], blocks=[])


class CheckerParserTest(unittest.TestCase):
    def test_empty(self):
        draft = cp.CheckerDraft(raw())
        self.assertEqual((draft.source_file, draft.blocks(), draft.primitives()),
                         ("a.net", [], []))
        self.assertIsNone(draft.find_block("x"))
        self.assertIsNone(draft.find_primitive("x"))
        self.assertIn("a.net", repr(draft))

    def test_order_lookup_and_connections(self):
        loc = SourceLoc("a.net", 2)
        prim = Row(name="D", pin_names=["p"], loc=loc)
        library = Row(name="D", pin_names=["q"], loc=SourceLoc("lib", 1))
        inst = Row(name="i", kind="prim", type_name="D", loc=loc)
        def conn(name, line):
            return Row(instance=name, pin="p", net=None, loc=SourceLoc("a.net", line))
        block = Row(name="main", loc=loc, pins=[Row(name="p", net="n", loc=loc)],
                    nets=[Row(name="n", loc=loc)], instances=[inst, inst],
                    connections=[conn("i", 1), conn("missing", 3), conn("i", 4)])
        source = raw()
        source.primitives = [prim]
        source.blocks = [block, block]
        draft = cp.CheckerDraft(source, [library])
        prim.pin_names.clear()
        source.blocks.clear()
        self.assertEqual([p.pin_names for p in draft.primitives()], [["p"], ["q"]])
        self.assertIs(draft.find_primitive("D"), draft.primitives()[0])
        self.assertEqual(len(draft.blocks()), 2)
        b = draft.find_block("main")
        self.assertIs(b, draft.blocks()[0])
        self.assertEqual(b.pins(), [cp.DraftPin("p", "n", loc)])
        self.assertEqual(b.nets(), [cp.DraftNet("n", loc)])
        first, second = b.instances()
        self.assertEqual((first.name, first.kind, first.type_name), ("i", "prim", "D"))
        self.assertEqual(first.pins(), [cp.DraftPin("p", None, SourceLoc("a.net", n))
                                         for n in (1, 4)])
        self.assertEqual(second.pins(), [])
        self.assertEqual(b.orphan_connections(),
                         [cp.DraftOrphan(PinRef("missing", "p"), None, SourceLoc("a.net", 3))])
        for getter in (draft.blocks, draft.primitives, b.pins, b.nets, b.instances,
                       b.orphan_connections, first.pins):
            before = getter()
            getter().clear()
            self.assertEqual(getter(), before)

    def test_primitive_pins_do_not_mutate_draft(self):
        source = raw()
        source.primitives = [Row(name="D", pin_names=["p"], loc=SourceLoc(None, 1))]
        draft = cp.CheckerDraft(source)
        draft.primitives()[0].pin_names.append("extra")
        self.assertEqual(draft.find_primitive("D").pin_names, ["p"])

    @patch("src.core.parser.NetlistParser", autospec=True)
    def test_load_with_and_without_library(self, constructor):
        parser = constructor.return_value
        source = raw()
        source.primitives = [Row(name="D", pin_names=[], loc=SourceLoc("a.net", 1))]
        parser.parse.return_value = Row(raw=source, diagnostics=["file error"])
        lib = raw()
        lib.primitives = [Row(name="P", pin_names=[], loc=SourceLoc("lib", 1))]
        parser.parse_library.return_value = Row(raw=lib, diagnostics=["library error"])
        result = cp.load("input")
        parser.parse.assert_called_once_with("input", None)
        parser.parse_library.assert_not_called()
        self.assertEqual(result.diagnostics, ["file error"])
        self.assertIsNotNone(result.draft.find_primitive("D"))
        result = cp.load("input", "a.net", "", "lib")
        parser.parse.assert_called_with("input", "a.net")
        parser.parse_library.assert_called_once_with("", "lib")
        self.assertEqual(result.diagnostics, ["file error", "library error"])
        self.assertEqual([p.name for p in result.draft.primitives()], ["D", "P"])
        self.assertEqual(parser.parse.return_value.diagnostics, ["file error"])

    def test_copy_and_lifetime(self):
        loc = SourceLoc(None, 1)
        for factory in (lambda: cp.DraftPrimitive("D", ["p"], loc),
                        lambda: cp.DraftPin("p", None, loc), lambda: cp.DraftNet("n", loc),
                        lambda: cp.DraftOrphan(PinRef("i", "p"), None, loc),
                        lambda: cp.DraftInstance("i", "sub", "B", loc),
                        lambda: cp.DraftBlock("B", loc),
                        lambda: cp.LoadResult(cp.CheckerDraft(raw()), [])):
            value = factory()
            clone = copy.copy(value)
            self.assertEqual(value, clone)
            self.assertIsNot(value, clone)
            ref = weakref.ref(value)
            del value
            gc.collect()
            self.assertIsNone(ref())
        draft = cp.CheckerDraft(raw())
        ref = weakref.ref(draft)
        del draft
        gc.collect()
        self.assertIsNone(ref())


if __name__ == "__main__":
    unittest.main()
