"""SyntaxCheck: грамматика .net и общая лексика для NetlistParser.

Исправленная версия приложенного syntaxcheck.py по review_checker_modules.md.
Семантические ссылки и повторные имена здесь не проверяются.
"""

from __future__ import annotations

import re
from enum import Enum, auto
from typing import Sequence

from netlist.core.model import CheckOptions, Diagnostic, NetlistView, Severity, SourceLoc

NAME = "syntax"
CODES: dict[str, str] = {
    "E101": "нераспознанная строка",
    "E102": "некорректный идентификатор",
    "E103": "неверное число элементов строки",
    "E104": "строка блока вне блока",
    "E105": "вложенный или незакрытый блок",
    "E106": "end без открытого блока",
    "E107": "зарезервированное слово вместо идентификатора",
    "E108": "недопустимый не-ASCII символ",
    "E109": "primitive внутри блока",
}
KEYWORDS = frozenset({"block", "end", "pin", "net", "inst", "sub", "prim", "primitive"})
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class LineKind(Enum):
    BLANK = auto()
    PRIMITIVE = auto()
    BLOCK = auto()
    END = auto()
    PIN = auto()
    NET = auto()
    INST = auto()
    CONN = auto()


def tokenize(line: str) -> list[str]:
    """Удалить комментарий; разделители — только пробел и табуляция."""
    text = line.split("#", 1)[0].strip(" \t")
    return re.split(r"[ \t]+", text) if text else []


def classify(tokens: list[str]) -> LineKind | None:
    """Определить вид строки независимо от количества аргументов."""
    if not tokens:
        return LineKind.BLANK
    kinds = {
        "primitive": LineKind.PRIMITIVE, "block": LineKind.BLOCK,
        "end": LineKind.END, "pin": LineKind.PIN,
        "net": LineKind.NET, "inst": LineKind.INST,
    }
    if tokens[0] in kinds:
        return kinds[tokens[0]]
    if "." in tokens[0]:
        return LineKind.CONN
    return None


class SyntaxCheck:
    def check_lines(self, lines: Sequence[str], filename: str | None = None
                    ) -> list[Diagnostic]:
        diags: list[Diagnostic] = []
        current: str | None = None
        opened_at: int | None = None

        def error(code: str, message: str, number: int,
                  obj: str | None = None) -> None:
            diags.append(Diagnostic(code, Severity.ERROR, NAME, message,
                                    SourceLoc(filename, number), current, obj))

        counts = {LineKind.BLOCK: 2, LineKind.END: 1, LineKind.PIN: 3,
                  LineKind.NET: 2, LineKind.INST: 4, LineKind.CONN: 2}
        for number, line in enumerate(lines, 1):
            # Проверяем и комментарии: весь файл должен быть ASCII.
            if not line.isascii():
                error("E108", "недопустимый не-ASCII символ", number)
                continue
            if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in line):
                error("E101", "недопустимый управляющий символ", number)
                continue
            tokens = tokenize(line)
            kind = classify(tokens)
            if kind == LineKind.BLANK:
                continue
            if kind is None:
                error("E101", "нераспознанный тип строки", number)
                continue
            if (kind == LineKind.PRIMITIVE and len(tokens) < 2
                    or kind in counts and len(tokens) != counts[kind]):
                error("E103", "неверное число элементов строки", number)
                continue

            before = len(diags)
            names: list[str] = []
            if kind in (LineKind.BLOCK, LineKind.NET):
                names = tokens[1:]
            elif kind == LineKind.PRIMITIVE:
                names = tokens[1:]
            elif kind == LineKind.PIN:
                names = [tokens[1]] + ([] if tokens[2] == "-" else [tokens[2]])
            elif kind == LineKind.INST:
                names = [tokens[1], tokens[3]]
                if tokens[2] not in ("sub", "prim"):
                    error("E101", "вид инстанса должен быть sub или prim", number)
            elif kind == LineKind.CONN:
                parts = tokens[0].split(".")
                if len(parts) != 2:
                    error("E102", "подключение должно иметь вид INST.PIN", number)
                else:
                    names = parts
                if tokens[1] != "-":
                    names.append(tokens[1])
            for name in names:
                if name in KEYWORDS:
                    error("E107", f"зарезервированное слово '{name}' вместо имени", number, name)
                elif ID_RE.fullmatch(name) is None:
                    error("E102", f"некорректный идентификатор '{name}'", number, name)
            if len(diags) != before:
                continue

            if kind == LineKind.BLOCK:
                if current is not None:
                    error("E105", "block внутри незакрытого блока", number, tokens[1])
                else:
                    current, opened_at = tokens[1], number
            elif kind == LineKind.END:
                if current is None:
                    error("E106", "end без открытого блока", number)
                else:
                    current, opened_at = None, None
            elif kind == LineKind.PRIMITIVE:
                if current is not None:
                    error("E109", "primitive внутри блока", number, tokens[1])
            elif current is None:
                error("E104", "строка блока вне блока", number)

        if opened_at is not None:
            # Парсер сохраняет объявление с E105 на открывающей строке.
            error("E105", "блок не закрыт к концу файла", opened_at, current)
        return diags


def check(view: NetlistView, opts: CheckOptions | None = None, **kw
          ) -> list[Diagnostic]:
    """Синтаксические сообщения уже получены на этапе разбора текста."""
    return []
