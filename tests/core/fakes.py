"""Заглушки для юнит-тестов модулей Н. Бучилкина (parser, serializer, hierarchy).

Модули нижнего уровня подменяются простыми имитациями, чтобы тесты
проверяли только тестируемый модуль и опирались на контракт из
docs/interfaces.md, а не на текущее состояние чужого кода:

* `make_syntax_module()` — модуль `netlist.checker.syntax_check` по
  контракту п. 4.2: LineKind, tokenize, classify и класс SyntaxCheck.
  Лексика упрощена до минимума, нужного парсеру.
* `StubSyntaxCheck` — объект с `check_lines`, который возвращает заранее
  заданные диагностики и запоминает, с какими аргументами его вызвали.
* `replaced_modules(...)` — временно подставляет модули в `sys.modules`
  (и в атрибуты пакета), чтобы `from netlist.checker import X` внутри
  тестируемого кода получил заглушку.
* `FakeView` и компания — минимальная реализация протокола NetlistView
  (раздел 2.1) для HierarchyWalker и NetlistSerializer.
"""

from __future__ import annotations

import importlib
import sys
import types
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Sequence
from unittest import mock

from netlist.core.model import Diagnostic, Severity, SourceLoc


# Подмена модулей
@contextmanager
def replaced_modules(modules: dict[str, types.ModuleType]) -> Iterator[None]:
    """Подставить модули по полным именам на время блока `with`.

    Пример: replaced_modules({"netlist.checker.syntax_check": fake}).
    После выхода всё возвращается как было, даже если тест упал.
    """
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(sys.modules, modules))
        for full_name, module in modules.items():
            package_name, _, attr = full_name.rpartition(".")
            package = importlib.import_module(package_name)
            stack.enter_context(
                mock.patch.object(package, attr, module, create=True))
        yield


# Заглушка SyntaxCheck
def error(line: int | None, code: str = "E103",
          filename: str | None = None) -> Diagnostic:
    """Синтаксическая ошибка на строке `line` (как выдаёт SyntaxCheck)."""
    return Diagnostic(code, Severity.ERROR, "syntax",
                      f"тестовая ошибка {code}", SourceLoc(filename, line))


def warning(line: int | None, code: str = "W199",
            filename: str | None = None) -> Diagnostic:
    """Синтаксическое предупреждение на строке `line`."""
    return Diagnostic(code, Severity.WARNING, "syntax",
                      f"тестовое предупреждение {code}",
                      SourceLoc(filename, line))


class StubSyntaxCheck:
    """Заглушка SyntaxCheck: отдаёт заданные диагностики, пишет журнал.

    `calls` — список пар (строки, имя файла) для каждого вызова
    check_lines. Строки копируются, чтобы журнал не зависел от того,
    что вызывающий потом сделает со своим списком.
    """

    def __init__(self, diagnostics: Sequence[Diagnostic] = ()) -> None:
        self.diagnostics = list(diagnostics)
        self.calls: list[tuple[list[str], str | None]] = []

    def check_lines(self, lines: Sequence[str],
                    filename: str | None = None) -> list[Diagnostic]:
        self.calls.append((list(lines), filename))
        return list(self.diagnostics)


class LineKind(Enum):
    """Типы строк из контракта SyntaxCheck (docs/interfaces.md, п. 4.2)."""
    BLANK = "blank"
    PRIMITIVE = "primitive"
    BLOCK = "block"
    END = "end"
    PIN = "pin"
    NET = "net"
    INST = "inst"
    CONN = "conn"


_BY_KEYWORD = {
    "primitive": LineKind.PRIMITIVE,
    "block": LineKind.BLOCK,
    "end": LineKind.END,
    "pin": LineKind.PIN,
    "net": LineKind.NET,
    "inst": LineKind.INST,
}


def make_syntax_module(default_diagnostics: Sequence[Diagnostic] = ()
                       ) -> types.ModuleType:
    """Новый модуль-заглушка `syntax_check` с журналом вызовов.

    Атрибуты модуля:
      LineKind, tokenize, classify — как в контракте;
      SyntaxCheck — класс, экземпляры которого ведут себя как
                    StubSyntaxCheck(default_diagnostics);
      created     — список созданных экземпляров SyntaxCheck;
      tokenized   — список строк, переданных в tokenize.
    """
    module = types.ModuleType("fake_syntax_check")
    module.LineKind = LineKind
    module.created = []
    module.tokenized = []

    def tokenize(line: str) -> list[str]:
        module.tokenized.append(line)
        return line.split("#", 1)[0].split()

    def classify(tokens: list[str]) -> LineKind | None:
        if not tokens:
            return LineKind.BLANK
        if tokens[0] in _BY_KEYWORD:
            return _BY_KEYWORD[tokens[0]]
        if "." in tokens[0]:
            return LineKind.CONN
        return None

    class SyntaxCheck(StubSyntaxCheck):
        def __init__(self) -> None:
            super().__init__(default_diagnostics)
            module.created.append(self)

    module.tokenize = tokenize
    module.classify = classify
    module.SyntaxCheck = SyntaxCheck
    return module


# Простая реализация NetlistView
def loc(line: int | None, file: str | None = "test.net") -> SourceLoc:
    return SourceLoc(file, line)


@dataclass
class FakePin:
    name: str
    net: str | None = None
    loc: SourceLoc = field(default_factory=lambda: loc(None))


@dataclass
class FakeNet:
    name: str
    loc: SourceLoc = field(default_factory=lambda: loc(None))


@dataclass
class FakePrimitive:
    name: str
    pin_names: list[str] = field(default_factory=list)
    loc: SourceLoc = field(default_factory=lambda: loc(None))


@dataclass
class FakeInstance:
    name: str
    kind: str
    type_name: str
    loc: SourceLoc = field(default_factory=lambda: loc(None))
    pin_list: list[FakePin] = field(default_factory=list)

    def pins(self) -> list[FakePin]:
        return list(self.pin_list)


@dataclass
class FakeBlock:
    name: str
    loc: SourceLoc = field(default_factory=lambda: loc(None))
    pin_list: list[FakePin] = field(default_factory=list)
    net_list: list[FakeNet] = field(default_factory=list)
    instance_list: list[FakeInstance] = field(default_factory=list)

    def pins(self) -> list[FakePin]:
        return list(self.pin_list)

    def nets(self) -> list[FakeNet]:
        return list(self.net_list)

    def instances(self) -> list[FakeInstance]:
        return list(self.instance_list)

    def orphan_connections(self) -> list:
        return []


class FakeView:
    """NetlistView из готовых списков; считает вызовы blocks()."""

    def __init__(self, blocks: Sequence[FakeBlock] = (),
                 primitives: Sequence[FakePrimitive] = (),
                 source_file: str | None = "test.net") -> None:
        self.source_file = source_file
        self.block_list = list(blocks)
        self.primitive_list = list(primitives)
        self.blocks_calls = 0

    def blocks(self) -> list[FakeBlock]:
        self.blocks_calls += 1
        return list(self.block_list)

    def primitives(self) -> list[FakePrimitive]:
        return list(self.primitive_list)

    def find_block(self, name: str) -> FakeBlock | None:
        return next((b for b in self.block_list if b.name == name), None)

    def find_primitive(self, name: str) -> FakePrimitive | None:
        return next((p for p in self.primitive_list if p.name == name), None)


def hierarchy(spec: dict[str, list[str]],
              prims: dict[str, list[str]] | None = None) -> FakeView:
    """Представление по короткой записи «блок -> типы его подсхем».

    hierarchy({"main": ["a", "b"], "a": ["b"], "b": []}) даёт блоки main,
    a, b в этом порядке. Инстансы-подсхемы называются по типу и номеру
    в блоке: у main это a1 (тип a) и b2 (тип b). Строка `inst` каждого
    инстанса имеет номер 10 * (номер блока) + (номер инстанса), так что
    по loc легко понять, какой это инстанс.

    `prims` добавляет в блоки инстансы-примитивы: {"main": ["r"]}.
    """
    blocks = []
    for b_index, (name, children) in enumerate(spec.items(), 1):
        instances = [
            FakeInstance(f"{child}{i}", "sub", child, loc(10 * b_index + i))
            for i, child in enumerate(children, 1)
        ]
        for j, prim in enumerate((prims or {}).get(name, []), 1):
            instances.append(FakeInstance(f"{prim}_p{j}", "prim", prim,
                                          loc(10 * b_index + 9)))
        blocks.append(FakeBlock(name, loc(10 * b_index), instance_list=instances))
    return FakeView(blocks)
