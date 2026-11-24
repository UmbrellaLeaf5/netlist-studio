"""Выбор параметров CheckerCLI и показ его вывода.

Диалог не читает содержимое нетлиста и не импортирует проверки.
Он передаёт список аргументов через runRequested; процессом управляет
ProcessRunner. Автор модуля по спецификации: К. Транович.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

try:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as error:
    # Чистую сборку аргументов можно импортировать без Qt.
    # Ошибки внутри установленного PySide6 не скрываем.
    if error.name != "PySide6":
        raise
    _QT_AVAILABLE = False
else:
    _QT_AVAILABLE = True

from .settings import (
    ALL_CHECKS,
    MAX_ERRORS,
    CheckerDialogState,
    Settings,
    _validate_state,
)

_CHECK_LABELS = (
    "Синтаксис", "Иерархия", "Циклы", "Подсхемы", "Пины", "Цепи"
)
_FLAG_LABELS = {
    "flat_nets": "Цепи после раскрытия иерархии",
    "warnings_as_errors": "Предупреждения как ошибки",
    "no_warnings": "Скрыть предупреждения",
}
_RESERVED = {"block", "end", "pin", "net", "inst", "sub", "prim", "primitive"}


def build_args(state: CheckerDialogState) -> list[str]:
    """Вернуть аргументы CheckerCLI без имени исполняемой программы.

    Состояние не меняется; файлы не открываются, QApplication не нужен.
    TypeError означает неверный тип поля. ValueError означает пустой файл,
    отсутствие проверок, неверное имя блока или несогласованные кавычки.
    Дополнительные аргументы дописываются последними, как в интерфейсе.
    """
    _validate_state(state)
    _validate_launch(state)
    args = [state.input_file, "--checks", ",".join(state.checks)]
    if state.primitives_file:
        args.extend(["--primitives", state.primitives_file])
    args.extend(["--top", state.top, "--format", state.output_format])
    for enabled, option in (
        (state.flat_nets, "--flat-nets"),
        (state.warnings_as_errors, "-W"),
        (state.no_warnings, "--no-warnings"),
    ):
        if enabled:
            args.append(option)
    if state.max_errors:
        args.extend(["--max-errors", str(state.max_errors)])
    # shlex только разбирает кавычки; он не выполняет shell-команды.
    # На Windows в этом поле удобнее писать пути через / или в кавычках.
    args.extend(shlex.split(state.extra_args))
    return args


def _validate_launch(state: CheckerDialogState) -> None:
    """Проверить готовность снимка к запуску без обращения к диску."""
    if not state.input_file.strip():
        raise ValueError("Выберите файл нетлиста.")
    if state.input_file.startswith("-"):
        raise ValueError("Для имени с '-' укажите полный путь или ./имя.")
    if not state.checks:
        raise ValueError("Выберите хотя бы одну проверку.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", state.top):
        raise ValueError("Корневой блок: латинские буквы, цифры и '_'.")
    if state.top in _RESERVED:
        raise ValueError("Имя корневого блока совпало с ключевым словом.")


if _QT_AVAILABLE:
    class CheckerDialog(QDialog):
        """Немодальное окно параметров и результата одной проверки.

        _text_fields, _checks и _flags связывают имена полей снимка с виджетами.
        _running защищает от повторного нажатия запуска. _output и _result
        хранят только отображение: состояние процесса остаётся в ProcessRunner.
        """

        runRequested = Signal(list)
        # В спецификации есть кнопка остановки, но нет её сигнала. Этот
        # дополнительный сигнал позволяет не раскрывать внутренний QPushButton.
        stopRequested = Signal()

        def __init__(
            self, settings: Settings, parent: QWidget | None = None
        ) -> None:
            """Создать форму и восстановить настройки; ничего не запускать.

            settings — общее хранилище оболочки, parent — родительское окно.
            Закрытие скрывает диалог, но не удаляет его: вывод продолжающейся
            проверки останется доступен при повторном открытии.
            """
            super().__init__(parent)
            self._settings = settings
            self._running = False
            self.setWindowTitle("Запуск чекера")
            self.setModal(False)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self.resize(760, 720)
            self._create_fields()
            self._build_ui()
            self.set_state(settings.checker_dialog_state())
            self.set_running(False)

        def set_state(self, state: CheckerDialogState) -> None:
            """Заполнить форму по state без сохранения и запуска процесса.

            Перед изменением проверяются типы и допустимые значения. Ошибка
            вызывает TypeError или ValueError, оставляя прежнюю форму целой.
            Сам снимок не меняется; метод возвращает None.
            """
            _validate_state(state)
            for name, edit in self._text_fields.items():
                edit.setText(getattr(state, name))
            for name, checkbox in self._checks.items():
                checkbox.setChecked(name in state.checks)
            for name, checkbox in self._flags.items():
                checkbox.setChecked(getattr(state, name))
            self._format.setCurrentText(state.output_format)
            self._max_errors.setValue(state.max_errors)

        def state(self) -> CheckerDialogState:
            """Вернуть новый снимок текущей формы, ничего не сохраняя.

            Проверки перечисляются в порядке ALL_CHECKS. Строки не обрезаются:
            пробелы могут быть частью пути. Неполный ввод разрешён до запуска.
            """
            values = {
                name: edit.text() for name, edit in self._text_fields.items()
            }
            values.update({
                name: checkbox.isChecked()
                for name, checkbox in self._flags.items()
            })
            return CheckerDialogState(
                **values,
                checks=[
                    name for name, box in self._checks.items()
                    if box.isChecked()
                ],
                output_format=self._format.currentText(),
                max_errors=self._max_errors.value(),
            )

        def append_output(self, text: str) -> None:
            """Добавить строку text в конец журнала, сохранив предыдущие строки.

            Текст показывается буквально, без интерпретации HTML. Его можно
            копировать, но нельзя редактировать. Метод возвращает None.
            """
            self._output.appendPlainText(text)

        def set_running(self, running: bool) -> None:
            """Обновить доступность кнопок и параметров; не управлять процессом.

            При True доступны остановка и закрытие; при False — новый запуск.
            Блокировка параметров не позволяет спутать текущий отчёт с другой
            конфигурацией. Метод возвращает None.
            """
            self._running = running
            self._run_button.setEnabled(not running)
            self._stop_button.setEnabled(running)
            self._options.setEnabled(not running)
            if running:
                self._result.setText("Проверка выполняется…")

        def show_exit_code(self, code: int) -> None:
            """Показать итог code и снова разрешить запуск; вернуть None.

            Коды 0–3 принадлежат CheckerCLI. Внутренний -1 означает прерывание
            или аварию, -2 — невозможность запуска. Эти отрицательные значения
            не передаются CheckerCLI и не заменяют его обычные коды завершения.
            """
            messages = {
                0: "Ошибок нет (предупреждения могут присутствовать)",
                1: "Найдены ошибки или предупреждения при включённом -W",
                2: "Ошибка запуска: неверные аргументы чекера",
                3: "Ошибка запуска: не удалось прочитать файл",
                -1: "Проверка прервана: остановка или аварийное завершение",
                -2: "Не удалось запустить процесс чекера",
            }
            self.set_running(False)
            message = messages.get(code, "Чекер завершился с неизвестным кодом")
            suffix = f". Код завершения: {code}." if code >= 0 else "."
            self._result.setText(message + suffix)

        def reject(self) -> None:
            """Сохранить даже незапущенную форму при Закрыть, Esc или крестике.

            Скрытие окна не останавливает чекер: для этого есть своя кнопка.
            Ошибка записи показывается, но не лишает пользователя права закрыть
            окно. Завершением процесса при выходе из оболочки ведает MainWindow.
            """
            try:
                self._settings.save_checker_dialog_state(self.state())
            except (OSError, TypeError, ValueError) as error:
                QMessageBox.warning(self, "Настройки не сохранены", str(error))
            super().reject()

        def _create_fields(self) -> None:
            """Создать виджеты один раз и связать их с именами полей."""
            names = ("input_file", "primitives_file", "top", "extra_args")
            self._text_fields = {name: QLineEdit(self) for name in names}
            self._checks = {
                name: QCheckBox(label, self)
                for name, label in zip(ALL_CHECKS, _CHECK_LABELS, strict=True)
            }
            self._flags = {
                name: QCheckBox(label, self)
                for name, label in _FLAG_LABELS.items()
            }
            self._format = QComboBox(self)
            self._format.addItems(["text", "json"])
            self._max_errors = QSpinBox(self)
            self._max_errors.setRange(0, MAX_ERRORS)
            self._max_errors.setSpecialValueText("Без ограничения")
            self._text_fields["extra_args"].setPlaceholderText(
                '--quiet или --output "report file.txt"'
            )
            # Стабильные objectName нужны автотестам и отладке формы, не логике.
            for name, widget in self._text_fields.items():
                widget.setObjectName(name)
            for name, widget in self._checks.items():
                widget.setObjectName(f"check_{name}")

        def _build_ui(self) -> None:
            """Собрать форму без внешних .ui, иконок и генерируемых ресурсов."""
            root = QVBoxLayout(self)
            self._options = self._make_options()
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setWidget(self._options)
            root.addWidget(scroll, 3)
            root.addWidget(QLabel("Вывод чекера:", self))
            self._output = QPlainTextEdit(self)
            self._output.setObjectName("checker_output")
            self._output.setReadOnly(True)
            self._output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self._output.setFont(
                QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            )
            root.addWidget(self._output, 2)
            self._result = QLabel("Проверка ещё не запускалась.", self)
            self._result.setObjectName("checker_result")
            self._result.setWordWrap(True)
            self._result.setTextFormat(Qt.TextFormat.PlainText)
            root.addWidget(self._result)
            root.addLayout(self._make_buttons())

        def _make_options(self) -> QWidget:
            """Собрать параметры в прокручиваемую область для малых экранов."""
            panel = QWidget(self)
            layout = QVBoxLayout(panel)
            form = QFormLayout()
            form.addRow("Входной файл:", self._path_row("input_file"))
            form.addRow(
                "Библиотека примитивов:", self._path_row("primitives_file")
            )
            form.addRow("Корневой блок:", self._text_fields["top"])
            layout.addLayout(form)
            group = QGroupBox("Проверки", panel)
            grid = QGridLayout(group)
            for index, checkbox in enumerate(self._checks.values()):
                grid.addWidget(checkbox, index // 3, index % 3)
            layout.addWidget(group)
            for checkbox in self._flags.values():
                layout.addWidget(checkbox)
            extra = QFormLayout()
            extra.addRow("Формат отчёта:", self._format)
            extra.addRow("Максимум ошибок:", self._max_errors)
            extra.addRow(
                "Дополнительные аргументы:", self._text_fields["extra_args"]
            )
            layout.addLayout(extra)
            layout.addStretch()
            return panel

        def _path_row(self, field: str) -> QWidget:
            """Создать поле пути с кнопкой выбора файла любого расширения."""
            row = QWidget(self)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self._text_fields[field], 1)
            button = QPushButton("Обзор…", row)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _=False: self._browse_file(field))
            layout.addWidget(button)
            return row

        def _browse_file(self, field: str) -> None:
            """Выбрать путь; отмена диалога сохраняет прежнее значение."""
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Выберите файл",
                self._text_fields[field].text(),
                "Файлы нетлиста (*.net);;Все файлы (*)",
            )
            if path:
                self._text_fields[field].setText(path)

        def _make_buttons(self) -> QHBoxLayout:
            """Подключить кнопки к действиям формы и сигналу остановки."""
            layout = QHBoxLayout()
            layout.addStretch()
            self._run_button = QPushButton("Запустить", self)
            self._run_button.setObjectName("run_button")
            self._run_button.setDefault(True)
            self._run_button.clicked.connect(self._request_run)
            self._stop_button = QPushButton("Остановить", self)
            self._stop_button.setObjectName("stop_button")
            self._stop_button.setAutoDefault(False)
            self._stop_button.clicked.connect(lambda: self.stopRequested.emit())
            close_button = QPushButton("Закрыть", self)
            close_button.setAutoDefault(False)
            close_button.clicked.connect(self.reject)
            for button in (self._run_button, self._stop_button, close_button):
                layout.addWidget(button)
            return layout

        def _request_run(self) -> None:
            """Проверить ввод, сохранить его и только затем запросить запуск.

            Наличие файла проверяется здесь, а его чтение всё равно остаётся
            за CheckerCLI: файл может исчезнуть между проверкой и запуском.
            """
            if self._running:
                return
            try:
                state = self.state()
                self._resolve_files(state)
                args = build_args(state)
                self._settings.save_checker_dialog_state(state)
            except (OSError, TypeError, ValueError) as error:
                QMessageBox.warning(self, "Проверка не запущена", str(error))
                return
            self.set_state(state)
            self._output.clear()
            # Сигнал об ошибке запуска может прийти сразу из runRequested.
            # Поэтому блокируем кнопки ДО emit, а не после него.
            self.set_running(True)
            self.runRequested.emit(args)

        @staticmethod
        def _resolve_files(state: CheckerDialogState) -> None:
            """Нормализовать пути в новом снимке и проверить, что это файлы.

            Абсолютный путь не будет принят argparse за опцию, даже если имя
            начинается с '-'. Файл библиотеки необязателен; его синтаксис
            проверяет только чекер, оболочка содержимое не разбирает.
            """
            fields = (("input_file", True), ("primitives_file", False))
            for name, required in fields:
                value = getattr(state, name)
                if not value.strip():
                    if required:
                        raise ValueError("Выберите файл нетлиста.")
                    setattr(state, name, "")
                    continue
                try:
                    path = Path(value).expanduser().absolute()
                except RuntimeError as error:
                    # Неизвестный ~user — ошибка ввода, которую показывает GUI.
                    raise ValueError(
                        f"Не удалось раскрыть путь: {value}"
                    ) from error
                if not path.is_file():
                    raise ValueError(
                        f"Файл не найден или указан каталог: {path}"
                    )
                setattr(state, name, str(path))


def __getattr__(name: str) -> type:
    """Сообщить об отсутствующем Qt только при обращении к GUI-классу."""
    if name == "CheckerDialog" and not _QT_AVAILABLE:
        raise ModuleNotFoundError(
            "Для окон оболочки установите PySide6: "
            "python -m pip install PySide6==6.10.1",
            name="PySide6",
        )
    raise AttributeError(f"В модуле {__name__!r} нет атрибута {name!r}.")
