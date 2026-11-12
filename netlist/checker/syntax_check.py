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

if TYPE_CHECKING:
    from netlist.core.model import Diagnostic, NetlistView, CheckOptions

NAME = "syntax"

# Базовый набор зарезервированных слов (требует согласования).
KEYWORDS: frozenset[str] = frozenset({
    "main", "pin", "instance", "primitive", "net"
})

# Регулярное выражение для допустимого идентификатора.
# Разрешаем буквы, цифры и подчеркивания, не начиная с цифры.
ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class LineKind(Enum):
    """
    Виды синтаксических строк нетлиста.

    Каждое значение соответствует определенной грамматической форме.
    """
    EMPTY = auto()  # Пустая строка или только комментарий
    BLOCK_START = auto()  # Начало блока: "block_name:"
    PIN = auto()  # Внешний пин: "pin name net"
    INSTANCE = auto()  # Объявление инстанса: "inst name: type"
    CONNECTION = auto()  # Подключение: "inst.pin net"
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

    # Начало блока: одиночный токен, заканчивающийся на ':'
    if len(tokens) == 1 and first.endswith(":"):
        return LineKind.BLOCK_START

    # Внешний пин блока: "pin name net"
    if first == "pin" and len(tokens) == 3:
        return LineKind.PIN

    # Объявление инстанса: "instance name: type"
    # или просто "inst name type" (требует согласования).
    if first in ("instance", "inst") and len(tokens) in (3, 4):
        return LineKind.INSTANCE

    # Подключение пина: "inst.pin net"
    # Проверяем, что первый токен содержит точку.
    if "." in first and len(tokens) == 2:
        return LineKind.CONNECTION

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
                    "E001", "Нераспознанная строка",
                    line_num, filename
                ))
                continue

            # Проверка контекста: внутри блока или снаружи.
            if kind == LineKind.BLOCK_START:
                if in_block:
                    diags.append(self._make_diag(
                        "E002", "Вложенные блоки запрещены",
                        line_num, filename
                    ))
                else:
                    in_block = True
                    # Проверяем, что имя блока корректно (без ':').
                    block_name = tokens[0][:-1]
                    if not ID_RE.match(block_name):
                        diags.append(self._make_diag(
                            "E003", f"Некорректное имя блока: {block_name}",
                            line_num, filename
                        ))
            elif not in_block:
                diags.append(self._make_diag(
                    "E004", "Строка вне блока",
                    line_num, filename
                ))

        # В конце проверяем, закрыт ли блок.
        if in_block:
            diags.append(self._make_diag(
                "E005", "Незакрытый блок в конце файла",
                len(lines), filename
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
        errors = []
        # Для "pin name net" проверяем name (индекс 1).
        if kind == LineKind.PIN:
            name = tokens[1]
            if not ID_RE.match(name):
                errors.append(self._make_diag(
                    "E003", f"Некорректный идентификатор пина: {name}",
                    line_num, filename
                ))
        # Для "inst.pin net" проверяем обе части.
        elif kind == LineKind.CONNECTION:
            parts = tokens[0].split(".")
            if len(parts) == 2:
                inst_name, pin_name = parts
                if not ID_RE.match(inst_name) or not ID_RE.match(pin_name):
                    errors.append(self._make_diag(
                        "E003",
                        f"Некорректный идентификатор подключения: {tokens[0]}",
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
        return {
            "code": code,
            "message": message,
            "loc": {"filename": filename, "line": line_num},
            "check": NAME
        }

    def check(
            self,
            view: NetlistView,
            opts: CheckOptions = None,
            **kw
    ) -> list[Diagnostic]:
        """
        Заглушка для совместимости с общим интерфейсом проверок.

        Возвращает пустой список, так как синтаксические ошибки
        уже были получены при разборе исходного текста.
        """
        return []
