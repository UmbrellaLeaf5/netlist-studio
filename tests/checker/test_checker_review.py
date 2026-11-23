"""Регрессии четырёх замечаний из review_checker_modules.md.

Запуск: python -m unittest discover -s tests -v
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.checker import hierarchy_check, model_utils, pin_check, subcircuit_check
from src.checker.syntax_check import LineKind, SyntaxCheck, classify, tokenize
from src.core.hierarchy import HierarchyWalker
from src.core.model import CheckOptions, Diagnostic, Netlist, PinRef, Severity, SourceLoc
from src.core.parser import NetlistParser
from src.core.serializer import NetlistSerializer


class SyntaxTests(unittest.TestCase):
    def setUp(self):
        self.parser = NetlistParser()

    def test_valid_document_and_multiple_blocks(self):
        text = ("primitive D p\r\nblock main\r\nnet n\r\n"
                "inst d prim D\r\nd.p n\r\nend\r\nblock other\r\nend")
        result = self.parser.parse(text, "test.net")
        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual([b.name for b in result.raw.blocks], ["main", "other"])
        self.assertEqual(result.raw.blocks[0].connections[0].loc, SourceLoc("test.net", 5))

    def test_all_line_kinds_and_counts(self):
        cases = {"": LineKind.BLANK, "primitive": LineKind.PRIMITIVE,
                 "block": LineKind.BLOCK, "end extra": LineKind.END,
                 "pin": LineKind.PIN, "net": LineKind.NET,
                 "inst": LineKind.INST, "i.p": LineKind.CONN}
        for line, kind in cases.items():
            with self.subTest(line=line):
                self.assertEqual(classify(tokenize(line)), kind)
                if line:
                    diags = SyntaxCheck().check_lines([line])
                    self.assertEqual([d.code for d in diags], ["E103"])

    def test_context_diagnostics(self):
        cases = [("net n", "E104"), ("end", "E106"),
                 ("block main\nblock nested\nend", "E105"),
                 ("block main\nprimitive D p\nend", "E109"),
                 ("main:", "E101")]
        for text, code in cases:
            with self.subTest(text=text):
                result = self.parser.parse(text)
                self.assertFalse(result.ok)
                self.assertEqual([d.code for d in result.diagnostics], [code])

    def test_identifier_and_kind_errors_recover(self):
        for line, code in [("net bad-name", "E102"), ("pin p 4bad", "E102"),
                           ("net block", "E107"), ("inst i wrong T", "E101"),
                           ("i.p.q n", "E102"), ("i.p net", "E107")]:
            with self.subTest(line=line):
                result = self.parser.parse(f"block main\n{line}\nnet good\nend")
                self.assertEqual([d.code for d in result.diagnostics], [code])
                self.assertEqual([n.name for n in result.raw.blocks[0].nets], ["good"])

    def test_ascii_and_control_characters(self):
        for line in ["net café", "# комментарий", "net n\u2028end"]:
            self.assertEqual(SyntaxCheck().check_lines([line])[0].code, "E108")
        self.assertEqual(SyntaxCheck().check_lines(["net n\v"])[0].code, "E101")

    def test_unclosed_block_preserved(self):
        result = self.parser.parse("block main\nnet n", "open.net")
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics[0].code, "E105")
        self.assertEqual(result.raw.blocks[0].nets[0].name, "n")

    def test_semantic_duplicates_and_unknown_connections_preserved(self):
        result = self.parser.parse("block main\nnet n\nnet n\ni.p -\nend")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.raw.blocks[0].nets), 2)
        self.assertIsNone(result.raw.blocks[0].connections[0].net)

    def test_library(self):
        result = self.parser.parse_library("primitive D p\nblock main\nprimitive E q\nend")
        self.assertFalse(result.ok)
        self.assertEqual([p.name for p in result.raw.primitives], ["D", "E"])
        self.assertEqual([d.loc.line for d in result.diagnostics], [2, 4])

    def test_diagnostics_are_shared_types(self):
        result = self.parser.parse("bad", "bad.net")
        diag = result.diagnostics[0]
        self.assertIsInstance(diag, Diagnostic)
        self.assertIs(diag.severity, Severity.ERROR)
        self.assertEqual(diag.loc, SourceLoc("bad.net", 1))
        self.assertIn("E101", diag.format())
        self.assertEqual(json.loads(json.dumps(diag.to_dict()))["severity"], "error")

    def test_serializer_output_is_accepted(self):
        model = Netlist()
        model.add_primitive("D", ["p", "q"])
        block = model.add_block("main")
        block.add_net("n")
        block.add_pin("out", "n")
        inst = block.add_instance("d", "prim", "D")
        inst.add_pin("p", "n")
        inst.add_pin("q")
        result = self.parser.parse(NetlistSerializer().serialize(model, "header"))
        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual([p.net for p in result.raw.blocks[0].connections], ["n", None])


class ModelUtilsTests(unittest.TestCase):
    def test_real_model_connections_and_cache(self):
        model = Netlist()
        block = model.add_block("main")
        block.add_net("n")
        block.add_net("unused")
        block.add_pin("p", "n")
        block.add_pin("free")
        inst = block.add_instance("i", "prim", "D")
        inst.add_pin("a", "n")
        inst.add_pin("b")
        expected = [PinRef(None, "p"), PinRef("i", "a")]
        result = model_utils.pins_of_net(block)
        self.assertEqual([ref for ref, _ in result["n"]], expected)
        self.assertEqual(set(result), {"n"})
        index = model_utils.ModelIndex(model)
        self.assertEqual(index.pins_of_net(block), result)
        self.assertIs(index.pins_of_net(block), index.pins_of_net(block))
        self.assertEqual(set(index.nets(block)), {"n", "unused"})

    def test_draft_duplicates_and_orphans(self):
        loc = SourceLoc("draft.net", 7)
        pin = SimpleNamespace(name="p", net="n", loc=loc)
        orphan = SimpleNamespace(ref=PinRef("missing", "a"), net="n", loc=loc)
        block = SimpleNamespace(pins=lambda: [pin, pin], instances=lambda: [],
                                orphan_connections=lambda: [orphan, orphan])
        connections = model_utils.pins_of_net(block)["n"]
        self.assertEqual(connections, [(PinRef(None, "p"), loc)] * 2
                         + [(orphan.ref, loc)] * 2)

    def test_type_lookup_uses_kind_and_peer_checks_import(self):
        model = Netlist()
        model.add_primitive("T", ["p"])
        target = model.add_block("T")
        block = model.add_block("main")
        sub = block.add_instance("s", "sub", "T")
        prim = block.add_instance("p", "prim", "T")
        self.assertIs(model_utils.resolve_type(model, sub), target)
        self.assertIs(model_utils.resolve_type(model, prim), model.find_primitive("T"))
        self.assertEqual(subcircuit_check.check(model)[0].code, "E401")
        self.assertEqual(pin_check.check(model), [])


class HierarchyTests(unittest.TestCase):
    def test_selected_root_and_reachability(self):
        model = Netlist()
        top = model.add_block("top")
        top.add_instance("x", "sub", "child")
        model.add_block("child")
        model.add_block("main")
        walker = Mock(wraps=HierarchyWalker(model))
        diags = hierarchy_check.check(model, CheckOptions(top="top"), walker=walker)
        self.assertEqual([(d.code, d.block) for d in diags], [("W206", "main")])
        walker.reachable.assert_called_once_with("top")
        walker.missing.assert_called_once_with()

    def test_default_strict_and_relaxed(self):
        model = Netlist()
        inst = model.add_block("main").add_instance("d", "prim", "unknown")
        inst.loc = SourceLoc("test.net", 3)
        default = hierarchy_check.check(model)
        self.assertEqual(default, hierarchy_check.check(model, CheckOptions()))
        self.assertEqual(default[0].code, "E202")
        self.assertIs(default[0].severity, Severity.ERROR)
        self.assertEqual((default[0].loc, default[0].block, default[0].obj),
                         (inst.loc, "main", "d"))
        relaxed = hierarchy_check.check(model, CheckOptions(strict_primitives=False))
        self.assertEqual(relaxed[0].code, "W202")
        self.assertIs(relaxed[0].severity, Severity.WARNING)
        json.dumps(default[0].to_dict())

    def test_missing_root_and_block(self):
        model = Netlist()
        model.source_file = "test.net"
        inst = model.add_block("other").add_instance("x", "sub", "missing")
        inst.loc = SourceLoc("test.net", 2)
        diags = hierarchy_check.check(model)
        self.assertEqual([d.code for d in diags], ["E201", "E204"])
        self.assertEqual(diags[0].loc, inst.loc)
        self.assertEqual(diags[1].loc, SourceLoc("test.net", None))

    def test_duplicate_locations_and_first_definition_graph(self):
        first = Netlist(); first.add_block("main"); first.add_primitive("D", [])
        second = Netlist(); second.add_block("main"); second.add_primitive("D", [])
        second.block("main").loc = SourceLoc("test.net", 10)
        second.find_primitive("D").loc = SourceLoc("test.net", 11)
        second.block("main").add_instance("x", "sub", "other")
        first.add_block("other")
        view = SimpleNamespace(source_file="test.net",
                               blocks=lambda: first.blocks() + second.blocks(),
                               primitives=lambda: first.primitives() + second.primitives())
        diags = hierarchy_check.check(view)
        by_code = {d.code: d for d in diags}
        self.assertEqual(set(by_code), {"E203", "E205", "W206"})
        self.assertEqual(by_code["E203"].loc.line, 10)
        self.assertEqual(by_code["E205"].loc.line, 11)
        self.assertEqual(by_code["W206"].block, "other")


if __name__ == "__main__":
    unittest.main()
