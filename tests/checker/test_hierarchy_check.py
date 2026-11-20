import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from netlist.checker import hierarchy_check
from netlist.core.hierarchy import HierEdge
from netlist.core.model import CheckOptions, Severity, SourceLoc
from tests.core.fakes import FakeBlock, FakeInstance, FakePrimitive, FakeView, loc


def walker(missing=(), reachable=()):
    result = Mock(spec_set=["missing", "reachable"])
    result.missing.return_value = list(missing)
    result.reachable.return_value = set(reachable)
    return result


def index(blocks, primitives=None):
    return SimpleNamespace(blocks=blocks, primitives=primitives or {})


class HierarchyCheckTest(unittest.TestCase):
    def test_valid_hierarchy(self):
        main = FakeBlock("main")
        view = FakeView([main])
        graph = walker(reachable={"main"})

        self.assertEqual(
            hierarchy_check.check(view, walker=graph,
                                  index=index({"main": [main]})),
            [],
        )
        graph.missing.assert_called_once_with()
        graph.reachable.assert_called_once_with("main")

    def test_duplicate_definitions(self):
        main = FakeBlock("main", loc(1))
        duplicate = FakeBlock("main", loc(4))
        first_primitive = FakePrimitive("D", loc=loc(2))
        duplicate_primitive = FakePrimitive("D", loc=loc(6))
        view = FakeView([main, duplicate], [first_primitive, duplicate_primitive])

        result = hierarchy_check.check(
            view,
            walker=walker(reachable={"main"}),
            index=index({"main": [main, duplicate]},
                        {"D": [first_primitive, duplicate_primitive]}),
        )

        self.assertEqual([(diag.code, diag.loc.line) for diag in result],
                         [("E203", 4), ("E205", 6)])

    def test_missing_block_and_root(self):
        other = FakeBlock("other", loc(1))
        edge = HierEdge("other", "x", "missing", loc(2))
        view = FakeView([other], source_file="test.net")
        graph = walker(missing=[edge])

        result = hierarchy_check.check(
            view, walker=graph, index=index({"other": [other]})
        )

        self.assertEqual([diag.code for diag in result], ["E201", "E204"])
        self.assertEqual(result[0].loc, loc(2))
        self.assertEqual(result[1].loc, SourceLoc("test.net", None))
        graph.reachable.assert_not_called()

    def test_unknown_primitive_strict_and_relaxed(self):
        instance = FakeInstance("d", "prim", "unknown", loc(3))
        main = FakeBlock("main", instance_list=[instance])
        view = FakeView([main])
        graph = walker(reachable={"main"})
        model_index = index({"main": [main]})

        strict = hierarchy_check.check(view, CheckOptions(), graph, model_index)
        relaxed = hierarchy_check.check(
            view, CheckOptions(strict_primitives=False), graph, model_index
        )

        self.assertEqual((strict[0].code, strict[0].severity),
                         ("E202", Severity.ERROR))
        self.assertEqual((relaxed[0].code, relaxed[0].severity),
                         ("W202", Severity.WARNING))

    def test_unreachable_block(self):
        main = FakeBlock("main", loc(1))
        unused = FakeBlock("unused", loc(5))
        view = FakeView([main, unused])

        result = hierarchy_check.check(
            view,
            walker=walker(reachable={"main"}),
            index=index({"main": [main], "unused": [unused]}),
        )

        self.assertEqual([(diag.code, diag.block, diag.loc.line) for diag in result],
                         [("W206", "unused", 5)])


if __name__ == "__main__":
    unittest.main()
