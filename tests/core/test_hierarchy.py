"""Юнит-тесты HierarchyWalker (netlist/core/hierarchy.py, автор Н. Бучилкин).

Контракт: docs/interfaces.md, п. 2.4. Вместо настоящего нетлиста
используется FakeView из fakes.py. Иерархия задаётся короткой записью
`hierarchy({"main": ["a", "b"], "a": [], "b": []})`: блоки в этом порядке,
у main два инстанса-подсхемы, a1 (тип a) и b2 (тип b). Строка `inst`
инстанса номер i в блоке номер k — это 10*k + i (у b2 из примера: 12).

Для validate() модули HierarchyCheck и CycleCheck заменены заглушками,
которые запоминают, с какими аргументами их вызвали.

Запуск из корня репозитория:
    python -m unittest tests.core.test_hierarchy -v
"""

from __future__ import annotations

import copy
import dataclasses
import types
import unittest

from netlist.core.hierarchy import HierarchyCycleError, HierarchyWalker, HierEdge
from netlist.core.model import CheckOptions, Diagnostic, Severity
from tests.core.fakes import FakeBlock, FakeInstance, FakeView, hierarchy, loc
from tests.core.fakes import replaced_modules


def edge(parent: str, instance: str, child: str, line: int) -> HierEdge:
    return HierEdge(parent, instance, child, loc(line))


def walk(spec, root: str = "main", prims=None):
    """Список (имя блока, глубина) обхода iter_dfs: так проще читать порядок."""
    walker = HierarchyWalker(hierarchy(spec, prims))
    return [(root if e is None else e.child, depth)
            for e, depth in walker.iter_dfs(root)]


# Объекты данных
class HierEdgeTest(unittest.TestCase):

    def test_fields(self):
        """Ребро хранит родителя, инстанс, подсхему и место строки inst."""
        e = edge("main", "x1", "amp", 12)
        self.assertEqual((e.parent, e.instance, e.child, e.loc),
                         ("main", "x1", "amp", loc(12)))

    def test_equality(self):
        """Рёбра с одинаковыми полями равны, при отличии любого поля — нет."""
        self.assertEqual(edge("m", "x", "a", 1), edge("m", "x", "a", 1))
        for other in (edge("n", "x", "a", 1), edge("m", "y", "a", 1),
                      edge("m", "x", "b", 1), edge("m", "x", "a", 2)):
            self.assertNotEqual(edge("m", "x", "a", 1), other)

    def test_hashable(self):
        """Ребро можно положить в set: одинаковые рёбра схлопываются."""
        self.assertEqual(len({edge("m", "x", "a", 1), edge("m", "x", "a", 1),
                              edge("m", "y", "a", 2)}), 2)

    def test_frozen(self):
        """Ребро неизменяемо: присваивание полю бросает FrozenInstanceError."""
        e = edge("m", "x", "a", 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.child = "b"  # type: ignore[misc]

    def test_copy(self):
        """Копия и глубокая копия ребра равны оригиналу."""
        e = edge("m", "x", "a", 1)
        self.assertEqual(copy.copy(e), e)
        self.assertEqual(copy.deepcopy(e), e)


class HierarchyCycleErrorTest(unittest.TestCase):

    def test_is_exception_with_cycle(self):
        """Это исключение, а в поле cycle лежит путь цикла."""
        exc = HierarchyCycleError(["a", "b", "a"])
        self.assertIsInstance(exc, Exception)
        self.assertEqual(exc.cycle, ["a", "b", "a"])

    def test_can_be_raised_and_caught(self):
        """Исключение бросается и ловится, cycle доступен в обработчике."""
        with self.assertRaises(HierarchyCycleError) as caught:
            raise HierarchyCycleError(["x", "x"])
        self.assertEqual(caught.exception.cycle, ["x", "x"])


# Конструктор
class InitTest(unittest.TestCase):

    def test_graph_built_once(self):
        """view.blocks() читается при создании, запросы его больше не вызывают."""
        view = hierarchy({"main": ["a"], "a": ["b"], "b": []})
        walker = HierarchyWalker(view)
        calls = view.blocks_calls
        walker.children("main")
        walker.parents("a")
        walker.missing()
        list(walker.iter_dfs())
        walker.reachable()
        walker.find_cycles()
        walker.topological_order()
        self.assertEqual(view.blocks_calls, calls)

    def test_snapshot_does_not_follow_view_changes(self):
        """После изменения view старый walker отвечает по старому графу."""
        view = hierarchy({"main": ["a"], "a": []})
        walker = HierarchyWalker(view)
        view.block_list[0].instance_list.append(FakeInstance("z", "sub", "zzz"))
        self.assertEqual([e.child for e in walker.children("main")], ["a"])
        self.assertEqual(walker.missing(), [])

    def test_empty_view(self):
        """Пустой нетлист: все запросы пустые, исключений нет."""
        walker = HierarchyWalker(FakeView())
        self.assertEqual(walker.children("main"), [])
        self.assertEqual(walker.parents("main"), [])
        self.assertEqual(walker.missing(), [])
        self.assertEqual(list(walker.iter_dfs()), [])
        self.assertEqual(walker.find_cycles(), [])
        self.assertEqual(walker.topological_order(), [])

    def test_only_sub_instances_make_edges(self):
        """Инстанс-примитив не даёт ребра, даже если есть блок с тем же именем."""
        view = FakeView([
            FakeBlock("main", instance_list=[
                FakeInstance("r1", "prim", "a"),
                FakeInstance("x1", "sub", "a", loc(3))]),
            FakeBlock("a"),
        ])
        walker = HierarchyWalker(view)
        self.assertEqual(walker.children("main"), [edge("main", "x1", "a", 3)])

    def test_duplicate_block_uses_first_definition(self):
        """При двух блоках с одним именем берётся первое определение."""
        view = FakeView([
            FakeBlock("a", instance_list=[FakeInstance("x", "sub", "b", loc(2))]),
            FakeBlock("a", instance_list=[FakeInstance("y", "sub", "c", loc(5))]),
            FakeBlock("b"), FakeBlock("c"),
        ])
        walker = HierarchyWalker(view)
        self.assertEqual(walker.children("a"), [edge("a", "x", "b", 2)])
        self.assertEqual(walker.parents("c"), [])


# children / parents / missing
class ChildrenTest(unittest.TestCase):

    def test_edges_in_instance_order(self):
        """Рёбра идут в порядке инстансов, повтор типа даёт отдельное ребро."""
        walker = HierarchyWalker(hierarchy({"main": ["a", "b", "a"],
                                            "a": [], "b": []}))
        self.assertEqual(walker.children("main"), [
            edge("main", "a1", "a", 11),
            edge("main", "b2", "b", 12),
            edge("main", "a3", "a", 13),
        ])

    def test_leaf_block_has_no_children(self):
        """У блока без подсхем список пустой."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": []}))
        self.assertEqual(walker.children("a"), [])

    def test_unknown_block_gives_empty_list(self):
        """Для несуществующего блока — пустой список, а не исключение."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": []}))
        self.assertEqual(walker.children("nope"), [])

    def test_edge_to_missing_block_is_a_child(self):
        """Ребро на несуществующий блок тоже входит в children."""
        walker = HierarchyWalker(hierarchy({"main": ["ghost"]}))
        self.assertEqual(walker.children("main"), [edge("main", "ghost1", "ghost", 11)])

    def test_primitives_are_ignored(self):
        """Инстансы-примитивы в children не попадают."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": []},
                                           prims={"main": ["r", "c"]}))
        self.assertEqual([e.child for e in walker.children("main")], ["a"])


class ParentsTest(unittest.TestCase):

    def test_all_edges_into_block(self):
        """parents даёт все рёбра в блок: от разных родителей и повторные."""
        walker = HierarchyWalker(hierarchy({"main": ["a", "a"], "x": ["a"],
                                            "a": []}))
        self.assertCountEqual(walker.parents("a"), [
            edge("main", "a1", "a", 11),
            edge("main", "a2", "a", 12),
            edge("x", "a1", "a", 21),
        ])

    def test_root_has_no_parents(self):
        """У корня родителей нет."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": []}))
        self.assertEqual(walker.parents("main"), [])

    def test_parents_of_missing_block(self):
        """У несуществующего блока родители есть, если на него ссылаются."""
        walker = HierarchyWalker(hierarchy({"main": ["ghost"]}))
        self.assertEqual(walker.parents("ghost"), [edge("main", "ghost1", "ghost", 11)])

    def test_unknown_name_gives_empty_list(self):
        """Имя, на которое никто не ссылается, даёт пустой список."""
        walker = HierarchyWalker(hierarchy({"main": []}))
        self.assertEqual(walker.parents("nope"), [])


class MissingTest(unittest.TestCase):

    def test_complete_hierarchy(self):
        """Если все подсхемы существуют, missing пуст."""
        walker = HierarchyWalker(hierarchy({"main": ["a", "b"], "a": ["b"],
                                            "b": []}))
        self.assertEqual(walker.missing(), [])

    def test_edges_to_missing_blocks(self):
        """Каждое ребро на несуществующий блок попадает в missing."""
        walker = HierarchyWalker(hierarchy({"main": ["a", "ghost"],
                                            "a": ["ghost", "phantom"]}))
        self.assertCountEqual(walker.missing(), [
            edge("main", "ghost2", "ghost", 12),
            edge("a", "ghost1", "ghost", 21),
            edge("a", "phantom2", "phantom", 22),
        ])

    def test_unknown_primitive_is_not_missing(self):
        """Инстанс неизвестного примитива — не неполнота иерархии блоков."""
        walker = HierarchyWalker(hierarchy({"main": []}, prims={"main": ["zzz"]}))
        self.assertEqual(walker.missing(), [])


# Обходы
class IterDfsTest(unittest.TestCase):

    def test_root_first_with_depth_zero(self):
        """Первой парой идёт (None, 0) для корня."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": []}))
        self.assertEqual(next(iter(walker.iter_dfs("main"))), (None, 0))

    def test_chain_gives_edges_and_depths(self):
        """Цепочка main -> a -> b: рёбра с глубинами 1 и 2."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": ["b"], "b": []}))
        self.assertEqual(list(walker.iter_dfs("main")), [
            (None, 0),
            (edge("main", "a1", "a", 11), 1),
            (edge("a", "b1", "b", 21), 2),
        ])

    def test_default_root_is_main(self):
        """Корень по умолчанию — main."""
        walker = HierarchyWalker(hierarchy({"x": ["a"], "main": ["b"],
                                            "a": [], "b": []}))
        self.assertEqual(list(walker.iter_dfs()), list(walker.iter_dfs("main")))

    def test_depth_first_in_instance_order(self):
        """Обход в глубину: сначала всё поддерево a, потом b."""
        self.assertEqual(
            walk({"main": ["a", "b"], "a": ["c"], "b": [], "c": []}),
            [("main", 0), ("a", 1), ("c", 2), ("b", 1)])

    def test_each_definition_expanded_once(self):
        """Ромб main->a->d, main->b->d: блок d обходится один раз."""
        self.assertEqual(
            walk({"main": ["a", "b"], "a": ["d"], "b": ["d"], "d": []}),
            [("main", 0), ("a", 1), ("d", 2), ("b", 1)])

    def test_two_instances_of_one_block(self):
        """Два инстанса одного блока: определение раскрывается один раз."""
        self.assertEqual(walk({"main": ["a", "a"], "a": []}),
                         [("main", 0), ("a", 1)])

    def test_cycle_is_finite(self):
        """На цикле main -> a -> main обход конечен."""
        self.assertEqual(walk({"main": ["a"], "a": ["main"]}),
                         [("main", 0), ("a", 1)])

    def test_self_loop_is_finite(self):
        """Блок, содержащий сам себя, не раскрывается повторно."""
        self.assertEqual(walk({"main": ["main"]}), [("main", 0)])

    def test_other_root(self):
        """Обход от заданного корня видит только его поддерево."""
        self.assertEqual(walk({"main": ["a"], "a": ["b"], "b": []}, root="a"),
                         [("a", 0), ("b", 1)])

    def test_unknown_root_gives_nothing(self):
        """Несуществующий корень: обход пустой."""
        self.assertEqual(walk({"main": []}, root="nope"), [])

    def test_deep_hierarchy_without_recursion(self):
        """Цепочка глубже предела рекурсии Python обходится без ошибок."""
        depth = 1500  # больше sys.getrecursionlimit() по умолчанию (1000)
        spec = {f"b{i}": [f"b{i + 1}"] for i in range(depth)}
        spec[f"b{depth}"] = []
        result = walk(spec, root="b0")
        self.assertEqual(len(result), depth + 1)
        self.assertEqual(result[-1], (f"b{depth}", depth))


class ReachableTest(unittest.TestCase):

    def test_root_alone(self):
        """Корень без подсхем: множество из одного корня."""
        walker = HierarchyWalker(hierarchy({"main": []}))
        self.assertEqual(walker.reachable("main"), {"main"})

    def test_tree_and_diamond(self):
        """Все блоки поддерева, включая корень; общий блок — один раз."""
        walker = HierarchyWalker(hierarchy({"main": ["a", "b"], "a": ["d"],
                                            "b": ["d"], "d": []}))
        self.assertEqual(walker.reachable(), {"main", "a", "b", "d"})

    def test_unreachable_block_excluded(self):
        """Блок, на который никто не ссылается, недостижим."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": [],
                                            "orphan": ["a"]}))
        self.assertEqual(walker.reachable("main"), {"main", "a"})

    def test_from_other_root(self):
        """От другого корня достижимо только его поддерево."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": ["b"], "b": []}))
        self.assertEqual(walker.reachable("a"), {"a", "b"})

    def test_with_cycle(self):
        """Цикл не мешает: каждый блок цикла достижим."""
        walker = HierarchyWalker(hierarchy({"main": ["a"], "a": ["main"]}))
        self.assertEqual(walker.reachable(), {"main", "a"})

    def test_missing_block_is_not_reachable(self):
        """Несуществующий блок не входит в достижимые: это не блок схемы."""
        walker = HierarchyWalker(hierarchy({"main": ["ghost"]}))
        self.assertEqual(walker.reachable(), {"main"})


class FindCyclesTest(unittest.TestCase):

    def cycles(self, spec):
        return HierarchyWalker(hierarchy(spec)).find_cycles()

    def test_tree_has_no_cycles(self):
        """В дереве циклов нет."""
        self.assertEqual(self.cycles({"main": ["a", "b"], "a": [], "b": []}), [])

    def test_diamond_is_not_a_cycle(self):
        """Ромб (две дороги к одному блоку) — это не цикл."""
        self.assertEqual(self.cycles({"main": ["a", "b"], "a": ["d"],
                                      "b": ["d"], "d": []}), [])

    def test_self_loop(self):
        """Блок внутри самого себя: цикл [a, a]."""
        self.assertEqual(self.cycles({"main": ["a"], "a": ["a"]}), [["a", "a"]])

    def test_two_block_cycle_starts_with_min_name(self):
        """Цикл b -> a -> b записывается с минимального имени: [a, b, a]."""
        self.assertEqual(self.cycles({"main": ["b"], "b": ["a"], "a": ["b"]}),
                         [["a", "b", "a"]])

    def test_three_block_cycle_keeps_direction(self):
        """Цикл c -> a -> b -> c: начинается с a, направление сохраняется."""
        self.assertEqual(self.cycles({"c": ["a"], "a": ["b"], "b": ["c"]}),
                         [["a", "b", "c", "a"]])

    def test_first_and_last_names_equal(self):
        """У каждого цикла первое и последнее имя совпадают."""
        for cycle in self.cycles({"main": ["a", "c"], "a": ["b"], "b": ["a"],
                                  "c": ["c"]}):
            self.assertEqual(cycle[0], cycle[-1])

    def test_unreachable_cycle_is_found(self):
        """Цикл вне поддерева main тоже находится."""
        self.assertEqual(self.cycles({"main": [], "x": ["y"], "y": ["x"]}),
                         [["x", "y", "x"]])

    def test_two_independent_cycles(self):
        """Два независимых цикла — два элемента списка."""
        self.assertCountEqual(
            self.cycles({"main": ["a", "c"], "a": ["b"], "b": ["a"],
                         "c": ["d"], "d": ["c"]}),
            [["a", "b", "a"], ["c", "d", "c"]])

    def test_cycle_entered_from_several_blocks_reported_once(self):
        """Цикл, в который входят из разных блоков, выдаётся один раз."""
        self.assertEqual(
            self.cycles({"x": ["a"], "y": ["b"], "a": ["b"], "b": ["a"]}),
            [["a", "b", "a"]])

    def test_missing_block_does_not_break_search(self):
        """Ссылка на несуществующий блок не мешает и не считается циклом."""
        self.assertEqual(self.cycles({"main": ["ghost", "a"], "a": []}), [])


class TopologicalOrderTest(unittest.TestCase):

    def order(self, spec):
        return HierarchyWalker(hierarchy(spec)).topological_order()

    def test_chain(self):
        """Цепочка main -> a -> b: порядок [b, a, main]."""
        self.assertEqual(self.order({"main": ["a"], "a": ["b"], "b": []}),
                         ["b", "a", "main"])

    def test_child_before_parent_in_diamond(self):
        """В ромбе каждая подсхема стоит раньше использующего её блока."""
        spec = {"main": ["a", "b"], "a": ["d"], "b": ["d"], "d": []}
        order = self.order(spec)
        for parent, children in spec.items():
            for child in children:
                self.assertLess(order.index(child), order.index(parent),
                                f"{child} должен стоять раньше {parent}")

    def test_every_block_exactly_once(self):
        """Каждый блок ровно один раз, в том числе недостижимые из main."""
        order = self.order({"main": ["a"], "a": [], "orphan": ["a"],
                            "lonely": []})
        self.assertCountEqual(order, ["main", "a", "orphan", "lonely"])

    def test_missing_blocks_not_included(self):
        """Несуществующие блоки в порядок не попадают."""
        self.assertEqual(self.order({"main": ["ghost"]}), ["main"])

    def test_cycle_raises_with_cycle(self):
        """При цикле бросается HierarchyCycleError с путём цикла."""
        with self.assertRaises(HierarchyCycleError) as caught:
            self.order({"main": ["b"], "b": ["a"], "a": ["b"]})
        self.assertEqual(caught.exception.cycle, ["a", "b", "a"])

    def test_self_loop_raises(self):
        """Блок внутри самого себя — тоже цикл."""
        with self.assertRaises(HierarchyCycleError) as caught:
            self.order({"main": ["main"]})
        self.assertEqual(caught.exception.cycle, ["main", "main"])


# validate
def _diag(code: str, check: str) -> Diagnostic:
    return Diagnostic(code, Severity.ERROR, check, f"тест {code}", loc(1))


def _recording_check_module(name: str, result: list[Diagnostic]):
    """Заглушка модуля проверки: check() запоминает аргументы и отдаёт result."""
    module = types.ModuleType(name)
    module.calls = []

    def check(view, opts=None, **kw):
        module.calls.append((view, opts, kw))
        return list(result)

    module.check = check
    return module


class ValidateTest(unittest.TestCase):

    def setUp(self) -> None:
        self.h_diag = _diag("E201", "hierarchy")
        self.c_diag = _diag("E301", "cycle")
        self.hierarchy_check = _recording_check_module(
            "fake_hierarchy_check", [self.h_diag])
        self.cycle_check = _recording_check_module("fake_cycle_check",
                                                   [self.c_diag])
        patcher = replaced_modules({
            "netlist.checker.hierarchy_check": self.hierarchy_check,
            "netlist.checker.cycle_check": self.cycle_check,
        })
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.view = hierarchy({"main": ["a"], "a": []})
        self.walker = HierarchyWalker(self.view)

    def test_result_is_hierarchy_then_cycle(self):
        """Результат — ошибки HierarchyCheck, затем ошибки CycleCheck."""
        self.assertEqual(self.walker.validate(CheckOptions()),
                         [self.h_diag, self.c_diag])

    def test_each_check_called_once_with_view_and_self(self):
        """Каждая проверка вызвана один раз с тем же view и walker=self."""
        self.walker.validate(CheckOptions())
        for module in (self.hierarchy_check, self.cycle_check):
            with self.subTest(module.__name__):
                self.assertEqual(len(module.calls), 1)
                view, _, kw = module.calls[0]
                self.assertIs(view, self.view)
                self.assertIs(kw.get("walker"), self.walker)

    def test_given_options_passed_through(self):
        """Переданные opts доходят до обеих проверок без изменений."""
        opts = CheckOptions(top="root", strict_primitives=False)
        self.walker.validate(opts)
        self.assertIs(self.hierarchy_check.calls[0][1], opts)
        self.assertIs(self.cycle_check.calls[0][1], opts)

    def test_default_options_when_none(self):
        """Без opts проверки получают CheckOptions() по умолчанию."""
        self.walker.validate()
        self.assertEqual(self.hierarchy_check.calls[0][1], CheckOptions())
        self.assertEqual(self.cycle_check.calls[0][1], CheckOptions())

    def test_no_problems_gives_empty_list(self):
        """Если обе проверки ничего не нашли, результат пустой."""
        self.hierarchy_check.check = lambda view, opts=None, **kw: []
        self.cycle_check.check = lambda view, opts=None, **kw: []
        self.assertEqual(self.walker.validate(), [])


if __name__ == "__main__":
    unittest.main()
