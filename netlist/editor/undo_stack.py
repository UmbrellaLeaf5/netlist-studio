"""История Canvas по паттерну «команда», без зависимости от Qt.

Canvas уже содержит состояние на позиции index. При загрузке истории
команды не выполняются: восстанавливаются только возможность отмены и
ветка повтора. Конкретные изменения Canvas реализует автор его модуля.
"""

from __future__ import annotations

import abc
import json
from typing import TYPE_CHECKING, Callable, ClassVar

from .file_manager import read_text, write_text_atomic

if TYPE_CHECKING:
    from .canvas import Canvas


class CanvasCommand(abc.ABC):
    """Контракт одного обратимого изменения принятого состояния Canvas.

    Команда хранит прежние значения и имена, а не живые Qt-объекты.
    Это позволяет восстановить её после перезапуска редактора.

    Attributes:
        type_name: Уникальный ключ класса в реестре сериализации.
        text: Человекочитаемая подпись операции для меню.
    """

    type_name: ClassVar[str]  # Постоянный ключ сериализации в реестре.
    text: str  # Человекочитаемая подпись для меню отмены/повтора.

    @abc.abstractmethod
    def redo(self, canvas: Canvas) -> None:
        """Применить изменение, в том числе при первом push команды.

        При исключении реализация обязана оставить Canvas согласованным:
        стек не может восстановить частично выполненную чужую команду.

        Args:
            canvas: Получатель изменений, выполняемых командами истории.
        """

    @abc.abstractmethod
    def undo(self, canvas: Canvas) -> None:
        """Вернуть прежние значения, сохранённые конкретной командой.

        Args:
            canvas: Получатель изменений, выполняемых командами истории.
        """

    @abc.abstractmethod
    def to_dict(self) -> dict:
        """Вернуть JSON-совместимые данные для будущего from_dict.

        Returns:
            JSON-совместимый словарь, достаточный для восстановления.
        """

    @classmethod
    @abc.abstractmethod
    def from_dict(cls, data: dict) -> CanvasCommand:
        """Проверить свои поля и создать команду, не изменяя Canvas.

        Args:
            data: Словарь сериализованной команды или истории.

        Returns:
            Новая команда без изменения состояния Canvas.
        """

    def merge_with(self, other: CanvasCommand) -> bool:
        """Попытаться объединить уже выполненную other с этой командой.

        True означает, что сохранены исходное состояние первой команды
        и результат последней. По умолчанию объединения нет.

        Args:
            other: Следующая команда, уже применённая к Canvas.

        Returns:
            True при объединении данных команд, иначе False.
        """
        return False


# Реестр общий для процесса. Модули конкретных команд должны быть
# импортированы до UndoStack.load, иначе их типы будут неизвестны.
_REGISTRY: dict[str, type[CanvasCommand]] = {}


def register_command(cls: type[CanvasCommand]) -> type[CanvasCommand]:
    """Зарегистрировать класс по type_name и вернуть его для декоратора.

    Коллизия разных классов запрещена: одна и та же история не должна
    менять смысл в зависимости от порядка импортов модулей.

    Args:
        cls: Класс команды с уникальным непустым type_name.

    Returns:
        Тот же класс для применения функции как декоратора.

    Raises:
        ValueError: Тип некорректен либо его имя уже занято.
    """
    valid_class = isinstance(cls, type) and issubclass(cls, CanvasCommand)
    name = getattr(cls, "type_name", None)
    if not valid_class or not isinstance(name, str) or not name:
        raise ValueError("Команде необходим непустой type_name")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"Тип команды уже зарегистрирован: {name}")
    _REGISTRY[name] = cls
    return cls


def command_from_dict(data: dict) -> CanvasCommand:
    """Выбрать фабрику по data['type'], не выполняя восстановленную команду.

    Args:
        data: Словарь сериализованной команды или истории.

    Returns:
        Команда выбранного типа без применения к Canvas.

    Raises:
        ValueError: Тип неизвестен или фабрика вернула неверный объект.
    """
    if not isinstance(data, dict):
        raise ValueError("Команда должна быть словарём")
    name = data.get("type")
    if not isinstance(name, str) or name not in _REGISTRY:
        raise ValueError("Неизвестный тип команды")
    command_class = _REGISTRY[name]
    command = command_class.from_dict(data)
    if not isinstance(command, command_class):
        raise ValueError("from_dict вернул неверный тип команды")
    return command


def _encoded(command: CanvasCommand) -> dict:
    """Добавить ключ type, не изменяя словарь, принадлежащий команде.

    Args:
        command: Команда, предоставляющая сериализуемые данные.

    Returns:
        Копия данных команды с обязательным ключом type.

    Raises:
        ValueError: Тип данных команды не совпадает с type_name.
    """
    data = dict(command.to_dict())
    if data.get("type", command.type_name) != command.type_name:
        raise ValueError("Тип данных не совпадает с type_name команды")
    data["type"] = command.type_name
    return data


@register_command
class _Macro(CanvasCommand):
    """Внутренняя команда для группы, включая вложенные макросы.

    Порядок undo обратен redo, чтобы зависимые изменения откатывались
    корректно. Уже выполненные дочерние команды компенсируются при сбое.

    Attributes:
        type_name: Зарезервированный ключ сериализации группы.
        text: Подпись всей группы в меню истории.
        commands: Дочерние команды в порядке выполнения redo.
    """

    type_name = "__macro__"  # Зарезервирован и сохраняется в JSON истории.

    def __init__(self, text: str, commands: list[CanvasCommand]) -> None:
        """Сохранить подпись группы и последовательность дочерних команд.

        Args:
            text: Подпись группы команд для меню истории.
            commands: Упорядоченная последовательность команд.
        """
        self.text = text
        self.commands = commands

    def redo(self, canvas: Canvas) -> None:
        """Повторить группу; при сбое откатить успешно выполненную часть.

        Args:
            canvas: Получатель изменений, выполняемых командами истории.
        """
        done: list[CanvasCommand] = []
        try:
            for command in self.commands:
                command.redo(canvas)
                done.append(command)
        except Exception:
            for command in reversed(done):
                command.undo(canvas)
            raise

    def undo(self, canvas: Canvas) -> None:
        """Отменить группу; при сбое восстановить уже отменённые команды.

        Args:
            canvas: Получатель изменений, выполняемых командами истории.
        """
        done: list[CanvasCommand] = []
        try:
            for command in reversed(self.commands):
                command.undo(canvas)
                done.append(command)
        except Exception:
            for command in reversed(done):
                command.redo(canvas)
            raise

    def to_dict(self) -> dict:
        """Сохранить состав группы, чтобы отмена целиком пережила перезапуск.

        Returns:
            JSON-совместимый словарь, достаточный для восстановления.
        """
        return {
            "type": self.type_name,
            "text": self.text,
            "commands": [_encoded(command) for command in self.commands],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CanvasCommand:
        """Восстановить вложенные команды через тот же общий реестр.

        Args:
            data: Словарь сериализованной команды или истории.

        Returns:
            Новая команда без изменения состояния Canvas.

        Raises:
            ValueError: Неверные поля группы или неизвестные команды.
            KeyError: Отсутствуют обязательные поля группы.
        """
        if not isinstance(data["text"], str):
            raise ValueError("Неверная подпись макрокоманды")
        if not isinstance(data["commands"], list):
            raise ValueError("Неверный список команд макрокоманды")
        commands = [command_from_dict(item) for item in data["commands"]]
        return cls(data["text"], commands)


def _decode_history(
    data: dict, netlist_sha256: str
) -> tuple[list[CanvasCommand], int]:
    """Проверить всю историю до её принятия; Canvas здесь не используется.

    bool исключается явно: в Python он является подклассом int, но не
    является допустимым индексом или номером версии файлового формата.

    Args:
        data: Словарь сериализованной команды или истории.
        netlist_sha256: SHA-256 байтов нетлиста, которому соответствует
            история.

    Returns:
        Восстановленные команды и проверенный индекс истории.

    Raises:
        ValueError: Версия, хэш или индекс истории некорректны.
    """
    valid_header = (
        isinstance(data, dict)
        and type(data.get("version")) is int
        and data["version"] == 1
        and data.get("netlist_sha256") == netlist_sha256
    )
    if not valid_header:
        raise ValueError("История не соответствует нетлисту")
    if not isinstance(data.get("commands"), list):
        raise ValueError("Неверный список команд")
    commands = [command_from_dict(item) for item in data["commands"]]
    index = data.get("index")
    if type(index) is not int or not 0 <= index <= len(commands):
        raise ValueError("Неверный индекс истории")
    return commands, index


class UndoStack:
    """Неограниченная история одного Canvas с уведомлениями через fn().

    _index разделяет выполненные команды [0:index] и ветку повтора
    [index:count]. В открытом макросе команды уже применяются к Canvas,
    но единый шаг истории появляется только после end_macro.

    Attributes:
        index: Позиция истории только для чтения.
        count: Количество шагов истории, включая ветку повтора.
        _canvas: получатель redo/undo; стек не владеет его моделью
            и не изменяет её при загрузке или очистке истории.
        _commands: упорядоченные завершённые шаги истории, включая
            отменённые команды, доступные для повторного выполнения.
        _index: граница выполненной части _commands; предыдущая
            команда отменяется, следующая повторяется.
        _macros: стек открытых групп; последняя принимает команды,
            а завершённая вложенная группа включается в родительскую.
        _listeners: callbacks без аргументов для обновления меню
            и кнопок после изменения содержимого или позиции стека.
    """

    def __init__(self, canvas: Canvas) -> None:
        """Привязать стек к Canvas, не изменяя его начальное состояние.

        Args:
            canvas: Получатель изменений, выполняемых командами истории.
        """
        self._canvas = canvas
        self._commands: list[CanvasCommand] = []
        self._index = 0
        self._macros: list[_Macro] = []
        self._listeners: list[Callable[[], None]] = []

    @property
    def index(self) -> int:
        """Число выполненных шагов; извне изменять позицию нельзя.

        Returns:
            Количество выполненных шагов истории.
        """
        return self._index

    @property
    def count(self) -> int:
        """Число всех шагов, включая пока отменённую ветку redo.

        Returns:
            Общее число шагов, включая доступные для повтора.
        """
        return len(self._commands)

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Подписать GUI на изменения; один callback хранится один раз.

        Args:
            fn: Callback без аргументов для обновления состояния GUI.
        """
        if fn not in self._listeners:
            self._listeners.append(fn)

    def _notify(self) -> None:
        """Оповестить снимок подписчиков: callback может добавить слушателя."""
        for fn in tuple(self._listeners):
            fn()

    def _idle(self) -> None:
        """Не разрешить сохранение или сброс незавершённой группы команд.

        Raises:
            RuntimeError: В стеке есть незавершённый макрос.
        """
        if self._macros:
            raise RuntimeError("Сначала завершите макрокоманду")

    @staticmethod
    def _append(commands: list[CanvasCommand], cmd: CanvasCommand) -> None:
        """Слить последовательные команды одного класса либо добавить шаг.

        redo новой команды уже выполнен. merge_with меняет только данные
        истории, иначе одно движение могло бы примениться к Canvas дважды.

        Args:
            commands: Упорядоченная последовательность команд.
            cmd: Новая команда для выполнения и добавления в историю.
        """
        if commands and type(commands[-1]) is type(cmd):
            if commands[-1].merge_with(cmd):
                return
        commands.append(cmd)

    def push(self, cmd: CanvasCommand) -> None:
        """Выполнить команду, заменить ветку redo и уведомить GUI.

        Ветка повтора отбрасывается после redo: если оно выбросит
        исключение, прежняя история останется доступной пользователю.

        Args:
            cmd: Новая команда для выполнения и добавления в историю.
        """
        cmd.redo(self._canvas)
        if self._macros:
            self._append(self._macros[-1].commands, cmd)
        else:
            del self._commands[self._index :]
            self._append(self._commands, cmd)
            self._index = len(self._commands)
        self._notify()

    def can_undo(self) -> bool:
        """Разрешить отмену только завершённого шага истории.

        Returns:
            True при наличии завершённого шага для отмены.
        """
        return not self._macros and self._index > 0

    def can_redo(self) -> bool:
        """Разрешить повтор при наличии ветки и отсутствии открытой группы.

        Returns:
            True при наличии шага для повторного выполнения.
        """
        return not self._macros and self._index < self.count

    def undo_text(self) -> str | None:
        """Получить подпись ближайшей отмены для ToolPalette или меню.

        Returns:
            Подпись ближайшей отмены либо None, если отмена недоступна.
        """
        if self.can_undo():
            return self._commands[self._index - 1].text
        return None

    def redo_text(self) -> str | None:
        """Получить подпись ближайшего повтора либо None при его отсутствии.

        Returns:
            Подпись ближайшего повтора либо None, если повтор недоступен.
        """
        if self.can_redo():
            return self._commands[self._index].text
        return None

    def undo(self) -> bool:
        """Отменить один завершённый шаг; без доступного шага вернуть False.

        Returns:
            True после отмены, False при отсутствии доступного шага.
        """
        if not self.can_undo():
            return False
        self._commands[self._index - 1].undo(self._canvas)
        self._index -= 1  # Исключение команды не должно сдвигать позицию.
        self._notify()
        return True

    def redo(self) -> bool:
        """Повторить один шаг, сдвигая индекс только после успешного redo.

        Returns:
            True после повтора, False при отсутствии доступного шага.
        """
        if not self.can_redo():
            return False
        self._commands[self._index].redo(self._canvas)
        self._index += 1
        self._notify()
        return True

    def begin_macro(self, text: str) -> None:
        """Начать группу; вложенная группа станет командой внешнего макроса.

        Args:
            text: Подпись группы команд для меню истории.
        """
        self._macros.append(_Macro(text, []))
        self._notify()  # Кнопки undo/redo временно становятся недоступными.

    def end_macro(self) -> None:
        """Закончить группу без повторного применения её команд к Canvas.

        Raises:
            RuntimeError: Нет открытого макроса для завершения.
        """
        if not self._macros:
            raise RuntimeError("Нет открытой макрокоманды")
        macro = self._macros.pop()
        if macro.commands:
            if self._macros:
                self._macros[-1].commands.append(macro)
            else:
                del self._commands[self._index :]
                self._commands.append(macro)
                self._index = self.count
        # Пустая группа не создаёт шаг и не уничтожает ветку повтора.
        self._notify()

    def clear(self) -> None:
        """Забыть историю нового документа, не откатывая содержимое Canvas.

        Raises:
            RuntimeError: Нельзя очистить историю внутри открытого макроса.
        """
        self._idle()
        self._commands.clear()
        self._index = 0
        self._notify()

    def save(self, path: str, netlist_sha256: str) -> None:
        """Атомарно сохранить все шаги и позицию с хэшем текущего .net.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            netlist_sha256: SHA-256 байтов нетлиста, которому соответствует
                история.

        Raises:
            RuntimeError: Не завершён открытый макрос.
            FileAccessError: Не удалось записать историю.
        """
        self._idle()
        data = {
            "version": 1,
            "netlist_sha256": netlist_sha256,
            "index": self._index,
            "commands": [_encoded(cmd) for cmd in self._commands],
        }
        text = json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n"
        write_text_atomic(path, text)

    def load(self, path: str, netlist_sha256: str) -> bool:
        """Принять историю целиком либо оставить пустую; Canvas не менять.

        Ошибки фабрик сторонних команд также означают отказ истории.
        Показ W702 остаётся обязанностью окна: стек ничего не знает о GUI.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            netlist_sha256: SHA-256 байтов нетлиста, которому соответствует
                история.

        Returns:
            True при принятии истории, иначе False с очищенным стеком.

        Raises:
            RuntimeError: Нельзя загружать историю внутри открытого макроса.
        """
        self._idle()
        self._commands, self._index = [], 0
        try:
            data = json.loads(read_text(path))
            commands, index = _decode_history(data, netlist_sha256)
        except Exception:
            # Частично восстановленная история опаснее полного отказа:
            # индекс больше не соответствовал бы состоянию Canvas.
            self._notify()
            return False
        self._commands, self._index = commands, index
        self._notify()
        return True
