"""Автосохранение принятого состояния документа по таймеру PySide6.

Сервис не вызывает Canvas.commit и не сохраняет непроверенный черновик.
EditorMainWindow передаёт get_state, возвращающий последнюю принятую
модель, геометрию и стек истории. Методы вызываются в GUI-потоке Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .file_manager import FileManager, history_path
from .undo_stack import UndoStack

if TYPE_CHECKING:
    from netlist.core.model import Netlist


@dataclass
class DocumentState:
    """Согласованный набор данных для одного прохода автосохранения.

    model должна быть принятой моделью, а не черновиком Canvas.
    История привязывается к байтам именно этой модели через SaveResult.

    Attributes:
        model: Принятая модель, прошедшая проверки редактора.
        schematic: Текущая геометрия блоков, экземпляров и цепей.
        undo_stack: История, соответствующая сохраняемому состоянию.
    """

    model: Netlist
    schematic: dict
    undo_stack: UndoStack


class Autosave(QObject):
    """Периодически сохранять изменения и сообщать результат сигналами.

    Остановка таймера не сбрасывает dirty: окно должно иметь возможность
    вызвать flush при закрытии. Новый документ обслуживает новый экземпляр.

    Attributes:
        _fm: файловый менеджер текущего документа; хранит его путь
            и выполняет сохранение модели вместе с геометрией.
        _get_state: callback, возвращающий актуальную принятую
            модель, геометрию и историю непосредственно перед записью.
        _dirty: есть ли изменения, ещё не записанные полностью:
            сбрасывается только после записи документа и истории.
        _revision: счётчик mark_dirty; позволяет сохранить _dirty,
            если новое изменение пришло во время текущего flush.
        _flushing: признак выполняющегося flush; защищает от
            повторного входа, например из обработчика сигнала saved.
        _timer: дочерний QTimer; вызывает flush через заданный
            интервал и уничтожается вместе с объектом Autosave.
        saved: Сигнал успешной записи с путём документа.
        failed: Сигнал ошибки с её текстовым описанием.
    """

    saved = Signal(str)  # Путь после успешной записи документа и истории.
    failed = Signal(str)  # Текст ошибки для отображения владельцем окна.

    def __init__(
        self,
        file_manager: FileManager,
        get_state: Callable[[], DocumentState],
        interval_ms: int = 30_000,
        parent: QObject | None = None,
    ) -> None:
        """Создать остановленный таймер; enable вызывается после открытия.

        Args:
            file_manager: Менеджер текущего документа и его блокировки.
            get_state: Callback получения принятой модели, геометрии и
                истории.
            interval_ms: Интервал таймера; по умолчанию 30 000 мс.
            parent: Родительский QObject, управляющий временем жизни
                сервиса.

        Raises:
            ValueError: Некорректный интервал таймера.
        """
        super().__init__(parent)
        self._fm = file_manager
        self._get_state = get_state
        self._dirty = False
        self._revision = 0  # Позволяет заметить изменение во время flush.
        self._flushing = False  # Защита от повторного входа из Qt-сигналов.
        # QObject-владелец управляет временем жизни таймера.
        self._timer = QTimer(self)
        self.set_interval(interval_ms)
        self._timer.timeout.connect(self.flush)

    def enable(self) -> None:
        """Запустить таймер; документ без пути требует первого сохранения.

        Raises:
            RuntimeError: У документа ещё нет пути для сохранения.
        """
        if self._fm.current_path is None:
            raise RuntimeError("Сначала сохраните или откройте документ")
        self._timer.start()

    def disable(self) -> None:
        """Остановить автоматические записи, сохранив возможность flush."""
        self._timer.stop()

    @property
    def enabled(self) -> bool:
        """Вернуть фактическую активность QTimer, а не отдельный флаг.

        Returns:
            True, если таймер автосохранения запущен.
        """
        return self._timer.isActive()

    def set_interval(self, ms: int) -> None:
        """Задать положительный интервал в диапазоне целого числа Qt.

        Нулевой интервал занял бы очередь событий непрерывными вызовами,
        а отрицательный имеет разное поведение в разных версиях Qt.

        Args:
            ms: Положительное число миллисекунд в диапазоне целого Qt.

        Raises:
            ValueError: Интервал не является допустимым целым числом Qt.
        """
        if type(ms) is not int or not 0 < ms <= 2_147_483_647:
            raise ValueError(
                "Интервал должен быть положительным целым числом миллисекунд"
            )
        self._timer.setInterval(ms)

    def mark_dirty(self) -> None:
        """Отметить изменение после успешного Canvas.commit, не писать сразу."""
        self._revision += 1
        self._dirty = True

    def _save_state(self) -> str:
        """Сохранить .net/.sch, затем историю с хэшем записанного .net.

        Любая ошибка передаётся flush; успехом считается завершение обеих
        операций. Нельзя привязывать историю к хэшу старого документа.

        Returns:
            Путь документа после успешной записи документа и истории.

        Raises:
            RuntimeError: У текущего документа нет пути.
            OSError: Ошибка записи документа или истории.
        """
        if self._fm.current_path is None:
            raise RuntimeError("У документа нет пути для автосохранения")
        state = self._get_state()
        result = self._fm.save(state.model, state.schematic)
        state.undo_stack.save(history_path(result.path), result.sha256)
        return result.path

    def flush(self) -> bool:
        """Сохранить dirty-состояние по таймеру или при закрытии окна.

        Нет изменений — успех без записи. При ошибке dirty сохраняется
        для следующей попытки. Изменения, пришедшие во время сохранения,
        требуют ещё одной записи и не должны быть ошибочно сброшены.

        Returns:
            True при успехе или отсутствии изменений, иначе False.
        """
        if self._flushing:
            return False
        if not self._dirty:
            return True
        self._flushing = True
        revision = self._revision
        try:
            path = self._save_state()
            self._dirty = revision != self._revision
            self.saved.emit(path)
            return True
        except Exception as exc:
            self.failed.emit(str(exc))
            return False
        finally:
            self._flushing = False
