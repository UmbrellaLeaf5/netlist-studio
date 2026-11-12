"""
SYNTAXCHECK

Модуль синтаксической проверки исходного текста нетлиста.

Отвечает за лексический анализ строк (токенизацию), классификацию
строк по видам (LineKind) и проверку синтаксической корректности
файла (check_lines).
Модульная проверка check() возвращает пустой список, так как
синтаксис уже был проверен на этапе разбора текста.
"""

from __future__ import annotations

import re
from enum import Enum, auto
from typing import TYPE_CHECKING, Sequence

from netlist.core.model import Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    from netlist.core.model import CheckOptions, NetlistView

NAME = "syntax"
CODES: dict[str, str] = {
    "E101": "нераспознанная строка",
    "E102": "некорректный или зарезервированный идентификатор",
    "E104": "строка расположена вне допустимого контекста",
    "E105": "вложенный или незакрытый блок",
    "E106": "end без открытого блока",
}

# Базовый набор зарезервированных слов (требует согласования).
KEYWORDS: frozenset[str] = frozenset({
    "main", "pin", "instance", "primitive", "net"
})

# Регулярное выражение для допустимого идентификатора.
# Разрешаем буквы, цифры и подчеркивания, не начиная с цифры.
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LineKind(Enum):
    """
    Виды синтаксических строк нетлиста.

    Каждое значение соответствует определенной грамматической форме.
    """
    EMPTY = auto()  # Пустая строка или только комментарий
    PRIMITIVE = auto()
    BLOCK = auto()  # Начало блока: "block_name:"
    END = auto()
    PIN = auto()  # Внешний пин: "pin name net"
    NET = auto()
    INST = auto()  # Объявление инстанса: "inst name: type"
    CONN = auto()  # Подключение: "inst.pin net"
    UNKNOWN = auto()  # Неизвестная форма


# Лексический анализ и классификация


def tokenize(line: str) -> list[str]:
    """
    Разбивает строку на токены, удаляя комментарии.

    Комментарием считается всё, что начинается с символа '#'
    (требует согласования, но это стандарт для подобных форматов).
    Возвращает пустой список для пустых строк или строк-комментариев.
    """
    # Отрезаем комментарий, если он есть.
    comment_start = line.find("#")
    if comment_start != -1:
        line = line[:comment_start]

    # Разбиваем по пробелам и отбрасываем пустые токены.
    # Используем split() без аргументов, чтобы убрать лишние пробелы.
    return line.split()


def classify(tokens: list[str]) -> LineKind:
    """
    Определяет вид строки по списку токенов.

    Возвращает LineKind.EMPTY для пустого списка, UNKNOWN, если
    форма не распознана. Диагностика ошибки формируется в
    check_lines, а не здесь.
    """
    if not tokens:
        return LineKind.EMPTY

    first = tokens[0]

    if first == "primitive" and len(tokens) >= 2:
        return LineKind.PRIMITIVE
    # Начало блока: одиночный токен, заканчивающийся на ':'
    if first == "block" and len(tokens) == 2:
        return LineKind.BLOCK
    if first == "end" and len(tokens) == 1:
        return LineKind.END
    # Внешний пин блока: "pin name net"
    if first == "pin" and len(tokens) == 3:
        return LineKind.PIN
    if first == "net" and len(tokens) == 2:
        return LineKind.NET
    # Объявление инстанса: "instance name: type"
    # или просто "inst name type" (требует согласования).
    if first == "inst" and len(tokens) == 4 and tokens[2] in ("sub", "prim"):
        return LineKind.INST

    # Подключение пина: "inst.pin net"
    # Проверяем, что первый токен содержит точку.
    if len(tokens) == 2 and "." in first:
        return LineKind.CONN
    return LineKind.UNKNOWN


# Синтаксическая проверка файла


class SyntaxCheck:
    """
    Проверяет синтаксис исходного текста нетлиста.

    Хранит состояние контекста (внутри блока или снаружи) и
    накапливает диагностики.
    """

    def check_lines(
            self,
            lines: Sequence[str],
            filename: str | None = None
    ) -> list[Diagnostic]:
        """
        Проверяет строки файла и возвращает список ошибок.

        Обрабатывает строки последовательно, собирая все возможные
        ошибки, а не останавливаясь на первой. При ошибке пытается
        восстановить контекст, чтобы продолжить проверку.
        """
        diags: list[Diagnostic] = []
        in_block = False

        for line_num, line in enumerate(lines, start=1):
            tokens = tokenize(line)
            kind = classify(tokens)

            if kind == LineKind.EMPTY:
                continue

            # Проверка идентификаторов в токенах, где они ожидаются.
            id_error = self._check_identifiers(tokens, kind, line_num, filename)
            if id_error:
                diags.extend(id_error)

            # Проверка количества токенов (уже частично сделана в classify).
            if kind == LineKind.UNKNOWN:
                diags.append(self._make_diag(
                    "E101", "Нераспознанная строка", line_num, filename
                ))
                continue

            # Проверка контекста: внутри блока или снаружи.
            if kind is LineKind.BLOCK:
                if in_block:
                    diags.append(self._make_diag(
                        "E105", "Вложенные блоки запрещены", line_num, filename
                    ))
                else:
                    in_block = True
            elif kind is LineKind.END:
                if not in_block:
                    diags.append(self._make_diag(
                        "E106", "end без открытого блока", line_num, filename
                    ))
                else:
                    in_block = False
            elif kind is LineKind.PRIMITIVE:
                if in_block:
                    diags.append(self._make_diag(
                        "E104", "primitive допустим только вне блока",
                        line_num, filename
                    ))
            elif not in_block:
                diags.append(self._make_diag(
                    "E104", "Строка вне блока", line_num, filename
                ))

            # Проверяем, что имя блока корректно (без ':').
            diags.extend(self._check_identifiers(tokens, kind, line_num, filename))

        # В конце проверяем, закрыт ли блок.
        if in_block:
            diags.append(self._make_diag(
                "E105", "Незакрытый блок в конце файла", len(lines), filename
            ))

        return diags

    def _check_identifiers(
            self, tokens: list[str], kind: LineKind,
            line_num: int, filename: str | None
    ) -> list[Diagnostic]:
        """
        Проверяет корректность идентификаторов в токенах.

        В зависимости от вида строки, проверяет определенные позиции.
        """
        if kind == LineKind.PRIMITIVE:
            names = tokens[1:]
        elif kind == LineKind.BLOCK:
            names = tokens[1:]
        # Для "pin name net" проверяем name (индекс 1).
        elif kind == LineKind.PIN:
            names = tokens[1:2] + ([] if tokens[2] == "-" else tokens[2:3])
        elif kind == LineKind.NET:
            names = tokens[1:]
        # Для "inst.pin net" проверяем обе части.
        elif kind == LineKind.INST:
            names = tokens[1:2] + tokens[3:]
        elif kind == LineKind.CONN:
            names = tokens[0].replace(".", " ").split()
            if tokens[1] != "-":
                names.append(tokens[1])
        else:
            names = []

        errors = []
        for name in names:
            if not ID_RE.fullmatch(name) or name in KEYWORDS:
                errors.append(self._make_diag(
                    "E102", f"Некорректный идентификатор: {name}",
                    line_num, filename
                ))
        return errors

    def _make_diag(
            self, code: str, message: str,
            line_num: int, filename: str | None
    ) -> Diagnostic:
        """
        Создает объект Diagnostic для синтаксической ошибки.

        Примечание: точная структура Diagnostic требует согласования.
        Здесь используется упрощенная версия.
        """
        # Заглушка: в реальном коде нужно импортировать Diagnostic
        # и создать его с нужными полями (loc, severity и т.д.).
        # Для компиляции без модели используем словарь.
        return Diagnostic(
            code, Severity.ERROR, NAME, message, SourceLoc(filename, line_num)
        )

    def check(
            self,
            view: NetlistView,
            opts: CheckOptions | None = None,
            **kwargs: object
    ) -> list[Diagnostic]:
        """
        Заглушка для совместимости с общим интерфейсом проверок.

        Возвращает пустой список, так как синтаксические ошибки
        уже были получены при разборе исходного текста.
        """
        return []


def check(
    view: NetlistView, opts: CheckOptions | None = None, **kwargs: object
) -> list[Diagnostic]:
    """Публичная функция проверки, используемая общим интерфейсом чекера."""
    return []
