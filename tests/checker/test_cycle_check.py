"""Юнит-тесты CycleCheck (автор М. Большаков), docs/interfaces.md, п. 4.5.

Walker отдаёт готовые циклы, рёбра и порядок DFS; индекс получает готовые
списки определений. Нетлист представлен FakeView, как в tests/core.

Запуск из корня репозитория:
    python -m unittest tests.checker.test_cycle_check -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from src.checker import cycle_check
from src.core.hierarchy import HierEdge
from src.core.model import CheckOptions, Diagnostic, Severity
from tests.core.fakes import FakeBlock, FakeInstance, FakeView, loc


def edge(parent, instance, child, line=None, file="test.net"):
    return HierEdge(parent, instance, child, loc(line, file))


def error(path, block, instance, line=None, file="test.net"):
    return Diagnostic(
        "E301",
        Severity.ERROR,
        "cycle",
        f"цикл в иерархии: {path}",
        loc(line, file),
        block,
        instance,
    )


def walker_stub(*, cycles=(), dfs=(), children=None):
    """Ничего не обходит и не ищет: ответы заданы тестом."""
    walker = mock.Mock(spec_set=["find_cycles", "iter_dfs", "children"])
    walker.find_cycles.return_value = list(cycles)
    walker.iter_dfs.side_effect = lambda _top: iter(dfs)
    replies = {} if children is None else children
    walker.children.side_effect = lambda parent: replies.get(parent, [])
    return walker


def index_stub(**blocks):
    return SimpleNamespace(blocks=blocks)


class CheckTest(unittest.TestCase):
    def test_metadata(self):
        self.assertEqual(cycle_check.NAME, "cycle")
        self.assertEqual(cycle_check.CODES, {"E301": "Цикл в иерархии блоков"})

    def test_no_cycles_skips_dfs_and_edge_lookup(self):
        walker = walker_stub()
        view = FakeView()
        self.assertEqual(cycle_check.check(view, walker=walker, index=index_stub()), [])
        walker.find_cycles.assert_called_once_with()
        walker.iter_dfs.assert_not_called()
        walker.children.assert_not_called()

    def test_missing_dependencies_are_constructed_with_view(self):
        view = FakeView([FakeBlock("main")])
        loop = edge("main", "self", "main", 5)
        walker = walker_stub(
            cycles=[["main", "main"]], dfs=[(None, 0)], children={"main": [loop]}
        )
        index = index_stub()
        expected = error("main -> self (main)", "main", "self", 5)

        for use_walker, use_index in ((False, False), (True, False), (False, True)):
            with self.subTest(walker_given=use_walker, index_given=use_index):
                walker.reset_mock()
                with (
                    mock.patch(
                        "src.core.hierarchy.HierarchyWalker", return_value=walker
                    ) as make_walker,
                    mock.patch.object(
                        cycle_check, "ModelIndex", return_value=index
                    ) as make_index,
                ):
                    result = cycle_check.check(
                        view,
                        walker=walker if use_walker else None,
                        index=index if use_index else None,
                        extra="ignored",
                    )
                self.assertEqual(result, [expected])
                walker.iter_dfs.assert_called_once_with("main")
                if use_walker:
                    make_walker.assert_not_called()
                else:
                    make_walker.assert_called_once_with(view)
                if use_index:
                    make_index.assert_not_called()
                else:
                    make_index.assert_called_once_with(view)

    def test_custom_top_rotates_reachable_cycle_and_keeps_detached_cycle(self):
        # start -> bridge -> b -> a -> b; z -> z недостижим из start.
        sb = edge("start", "via", "bridge", 3)
        bb = edge("bridge", "in_b", "b", 7)
        ba = edge("b", "to_a", "a", 12)
        ab = edge("a", "back", "b", 17, "child.net")
        zz = edge("z", "local", "z", 24)
        cycle = ["a", "b", "a"]
        walker = walker_stub(
            cycles=[["z", "z"], cycle],
            dfs=[(None, 0), (sb, 1), (bb, 2), (ba, 3)],
            children={"b": [ba], "a": [ab], "z": [zz]},
        )
        blocks = [FakeBlock(name) for name in ("start", "bridge", "b", "a", "z")]
        view = FakeView(blocks)
        opts = CheckOptions(top="start")

        result = cycle_check.check(view, opts, walker, index_stub())

        self.assertEqual(
            result,
            [
                error(
                    "start -> via (bridge) -> in_b (b) -> to_a (a) -> back (b)",
                    "a",
                    "back",
                    17,
                    "child.net",
                ),
                error("z -> local (z)", "z", "local", 24),
            ],
        )
        walker.iter_dfs.assert_called_once_with("start")
        self.assertEqual(cycle, ["a", "b", "a"])
        self.assertEqual(view.block_list, blocks)
        self.assertEqual(opts, CheckOptions(top="start"))

    def test_unknown_top_does_not_hide_cycles(self):
        ab = edge("a", "to_b", "b", 5)
        ba = edge("b", "to_a", "a", 9)
        walker = walker_stub(cycles=[["a", "b", "a"]], children={"a": [ab], "b": [ba]})
        view = FakeView([FakeBlock("a"), FakeBlock("b")])

        result = cycle_check.check(
            view, CheckOptions(top="missing"), walker, index_stub()
        )

        self.assertEqual(result, [error("a -> to_b (b) -> to_a (a)", "b", "to_a", 9)])
        walker.iter_dfs.assert_called_once_with("missing")

    def test_duplicate_cycle_and_parallel_instances_give_one_error(self):
        # Первый инстанс по порядку объявления, а не по имени или строке.
        first = edge("main", "z_first", "main", 9)
        second = edge("main", "a_second", "main", 5)
        walker = walker_stub(
            cycles=[["main", "main"], ["main", "main"]],
            dfs=[(None, 0)],
            children={"main": [first, second]},
        )
        view = FakeView([FakeBlock("main")])

        self.assertEqual(
            cycle_check.check(view, walker=walker, index=index_stub()),
            [error("main -> z_first (main)", "main", "z_first", 9)],
        )

    def test_limit_is_applied_after_sorting(self):
        aa = edge("a", "local_a", "a", 4)
        zz = edge("z", "local_z", "z", 8)
        # DFS-ответ начинается с a, но в представлении раньше объявлен z.
        walker = walker_stub(
            cycles=[["a", "a"], ["z", "z"]], children={"a": [aa], "z": [zz]}
        )
        view = FakeView([FakeBlock("z"), FakeBlock("a")])

        result = cycle_check.check(
            view, CheckOptions(max_errors=1), walker, index_stub()
        )

        self.assertEqual(result, [error("z -> local_z (z)", "z", "local_z", 8)])

    def test_cycle_with_missing_edge_raises_value_error(self):
        # Заглушка нарушает контракт: заявляет a -> b -> a без ребра b -> a.
        walker = walker_stub(
            cycles=[["a", "b", "a"]], children={"a": [edge("a", "to_b", "b", 5)]}
        )
        view = FakeView([FakeBlock("a"), FakeBlock("b")])
        with self.assertRaisesRegex(ValueError, r"b -> a.*children\(\)"):
            cycle_check.check(view, walker=walker, index=index_stub())


class PathTest(unittest.TestCase):
    def test_first_reach_empty_traversal(self):
        self.assertEqual(cycle_check._first_reach(walker_stub(), "missing"), {})

    def test_first_reach_keeps_first_predecessor_and_visit_number(self):
        rb = edge("root", "to_b", "b")
        ba = edge("b", "to_a", "a")
        ra = edge("root", "shortcut", "a")
        ar = edge("a", "return", "root")
        rc = edge("root", "to_c", "c")
        walker = walker_stub(
            dfs=[(None, 0), (rb, 1), (ba, 2), (ra, 1), (ar, 3), (rc, 1)]
        )

        self.assertEqual(
            cycle_check._first_reach(walker, "root"),
            {
                "root": (0, None),
                "b": (1, rb),
                "a": (2, ba),
                "c": (3, rc),
            },
        )

    def test_path_from_top_is_in_travel_order(self):
        rb = edge("root", "to_b", "b")
        ba = edge("b", "to_a", "a")
        reach = {"root": (0, None), "b": (1, rb), "a": (2, ba)}
        for target, expected in (("root", []), ("b", [rb]), ("a", [rb, ba])):
            with self.subTest(target=target):
                self.assertEqual(cycle_check._path_from_top(reach, target), expected)

    def test_entry_block_prefers_visit_order_and_ignores_other_blocks(self):
        nodes = ["a", "b", "c"]
        for reach, expected in (
            ({"root": (0, None)}, "a"),
            ({"c": (3, None)}, "c"),
            ({"root": (0, None), "a": (4, None), "b": (2, None), "c": (3, None)}, "b"),
        ):
            with self.subTest(reach=reach):
                self.assertEqual(cycle_check._entry_block(nodes, reach), expected)

    def test_edge_matches_child_and_returns_first_matching_instance(self):
        first = edge("a", "first", "b", 9)
        walker = walker_stub(
            children={
                "a": [edge("a", "other", "c", 3), first, edge("a", "second", "b", 5)]
            }
        )
        self.assertIs(cycle_check._edge(walker, "a", "b"), first)
        walker.children.assert_called_once_with("a")

    def test_edge_with_no_matching_child_raises(self):
        walker = walker_stub(children={"a": [edge("a", "other", "c", 3)]})
        with self.assertRaisesRegex(ValueError, r"a -> b.*children\(\)"):
            cycle_check._edge(walker, "a", "b")

    def test_cycle_error_returns_actual_closing_edge_without_source(self):
        # Обход вошёл с c; после поворота путь root -> c -> a -> b -> c.
        rc = edge("root", "into_c", "c", 3)
        ca = edge("c", "to_a", "a", 8)
        ab = edge("a", "to_b", "b", 12)
        bc = edge("b", "to_c", "c", file=None)
        walker = walker_stub(children={"a": [ab], "b": [bc], "c": [ca]})
        reach = {"root": (0, None), "c": (1, rc), "a": (2, ca), "b": (3, ab)}

        diag, closing = cycle_check._cycle_error(walker, ["a", "b", "c", "a"], reach)

        self.assertEqual(
            diag,
            error(
                "root -> into_c (c) -> to_a (a) -> to_b (b) -> to_c (c)",
                "b",
                "to_c",
                file=None,
            ),
        )
        self.assertIs(closing, bc)


class OrderAndLimitTest(unittest.TestCase):
    def test_sorted_uses_block_order_then_known_lines_then_instance_order(self):
        draft = FakeBlock(
            "z",
            instance_list=[
                FakeInstance("late", "prim", "b"),  # не тот вид
                FakeInstance("late", "sub", "c"),  # не тот тип
                FakeInstance("early", "sub", "b"),
                FakeInstance("late", "sub", "b"),
            ],
        )
        duplicate = FakeBlock("z", instance_list=[FakeInstance("late", "sub", "b")])
        a = FakeBlock("a")
        view = FakeView([draft, a, duplicate])
        index = index_stub(z=[draft, duplicate], a=[a])
        # Строки имеют приоритет над инстансами без строки, но не над блоками.
        last_block = error("a -> local (a)", "a", "local", 1)
        line_20 = error("z -> x (b)", "z", "x", 20)
        line_10 = error("z -> y (b)", "z", "y", 10)
        early = error("z -> early (b)", "z", "early")
        late = error("z -> late (b)", "z", "late")
        not_indexed = error("z -> absent (b)", "z", "absent")
        items = [
            (last_block, edge("a", "local", "a", 1)),
            (not_indexed, edge("z", "absent", "b")),
            (late, edge("z", "late", "b")),
            (line_20, edge("z", "x", "b", 20)),
            (early, edge("z", "early", "b")),
            (line_10, edge("z", "y", "b", 10)),
        ]

        self.assertEqual(
            cycle_check._sorted(items, view, index),
            [line_10, line_20, early, late, not_indexed, last_block],
        )

    def test_sorted_handles_missing_block_and_preserves_order_on_ties(self):
        first = error("z -> first (z)", "z", "first")
        second = error("z -> second (z)", "z", "second")
        known = error("a -> local (a)", "a", "local")
        items = [
            (first, edge("z", "first", "z")),
            (second, edge("z", "second", "z")),
            (known, edge("a", "local", "a")),
        ]
        view = FakeView([FakeBlock("a")])

        self.assertEqual(
            cycle_check._sorted(items, view, index_stub()), [known, first, second]
        )
        self.assertEqual(cycle_check._sorted([], view, index_stub()), [])

    def test_cut_after_errors(self):
        a = error("a -> local (a)", "a", "local", 1)
        b = error("b -> local (b)", "b", "local", 2)
        diags = [a, b]
        for limit, expected in (
            (None, [a, b]),
            (0, []),
            (1, [a]),
            (2, [a, b]),
            (3, [a, b]),
        ):
            with self.subTest(limit=limit):
                self.assertEqual(cycle_check._cut_after_errors(diags, limit), expected)
                self.assertEqual(diags, [a, b])


if __name__ == "__main__":
    unittest.main()
