"""Разбор текста в сырые записи без разрешения ссылок.

Автор: Н. Бучилкин. Лексика и диагностика принадлежат SyntaxCheck;
порядок объявлений, дубликаты и отдельные подключения сохраняются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from netlist.core.model import Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    from netlist.checker.syntax_check import SyntaxCheck


@dataclass
class RawPrimitive:
    name: str
    pin_names: list[str]
    loc: SourceLoc


@dataclass
class RawPin:
    name: str
    net: str | None
    loc: SourceLoc


@dataclass
class RawNet:
    name: str
    loc: SourceLoc


@dataclass
class RawInstance:
    name: str
    kind: str
    type_name: str
    loc: SourceLoc


@dataclass
class RawConnection:
    instance: str
    pin: str
    net: str | None
    loc: SourceLoc


@dataclass
class RawBlock:
    name: str
    loc: SourceLoc
    pins: list[RawPin] = field(default_factory=list)
    nets: list[RawNet] = field(default_factory=list)
    instances: list[RawInstance] = field(default_factory=list)
    connections: list[RawConnection] = field(default_factory=list)


@dataclass
class RawNetlist:
    source_file: str | None
    primitives: list[RawPrimitive] = field(default_factory=list)
    blocks: list[RawBlock] = field(default_factory=list)


@dataclass
class ParseResult:
    raw: RawNetlist
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        return not any(d.severity == Severity.ERROR for d in self.diagnostics)


class NetlistParser:
    """Использует модульные tokenize/classify из общего контракта.

    Зависимость импортируется при создании парсера: Raw* доступны другим
    компонентам независимо от готовности модуля синтаксической проверки.
    """

    def __init__(self, syntax_check: SyntaxCheck | None = None) -> None:
        from netlist.checker import syntax_check as syntax

        self._syntax = syntax
        self._check = syntax.SyntaxCheck() if syntax_check is None else syntax_check

    def parse(self, text: str, filename: str | None = None) -> ParseResult:
        lines = text.splitlines()
        diagnostics = list(self._check.check_lines(lines, filename))
        errors: dict[int | None, list[Diagnostic]] = {}
        for diag in diagnostics:
            if diag.severity == Severity.ERROR:
                errors.setdefault(diag.loc.line, []).append(diag)
        raw = RawNetlist(filename)
        current: RawBlock | None = None
        kinds = self._syntax.LineKind
        for number, line in enumerate(lines, 1):
            tokens = self._syntax.tokenize(line)
            if not tokens:
                continue
            kind = self._syntax.classify(tokens)
            if number in errors:
                # E105 может указывать на начало незакрытого блока.
                # Само объявление сохраняем, но вложенный block пропускаем.
                unclosed = (kind == kinds.BLOCK and len(tokens) == 2
                            and current is None
                            and all(d.code == "E105" for d in errors[number]))
                if not unclosed:
                    continue
            loc = SourceLoc(filename, number)
            if kind == kinds.PRIMITIVE and current is None and len(tokens) >= 2:
                raw.primitives.append(RawPrimitive(tokens[1], tokens[2:], loc))
            elif kind == kinds.BLOCK and current is None and len(tokens) == 2:
                current = RawBlock(tokens[1], loc)
                raw.blocks.append(current)
            elif kind == kinds.END and len(tokens) == 1:
                current = None
            elif current is not None:
                if kind == kinds.PIN and len(tokens) == 3:
                    current.pins.append(RawPin(tokens[1], self._net(tokens[2]), loc))
                elif kind == kinds.NET and len(tokens) == 2:
                    current.nets.append(RawNet(tokens[1], loc))
                elif kind == kinds.INST and len(tokens) == 4:
                    current.instances.append(RawInstance(*tokens[1:], loc))
                elif kind == kinds.CONN and len(tokens) == 2:
                    instance, separator, pin = tokens[0].partition(".")
                    if separator and instance and pin:
                        current.connections.append(
                            RawConnection(instance, pin, self._net(tokens[1]), loc)
                        )
        return ParseResult(raw, diagnostics)

    def parse_library(self, text: str, filename: str | None = None) -> ParseResult:
        """Проверить библиотеку, сохранив физические номера строк.

        Запрещённые строки заменяются пустыми перед check_lines, чтобы
        block/end не влияли на допустимые primitive ниже по файлу.
        Для них используется E101 (нераспознанный тип строки в библиотеке).
        Это не требует нового параметра library_mode у SyntaxCheck.
        """
        lines = text.splitlines()
        filtered: list[str] = []
        forbidden: list[Diagnostic] = []
        for number, line in enumerate(lines, 1):
            tokens = self._syntax.tokenize(line)
            if not tokens or tokens[0] == "primitive":
                filtered.append(line)
            else:
                filtered.append("")
                forbidden.append(Diagnostic(
                    "E101", Severity.ERROR, "syntax",
                    "в библиотеке допустимы только объявления primitive",
                    SourceLoc(filename, number),
                ))
        result = self.parse("\n".join(filtered) + "\n", filename)
        result.diagnostics.extend(forbidden)
        result.diagnostics.sort(key=lambda d: (
            d.loc.line is None, d.loc.line if d.loc.line is not None else 0
        ))
        return result

    @staticmethod
    def _net(value: str) -> str | None:
        return None if value == "-" else value
