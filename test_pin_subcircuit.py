"""Юнит-тесты PinCheck и SubcircuitCheck.

Запуск из корня репозитория:
    python3 test_pin_subcircuit.py              (краткий отчёт)
    python3 -m unittest test_pin_subcircuit -v  (подробно)

Проверяются настоящие модули src/checker/pin_check.py и
src/checker/subcircuit_check.py. Нетлист собирается из заглушек
tests/core/fakes.py (минимальная реализация протокола NetlistView,
docs/interfaces.md, п. 2.1), как в остальных тестах чекера. Типы
Severity, CheckOptions, PinRef и ModelIndex берутся настоящие.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field

from src.checker import pin_check as PIN
from src.checker import subcircuit_check as SUB
from src.checker.model_utils import ModelIndex
from src.core.model import CheckOptions, PinRef, Severity, SourceLoc
from tests.core.fakes import (
    FakeBlock,
    FakeInstance,
    FakePin,
    FakePrimitive,
    FakeView,
    loc,
)


@dataclass
class FakeOrphan:
    """Строка INST.PIN с необъявленным INST (OrphanConnection)."""
    ref: PinRef
    net: str | None = None
    loc: SourceLoc = field(default_factory=lambda: loc(None))


@dataclass
class OrphanBlock(FakeBlock):
    """FakeBlock, у которого можно задать orphan_connections()."""
    orphan_list: list[FakeOrphan] = field(default_factory=list)

    def orphan_connections(self) -> list[FakeOrphan]:
        return list(self.orphan_list)


Primitive = FakePrimitive


def Orphan(ref, where):
    return FakeOrphan(ref, None, where)


def Ref(instance, pin_name):
    return PinRef(instance, pin_name)


def pin(name, net, line=None):
    return FakePin(name, net, loc(line))


def inst(name, kind, type_name, pins=(), line=None):
    return FakeInstance(name, kind, type_name, loc(line), list(pins))


def block(name="main", pins=(), instances=(), orphans=()):
    return OrphanBlock(name, pin_list=list(pins),
                       instance_list=list(instances),
                       orphan_list=list(orphans))


def model(blocks=(), primitives=()):
    return FakeView(list(blocks), list(primitives))


def codes(diags):
    return [d.code for d in diags]


class PinCheckTests(unittest.TestCase):
    def test_01_name_and_codes(self):
        self.assertEqual(PIN.NAME, "pin")
        self.assertEqual(set(PIN.CODES), {"E501", "E502", "E503", "W504"})

    def test_02_empty_netlist(self):
        self.assertEqual(PIN.check(model()), [])

    def test_03_connected_external_pin(self):
        self.assertEqual(PIN.check(model([block(pins=[pin("a", "n1", 2)])])), [])

    def test_04_unconnected_external_pin(self):
        ds = PIN.check(model([block(pins=[pin("a", None, 5)])]))
        self.assertEqual(codes(ds), ["E501"])
        self.assertEqual((ds[0].loc.line, ds[0].obj), (5, "a"))
        self.assertEqual(ds[0].severity, Severity.ERROR)

    def test_05_unconnected_primitive_pin(self):
        x = inst("t1", "prim", "R", [pin("A", None, 8)])
        ds = PIN.check(model([block(instances=[x])], [Primitive("R", ["A"])]))
        self.assertEqual(codes(ds), ["E502"])
        self.assertEqual(ds[0].obj, "t1.A")

    def test_06_connected_instance_pin(self):
        x = inst("t1", "prim", "R", [pin("A", "n1", 8)])
        self.assertEqual(PIN.check(model([block(instances=[x])])), [])

    def test_07_unconnected_sub_pin_with_internal_net(self):
        child = block("child", pins=[pin("a", "inside", 2)])
        parent = block(instances=[inst("u1", "sub", "child",
                                       [pin("a", None, 9)])])
        ds = PIN.check(model([child, parent]))
        self.assertEqual(codes(ds), ["E502", "W504"])
        self.assertEqual([d.loc.line for d in ds], [9, 9])
        self.assertEqual(ds[1].severity, Severity.WARNING)

    def test_08_sub_pin_without_internal_net_has_no_warning(self):
        child = block("child", pins=[pin("a", None, 2)])
        x = inst("u1", "sub", "child", [pin("a", None, 9)])
        self.assertEqual(codes(PIN.check(model([child, block(instances=[x])]))),
                         ["E501", "E502"])

    def test_09_unknown_sub_does_not_create_warning(self):
        x = inst("u1", "sub", "missing", [pin("a", None, 3)])
        self.assertEqual(codes(PIN.check(model([block(instances=[x])]))),
                         ["E502"])

    def test_10_repeat_external_connected(self):
        b = block(pins=[pin("a", "n1", 2), pin("a", "n2", 7)])
        ds = PIN.check(model([b]))
        self.assertEqual(codes(ds), ["E503"])
        self.assertEqual(ds[0].loc.line, 7)
        self.assertIn("строка 2", ds[0].message)

    def test_11_repeat_external_uses_first_mention(self):
        b = block(pins=[pin("a", None, 2), pin("a", "n1", 7)])
        self.assertEqual(codes(PIN.check(model([b]))), ["E501", "E503"])

    def test_12_repeat_instance_pin(self):
        x = inst("t1", "prim", "R", [pin("A", "n1", 3),
                                          pin("A", None, 8)])
        self.assertEqual(codes(PIN.check(model([block(instances=[x])]))),
                         ["E503"])

    def test_13_sort_diagnostics_by_line(self):
        b = block(pins=[pin("a", None, 20)], instances=[
            inst("t1", "prim", "R", [pin("p", None, 3)])])
        self.assertEqual([d.loc.line for d in PIN.check(model([b]))], [3, 20])

    def test_14_max_one_error(self):
        b = block(pins=[pin("a", None, 3), pin("b", None, 5)])
        self.assertEqual(codes(PIN.check(model([b]), CheckOptions(max_errors=1))), ["E501"])

    def test_15_max_none_is_unlimited(self):
        b = block(pins=[pin("a", None), pin("b", None)])
        self.assertEqual(len(PIN.check(model([b]), CheckOptions(max_errors=None))), 2)

    def test_16_max_zero_stops_immediately(self):
        # «0 — без ограничения» переводит в None CLI (cli.py), а сама
        # проверка по контракту понимает 0 буквально: ни одной ошибки.
        b = block(pins=[pin("a", None)])
        self.assertEqual(codes(PIN.check(model([b]), CheckOptions(max_errors=0))), [])

    def test_17_extra_options_ignored(self):
        self.assertEqual(PIN.check(model(), walker=object(), other=True), [])

    def test_18_input_is_not_modified(self):
        p = pin("a", None, 7)
        v = model([block(pins=[p])])
        PIN.check(v)
        self.assertEqual((p.name, p.net, p.loc.line), ("a", None, 7))

    def test_19_definition_cache_and_first_pin(self):
        child = block("child", pins=[pin("a", "n1"), pin("a", "n2")])
        v = model([child])
        cache = PIN._DefinitionNets(v)
        x = inst("i", "sub", "child")
        first = cache.get(x)
        self.assertEqual(first, {"a": "n1"})
        self.assertIs(first, cache.get(x))

    def test_20_definition_cache_none(self):
        cache = PIN._DefinitionNets(model())
        self.assertIsNone(cache.get(inst("i", "prim", "R")))
        self.assertIsNone(cache.get(inst("i", "sub", "unknown")))

    def test_21_helpers(self):
        self.assertEqual(PIN._line_text(loc(7)), "строка 7")
        self.assertEqual(PIN._line_text(loc(None)), "без номера строки")
        a = PIN._diag("E501", Severity.ERROR, "x", loc(1), "main", "a")
        self.assertEqual((a.check, a.obj), ("pin", "a"))
        self.assertEqual(PIN._sorted_by_line([a]), [a])

    def test_22_warning_does_not_count_as_error_limit(self):
        child = block("child", pins=[pin("a", "n1")])
        main = block(instances=[inst("u", "sub", "child",
                                     [pin("a", None, 5)])])
        ds = PIN.check(model([child, main]), CheckOptions(max_errors=1))
        # W504 не считается ошибкой и уточняет E502 на той же строке,
        # поэтому лимит в одну ошибку её не отрезает (ревью, замечание 1).
        self.assertEqual(codes(ds), ["E502", "W504"])

    # E503 для повтора пина в строке `primitive` (ревью, замечание 4)
    def test_23_repeat_in_primitive_declaration(self):
        ds = PIN.check(model([], [Primitive("R", ["A", "A"], loc(1))]))
        self.assertEqual(codes(ds), ["E503"])
        d = ds[0]
        self.assertEqual(d.severity, Severity.ERROR)
        self.assertEqual((d.loc.line, d.block, d.obj), (1, "", "R.A"))
        self.assertIn("'R'", d.message)
        self.assertIn("'A'", d.message)

    def test_24_one_error_per_extra_mention(self):
        p = Primitive("R", ["A", "B", "A", "A", "B"], loc(1))
        self.assertEqual([d.obj for d in PIN.check(model([], [p]))],
                         ["R.A", "R.A", "R.B"])

    def test_25_primitive_without_repeats(self):
        p = Primitive("R", ["A", "B"], loc(1))
        self.assertEqual(PIN.check(model([], [p])), [])

    def test_26_repeat_in_library_primitive(self):
        p = Primitive("R", ["A", "A"], loc(3, "lib.net"))
        ds = PIN.check(model([], [p]))
        self.assertEqual([(d.code, d.loc) for d in ds],
                         [("E503", SourceLoc("lib.net", 3))])

    def test_27_primitives_go_first_and_count_to_limit(self):
        p = Primitive("R", ["A", "A"], loc(1))
        b = block(pins=[pin("x", None, 3)])
        self.assertEqual(codes(PIN.check(model([b], [p]))), ["E503", "E501"])
        self.assertEqual(codes(PIN.check(model([b], [p]),
                                         CheckOptions(max_errors=1))),
                         ["E503"])

    def test_28_instance_of_such_primitive_is_not_blamed(self):
        p = Primitive("R", ["A", "A"], loc(1))
        x = inst("t1", "prim", "R", [pin("A", "n1", 5)], 4)
        v = model([block(instances=[x])], [p])
        self.assertEqual(codes(PIN.check(v)), ["E503"])
        self.assertEqual(SUB.check(v), [])

    def test_29_limit_keeps_w504_but_cuts_next_errors(self):
        # За парой E502+W504 на строке 5 идут ещё ошибки (строки 6 и 8):
        # лимит сохраняет W504 своей E502, но всё дальше отрезает.
        child = block("child", pins=[pin("a", "n1")])
        main = block(pins=[pin("p", None, 6)], instances=[
            inst("u1", "sub", "child", [pin("a", None, 5)]),
            inst("u2", "sub", "child", [pin("a", None, 8)])])
        v = model([child, main])
        cases = {1: [("E502", 5), ("W504", 5)],
                 2: [("E502", 5), ("W504", 5), ("E501", 6)],
                 3: [("E502", 5), ("W504", 5), ("E501", 6),
                     ("E502", 8), ("W504", 8)]}
        for limit, expected in cases.items():
            with self.subTest(max_errors=limit):
                ds = PIN.check(v, CheckOptions(max_errors=limit))
                self.assertEqual([(d.code, d.loc.line) for d in ds],
                                 expected)


class SubcircuitCheckTests(unittest.TestCase):
    def test_01_name_and_codes(self):
        self.assertEqual(SUB.NAME, "subckt")
        self.assertEqual(set(SUB.CODES),
                         {"E401", "E402", "E403", "E404", "E405"})

    def test_02_empty_netlist(self):
        self.assertEqual(SUB.check(model()), [])

    def test_03_matching_primitive(self):
        p = Primitive("R", ["A", "B"])
        x = inst("t1", "prim", "R", [pin("A", "n1"), pin("B", "n2")])
        self.assertEqual(SUB.check(model([block(instances=[x])], [p])), [])

    def test_04_missing_pin_e401(self):
        p = Primitive("R", ["A", "B"])
        x = inst("t1", "prim", "R", [pin("A", "n1")], 9)
        ds = SUB.check(model([block(instances=[x])], [p]))
        self.assertEqual(codes(ds), ["E401"])
        self.assertEqual((ds[0].loc.line, ds[0].obj), (9, "t1.B"))

    def test_05_extra_pin_e402(self):
        p = Primitive("R", ["A"])
        x = inst("t1", "prim", "R", [pin("A", "n1"), pin("X", "n2", 8)])
        ds = SUB.check(model([block(instances=[x])], [p]))
        self.assertEqual(codes(ds), ["E402"])
        self.assertEqual((ds[0].loc.line, ds[0].obj), (8, "t1.X"))

    def test_06_multiple_missing_pins_in_definition_order(self):
        p = Primitive("R", ["A", "B", "C"])
        x = inst("t1", "prim", "R", [], 3)
        self.assertEqual([d.obj for d in SUB.check(model([block(instances=[x])], [p]))],
                         ["t1.A", "t1.B", "t1.C"])

    def test_07_wrong_kind_sub_is_primitive(self):
        x = inst("u", "sub", "R", [], 5)
        self.assertEqual(codes(SUB.check(model([block(instances=[x])],
                                               [Primitive("R", ["A"])]))),
                         ["E403"])

    def test_08_wrong_kind_primitive_is_sub(self):
        child = block("child", pins=[pin("A", "n1")])
        x = inst("u", "prim", "child", [], 5)
        self.assertEqual(codes(SUB.check(model([child, block(instances=[x])]))),
                         ["E403"])

    def test_09_unknown_type_handled_elsewhere(self):
        x = inst("u", "sub", "unknown", [], 5)
        self.assertEqual(SUB.check(model([block(instances=[x])])), [])

    def test_10_duplicate_instance_e404(self):
        a = inst("u", "prim", "R", [], 2)
        b = inst("u", "prim", "R", [], 8)
        ds = SUB.check(model([block(instances=[a, b])]))
        self.assertEqual(codes(ds), ["E404"])
        self.assertEqual(ds[0].loc.line, 8)
        self.assertIn("строка 2", ds[0].message)

    def test_11_orphan_connection_e405(self):
        o = Orphan(Ref("ghost", "A"), loc(12))
        ds = SUB.check(model([block(orphans=[o])]))
        self.assertEqual(codes(ds), ["E405"])
        self.assertEqual((ds[0].loc.line, ds[0].obj), (12, "ghost.A"))

    def test_12_duplicate_pin_not_double_reported(self):
        p = Primitive("R", ["A"])
        x = inst("t1", "prim", "R", [pin("A", "n1"), pin("A", "n2")])
        self.assertEqual(SUB.check(model([block(instances=[x])], [p])), [])

    def test_13_duplicate_instance_skips_missing_pin_for_duplicate(self):
        p = Primitive("R", ["A"])
        a = inst("u", "prim", "R", [pin("A", "n1")], 2)
        b = inst("u", "prim", "R", [], 9)
        self.assertEqual(codes(SUB.check(model([block(instances=[a, b])], [p]))),
                         ["E404"])

    def test_14_sort_by_location(self):
        p = Primitive("R", ["A"])
        x = inst("u", "prim", "R", [], 20)
        o = Orphan(Ref("ghost", "A"), loc(3))
        ds = SUB.check(model([block(instances=[x], orphans=[o])], [p]))
        self.assertEqual([d.loc.line for d in ds], [3, 20])

    def test_15_max_one_error(self):
        p = Primitive("R", ["A", "B"])
        x = inst("u", "prim", "R", [], 7)
        self.assertEqual(codes(SUB.check(
            model([block(instances=[x])], [p]), CheckOptions(max_errors=1))), ["E401"])

    def test_16_max_zero_stops_immediately(self):
        # См. PinCheckTests.test_16: 0 в None переводит CLI, не проверка.
        p = Primitive("R", ["A"])
        x = inst("u", "prim", "R", [], 7)
        self.assertEqual(codes(SUB.check(
            model([block(instances=[x])], [p]), CheckOptions(max_errors=0))), [])

    def test_17_distinct_types_same_name_have_correct_kind(self):
        child = block("R", pins=[pin("x", "n")])
        p = Primitive("R", ["A"])
        x = inst("u", "sub", "R", [pin("x", "n")], 7)
        self.assertEqual(SUB.check(model([child, block(instances=[x])], [p])), [])

    def test_18_uses_supplied_index(self):
        v = model([block(instances=[inst("u", "sub", "R")])],
                  [Primitive("R", [])])
        index = ModelIndex(v)
        self.assertEqual(codes(SUB.check(v, index=index, walker=object())),
                         ["E403"])

    def test_19_input_is_not_modified(self):
        x = inst("u", "prim", "R", [], 7)
        v = model([block(instances=[x])], [Primitive("R", ["A"])])
        SUB.check(v)
        self.assertEqual((x.name, x.kind, x.type_name, x.pin_list),
                         ("u", "prim", "R", []))

    def test_20_definition_cache(self):
        v = model([], [Primitive("R", ["A", "B", "A"])])
        cache = SUB._DefinitionPins(v)
        x = inst("u", "prim", "R")
        self.assertEqual(list(cache.get(x)), ["A", "B"])
        self.assertIs(cache.get(x), cache.get(x))
        self.assertIsNone(cache.get(inst("v", "prim", "missing")))

    def test_21_helpers(self):
        self.assertEqual(SUB._line_text(loc(7)), "строка 7")
        self.assertEqual(SUB._line_text(loc(None)), "без номера строки")
        d = SUB._error("E401", "x", loc(4), "main", "u")
        self.assertEqual((d.check, d.severity), ("subckt", Severity.ERROR))
        self.assertEqual(SUB._sorted_by_line([d]), [d])

    def test_22_no_mutation_of_shared_default_options(self):
        opts = SUB.check.__defaults__[0]
        previous = opts.max_errors
        SUB.check(model())
        self.assertEqual(opts.max_errors, previous)


class CompactReport(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.successes = 0

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes += 1


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(resultclass=CompactReport,
                                     verbosity=0)
    result = runner.run(suite)
    errors = len(result.errors) + len(result.failures)
    print("МОДУЛЬНЫЕ ТЕСТЫ")
    print("Модули: pin_check, subcircuit_check")
    print(f"\033[92mУСПЕШНО: {result.successes}\033[0m")
    print(f"\033[91mОШИБКИ: {errors}\033[0m")
    print(f"\033[93mПРОПУЩЕНО: {len(result.skipped)}\033[0m")
    print("ИТОГ: " + ("ВЫПОЛНЕННЫЕ ТЕСТЫ ПРОЙДЕНЫ"
                      if result.wasSuccessful() else "ЕСТЬ ОШИБКИ"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
