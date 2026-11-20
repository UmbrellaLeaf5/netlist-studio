"""Юнит-тесты NetlistSerializer (netlist/core/serializer.py, автор Н. Бучилкин).

Контракт: docs/interfaces.md, п. 2.3. Сериализатор читает модель только
через методы primitives(), blocks(), pins(), nets(), instances(), поэтому
вместо настоящего Netlist ему даётся простая заглушка FakeView. Ожидаемый
текст в тестах выписан целиком, его можно сверить глазами.

Отдельный класс RoundTripTest проверяет гарантию обратимости из
контракта на настоящих Netlist и CheckerDraft (лексика SyntaxCheck там
тоже заменена заглушкой). Это уже не изоляция, а проверка связки, но
контракт прямо требует такого теста.

Запуск из корня репозитория:
    python -m unittest tests.core.test_serializer -v
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack

from netlist.core.model import InstanceKind
from netlist.core.serializer import NetlistSerializer
from tests.core.fakes import (
    FakeBlock,
    FakeInstance,
    FakeNet,
    FakePin,
    FakePrimitive,
    FakeView,
    StubSyntaxCheck,
    make_syntax_module,
    replaced_modules,
)


def amp_block() -> FakeBlock:
    """Блок amp со всеми видами строк (см. AMP_LINES)."""
    return FakeBlock(
        "amp",
        pin_list=[FakePin("in", "n1"), FakePin("out", None)],
        net_list=[FakeNet("n1"), FakeNet("n2")],
        instance_list=[
            FakeInstance("r1", "prim", "r",
                         pin_list=[FakePin("a", "n1"), FakePin("b", "n2")]),
            FakeInstance("c1", "prim", "c",
                         pin_list=[FakePin("p", "n2"), FakePin("n", None)]),
        ],
    )


AMP_LINES = [
    "block amp",
    "  pin in n1",
    "  pin out -",
    "  net n1",
    "  net n2",
    "  inst r1 prim r",
    "  r1.a n1",
    "  r1.b n2",
    "  inst c1 prim c",
    "  c1.p n2",
    "  c1.n -",
    "end",
]


def main_block() -> FakeBlock:
    return FakeBlock(
        "main",
        net_list=[FakeNet("x")],
        instance_list=[FakeInstance(
            "u1", "sub", "amp",
            pin_list=[FakePin("in", "x"), FakePin("out", "x")])],
    )


MAIN_LINES = [
    "block main",
    "  net x",
    "  inst u1 sub amp",
    "  u1.in x",
    "  u1.out x",
    "end",
]


def full_model() -> FakeView:
    return FakeView(
        blocks=[amp_block(), main_block()],
        primitives=[FakePrimitive("r", ["a", "b"]),
                    FakePrimitive("c", ["p", "n"])],
    )


FULL_TEXT = "\n".join([
    "primitive r a b",
    "primitive c p n",
    "",
    *AMP_LINES,
    "",
    *MAIN_LINES,
]) + "\n"


# serialize
class SerializeTest(unittest.TestCase):

    def test_full_model_exact_text(self):
        """Модель с примитивами и двумя блоками даёт ровно ожидаемый текст."""
        self.assertEqual(NetlistSerializer().serialize(full_model()), FULL_TEXT)

    def test_primitives_then_blank_line_then_blocks(self):
        """Сначала все примитивы, затем пустая строка, затем блоки."""
        lines = NetlistSerializer().serialize(full_model()).split("\n")
        self.assertEqual(lines[:4], ["primitive r a b", "primitive c p n",
                                     "", "block amp"])

    def test_blocks_separated_by_one_blank_line_in_model_order(self):
        """Блоки идут в порядке модели и разделены одной пустой строкой."""
        model = FakeView(blocks=[FakeBlock("z"), FakeBlock("a"), FakeBlock("m")])
        self.assertEqual(NetlistSerializer().serialize(model),
                         "block z\nend\n\nblock a\nend\n\nblock m\nend\n")

    def test_without_primitives_text_starts_with_block(self):
        """Без примитивов текст начинается сразу с `block`, без пустой строки."""
        model = FakeView(blocks=[main_block()])
        self.assertEqual(NetlistSerializer().serialize(model),
                         "\n".join(MAIN_LINES) + "\n")

    def test_only_primitives(self):
        """Модель только из примитивов: строки primitive и перевод строки в конце."""
        model = FakeView(primitives=[FakePrimitive("r", ["a", "b"])])
        self.assertEqual(NetlistSerializer().serialize(model),
                         "primitive r a b\n")

    def test_primitive_without_pins(self):
        """Примитив без пинов выводится как `primitive NAME`."""
        model = FakeView(primitives=[FakePrimitive("gnd", [])])
        self.assertEqual(NetlistSerializer().serialize(model), "primitive gnd\n")

    def test_empty_model(self):
        """Пустая модель даёт текст без строк primitive и block, с \\n в конце."""
        result = NetlistSerializer().serialize(FakeView())
        self.assertTrue(result.endswith("\n"))
        self.assertEqual(result.strip(), "")

    def test_ends_with_single_newline_and_uses_lf(self):
        """Перевод строки — \\n (без \\r), в конце файла ровно один \\n."""
        result = NetlistSerializer().serialize(full_model())
        self.assertNotIn("\r", result)
        self.assertTrue(result.endswith("end\n"))
        self.assertFalse(result.endswith("\n\n"))

    def test_header_single_line(self):
        """Заголовок выводится комментарием `# ...` в самом начале файла."""
        result = NetlistSerializer().serialize(full_model(), header="схема усилителя")
        self.assertTrue(result.startswith("# схема усилителя\n"))
        self.assertIn(FULL_TEXT, result)

    def test_header_multiline(self):
        """Многострочный заголовок: каждая его строка — отдельный комментарий."""
        result = NetlistSerializer().serialize(
            FakeView(blocks=[FakeBlock("main")]), header="строка 1\nстрока 2")
        self.assertTrue(result.startswith("# строка 1\n# строка 2\n"))
        self.assertTrue(result.endswith("block main\nend\n"))

    def test_no_header_means_no_comment(self):
        """Без заголовка комментариев в тексте нет."""
        for header in (None, ""):
            with self.subTest(header=header):
                result = NetlistSerializer().serialize(full_model(), header=header)
                self.assertEqual(result, FULL_TEXT)

    def test_deterministic(self):
        """Результат детерминирован: повторный вызов и другой объект дают то же."""
        model = full_model()
        first = NetlistSerializer().serialize(model)
        self.assertEqual(NetlistSerializer().serialize(model), first)
        self.assertEqual(NetlistSerializer().serialize(full_model()), first)

    def test_model_is_not_changed(self):
        """Сериализация не меняет модель: повторный вывод совпадает с эталоном."""
        model = full_model()
        NetlistSerializer().serialize(model, header="h")
        self.assertEqual(NetlistSerializer().serialize(model), FULL_TEXT)

    def test_instance_kind_enum_written_as_word(self):
        """Вид инстанса из модели (InstanceKind) пишется как `sub` / `prim`."""
        block = FakeBlock("main", instance_list=[
            FakeInstance("u1", InstanceKind.SUB, "amp"),
            FakeInstance("r1", InstanceKind.PRIM, "r"),
        ])
        self.assertEqual(NetlistSerializer().serialize_block(block),
                         ["block main", "  inst u1 sub amp",
                          "  inst r1 prim r", "end"])


# serialize_block
class SerializeBlockTest(unittest.TestCase):

    def test_full_block_exact_lines(self):
        """Блок со всеми видами строк даёт ровно ожидаемый список строк."""
        self.assertEqual(NetlistSerializer().serialize_block(amp_block()),
                         AMP_LINES)

    def test_empty_block(self):
        """Пустой блок — это только `block NAME` и `end`."""
        self.assertEqual(NetlistSerializer().serialize_block(FakeBlock("e")),
                         ["block e", "end"])

    def test_order_pins_nets_then_instances(self):
        """Порядок внутри блока: pin, потом net, потом inst со своими пинами."""
        lines = NetlistSerializer().serialize_block(amp_block())
        kinds = [line.split()[0] if " " in line.strip() else line.strip()
                 for line in lines]
        self.assertEqual(kinds, ["block", "pin", "pin", "net", "net",
                                 "inst", "r1.a", "r1.b",
                                 "inst", "c1.p", "c1.n", "end"])

    def test_instance_pins_follow_their_instance(self):
        """Строки пинов инстанса стоят сразу после его строки `inst`."""
        lines = NetlistSerializer().serialize_block(amp_block())
        i = lines.index("  inst c1 prim c")
        self.assertEqual(lines[i + 1:i + 3], ["  c1.p n2", "  c1.n -"])

    def test_unconnected_pin_written_as_dash(self):
        """Неподключённый пин (net=None) пишется как `-`."""
        block = FakeBlock("b", pin_list=[FakePin("p", None)], instance_list=[
            FakeInstance("x", "sub", "leaf", pin_list=[FakePin("a", None)])])
        self.assertEqual(NetlistSerializer().serialize_block(block),
                         ["block b", "  pin p -", "  inst x sub leaf",
                          "  x.a -", "end"])

    def test_instance_without_pins(self):
        """Инстанс без пинов даёт одну строку `inst`."""
        block = FakeBlock("b", instance_list=[FakeInstance("x", "sub", "leaf")])
        self.assertEqual(NetlistSerializer().serialize_block(block),
                         ["block b", "  inst x sub leaf", "end"])

    def test_items_have_no_newlines(self):
        """Элементы списка — отдельные строки без символов перевода строки."""
        for line in NetlistSerializer().serialize_block(amp_block()):
            self.assertNotIn("\n", line)

    def test_serialize_contains_serialize_block(self):
        """В полном тексте блок выглядит ровно как его serialize_block."""
        serializer = NetlistSerializer()
        self.assertIn("\n".join(serializer.serialize_block(amp_block())),
                      serializer.serialize(full_model()))


# Конструктор: отступ
class IndentTest(unittest.TestCase):

    def test_default_indent_is_two_spaces(self):
        """По умолчанию строки внутри блока имеют отступ в два пробела."""
        self.assertEqual(NetlistSerializer().serialize_block(main_block())[1],
                         "  net x")

    def test_custom_indent(self):
        """Заданный отступ (табуляция) применяется ко всем строкам внутри блока."""
        lines = NetlistSerializer("\t").serialize_block(main_block())
        self.assertEqual(lines, ["block main", "\tnet x", "\tinst u1 sub amp",
                                 "\tu1.in x", "\tu1.out x", "end"])

    def test_custom_indent_on_every_kind_of_line(self):
        """Заданный отступ стоит перед каждой строкой pin, net, inst и пинов инстанса."""
        lines = NetlistSerializer("\t").serialize_block(amp_block())
        self.assertEqual(lines, [lines[0]]
                         + ["\t" + line.strip() for line in AMP_LINES[1:-1]]
                         + [lines[-1]])

    def test_empty_indent(self):
        """Пустой отступ: строки внутри блока без отступа."""
        lines = NetlistSerializer("").serialize_block(main_block())
        self.assertEqual(lines[1:3], ["net x", "inst u1 sub amp"])

    def test_indent_not_applied_to_block_end_and_primitives(self):
        """Отступ не ставится перед `block`, `end` и строками primitive."""
        result = NetlistSerializer("    ").serialize(full_model())
        for line in result.split("\n"):
            if line.split()[:1] in (["block"], ["end"], ["primitive"]):
                self.assertFalse(line.startswith(" "), line)

    def test_two_serializers_keep_own_indent(self):
        """У каждого сериализатора свой отступ, они не влияют друг на друга."""
        tab, space = NetlistSerializer("\t"), NetlistSerializer(" ")
        self.assertEqual(tab.serialize_block(main_block())[1], "\tnet x")
        self.assertEqual(space.serialize_block(main_block())[1], " net x")


# Гарантия обратимости (связка с парсером, CheckerDraft и Netlist)
class RoundTripTest(unittest.TestCase):
    """parse(serialize(m)) восстанавливает ту же модель.

    Контракт формулирует это как
    `Netlist.from_view(CheckerDraft(parse(serialize(m)).raw)) == m`, но у
    Netlist нет __eq__, и `==` сравнивает объекты по идентичности.
    Поэтому равенство моделей проверяется по их содержимому: по
    повторной сериализации и по составу блоков.
    """

    def setUp(self) -> None:
        stack = ExitStack()
        stack.enter_context(replaced_modules(
            {"netlist.checker.syntax_check": make_syntax_module()}))
        self.addCleanup(stack.close)

    def build_model(self):
        from netlist.core.model import Netlist

        model = Netlist()
        model.add_primitive("r", ["a", "b"])
        model.add_primitive("gnd", [])
        amp = model.add_block("amp")
        amp.add_net("n1")
        amp.add_net("n2")
        amp.add_pin("in", "n1")
        amp.add_pin("out", None)
        r1 = amp.add_instance("r1", "prim", "r")
        r1.add_pin("a", "n1")
        r1.add_pin("b", "n2")
        main = model.add_block("main")
        main.add_net("x")
        u1 = main.add_instance("u1", "sub", "amp")
        u1.add_pin("in", "x")
        u1.add_pin("out", None)
        return model

    def restore(self, text: str):
        from netlist.checker.checker_parser import CheckerDraft
        from netlist.core.model import Netlist
        from netlist.core.parser import NetlistParser

        result = NetlistParser(StubSyntaxCheck()).parse(text, "round.net")
        self.assertTrue(result.ok)
        return Netlist.from_view(CheckerDraft(result.raw))

    def test_serialize_parse_serialize_gives_same_text(self):
        """Текст восстановленной модели совпадает с исходным текстом."""
        serializer = NetlistSerializer()
        original = serializer.serialize(self.build_model())
        self.assertEqual(serializer.serialize(self.restore(original)), original)

    def test_restored_model_has_same_structure(self):
        """У восстановленной модели те же примитивы, блоки, пины, цепи, инстансы."""
        model = self.build_model()
        restored = self.restore(NetlistSerializer().serialize(model))

        def shape(m):
            return (
                [(p.name, list(p.pin_names)) for p in m.primitives()],
                [(b.name,
                  [(p.name, p.net) for p in b.pins()],
                  [n.name for n in b.nets()],
                  [(i.name, str(i.kind), i.type_name,
                    [(p.name, p.net) for p in i.pins()])
                   for i in b.instances()])
                 for b in m.blocks()],
            )

        self.assertEqual(shape(restored), shape(model))

    def test_header_does_not_break_round_trip(self):
        """Заголовок-комментарий при разборе игнорируется, модель та же."""
        serializer = NetlistSerializer()
        model = self.build_model()
        with_header = serializer.serialize(model, header="автор: тест")
        self.assertEqual(serializer.serialize(self.restore(with_header)),
                         serializer.serialize(model))


if __name__ == "__main__":
    unittest.main()
