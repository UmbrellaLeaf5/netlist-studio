"""Юнит-тесты NetlistParser (netlist/core/parser.py, автор Н. Бучилкин).

Контракт: docs/interfaces.md, п. 2.2. Модуль нижнего уровня SyntaxCheck
(`netlist.checker.syntax_check`) целиком заменён заглушкой из fakes.py:
лексика (tokenize/classify/LineKind) сделана по контракту п. 4.2, а
`check_lines` возвращает заданные тестом диагностики. Поэтому тесты
проверяют только сам парсер: какие записи он строит и какие строки
пропускает.

Тексты задаются списком строк через `text(...)`: номер строки — это
позиция в списке, считая с 1, так что loc в ожиданиях легко проверить
глазами.

Запуск из корня репозитория:
    python -m unittest tests.core.test_parser -v
"""

from __future__ import annotations

import copy
import unittest
from contextlib import ExitStack

from netlist.core.model import Severity, SourceLoc
from netlist.core.parser import (
    NetlistParser,
    ParseResult,
    RawBlock,
    RawConnection,
    RawInstance,
    RawNet,
    RawNetlist,
    RawPin,
    RawPrimitive,
)
from tests.core.fakes import (
    StubSyntaxCheck,
    error,
    make_syntax_module,
    replaced_modules,
    warning,
)

FILE = "design.net"


def text(*lines: str) -> str:
    """Текст файла из строк; в конце перевод строки, как в обычном файле."""
    return "\n".join(lines) + "\n"


def at(line: int | None, file: str | None = FILE) -> SourceLoc:
    return SourceLoc(file, line)


class ParserTestCase(unittest.TestCase):
    """Общая подготовка: модуль syntax_check заменён заглушкой."""

    def setUp(self) -> None:
        self.syntax = make_syntax_module()
        stack = ExitStack()
        stack.enter_context(
            replaced_modules({"netlist.checker.syntax_check": self.syntax}))
        self.addCleanup(stack.close)

    def parse(self, source: str, diagnostics=(), filename: str | None = FILE
              ) -> ParseResult:
        """Разобрать `source`; SyntaxCheck вернёт `diagnostics`."""
        self.check = StubSyntaxCheck(diagnostics)
        return NetlistParser(self.check).parse(source, filename)

    def parse_library(self, source: str, diagnostics=(),
                      filename: str | None = FILE) -> ParseResult:
        self.check = StubSyntaxCheck(diagnostics)
        return NetlistParser(self.check).parse_library(source, filename)


# Сырые данные и ParseResult
class RawDataTest(unittest.TestCase):
    """Классы сырых данных: создание, сравнение, копирование."""

    def examples(self):
        return [
            RawPrimitive("r", ["a", "b"], at(1)),
            RawPin("in", "n1", at(2)),
            RawPin("out", None, at(3)),
            RawNet("n1", at(4)),
            RawInstance("r1", "prim", "r", at(5)),
            RawConnection("r1", "a", "n1", at(6)),
            RawBlock("main", at(7), [RawPin("p", None, at(8))],
                     [RawNet("n", at(9))],
                     [RawInstance("x", "sub", "leaf", at(10))],
                     [RawConnection("x", "a", "n", at(11))]),
            RawNetlist(FILE, [RawPrimitive("r", [], at(1))],
                       [RawBlock("main", at(2), [], [], [], [])]),
        ]

    def test_fields_are_stored_as_given(self):
        """Поля записей хранят ровно то, что передали в конструктор."""
        conn = RawConnection("r1", "a", None, at(6))
        self.assertEqual((conn.instance, conn.pin, conn.net, conn.loc),
                         ("r1", "a", None, at(6)))
        block = RawBlock("main", at(1), [], [], [], [])
        self.assertEqual((block.name, block.loc, block.pins, block.nets,
                          block.instances, block.connections),
                         ("main", at(1), [], [], [], []))

    def test_equal_when_all_fields_equal(self):
        """Две записи с одинаковыми полями равны, с разными — нет."""
        self.assertEqual(RawNet("n", at(1)), RawNet("n", at(1)))
        self.assertNotEqual(RawNet("n", at(1)), RawNet("n", at(2)))
        self.assertNotEqual(RawPin("a", None, at(1)), RawPin("a", "n", at(1)))

    def test_deepcopy_is_equal_and_independent(self):
        """Глубокая копия равна оригиналу, и её изменение оригинал не трогает."""
        for original in self.examples():
            with self.subTest(type(original).__name__):
                clone = copy.deepcopy(original)
                self.assertEqual(clone, original)
                self.assertIsNot(clone, original)

        block = self.examples()[6]
        clone = copy.deepcopy(block)
        clone.pins.append(RawPin("extra", None, at(99)))
        self.assertEqual([p.name for p in block.pins], ["p"])

    def test_shallow_copy_shares_lists(self):
        """Обычная копия равна оригиналу (списки внутри общие)."""
        prim = RawPrimitive("r", ["a"], at(1))
        clone = copy.copy(prim)
        self.assertEqual(clone, prim)
        self.assertIs(clone.pin_names, prim.pin_names)


class ParseResultOkTest(unittest.TestCase):
    """ParseResult.ok: True, если нет ни одной диагностики уровня ERROR."""

    def result(self, *diagnostics) -> ParseResult:
        return ParseResult(RawNetlist(FILE, [], []), list(diagnostics))

    def test_ok_without_diagnostics(self):
        """Без диагностик результат ok."""
        self.assertTrue(self.result().ok)

    def test_ok_with_only_warnings(self):
        """Одни предупреждения ok не портят."""
        self.assertTrue(self.result(warning(1), warning(2)).ok)

    def test_not_ok_with_one_error(self):
        """Одна ошибка среди предупреждений делает результат не ok."""
        self.assertFalse(self.result(warning(1), error(2)).ok)

    def test_ok_follows_diagnostics_list(self):
        """ok вычисляется по текущему списку диагностик, а не запоминается."""
        result = self.result()
        result.diagnostics.append(error(5))
        self.assertFalse(result.ok)


# Конструктор
class ParserInitTest(ParserTestCase):

    def test_default_uses_syntax_check_from_module(self):
        """Без аргумента парсер создаёт SyntaxCheck() из модуля syntax_check."""
        parser = NetlistParser()
        self.assertEqual(len(self.syntax.created), 1)
        parser.parse(text("block main", "end"), FILE)
        self.assertEqual(self.syntax.created[0].calls,
                         [(["block main", "end"], FILE)])

    def test_injected_check_is_used_instead_of_default(self):
        """Переданная заглушка используется, свой SyntaxCheck не создаётся."""
        stub = StubSyntaxCheck()
        NetlistParser(stub).parse(text("block main", "end"), FILE)
        self.assertEqual(self.syntax.created, [])
        self.assertEqual(len(stub.calls), 1)

    def test_two_parsers_are_independent(self):
        """У каждого парсера своя проверка: вызовы не смешиваются."""
        first, second = StubSyntaxCheck(), StubSyntaxCheck()
        NetlistParser(first).parse(text("block a", "end"), "a.net")
        NetlistParser(second).parse(text("block b", "end"), "b.net")
        self.assertEqual(first.calls, [(["block a", "end"], "a.net")])
        self.assertEqual(second.calls, [(["block b", "end"], "b.net")])


# parse: какие записи строятся
class ParseRecordsTest(ParserTestCase):

    def test_empty_text(self):
        """Пустой текст даёт пустой нетлист без диагностик."""
        result = self.parse("")
        self.assertEqual(result.raw, RawNetlist(FILE, [], []))
        self.assertEqual(result.diagnostics, [])
        self.assertTrue(result.ok)

    def test_only_comments_and_blank_lines(self):
        """Комментарии и пустые строки записей не дают."""
        result = self.parse(text("# схема", "", "   ", "  # ещё комментарий"))
        self.assertEqual(result.raw, RawNetlist(FILE, [], []))

    def test_primitive_line(self):
        """`primitive r a b` даёт примитив r с пинами a, b на строке 1."""
        result = self.parse(text("primitive r a b"))
        self.assertEqual(result.raw.primitives,
                         [RawPrimitive("r", ["a", "b"], at(1))])

    def test_primitive_without_pins(self):
        """Примитив без пинов допустим по грамматике: список пинов пустой."""
        result = self.parse(text("primitive gnd"))
        self.assertEqual(result.raw.primitives, [RawPrimitive("gnd", [], at(1))])

    def test_block_with_every_kind_of_line(self):
        """Каждый вид строки блока попадает в свой список с номером строки."""
        result = self.parse(text(
            "block main",            # 1
            "  pin in n1",           # 2
            "  pin out -",           # 3
            "  net n1",              # 4
            "  inst r1 prim res",    # 5
            "  r1.a n1",             # 6
            "  r1.b -",              # 7
            "end",                   # 8
        ))
        self.assertEqual(result.raw.blocks, [RawBlock(
            "main", at(1),
            pins=[RawPin("in", "n1", at(2)), RawPin("out", None, at(3))],
            nets=[RawNet("n1", at(4))],
            instances=[RawInstance("r1", "prim", "res", at(5))],
            connections=[RawConnection("r1", "a", "n1", at(6)),
                         RawConnection("r1", "b", None, at(7))],
        )])

    def test_dash_becomes_none_other_names_stay(self):
        """`-` вместо цепи превращается в None, обычное имя цепи остаётся строкой."""
        block = self.parse(text(
            "block b", "pin p -", "pin q n", "x.a -", "x.b n", "end",
        )).raw.blocks[0]
        self.assertEqual([p.net for p in block.pins], [None, "n"])
        self.assertEqual([c.net for c in block.connections], [None, "n"])

    def test_sub_and_prim_instances(self):
        """Вид инстанса (sub или prim) и имя типа сохраняются как в файле."""
        block = self.parse(text(
            "block main", "inst u1 sub amp", "inst r1 prim res", "end",
        )).raw.blocks[0]
        self.assertEqual(block.instances, [
            RawInstance("u1", "sub", "amp", at(2)),
            RawInstance("r1", "prim", "res", at(3)),
        ])

    def test_blocks_keep_file_order_and_duplicates(self):
        """Блоки идут в порядке файла, повтор имени блока не схлопывается."""
        result = self.parse(text(
            "block b", "end", "block a", "end", "block b", "end",
        ))
        self.assertEqual([(b.name, b.loc.line) for b in result.raw.blocks],
                         [("b", 1), ("a", 3), ("b", 5)])

    def test_duplicates_inside_block_are_kept(self):
        """Повторы pin, net и подключений внутри блока сохраняются по порядку."""
        block = self.parse(text(
            "block main",   # 1
            "pin a n",      # 2
            "pin a m",      # 3
            "net n",        # 4
            "net n",        # 5
            "x.p n",        # 6
            "x.p m",        # 7
            "end",
        )).raw.blocks[0]
        self.assertEqual(block.pins, [RawPin("a", "n", at(2)),
                                      RawPin("a", "m", at(3))])
        self.assertEqual(block.nets, [RawNet("n", at(4)), RawNet("n", at(5))])
        self.assertEqual(block.connections, [
            RawConnection("x", "p", "n", at(6)),
            RawConnection("x", "p", "m", at(7)),
        ])

    def test_references_are_not_resolved(self):
        """Ссылки на несуществующие блоки, цепи и инстансы сохраняются как есть."""
        block = self.parse(text(
            "block main",
            "pin a undeclared_net",
            "inst x sub no_such_block",
            "ghost.p n",
            "end",
        )).raw.blocks[0]
        self.assertEqual(block.pins[0].net, "undeclared_net")
        self.assertEqual(block.instances[0].type_name, "no_such_block")
        self.assertEqual(block.connections[0].instance, "ghost")

    def test_connection_may_precede_its_instance(self):
        """Строка `x.a n` до строки `inst x` тоже записывается (порядок свободный)."""
        block = self.parse(text(
            "block main", "x.a n", "inst x prim r", "end",
        )).raw.blocks[0]
        self.assertEqual(block.connections, [RawConnection("x", "a", "n", at(2))])
        self.assertEqual(block.instances, [RawInstance("x", "prim", "r", at(3))])

    def test_primitives_anywhere_outside_blocks_in_file_order(self):
        """Примитивы до, между и после блоков собираются в порядке файла."""
        result = self.parse(text(
            "primitive a x",   # 1
            "block b1",        # 2
            "end",             # 3
            "primitive b y",   # 4
            "block b2",        # 5
            "end",             # 6
            "primitive c z",   # 7
        ))
        self.assertEqual([(p.name, p.loc.line) for p in result.raw.primitives],
                         [("a", 1), ("b", 4), ("c", 7)])
        self.assertEqual([b.name for b in result.raw.blocks], ["b1", "b2"])

    def test_lines_go_to_the_block_they_are_in(self):
        """Строки после `end` следующего блока относятся уже к нему."""
        result = self.parse(text(
            "block a", "net na", "end", "block b", "net nb", "end",
        ))
        self.assertEqual([[n.name for n in b.nets] for b in result.raw.blocks],
                         [["na"], ["nb"]])

    def test_line_numbers_count_comments_and_blank_lines(self):
        """Номера строк физические: пустые строки и комментарии тоже считаются."""
        block = self.parse(text(
            "# заголовок",     # 1
            "",                # 2
            "block main",      # 3
            "",                # 4
            "  # комментарий", # 5
            "  net n  # хвост",# 6
            "end",             # 7
        )).raw.blocks[0]
        self.assertEqual(block.loc, at(3))
        self.assertEqual(block.nets, [RawNet("n", at(6))])

    def test_filename_goes_to_source_file_and_every_loc(self):
        """Имя файла попадает в source_file и в loc каждой записи."""
        raw = self.parse(text("primitive r a", "block m", "net n", "end"),
                         filename="other.net").raw
        self.assertEqual(raw.source_file, "other.net")
        self.assertEqual(raw.primitives[0].loc, SourceLoc("other.net", 1))
        self.assertEqual(raw.blocks[0].loc, SourceLoc("other.net", 2))
        self.assertEqual(raw.blocks[0].nets[0].loc, SourceLoc("other.net", 3))

    def test_without_filename_file_is_none(self):
        """Без имени файла source_file и loc.file равны None, номера строк есть."""
        raw = self.parse(text("block m", "net n", "end"), filename=None).raw
        self.assertIsNone(raw.source_file)
        self.assertEqual(raw.blocks[0].nets[0].loc, SourceLoc(None, 2))

    def test_crlf_gives_same_result_as_lf(self):
        """Концы строк CRLF дают тот же результат, что и LF (без '\\r' в именах)."""
        lines = ["primitive r a b", "block main", "pin p n", "net n", "end"]
        lf = self.parse("\n".join(lines) + "\n")
        crlf = self.parse("\r\n".join(lines) + "\r\n")
        self.assertEqual(crlf.raw, lf.raw)

    def test_last_line_without_newline_is_parsed(self):
        """Последняя строка без перевода строки тоже разбирается."""
        result = self.parse("block main\nnet n\nend")
        self.assertEqual(result.raw.blocks[0].nets, [RawNet("n", at(2))])

    def test_unclosed_block_is_kept(self):
        """Блок без `end` всё равно попадает в результат со своими строками."""
        result = self.parse(text("block main", "net n", "pin p n"))
        self.assertEqual(result.raw.blocks, [RawBlock(
            "main", at(1), pins=[RawPin("p", "n", at(3))],
            nets=[RawNet("n", at(2))], instances=[], connections=[],
        )])

    def test_unclosed_block_kept_even_with_e105_on_its_line(self):
        """Если SyntaxCheck пометил строку `block` ошибкой E105, блок всё равно есть."""
        result = self.parse(text("block main", "net n"),
                            diagnostics=[error(1, "E105")])
        self.assertEqual([b.name for b in result.raw.blocks], ["main"])
        self.assertEqual(result.raw.blocks[0].nets, [RawNet("n", at(2))])
        self.assertFalse(result.ok)

    def test_block_lines_outside_block_are_dropped(self):
        """Строки pin/net вне блока не попадают ни в один блок."""
        result = self.parse(text(
            "pin early n", "block main", "net n", "end", "net late",
        ))
        self.assertEqual(len(result.raw.blocks), 1)
        self.assertEqual(result.raw.blocks[0].nets, [RawNet("n", at(3))])
        self.assertEqual(result.raw.blocks[0].pins, [])

    def test_garbage_does_not_raise(self):
        """На мусорных строках парсер не бросает исключений."""
        garbage = text("@@@", "block", "end end", "x. n", ".p n", "inst a b",
                       "pin", "primitive", "block a b c", "a.b.c n")
        result = self.parse(garbage)
        self.assertIsInstance(result, ParseResult)


# parse: взаимодействие с SyntaxCheck
class ParseUsesSyntaxCheckTest(ParserTestCase):

    def test_check_lines_called_once_with_lines_and_filename(self):
        """check_lines вызывается один раз: строки файла и имя файла."""
        self.parse(text("block main", "  net n", "end"))
        self.assertEqual(self.check.calls,
                         [(["block main", "  net n", "end"], FILE)])

    def test_diagnostics_of_syntax_check_are_returned(self):
        """Диагностики SyntaxCheck возвращаются в ParseResult без изменений."""
        diags = [error(1, "E101"), warning(2)]
        result = self.parse(text("bad line", "block main", "end"), diags)
        self.assertEqual(result.diagnostics, diags)
        self.assertTrue(all(d.check == "syntax" for d in result.diagnostics))
        self.assertFalse(result.ok)

    def test_line_with_error_is_skipped_others_are_kept(self):
        """Строка с синтаксической ошибкой пропускается, соседние разбираются."""
        result = self.parse(text(
            "block main",   # 1
            "net a",        # 2
            "net b",        # 3  <- ошибка
            "net c",        # 4
            "end",
        ), diagnostics=[error(3)])
        self.assertEqual(result.raw.blocks[0].nets,
                         [RawNet("a", at(2)), RawNet("c", at(4))])

    def test_several_errors_skip_several_lines(self):
        """Каждая строка с ошибкой пропускается, даже если ошибок на ней две."""
        result = self.parse(text(
            "primitive r a",  # 1  <- ошибка
            "primitive s a",  # 2
            "primitive t a",  # 3  <- две ошибки
        ), diagnostics=[error(1), error(3, "E102"), error(3, "E107")])
        self.assertEqual([p.name for p in result.raw.primitives], ["s"])

    def test_line_with_warning_is_not_skipped(self):
        """Строка только с предупреждением не ошибочна и разбирается."""
        result = self.parse(text("primitive r a"), diagnostics=[warning(1)])
        self.assertEqual([p.name for p in result.raw.primitives], ["r"])

    def test_error_without_line_number_skips_nothing(self):
        """Ошибка без номера строки не выбрасывает ни одной строки."""
        result = self.parse(text("primitive r a", "primitive s b"),
                            diagnostics=[error(None)])
        self.assertEqual([p.name for p in result.raw.primitives], ["r", "s"])
        self.assertFalse(result.ok)

    def test_nested_block_with_error_is_not_created(self):
        """Вложенный `block` с ошибкой E105 не создаёт второй блок."""
        result = self.parse(text(
            "block outer",   # 1
            "block inner",   # 2  <- E105: block внутри незакрытого блока
            "end",           # 3
        ), diagnostics=[error(2, "E105")])
        self.assertEqual([b.name for b in result.raw.blocks], ["outer"])

    def test_lexer_comes_from_syntax_module(self):
        """Строки разбиваются на токены функцией tokenize модуля SyntaxCheck."""
        lines = ["block main", "net n", "end"]
        self.parse(text(*lines))
        for line in lines:
            self.assertIn(line, self.syntax.tokenized)


# parse_library
class ParseLibraryTest(ParserTestCase):

    def test_primitives_are_parsed_with_physical_line_numbers(self):
        """Строки primitive разбираются, номера строк — как в файле библиотеки."""
        result = self.parse_library(text(
            "# библиотека",        # 1
            "primitive r a b",     # 2
            "",                    # 3
            "primitive c p n",     # 4
        ))
        self.assertEqual(result.raw.primitives, [
            RawPrimitive("r", ["a", "b"], at(2)),
            RawPrimitive("c", ["p", "n"], at(4)),
        ])
        self.assertEqual(result.raw.blocks, [])
        self.assertEqual(result.diagnostics, [])
        self.assertTrue(result.ok)

    def test_filename_in_library_records(self):
        """Имя файла библиотеки попадает в source_file и loc примитивов."""
        result = self.parse_library(text("primitive r a"), filename="lib.prim")
        self.assertEqual(result.raw.source_file, "lib.prim")
        self.assertEqual(result.raw.primitives[0].loc, SourceLoc("lib.prim", 1))

    def test_block_line_in_library_is_an_error(self):
        """Строка `block` в библиотеке даёт синтаксическую ошибку на своей строке."""
        result = self.parse_library(text("primitive r a", "block x"))
        self.assertFalse(result.ok)
        errors = [d for d in result.diagnostics if d.severity is Severity.ERROR]
        self.assertEqual([(d.loc, d.check) for d in errors], [(at(2), "syntax")])
        self.assertEqual(result.raw.blocks, [])

    def test_each_forbidden_line_gives_its_own_error(self):
        """Каждая строка pin/net/inst/подключения/end — отдельная ошибка."""
        result = self.parse_library(text(
            "primitive r a",   # 1
            "pin p n",         # 2
            "net n",           # 3
            "inst x prim r",   # 4
            "x.a n",           # 5
            "end",             # 6
        ))
        self.assertEqual(
            [d.loc.line for d in result.diagnostics
             if d.severity is Severity.ERROR],
            [2, 3, 4, 5, 6])
        self.assertEqual([p.name for p in result.raw.primitives], ["r"])

    def test_forbidden_block_does_not_hide_primitives_after_it(self):
        """`block` и `end` в библиотеке не «съедают» примитивы между ними."""
        result = self.parse_library(text(
            "block x",            # 1  <- ошибка
            "primitive r a b",    # 2
            "end",                # 3  <- ошибка
            "primitive c p",      # 4
        ))
        self.assertEqual([(p.name, p.loc.line) for p in result.raw.primitives],
                         [("r", 2), ("c", 4)])
        self.assertEqual(result.raw.blocks, [])

    def test_comments_and_blank_lines_are_allowed(self):
        """Комментарии и пустые строки в библиотеке ошибок не дают."""
        result = self.parse_library(text("", "# только комментарий", "   "))
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.raw.primitives, [])

    def test_syntax_check_diagnostics_are_kept_and_sorted(self):
        """Ошибки SyntaxCheck сохраняются, все диагностики идут по номеру строки."""
        result = self.parse_library(
            text("pin p n", "primitive r a", "primitive 1bad"),
            diagnostics=[error(3, "E102")])
        self.assertEqual([d.loc.line for d in result.diagnostics], [1, 3])
        self.assertEqual([p.name for p in result.raw.primitives], ["r"])

    def test_library_text_goes_through_syntax_check(self):
        """Библиотека тоже проверяется через check_lines с именем файла."""
        self.parse_library(text("primitive r a"), filename="lib.prim")
        self.assertEqual(len(self.check.calls), 1)
        self.assertEqual(self.check.calls[0][1], "lib.prim")


if __name__ == "__main__":
    unittest.main()
