"""Настройки оболочки и независимый снимок параметров чекера.

Данные формы отделены от виджетов: настройки можно читать и проверять,
не открывая окно. Реализации редактора и чекера здесь не импортируются.
Автор модуля по спецификации: К. Транович.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PySide6.QtCore import QSettings

# Порядок совпадает с CheckerCLI. Это имена проверок, а не их реализации.
ALL_CHECKS = ("syntax", "hierarchy", "cycle", "subckt", "pin", "net")
# QSpinBox хранит знаковое 32-битное число. Ограничение проверяем и в API,
# чтобы set_state() не обрезал переданное значение незаметно для вызывающего.
MAX_ERRORS = 2_147_483_647


@dataclass
class CheckerDialogState:
    """Значения формы, без ссылок на Qt-виджеты и без самого нетлиста.

    Пустой путь и пустой список проверок разрешены в незаполненной форме.
    Возможность запуска дополнительно проверяет build_args(). Изменение
    объекта не меняет ни окно, ни сохранённые настройки автоматически.
    """

    input_file: str = ""             # Путь к проверяемому нетлисту.
    checks: list[str] = field(default_factory=lambda: list(ALL_CHECKS))
    primitives_file: str = ""        # Пусто, если библиотека не выбрана.
    top: str = "main"                # Имя корневого блока.
    flat_nets: bool = False          # Проверять связи между уровнями.
    warnings_as_errors: bool = False  # Предупреждения меняют код выхода.
    no_warnings: bool = False        # Скрывать предупреждения в отчёте.
    output_format: str = "text"      # Поддерживаются "text" и "json".
    max_errors: int = 0              # Ноль означает отсутствие лимита.
    extra_args: str = ""             # Разбирается по правилам shlex.split.


def _validate_state(state: CheckerDialogState) -> None:
    """Отсеять ошибки вызывающего кода до изменения формы или хранилища.

    Незавершённый ввод допустим; наличие файлов здесь не проверяется.
    При неверном типе возникает TypeError, при неверном значении ValueError.
    """
    if not isinstance(state, CheckerDialogState):
        raise TypeError("Ожидается CheckerDialogState.")
    for name in ("input_file", "primitives_file", "top", "extra_args"):
        value = getattr(state, name)
        if not isinstance(value, str):
            raise TypeError(f"Поле {name} должно быть строкой.")
        if "\0" in value:
            raise ValueError(f"Поле {name} содержит нулевой символ.")
    if not isinstance(state.checks, list) or not all(
        isinstance(item, str) for item in state.checks
    ):
        raise TypeError("Проверки должны быть списком строк.")
    if any(item not in ALL_CHECKS for item in state.checks):
        raise ValueError("В списке есть неизвестная проверка.")
    if len(set(state.checks)) != len(state.checks):
        raise ValueError("Проверки не должны повторяться.")
    for name in ("flat_nets", "warnings_as_errors", "no_warnings"):
        if type(getattr(state, name)) is not bool:
            raise TypeError(f"Поле {name} должно иметь тип bool.")
    if state.output_format not in ("text", "json"):
        raise ValueError("Формат отчёта должен быть text или json.")
    if type(state.max_errors) is not int:
        raise TypeError("Максимум ошибок должен быть целым числом.")
    if not 0 <= state.max_errors <= MAX_ERRORS:
        raise ValueError(f"Максимум ошибок: от 0 до {MAX_ERRORS}.")


def _restored_value(name: str, raw: Any, default: Any) -> Any:
    """Восстановить тип после INI и заменить повреждённое поле дефолтом.

    В частности, bool("false") даёт True, поэтому флаги разбираются явно.
    Ошибка одной настройки не должна мешать пользователю открыть диалог.
    """
    if name == "checks":
        items = [raw] if isinstance(raw, str) else raw
        if isinstance(items, (list, tuple)) and all(
            isinstance(item, str) and item in ALL_CHECKS for item in items
        ):
            return list(dict.fromkeys(items))
        return list(default)
    if isinstance(default, bool):
        text = str(raw).lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
        return default
    if name == "max_errors":
        try:
            number = int(raw) if isinstance(raw, (str, int)) else -1
        except ValueError:
            return default
        if type(raw) is not bool and 0 <= number <= MAX_ERRORS:
            return number
        return default
    if not isinstance(raw, str) or "\0" in raw:
        return default
    if name == "output_format" and raw not in ("text", "json"):
        return default
    return raw


class Settings:
    """Хранить настройки через QSettings("Netlist", "Shell").

    _store — единственная ссылка на хранилище. Все ключи формы имеют
    префикс checker/. Команды лежат в paths/editor и paths/checker,
    геометрия главного окна — в window/geometry. Публичных полей нет.
    """

    def __init__(self, qsettings: QSettings | None = None) -> None:
        """Принять готовое хранилище или открыть стандартное QSettings.

        Параметр qsettings нужен, в частности, для временного INI в тестах.
        Конструктор не перезаписывает существующие пользовательские данные.
        """
        if qsettings is None:
            # Отложенный импорт позволяет работать со снимком формы даже
            # в среде без Qt; настоящий Settings по умолчанию требует Qt.
            from PySide6.QtCore import QSettings

            qsettings = QSettings("Netlist", "Shell")
        self._store = qsettings

    def get(self, key: str, default: Any = None) -> Any:
        """Вернуть значение key, а при отсутствии ключа — default.

        key должен быть непустой строкой. Чтение отсутствующего ключа
        не записывает default в хранилище и не меняет другие настройки.
        """
        self._check_key(key)
        return self._store.value(key, default)

    def set(self, key: str, value: Any) -> None:
        """Записать поддерживаемое QSettings значение value под ключом key.

        Старое значение заменяется. Возвращается None; фактическую запись
        на диск и проверку её результата выполняет отдельный метод sync().
        """
        self._check_key(key)
        self._store.setValue(key, value)

    def checker_dialog_state(self) -> CheckerDialogState:
        """Вернуть новый снимок формы из группы checker/.

        Пропущенные и повреждённые поля заменяются значениями по умолчанию.
        Список checks копируется: изменение снимка не меняет хранилище.
        """
        defaults = CheckerDialogState()
        values = {}
        for item in fields(defaults):
            default = getattr(defaults, item.name)
            raw = self.get(f"checker/{item.name}", default)
            values[item.name] = _restored_value(item.name, raw, default)
        return CheckerDialogState(**values)

    def save_checker_dialog_state(self, state: CheckerDialogState) -> None:
        """Сохранить все поля state и сразу синхронизировать хранилище.

        Незаполненные пути допустимы. TypeError и ValueError сообщают
        об ошибке данных до записи; OSError — об ошибке сохранения в sync().
        Переданный объект не меняется. При успехе возвращается None.
        """
        _validate_state(state)
        for item in fields(state):
            value = getattr(state, item.name)
            if isinstance(value, list):
                value = value.copy()
            self.set(f"checker/{item.name}", value)
        self.sync()

    def editor_command(self) -> list[str]:
        """Вернуть новый список программы и начальных аргументов редактора.

        По умолчанию это [sys.executable, "-m", "netlist.editor"].
        Файл документа добавляет ProcessRunner. Ошибочная настройка
        paths/editor вызывает ValueError, но программа не запускается.
        """
        return self._command("editor")

    def checker_command(self) -> list[str]:
        """Вернуть новый список программы и начальных аргументов чекера.

        По умолчанию это [sys.executable, "-m", "netlist.checker"].
        Файл и параметры проверки добавляет ProcessRunner. Ошибочная
        настройка paths/checker вызывает ValueError.
        """
        return self._command("checker")

    def sync(self) -> None:
        """Записать изменения; при ошибке хранилища поднять OSError.

        QSettings не выбрасывает исключение при неудачной записи сам,
        поэтому после sync() обязательно проверяется status(). GUI решает,
        как сообщить об ошибке пользователю. При успехе возвращается None.
        """
        self._store.sync()
        status = self._store.status()
        if status != self._store.Status.NoError:
            raise OSError(
                "Не удалось сохранить настройки: "
                f"{self._store.fileName()} ({status.name})."
            )

    def _command(self, name: str) -> list[str]:
        """Не интерпретировать команду как строку системной оболочки.

        Каждый аргумент хранится отдельно, поэтому пути с пробелами
        не требуют ручного экранирования и не превращаются в shell-код.
        """
        value = self.get(f"paths/{name}")
        if value is None:
            return [sys.executable, "-m", f"netlist.{name}"]
        # INI может вернуть список из одного аргумента как строку.
        # Считаем её одним путём, а не строкой для shell-разбора.
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"paths/{name}: нужен непустой список строк.")
        if not all(isinstance(arg, str) and "\0" not in arg for arg in value):
            raise ValueError(f"paths/{name}: некорректный аргумент команды.")
        if not value[0].strip():
            raise ValueError(f"paths/{name}: не указана программа.")
        return list(value)

    @staticmethod
    def _check_key(key: str) -> None:
        """Не позволять случайно читать или записывать пустое имя ключа."""
        if not isinstance(key, str):
            raise TypeError("Ключ настройки должен быть строкой.")
        if not key.strip():
            raise ValueError("Ключ настройки не может быть пустым.")
