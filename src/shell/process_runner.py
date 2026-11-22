"""Асинхронный запуск редактора и одного чекера через QProcess.

Здесь нет разбора нетлиста и прямых вызовов кода редактора или проверок.
Аргументы передаются списком, без системной командной оболочки.
Автор модуля по спецификации: К. Транович.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal
from PySide6.QtWidgets import QWidget

from .settings import Settings

if TYPE_CHECKING:
    from .checker_dialog import CheckerDialog


class ProcessRunner(QObject):
    """Владелец управляемого процесса чекера и единственного его диалога.

    _active закрывает повторный запуск, включая стадию Starting и слив
    последнего вывода. _pending хранит незавершённую строку в байтах,
    чтобы UTF-8 не ломался на границах порций. _cancelled и _io_failed
    не позволяют выдать прерванную проверку за успешную.
    """

    checkerOutput = Signal(str)
    checkerFinished = Signal(int)
    failedToStart = Signal(str)

    def __init__(
        self, settings: Settings, parent: QObject | None = None
    ) -> None:
        """Сохранить settings и подготовить QProcess, не запуская программ.

        parent управляет временем жизни объекта в Qt. Диалог создаётся
        лениво, поэтому ProcessRunner можно использовать и без его окна.
        """
        super().__init__(parent)
        self._settings = settings
        self._dialog: CheckerDialog | None = None
        self._active = False
        self._cancelled = False
        self._io_failed = False
        self._pending = b""
        self._process = QProcess(self)
        self._process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.started.connect(self._on_started)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)
        # Этот слот подключается раньше внешних подписчиков: старый итог
        # не должен снять блокировку формы после запуска следующей проверки.
        self.checkerFinished.connect(self._show_checker_result)

    def run_editor(self, file: str | None = None) -> bool:
        """Запустить независимый редактор с необязательным путём file.

        True означает успешное создание процесса ОС, а не успешную загрузку
        документа. False сопровождается failedToStart с причиной. Пустая
        строка, как и None, означает запуск без документа.
        """
        if file is not None and not isinstance(file, str):
            raise TypeError("Путь документа должен быть строкой или None.")
        if file is not None and "\0" in file:
            raise ValueError("Путь документа содержит нулевой символ.")
        try:
            command = self._settings.editor_command()
            args = command[1:] + ([file] if file else [])
            # PySide6 возвращает пару (успех, pid), а не один bool.
            # Сам редактор не становится дочерним управляемым QProcess.
            started, _pid = QProcess.startDetached(command[0], args)
        except (OSError, TypeError, ValueError) as error:
            self.failedToStart.emit(f"Не удалось запустить редактор: {error}")
            return False
        if not started:
            self.failedToStart.emit(
                f"Не удалось запустить редактор: {command[0]}. "
                "Проверьте команду и права доступа."
            )
        return started

    def open_checker_dialog(self, parent: QWidget) -> None:
        """Открыть немодальный диалог или поднять уже существующий.

        parent становится владельцем окна. Связи устанавливаются только
        при первом создании: повторное открытие не дублирует запуск и вывод.
        Закрытие диалога скрывает его, но не останавливает проверку.
        """
        if self._dialog is None:
            from .checker_dialog import CheckerDialog

            dialog = CheckerDialog(self._settings, parent)
            self._dialog = dialog
            dialog.runRequested.connect(self.run_checker)
            dialog.stopRequested.connect(self.kill_checker)
            dialog.destroyed.connect(self._forget_dialog)
            self.checkerOutput.connect(dialog.append_output)
            dialog.set_running(self.is_checker_running())
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def run_checker(self, args: list[str]) -> bool:
        """Принять аргументы без программы и начать асинхронный запуск.

        False означает, что другой запрос ещё выполняется. True означает
        принятие запроса, но не успех проверки: ошибка запуска сообщается
        failedToStart, а результат работающего процесса — checkerFinished.
        Неверный тип args вызывает TypeError, нулевой символ — ValueError.
        """
        if self.is_checker_running():
            return False
        self._validate_args(args)
        try:
            command = self._settings.checker_command()
        except (OSError, TypeError, ValueError) as error:
            self._notify_start_failure(str(error))
            return True
        self._pending = b""
        self._cancelled = False
        self._io_failed = False
        self._active = True
        self._set_dialog_running(True)
        environment = QProcessEnvironment.systemEnvironment()
        # Python-чекер должен отдавать строки сразу и в известной кодировке.
        # Остальное окружение сохраняется, включая PATH и PYTHONPATH.
        environment.insert("PYTHONIOENCODING", "utf-8:replace")
        environment.insert("PYTHONUNBUFFERED", "1")
        self._process.setProcessEnvironment(environment)
        self._process.start(command[0], command[1:] + list(args))
        return True

    def is_checker_running(self) -> bool:
        """Вернуть True с принятия запуска до обработки его результата.

        Стадия Starting тоже занята. Нельзя полагаться только на состояние
        ОС: последнее finished-событие ещё может ожидать обработки в Qt.
        """
        return self._active

    def kill_checker(self) -> None:
        """Остановить текущий чекер без ожидания в потоке интерфейса.

        При отсутствии процесса ничего не происходит. Остановка во время
        Starting запоминается и выполняется в started. После завершения
        оставшийся вывод передаётся до checkerFinished(-1).
        """
        if not self._active:
            return
        self._cancelled = True
        if self._process.state() == QProcess.ProcessState.Running:
            # Для консольного процесса Windows terminate() недостаточно.
            # Чекер не редактирует исходный нетлист; используем kill().
            self._process.kill()

    def _on_started(self) -> None:
        """Закрыть ненужный stdin и выполнить отложенную остановку."""
        self._process.closeWriteChannel()
        if self._cancelled:
            self._process.kill()

    def _read_output(self) -> None:
        """Передать все полные строки; остаток дождётся следующей порции."""
        self._drain_output(final=False)

    def _drain_output(self, *, final: bool) -> None:
        """Собрать LF/CRLF и UTF-8 независимо от размера порций QProcess.

        Декодируем только полную строку, поэтому один русский символ может
        безопасно прийти несколькими порциями. При завершении передаём
        и последнюю строку без перевода. Пустые строки внутри вывода важны.
        """
        data = self._pending + bytes(self._process.readAllStandardOutput())
        lines = data.split(b"\n")
        self._pending = lines.pop()
        if final and self._pending:
            lines.append(self._pending)
            self._pending = b""
        for line in lines:
            text = line.removesuffix(b"\r").decode("utf-8", errors="replace")
            self.checkerOutput.emit(text)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        """Сначала передать хвост вывода, затем освободить запуск и итог.

        При аварии exitCode ненадёжен; используем внутренний -1. Все поля
        сбрасываются ДО сигнала, чтобы его получатель мог начать новую
        проверку прямо из обработчика checkerFinished.
        """
        if not self._active:
            return
        self._drain_output(final=True)
        interrupted = (
            self._cancelled
            or self._io_failed
            or status == QProcess.ExitStatus.CrashExit
        )
        result = -1 if interrupted else code
        self._active = False
        self._set_dialog_running(False)
        self.checkerFinished.emit(result)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        """Различить невозможность запуска и аварию уже работающего чекера.

        При FailedToStart finished может не прийти, поэтому освобождаем
        состояние здесь. При Crashed ждём finished, чтобы не потерять вывод
        и не выдать два результата для одного запуска.
        """
        if not self._active:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._notify_start_failure(self._process.errorString())
        elif error in (
            QProcess.ProcessError.ReadError,
            QProcess.ProcessError.WriteError,
        ):
            self._io_failed = True
            self.kill_checker()

    def _notify_start_failure(self, message: str) -> None:
        """Снять занятость даже тогда, когда процесс так и не был создан."""
        self._active = False
        self._pending = b""
        if self._dialog is not None:
            self._dialog.show_exit_code(-2)
        self.failedToStart.emit(f"Не удалось запустить чекер: {message}")

    def _set_dialog_running(self, running: bool) -> None:
        """Обновить окно, если его вообще открывали."""
        if self._dialog is not None:
            self._dialog.set_running(running)

    def _show_checker_result(self, code: int) -> None:
        """Показать старый итог до возможного перезапуска внешним слотом.

        Подключение сделано в конструкторе, когда внешних подписчиков ещё
        нет. Следующий запуск затем сам установит правильные кнопки формы.
        """
        if self._dialog is not None:
            self._dialog.show_exit_code(code)

    def _forget_dialog(self, _object: QObject | None = None) -> None:
        """Не оставлять ссылку на удалённый Qt-объект после удаления parent."""
        self._dialog = None

    @staticmethod
    def _validate_args(args: list[str]) -> None:
        """Не передавать Qt некорректные типы и скрыто обрезаемые строки."""
        if not isinstance(args, list) or not all(
            isinstance(arg, str) for arg in args
        ):
            raise TypeError("Аргументы должны быть списком строк.")
        if any("\0" in arg for arg in args):
            raise ValueError("Аргументы не могут содержать нулевой символ.")
