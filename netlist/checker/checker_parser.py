"""CheckerParser: черновик нетлиста для чекера.

Модуль чекера проекта «Нетлист»: `netlist/checker/checker_parser.py`.
Автор: М. Шифрин. Интерфейс согласовать с Н. Бучилкиным (NetlistParser),
см. interfaces.md, п. 4.2 и п. 2.2.

Зачем он нужен
--------------
NetlistParser (общий для редактора и чекера) превращает текст в «сырые»
данные: записи строк файла, сгруппированные по блокам, без разрешения
ссылок. Проверкам же нужен протокол чтения `NetlistView`. Этот модуль
делает из сырых данных `CheckerDraft`, который реализует `NetlistView`.

В отличие от принятой модели `Netlist`, черновик НЕ требует
корректности: дубликаты имён, ссылки на необъявленные цепи, блоки и
инстансы сохраняются как есть, чтобы проверки могли их найти.

Правила построения
------------------
* Порядок примитивов, блоков, пинов, цепей и инстансов — как в файле.
* Примитивы библиотеки (`--primitives`) идут ПОСЛЕ примитивов файла.
  Поэтому `find_primitive` при совпадении имён находит примитив файла,
  а дубликат видит HierarchyCheck (E205).
* Строка `INST.PIN` привязывается к ПЕРВОМУ инстансу с именем `INST` в
  своём блоке. Порядок строк в блоке свободный, так что строка может
  стоять и раньше строки `inst`. Если инстанса с таким именем в блоке
  нет, строка попадает в `orphan_connections()` (E405 в SubcircuitCheck).
* `find_block` и `find_primitive` возвращают первое вхождение имени.

Черновик неизменяем: при создании данные копируются в собственные
замороженные объекты, а методы отдают новые списки. Поэтому ни
проверки, ни дальнейшие изменения сырых данных его не портят.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from netlist.core.model import Diagnostic, PinRef, SourceLoc

if TYPE_CHECKING:
    # Сырые данные нужны только для аннотаций. NetlistParser во время
    # выполнения импортируется внутри load(), см. комментарий там.
    from netlist.core.parser import (
        RawBlock,
        RawConnection,
        RawNetlist,
        RawPrimitive,
    )


# Объекты черновика. Все поля совпадают с протоколами NetlistView
# (interfaces.md, п. 2.1), так что проверки работают с ними напрямую.

@dataclass(frozen=True)
class DraftPrimitive:
    """Строка `primitive NAME PIN...` (из файла или из библиотеки)."""
    name: str
    pin_names: list[str]
    loc: SourceLoc


@dataclass(frozen=True)
class DraftPin:
    """Строка `pin NAME NET` или `INST.PIN NET`. `net=None` — это `-`."""
    name: str
    net: str | None
    loc: SourceLoc


@dataclass(frozen=True)
class DraftNet:
    """Строка `net NAME`."""
    name: str
    loc: SourceLoc


@dataclass(frozen=True)
class DraftOrphan:
    """Строка `INST.PIN NET`, у которой в блоке нет инстанса `INST`."""
    ref: PinRef
    net: str | None
    loc: SourceLoc


@dataclass(frozen=True)
class DraftInstance:
    """Строка `inst NAME sub|prim TYPE` вместе со строками своих пинов."""
    name: str
    kind: Literal["sub", "prim"]
    type_name: str
    loc: SourceLoc
    _pins: tuple[DraftPin, ...] = ()

    def pins(self) -> list[DraftPin]:
        """Пины, упомянутые в строках INST.PIN, в порядке файла."""
        return list(self._pins)


@dataclass(frozen=True)
class DraftBlock:
    """Блок от строки `block` до `end`."""
    name: str
    loc: SourceLoc
    _pins: tuple[DraftPin, ...] = ()
    _nets: tuple[DraftNet, ...] = ()
    _instances: tuple[DraftInstance, ...] = ()
    _orphans: tuple[DraftOrphan, ...] = ()

    def pins(self) -> list[DraftPin]:
        """Внешние пины блока (строки `pin`)."""
        return list(self._pins)

    def nets(self) -> list[DraftNet]:
        return list(self._nets)

    def instances(self) -> list[DraftInstance]:
        return list(self._instances)

    def orphan_connections(self) -> list[DraftOrphan]:
        return list(self._orphans)


class CheckerDraft:
    """Черновик чекера: неизменяемая обёртка над сырыми данными.

    Реализует протокол `NetlistView`. Может содержать любые дефекты
    нетлиста, см. docstring модуля.
    """

    def __init__(self, raw: RawNetlist,
                 library: list[RawPrimitive] | None = None) -> None:
        self.source_file: str | None = raw.source_file
        self._primitives: tuple[DraftPrimitive, ...] = tuple(
            _primitive(p) for p in [*raw.primitives, *(library or [])])
        self._blocks: tuple[DraftBlock, ...] = tuple(
            _block(b) for b in raw.blocks)
        # Первое вхождение каждого имени, для find_* за O(1).
        self._block_by_name: dict[str, DraftBlock] = {}
        for block in self._blocks:
            self._block_by_name.setdefault(block.name, block)
        self._primitive_by_name: dict[str, DraftPrimitive] = {}
        for prim in self._primitives:
            self._primitive_by_name.setdefault(prim.name, prim)

    def primitives(self) -> list[DraftPrimitive]:
        """Примитивы файла, затем примитивы библиотеки."""
        return list(self._primitives)

    def blocks(self) -> list[DraftBlock]:
        """Блоки в порядке файла, включая дубликаты."""
        return list(self._blocks)

    def find_block(self, name: str) -> DraftBlock | None:
        """Первый блок с таким именем или None."""
        return self._block_by_name.get(name)

    def find_primitive(self, name: str) -> DraftPrimitive | None:
        """Первый примитив с таким именем или None."""
        return self._primitive_by_name.get(name)

    def __repr__(self) -> str:
        return (f"CheckerDraft(source_file={self.source_file!r}, "
                f"primitives={len(self._primitives)}, "
                f"blocks={len(self._blocks)})")


@dataclass
class LoadResult:
    """Результат load(): черновик и диагностики разбора."""
    draft: CheckerDraft
    diagnostics: list[Diagnostic]   # синтаксические и ошибки библиотеки


def load(text: str, filename: str | None = None,
         library_text: str | None = None,
         library_name: str | None = None) -> LoadResult:
    """Разобрать текст нетлиста (и библиотеку примитивов) в черновик.

    Аргументы:
        text:         текст файла нетлиста.
        filename:     путь к нему для SourceLoc и сообщений.
        library_text: текст библиотеки примитивов (`--primitives`) или
                      None, если библиотеки нет.
        library_name: путь к библиотеке для SourceLoc.

    Черновик возвращается даже при синтаксических ошибках: в нём есть
    всё, что удалось разобрать. Диагностики идут в порядке: сначала
    файла, затем библиотеки. Исключений из-за содержимого не бросает.
    """
    # Импорт здесь, а не наверху: CheckerDraft нужен и без парсера
    # (например, в тестах проверок), а парсер тянет за собой SyntaxCheck.
    from netlist.core.parser import NetlistParser

    parser = NetlistParser()
    parsed = parser.parse(text, filename)
    diagnostics = list(parsed.diagnostics)
    library: list[RawPrimitive] | None = None
    if library_text is not None:
        lib = parser.parse_library(library_text, library_name)
        diagnostics.extend(lib.diagnostics)
        library = lib.raw.primitives
    return LoadResult(CheckerDraft(parsed.raw, library), diagnostics)


# Построение объектов черновика из сырых данных
def _primitive(raw: RawPrimitive) -> DraftPrimitive:
    return DraftPrimitive(raw.name, list(raw.pin_names), raw.loc)


def _block(raw: RawBlock) -> DraftBlock:
    """Собрать блок и привязать строки INST.PIN к инстансам.

    Строки собираются по имени инстанса за один проход, затем каждый
    инстанс забирает свои. Первому инстансу с данным именем достаются
    все строки этого имени, дубликатам (E404) — ни одной. Строки без
    инстанса становятся сиротами. Всё за O(размер блока).
    """
    by_instance: dict[str, list[RawConnection]] = {}
    for conn in raw.connections:
        by_instance.setdefault(conn.instance, []).append(conn)

    instances: list[DraftInstance] = []
    for inst in raw.instances:
        conns = by_instance.pop(inst.name, [])
        instances.append(DraftInstance(
            inst.name, cast("Literal['sub', 'prim']", inst.kind),
            inst.type_name, inst.loc,
            tuple(DraftPin(c.pin, c.net, c.loc) for c in conns)))

    # Всё, что осталось в by_instance, не нашло своего инстанса. Порядок
    # сирот — как в файле, а не по именам инстансов.
    orphans = tuple(
        DraftOrphan(PinRef(c.instance, c.pin), c.net, c.loc)
        for c in raw.connections if c.instance in by_instance)

    return DraftBlock(
        raw.name, raw.loc,
        tuple(DraftPin(p.name, p.net, p.loc) for p in raw.pins),
        tuple(DraftNet(n.name, n.loc) for n in raw.nets),
        tuple(instances),
        orphans)
