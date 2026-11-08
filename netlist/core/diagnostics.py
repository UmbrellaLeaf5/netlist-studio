"""Общие типы сообщений и параметров ядра Netlist Studio.

Автор: Д. Криворучко. Контракт: docs/interfaces.md, раздел 2.1.

Типы этого файла не зависят от модели, парсера, проверок или GUI.
Их импортируют и ядро, и другие слои проекта, поэтому здесь оставлены
только простые записи: диагностика, положение в источнике, ссылка на
пин и настройки чекера.
"""

from dataclasses import asdict, dataclass
from enum import Enum


class Severity(Enum):
    """Уровень сообщения; строковые значения используются в JSON."""

    ERROR = "error"
    WARNING = "warning"


class InstanceKind(str, Enum):
    """Допустимый вид инстанса в формате нетлиста.

    Наследование от str сохраняет совместимость с проверками и
    сериализацией, которые сравнивают значение с "sub" или "prim".
    В модели при этом используется конечный enum, а не произвольная
    строка, поэтому ошибочный вид отклоняется на границе изменения.
    """

    SUB = "sub"
    PRIM = "prim"

    def __str__(self) -> str:
        """Сериализовать имя вида тем же текстом, что и его value."""
        return self.value


@dataclass(frozen=True)
class SourceLoc:
    """Положение в источнике; None означает отсутствие сведений."""

    file: str | None       # None: объект создан в редакторе, файла нет
    line: int | None       # номер с 1; None: положение строки неизвестно


@dataclass(frozen=True)
class Diagnostic:
    """Неизменяемое сообщение проверки, без вывода и файлового ввода."""

    code: str              # код из таблицы ошибок: "E501", "W604", ...
    severity: Severity
    check: str             # имя проверки: "pin", "subckt", ...
    message: str           # человекочитаемый текст, по-русски
    loc: SourceLoc
    block: str | None = None
    obj: str | None = None

    def format(self) -> str:
        """Представить сообщение как file:line: level CODE [check] text.

        Возвращает строку без завершающего перевода строки. Сам метод
        ничего не печатает: место вывода выбирает CLI или редактор.
        У объектов редактора файла нет, поэтому используется <editor>.
        Отсутствующий номер строки пропускается, а не заменяется нулём.
        """
        location = self.loc.file if self.loc.file is not None else "<editor>"
        if self.loc.line is not None:
            location += f":{self.loc.line}"
        return (f"{location}: {self.severity.value} {self.code} "
                f"[{self.check}] {self.message}")

    def to_dict(self) -> dict:
        """Получить поля сообщения для JSON-вывода.

        Возвращает новый словарь: loc становится вложенным словарём,
        severity — строкой "error" или "warning". Сам Enum нельзя
        передать стандартному json.dumps, поэтому заменяем его явно.
        Необязательные поля сохраняются со значением None.
        """
        result = asdict(self)
        result["severity"] = self.severity.value
        return result


@dataclass(frozen=True)
class PinRef:
    """Адрес пина внутри блока; instance=None обозначает внешний пин."""

    instance: str | None
    pin: str

    def __str__(self) -> str:
        """Вернуть имя внешнего пина либо пару instance.pin."""
        if self.instance is None:
            return self.pin
        return f"{self.instance}.{self.pin}"


@dataclass
class CheckOptions:
    """Параметры проверок; модель их хранить или применять не должна."""

    top: str = "main"                   # корень обхода иерархии
    strict_primitives: bool = True      # неизвестный примитив — ошибка
    flat_nets: bool = False             # искать цепи после раскрытия
    max_errors: int | None = None       # None: число ошибок не ограничено
