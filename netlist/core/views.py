"""Протоколы чтения нетлиста для модели и промежуточных черновиков.

Автор: Д. Криворучко. Контракт: docs/interfaces.md, раздел 2.1.

Протоколы намеренно не требуют наследования от классов принятой модели.
Редактор и чекер смогут предоставить свои черновики, в том числе с
дубликатами и некорректными ссылками, не копируя данные заранее.
Поэтому коллекции здесь являются упорядоченными списками, а поиск
возвращает первое совпадение. Полная проверка черновика остаётся делом
чекера; модель при принятии проверяет только свои структурные инварианты.
"""

from typing import Protocol

from netlist.core.diagnostics import InstanceKind, PinRef, SourceLoc


class PrimitiveView(Protocol):
    """Определение разрешённого примитива для чтения."""

    name: str
    pin_names: list[str]
    loc: SourceLoc


class PinView(Protocol):
    """Пин и имя подключённой цепи; None означает отсутствие подключения."""

    name: str
    net: str | None
    loc: SourceLoc


class NetView(Protocol):
    """Объявление цепи без графической геометрии."""

    name: str
    loc: SourceLoc


class InstanceView(Protocol):
    """Размещённое использование определения блока или примитива."""

    name: str
    kind: InstanceKind
    type_name: str
    loc: SourceLoc

    def pins(self) -> list[PinView]:
        """Пины в порядке упоминания, включая повторы в черновике."""
        ...


class OrphanConnection(Protocol):
    """Подключение к необъявленному инстансу в некорректном черновике."""

    ref: PinRef
    net: str | None
    loc: SourceLoc


class BlockView(Protocol):
    """Определение подсхемы, общее для модели и черновиков."""

    name: str
    loc: SourceLoc

    def pins(self) -> list[PinView]:
        """Внешние пины в порядке документа."""
        ...

    def nets(self) -> list[NetView]:
        """Объявления цепей в порядке документа."""
        ...

    def instances(self) -> list[InstanceView]:
        """Экземпляры в порядке документа."""
        ...

    def orphan_connections(self) -> list[OrphanConnection]:
        """Подключения, для которых не найден инстанс."""
        ...


class NetlistView(Protocol):
    """Общий интерфейс чтения принятого состояния и черновиков."""

    source_file: str | None

    def primitives(self) -> list[PrimitiveView]:
        """Все определения примитивов в порядке документа."""
        ...

    def blocks(self) -> list[BlockView]:
        """Все определения блоков, а не развёрнутые экземпляры."""
        ...

    def find_block(self, name: str) -> BlockView | None:
        """Первое определение с таким именем либо None."""
        ...

    def find_primitive(self, name: str) -> PrimitiveView | None:
        """Первое определение примитива с таким именем либо None."""
        ...
