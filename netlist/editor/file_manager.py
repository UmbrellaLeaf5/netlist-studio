"""Файловое хранение нетлиста, геометрии и блокировки документа.

Модуль намеренно не импортирует Qt: read_text нужен также CLI-чекеру.
Парсер и сериализатор передаются извне; FileManager отвечает только за
ввод-вывод, вспомогательные файлы и владение блокировкой документа.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from netlist.core.model import Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    # Аннотации сохраняют согласованные имена, не создавая импортов ядра
    # во время запуска чекера или изолированного тестирования сервисов.
    from netlist.core.model import Netlist
    from netlist.core.parser import NetlistParser, ParseResult
    from netlist.core.serializer import NetlistSerializer


class FileAccessError(OSError):
    """Ошибка доступа к документу или его вспомогательным файлам."""


@dataclass(frozen=True)
class LockInfo:
    """Неизменяемые сведения о владельце .net.lock.

    stale означает доказанно завершившийся локальный процесс. Недоступный
    PID или другой компьютер не дают права удалять чужую блокировку.

    Attributes:
        pid: PID процесса, создавшего блокировку.
        host: Имя компьютера владельца.
        time: Время создания маркера блокировки.
        stale: Доказанно завершившийся локальный владелец.
    """

    pid: int
    host: str
    time: datetime
    stale: bool


class FileLockedError(FileAccessError):
    """Документ занят; info позволяет окну объяснить причину пользователю.

    Attributes:
        info: Сведения о владельце занятого документа.
    """

    info: LockInfo

    def __init__(self, path: str, info: LockInfo) -> None:
        """Сохранить владельца и стандартные поля исключения OSError.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            info: Сведения о владельце существующей блокировки.
        """
        self.info = info
        message = f"Файл заблокирован: pid={info.pid} host={info.host}"
        super().__init__(errno.EBUSY, message, path)


@dataclass
class OpenResult:
    """Результат чтения без принятия сырых данных в модель редактора.

    Attributes:
        path: Абсолютный путь прочитанного документа.
        parse: Сырые данные и синтаксические диагностики парсера.
        schematic: Геометрия документа либо None при её отсутствии.
        ok: Результат проверки ошибок парсером; только для чтения.
    """

    path: str
    parse: ParseResult
    schematic: dict | None

    @property
    def ok(self) -> bool:
        """Делегировать успех парсеру: предупреждение W701 не есть ошибка.

        Returns:
            Признак отсутствия ошибок в результате парсера.
        """
        return self.parse.ok


@dataclass(frozen=True)
class SaveResult:
    """Путь и хэш записанного .net для привязки истории UndoStack.

    Attributes:
        path: Абсолютный путь сохранённого документа.
        sha256: SHA-256 фактически записанных байтов нетлиста.
    """

    path: str
    sha256: str


def read_text(path: str) -> str:
    """Прочитать UTF-8, оставив ошибки содержимого синтаксической проверке.

    Замена неверных байтов сохраняет строки: SyntaxCheck сможет сообщить
    E108 с номером строки. Ошибки доступа имеют единый тип FileAccessError.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.

    Returns:
        Содержимое файла с заменой некорректных UTF-8-байтов.

    Raises:
        FileAccessError: Файл отсутствует либо недоступен для чтения.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FileAccessError(*exc.args) from exc


def write_text_atomic(path: str, text: str) -> str:
    """Атомарно заменить один файл и вернуть SHA-256 его UTF-8-байтов.

    Бинарная запись не преобразует LF в CRLF, поэтому возвращаемый хэш
    совпадает с file_sha256 на любой поддерживаемой платформе.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.
        text: Текст для записи в кодировке UTF-8.

    Returns:
        SHA-256 фактически записанных UTF-8-байтов.

    Raises:
        FileAccessError: Не удалось записать или заменить файл.
        UnicodeEncodeError: Текст нельзя представить в UTF-8.
    """
    return _write_bytes_atomic(path, text.encode("utf-8"))


def _write_bytes_atomic(path: str, data: bytes) -> str:
    """Записать рядом временный файл, затем заменить целевой os.replace.

    Общий каталог обеспечивает одну файловую систему для атомарной замены.
    До replace исходный файл не меняется; после сбоя временный удаляется.
    Этот помощник также восстанавливает исходные байты схематика.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.
        data: Данные для записи или восстановления объекта.

    Returns:
        SHA-256 байтов, переданных для записи.

    Raises:
        FileAccessError: Не удалось записать или заменить файл.
    """
    temporary = None
    try:
        directory = Path(path).absolute().parent
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None  # После replace временного имени уже нет.
    except OSError as exc:
        raise FileAccessError(*exc.args) from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return hashlib.sha256(data).hexdigest()


def schematic_path(path: str) -> str:
    """Получить a/b.sch.json из a/b.net, сохранив каталог документа.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.

    Returns:
        Путь .sch.json в каталоге исходного документа.
    """
    return str(Path(path).with_suffix(".sch.json"))


def history_path(path: str) -> str:
    """Получить a/b.history.json: историю связывает с .net хэш, не имя.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.

    Returns:
        Путь .history.json в каталоге исходного документа.
    """
    return str(Path(path).with_suffix(".history.json"))


def lock_path(path: str) -> str:
    """Добавить .lock к полному имени, включая исходное расширение .net.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.

    Returns:
        Путь с добавленным суффиксом .lock.
    """
    return str(path) + ".lock"


def file_sha256(path: str) -> str:
    """Потоково вычислить хэш фактических байтов, не декодируя документ.

    Args:
        path: Путь к файлу документа или вспомогательному файлу.

    Returns:
        Шестнадцатеричная строка SHA-256 содержимого файла.

    Raises:
        FileAccessError: Не удалось прочитать байты файла.
    """
    try:
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise FileAccessError(*exc.args) from exc


def _windows_alive(pid: int) -> bool:
    """Проверить Windows PID без посылки сигнала целевому процессу.

    Только отсутствие PID доказывает stale. При запрете доступа считаем
    владельца живым, чтобы не удалить блокировку другого пользователя.

    Args:
        pid: Идентификатор локального процесса владельца.

    Returns:
        False при отсутствии PID, иначе True, включая отказ доступа.
    """
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER.
    try:
        code = ctypes.c_ulong()
        success = kernel.GetExitCodeProcess(
            ctypes.c_void_p(handle), ctypes.byref(code)
        )
        return not success or code.value == 259  # STILL_ACTIVE.
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def _open_windows_data(path: str) -> int:
    """Разрешает другим процессам открывать документ только для чтения.

    Args:
        path: Путь к существующему нетлисту.

    Returns:
        Дескриптор, закрытие которого освобождает запрет Windows.

    Raises:
        OSError: Документ недоступен или уже открыт на запись.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_read, open_existing = 0x80000000, 1, 3
    handle = create(
        path, generic_read, share_read, None, open_existing, 0, None
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


class FileManager:
    """Владеть одним открытым документом и его файловыми блокировками.

    Переход к другому документу происходит только после успешной операции.
    Методы вызываются последовательно из потока редактора; close обязателен
    при закрытии окна, освобождение ресурсов не отложено до сборки мусора.

    Attributes:
        current_path: Путь документа только для чтения либо None.
        is_open: Признак открытого файла с удерживаемой блокировкой.
        _parser: переданный парсер; превращает текст в сырые данные
            и диагностики, не принимая их в модель редактора.
        _serializer: переданный сериализатор; получает принятую
            модель и возвращает текст для записи в .net.
        _path: абсолютный путь текущего документа; None, если
            документ ещё не сохранён либо менеджер закрыт.
        _lock_fd: дескриптор маркера .net.lock, удерживающий
            владение документом, в том числе во время замены .net.
        _data_fd: дескриптор блокировки данных .net; временно None
            при атомарной замене, после которой файл блокируется заново.
    """

    def __init__(
        self, parser: NetlistParser, serializer: NetlistSerializer
    ) -> None:
        """Принять зависимости, не создавая парсер и модель самостоятельно.

        Args:
            parser: Парсер текста нетлиста в сырые данные и диагностики.
            serializer: Сериализатор принятой модели в текст нетлиста.
        """
        self._parser = parser
        self._serializer = serializer
        self._path: str | None = None
        self._lock_fd: int | None = None
        self._data_fd: int | None = None

    @property
    def current_path(self) -> str | None:
        """Вернуть путь только для чтения; None означает документ без пути.

        Returns:
            Абсолютный путь документа либо None, если путь не задан.
        """
        return self._path

    @property
    def is_open(self) -> bool:
        """Сообщить, есть ли открытый файл с удерживаемой блокировкой.

        Returns:
            True, если менеджер удерживает открытый документ с путём.
        """
        return self._path is not None

    def new(self) -> None:
        """Отпустить прежний файл; создание пустой модели поручено Canvas.

        Raises:
            FileAccessError: Не удалось освободить предыдущий документ.
        """
        self.close()

    def check_lock(self, path: str) -> LockInfo | None:
        """Прочитать владельца, не изменяя документ или его блокировку.

        Удалённый компьютер проверить нельзя. Повреждённый маркер также
        нельзя объявлять stale: force_unlock не должен красть живой файл.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.

        Returns:
            Сведения о владельце либо None при отсутствии маркера.

        Raises:
            FileAccessError: Не удалось прочитать маркер блокировки.
        """
        try:
            text = Path(lock_path(path)).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FileAccessError(*exc.args) from exc
        except UnicodeError:
            epoch = datetime.fromtimestamp(0, timezone.utc)
            return LockInfo(0, "unknown", epoch, False)
        try:
            fields = dict(item.split("=", 1) for item in text.split())
            pid, host = int(fields["pid"]), fields["host"]
            timestamp = datetime.fromisoformat(fields["time"])
            local_owner = host == socket.gethostname()
            stale = local_owner and pid > 0 and not _process_alive(pid)
            return LockInfo(pid, host, timestamp, stale)
        except (KeyError, ValueError, OverflowError):
            epoch = datetime.fromtimestamp(0, timezone.utc)
            return LockInfo(0, "unknown", epoch, False)

    def _create_marker(self, path: str, force: bool) -> int:
        """Создать единственный маркер; проверка наличия не заменяет O_EXCL.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            force: Разрешить удаление доказанно устаревшего маркера.

        Returns:
            Дескриптор созданного маркера блокировки.

        Raises:
            FileLockedError: Файл занят и снятие блокировки запрещено.
            OSError: Не удалось создать или удалить маркер.
        """
        with _ownership(path):
            info = self.check_lock(path)
            if info is not None:
                if not (force and info.stale):
                    raise FileLockedError(path, info)
                os.unlink(lock_path(path))
            try:
                fd = os.open(
                    lock_path(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                )
            except FileExistsError:
                # Другой процесс мог захватить файл после check_lock.
                owner = self.check_lock(path)
                if owner is None:
                    epoch = datetime.fromtimestamp(0, timezone.utc)
                    owner = LockInfo(0, "unknown", epoch, False)
                raise FileLockedError(path, owner) from None
            try:
                _write_marker(fd)
            except BaseException:
                try:
                    os.close(fd)
                finally:
                    os.unlink(lock_path(path))
                raise
            return fd

    def _acquire(
        self, path: str, force: bool = False
    ) -> tuple[int, int | None]:
        """Захватить маркер и данные; при ошибке убрать свои ресурсы.

        Отсутствующий .net проверяет чтение; finally освободит маркер.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            force: Разрешить удаление доказанно устаревшего маркера.

        Returns:
            Дескрипторы маркера и данных; второй может быть None.

        Raises:
            FileLockedError: Документ уже принадлежит другому владельцу.
            FileAccessError: Не удалось создать маркер или открыть данные.
        """
        try:
            fd = self._create_marker(path, force)
        except OSError as exc:
            if isinstance(exc, FileAccessError):
                raise
            raise FileAccessError(*exc.args) from exc
        data_fd = None
        try:
            _restore_transaction(path)
            if os.path.exists(path):
                data_fd = _open_locked_data(path)
            return fd, data_fd
        except BaseException as exc:
            try:
                self._release(path, fd, data_fd)
            except OSError as cleanup:
                exc.add_note(f"Ошибка очистки блокировки: {cleanup}")
            if isinstance(exc, OSError) and not isinstance(
                exc, FileAccessError
            ):
                raise FileAccessError(*exc.args) from exc
            raise

    @staticmethod
    def _release(path: str, fd: int, data_fd: int | None) -> None:
        """Освободить данные раньше маркера, затем удалить свой lock-файл.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            fd: Открытый дескриптор файла, принадлежащий вызывающему коду.
            data_fd: Дескриптор данных либо None, если данные не
                открывались.

        Raises:
            OSError: Не удалось закрыть дескриптор или удалить маркер.
        """
        with _ownership(path):
            errors = []
            for handle in (data_fd, fd):
                if handle is not None:
                    try:
                        os.close(handle)
                    except OSError as exc:
                        errors.append(exc)
            try:
                os.unlink(lock_path(path))
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(exc)
            if errors:
                raise errors[0]

    @staticmethod
    def _read_schematic(path: str, parsed: ParseResult) -> dict | None:
        """Прочитать геометрию; повреждение даёт W701 вместо отказа open.

        Проверяется JSON-контейнер; проверку координат выполняет Canvas.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            parsed: Результат парсера, получающий предупреждение W701.

        Returns:
            Словарь геометрии либо None при отсутствии или повреждении.

        Raises:
            OSError: Существующий файл геометрии недоступен для чтения.
        """
        companion = schematic_path(path)
        try:
            data = json.loads(Path(companion).read_text(encoding="utf-8"))
            valid = (
                isinstance(data, dict)
                and type(data.get("version")) is int
                and data["version"] == 1
                and isinstance(data.get("blocks"), dict)
            )
            if not valid:
                raise ValueError("Неверный формат схематика")
            return data
        except FileNotFoundError:
            return None  # Отсутствие схематика разрешено форматом.
        except (ValueError, UnicodeError):
            parsed.diagnostics.append(
                Diagnostic(
                    "W701",
                    Severity.WARNING,
                    "editor",
                    "Файл схематика повреждён, используется авторазмещение",
                    SourceLoc(companion, None),
                )
            )
            return None

    def open(self, path: str, force_unlock: bool = False) -> OpenResult:
        """Открыть файл, не теряя прежний при ошибке доступа или синтаксиса.

        Сырые данные возвращаются даже при not ok. При повторном открытии
        своего файла существующая блокировка не захватывается повторно.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            force_unlock: Разрешить снятие только устаревшей блокировки.

        Returns:
            Результат парсера и геометрия; поле ok определяет успех.

        Raises:
            FileLockedError: Документ занят, а снятие блокировки невозможно.
            FileAccessError: Не удалось прочитать файл или его геометрию.
        """
        path = os.path.realpath(path)
        same = self._path is not None and os.path.normcase(
            path
        ) == os.path.normcase(self._path)
        handles = None if same else self._acquire(path, force_unlock)
        try:
            parsed = self._parser.parse(read_text(path), filename=path)
            schematic = self._read_schematic(path, parsed)
            result = OpenResult(path, parsed, schematic)
            if result.ok and not same:
                self.close()
                self._path, self._lock_fd, self._data_fd = path, *handles
                handles = None  # Владение перешло менеджеру.
            return result
        except OSError as exc:
            if isinstance(exc, FileAccessError):
                raise
            raise FileAccessError(*exc.args) from exc
        finally:
            if handles is not None:
                self._release(path, *handles)

    @staticmethod
    def _write_document(path: str, text: str, schematic: str) -> str:
        """Заменить оба файла и вернуть хэш .net, восстановив .sch при сбое.

        Каждый replace атомарен отдельно. При падении процесса между ними
        общей транзакции нет; обещать её без журнала восстановления нельзя.

        Args:
            path: Путь к файлу документа или вспомогательному файлу.
            text: Текст для записи в кодировке UTF-8.
            schematic: Готовый JSON-текст геометрии для записи.

        Returns:
            SHA-256 записанного файла нетлиста.

        Raises:
            OSError: Ошибка записи документа или восстановления геометрии.
        """
        companion = schematic_path(path)
        try:
            old_schematic = Path(companion).read_bytes()
        except FileNotFoundError:
            old_schematic = None
        write_text_atomic(companion, schematic)
        try:
            return write_text_atomic(path, text)
        except OSError:
            # Не оставляем новую геометрию рядом со старым нетлистом,
            # если ОС отказала в замене самого документа.
            if old_schematic is None:
                os.unlink(companion)
            else:
                _write_bytes_atomic(companion, old_schematic)
            raise

    def _restore_data_lock(self) -> None:
        """Захватить новый файл после replace, не сохраняя неверный fd.

        Raises:
            FileAccessError: Не удалось заново заблокировать файл данных.
        """
        if self._path is None or self._data_fd is not None:
            return
        fd = None
        try:
            fd = _open_locked_data(self._path)
            self._data_fd = fd  # Сохраняем только успешно захваченный fd.
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise FileAccessError(*exc.args) from exc

    def _save_locked(
        self, target: str, text: str, schematic: str
    ) -> SaveResult:
        """Сохранить под маркером и сменить документ только после записи.

        Args:
            target: Абсолютный путь назначения сохраняемого документа.
            text: Текст для записи в кодировке UTF-8.
            schematic: Готовый JSON-текст геометрии для записи.

        Returns:
            Путь сохранённого документа и SHA-256 его байтов.

        Raises:
            FileAccessError: Не удалось записать или заблокировать документ.
        """
        current = os.path.normcase(self._path or "")
        same = current == os.path.normcase(target)
        handles = None if same else self._acquire(target)
        try:
            if same and self._data_fd is not None:
                os.close(self._data_fd)
                self._data_fd = None
            elif handles is not None and handles[1] is not None:
                os.close(handles[1])
                handles = (handles[0], None)
            sha = self._write_document(target, text, schematic)
            if not same:
                self.close()
                self._path, self._lock_fd = target, handles[0]
                handles = None
            return SaveResult(target, sha)
        except OSError as exc:
            if isinstance(exc, FileAccessError):
                raise
            raise FileAccessError(*exc.args) from exc
        finally:
            if handles is not None:
                self._release(target, *handles)
            self._restore_data_lock()

    def save(
        self, model: Netlist, schematic: dict, path: str | None = None
    ) -> SaveResult:
        """Сохранить принятую модель; path задаёт операцию «Сохранить как».

        Сериализация идёт до захвата нового файла: неверные входные данные
        не должны менять документ или его блокировки. Новый документ без
        path даёт ValueError, а ошибки файловой системы — FileAccessError.

        Args:
            model: Принятая модель нетлиста, прошедшая проверки редактора.
            schematic: Геометрия блоков, экземпляров и цепей документа.
            path: Путь к файлу документа или вспомогательному файлу.

        Returns:
            Путь сохранённого документа и SHA-256 его байтов.

        Raises:
            ValueError: Не задан путь нового документа или данные
                некорректны.
            FileAccessError: Не удалось записать или заблокировать файлы.
        """
        target = os.path.realpath(path) if path is not None else self._path
        if target is None:
            raise ValueError("Для нового документа требуется путь")
        text = self._serializer.serialize(model)
        sch_text = (
            json.dumps(schematic, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        )
        return self._save_locked(target, text, sch_text)

    @contextmanager
    def transaction(self):
        """Сохраняет журнал старого поколения документа и истории.

        При ошибке восстанавливает файлы; после аварии восстановление
        происходит при следующем открытии. Успех фиксирует удаление журнала.
        """
        path = self._path
        if path is None:
            raise ValueError("Транзакция требует открытого документа")
        targets = [path, schematic_path(path), history_path(path)]
        snapshots = {}
        for target in targets:
            payload = (
                Path(target).read_bytes() if Path(target).exists() else None
            )
            snapshots[target] = (
                base64.b64encode(payload).decode("ascii")
                if payload is not None
                else None
            )
        journal = path + ".transaction.json"
        if Path(journal).exists():
            raise FileAccessError("Есть незавершённая транзакция")
        write_text_atomic(journal, json.dumps(snapshots))
        try:
            yield
            Path(journal).unlink()
        except BaseException:
            if self._data_fd is not None:
                os.close(self._data_fd)
                self._data_fd = None
            try:
                _restore_transaction(path)
            finally:
                self._restore_data_lock()
            raise

    def close(self) -> None:
        """Освободить документ; повторный вызов ничего не делает.

        Raises:
            FileAccessError: Не удалось освободить ресурсы документа.
        """
        if self._path is not None:
            try:
                self._release(self._path, self._lock_fd, self._data_fd)
            except OSError as exc:
                raise FileAccessError(*exc.args) from exc
            finally:
                self._path = self._lock_fd = self._data_fd = None


@contextmanager
def _windows_ownership(path: str):
    """Сериализует операции с маркером документа через mutex Windows.

    Args:
        path: Канонический путь документа.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    key = os.path.normcase(os.path.realpath(path)).encode("utf-8")
    handle = kernel.CreateMutexW(
        None, False, "Local\\Netlist-" + hashlib.sha256(key).hexdigest()
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        if status not in (0, 0x80):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
    finally:
        kernel.CloseHandle(handle)


def _restore_transaction(path: str) -> None:
    """Восстанавливает прежнее поколение из журнала перед открытием.

    Args:
        path: Путь документа, маркер которого уже захвачен.
    """
    journal = path + ".transaction.json"
    if not Path(journal).exists():
        return
    allowed = [path, schematic_path(path), history_path(path)]
    try:
        data = json.loads(read_text(journal))
        if not isinstance(data, dict) or set(data) != set(allowed):
            raise ValueError("Неожиданные файлы в журнале")
        snapshots = {
            target: (
                base64.b64decode(payload, validate=True)
                if payload is not None else None
            )
            for target, payload in data.items()
        }
    except (TypeError, ValueError) as exc:
        raise FileAccessError("Некорректный журнал восстановления") from exc

    # Сначала проверяем все записи, чтобы битый журнал не восстановил
    # только часть комплекта файлов.
    for target in allowed:
        payload = snapshots[target]
        if payload is None:
            Path(target).unlink(missing_ok=True)
        else:
            _write_bytes_atomic(target, payload)
    Path(journal).unlink()


def _write_marker(fd: int) -> None:
    """Записывает владельца и блокирует служебный диапазон маркера.

    Args:
        fd: Дескриптор созданного маркера.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    owner = (
        f"pid={os.getpid()} host={socket.gethostname()} " f"time={timestamp}\n"
    )
    os.write(fd, owner.encode("utf-8"))
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0x7FFFFFFF, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _process_alive(pid: int) -> bool:
    """Проверяет локальный PID без отправки сигнала процессу."""
    if os.name == "nt":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        # Недоступный или сомнительный PID не даёт права снять маркер.
        return True
    return True


def _open_locked_data(path: str) -> int:
    """Открывает данные под системной блокировкой текущей платформы."""
    if os.name == "nt":
        return _open_windows_data(path)

    import fcntl

    fd = os.open(path, os.O_RDWR)
    try:
        # Чтение через другой дескриптор не снимает flock.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _ownership(path: str):
    """Сериализует чтение, замену и удаление маркера между процессами."""
    if os.name == "nt":
        with _windows_ownership(path):
            yield
        return

    import fcntl

    # Имя должно всегда указывать на один inode. Удаление после unlock
    # позволило бы ожидающему процессу и новичку взять разные блокировки.
    guard = lock_path(path) + ".guard"
    fd = os.open(guard, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
