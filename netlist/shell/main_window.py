"""Главное окно оболочки и точка входа Qt-приложения.

Окно знает только Settings и ProcessRunner. Оно не импортирует редактор,
чекер, парсер или модель. Автор модуля по спецификации: К. Транович.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QByteArray, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .process_runner import ProcessRunner
from .settings import Settings


class MainWindow(QMainWindow):
    """Главное окно: редактор, чекер и выход.

    _settings и _runner — переданные общие сервисы. _exit_requested
    означает, что пользователь подтвердил остановку чекера для выхода.
    Этот флаг позволяет дождаться завершения без блокировки потока GUI.
    """

    def __init__(self, settings: Settings, runner: ProcessRunner) -> None:
        """Создать кнопки и восстановить геометрию из settings.

        runner уже создан вызывающим кодом; новое управление процессами
        здесь не создаётся. Все ошибки запуска показываются через его
        failedToStart. Конструктор не запускает редактор или чекер.
        """
        super().__init__()
        self._settings = settings
        self._runner = runner
        self._exit_requested = False
        self.setWindowTitle("Нетлист")
        self.resize(340, 240)
        self._build_ui()
        self._restore_geometry()
        runner.failedToStart.connect(self._show_launch_error)
        runner.checkerFinished.connect(self._finish_shutdown)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Сохранить окно и не оставить управляемый чекер без владельца.

        При активной проверке сначала спрашиваем разрешение на остановку.
        Закрытие откладывается до сигнала завершения, поэтому QProcess
        не уничтожается во время работы. Отдельный редактор не затрагивается.
        """
        if self._runner.is_checker_running():
            event.ignore()
            self._request_shutdown()
            return
        # Закрытие дочернего диалога сохраняет и ещё не запущенную форму.
        for dialog in self.findChildren(QDialog):
            dialog.close()
        try:
            self._settings.set("window/geometry", self.saveGeometry())
            self._settings.sync()
        except OSError as error:
            QMessageBox.warning(self, "Настройки не сохранены", str(error))
        super().closeEvent(event)

    def _build_ui(self) -> None:
        """Создать предусмотренные интерфейсом три действия без ресурсов."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 24, 24, 24)
        heading = QLabel("Проект «Нетлист»", central)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        actions = (
            ("Редактор", "editor_button", lambda: self._runner.run_editor()),
            ("Чекер…", "checker_button", self._open_checker),
            ("Выход", "exit_button", self.close),
        )
        for text, name, action in actions:
            button = QPushButton(text, central)
            button.setObjectName(name)
            button.setMinimumHeight(36)
            button.clicked.connect(action)
            layout.addWidget(button)
        self.setCentralWidget(central)

    def _restore_geometry(self) -> None:
        """Пропустить повреждённую геометрию вместо отказа при старте."""
        geometry = self._settings.get("window/geometry")
        if isinstance(geometry, (QByteArray, bytes, bytearray)):
            self.restoreGeometry(QByteArray(geometry))

    def _open_checker(self) -> None:
        """Передать главное окно как владельца немодального диалога."""
        self._runner.open_checker_dialog(self)

    def _request_shutdown(self) -> None:
        """Запросить остановку только один раз, даже при повторном закрытии."""
        if self._exit_requested:
            return
        answer = QMessageBox.question(
            self,
            "Завершить проверку?",
            "Чекер ещё работает. Остановить его и закрыть оболочку?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._exit_requested = True
        self.setEnabled(False)
        self._runner.kill_checker()
        # Процесс мог закончиться, пока был открыт вопрос. В этом случае
        # нового finished уже не будет, а окно всё равно нужно закрыть.
        if not self._runner.is_checker_running():
            QTimer.singleShot(0, self.close)

    def _finish_shutdown(self, _code: int) -> None:
        """Продолжить ранее подтверждённый выход после завершения чекера."""
        if self._exit_requested:
            QTimer.singleShot(0, self.close)

    def _show_launch_error(self, message: str) -> None:
        """Показать ошибку или завершить уже подтверждённый выход."""
        if self._exit_requested:
            # FailedToStart не сопровождается checkerFinished.
            QTimer.singleShot(0, self.close)
            return
        QMessageBox.critical(self, "Ошибка запуска", message)


def main(argv: list[str] | None = None) -> int:
    """Создать приложение и вернуть код его цикла событий.

    argv — список аргументов Qt, включая имя программы; None берёт sys.argv.
    Функция предназначена для отдельного запуска оболочки. В тестах с уже
    созданным QApplication нужно создавать MainWindow, а не второй цикл.
    """
    if QApplication.instance() is not None:
        raise RuntimeError("QApplication уже существует; создайте MainWindow.")
    arguments = list(sys.argv if argv is None else argv)
    app = QApplication(arguments or ["netlist-shell"])
    app.setOrganizationName("Netlist")
    app.setApplicationName("Shell")
    settings = Settings()
    runner = ProcessRunner(settings)
    window = MainWindow(settings, runner)
    window.show()
    # Ссылки на window и runner живут до возвращения exec(): сборщик мусора
    # не должен уничтожить владельца QProcess во время обработки событий.
    return app.exec()
