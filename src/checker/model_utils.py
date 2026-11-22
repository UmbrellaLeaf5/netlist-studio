"""Индексы и поиск по NetlistView без изменения модели.

Исправленная версия приложенного model_utils.py. Коллекции сохраняют
дубликаты; подключения читаются через публичные протоколы модели.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TypeVar

from src.core.model import (
    BlockView, InstanceView, NetlistView, NetView, PinRef, PinView,
    PrimitiveView, SourceLoc,
)

T = TypeVar("T")


def _index(items: Iterable[T], key: Callable[[T], str]) -> dict[str, list[T]]:
    result: dict[str, list[T]] = {}
    for item in items:
        result.setdefault(key(item), []).append(item)
    return result


def find_duplicates(items: Iterable[T], key: Callable[[T], str]) -> dict[str, list[T]]:
    return {name: group for name, group in _index(items, key).items() if len(group) > 1}


def index_blocks(view: NetlistView) -> dict[str, list[BlockView]]:
    return _index(view.blocks(), lambda b: b.name)


def index_primitives(view: NetlistView) -> dict[str, list[PrimitiveView]]:
    return _index(view.primitives(), lambda p: p.name)


def index_instances(block: BlockView) -> dict[str, list[InstanceView]]:
    return _index(block.instances(), lambda i: i.name)


def index_nets(block: BlockView) -> dict[str, list[NetView]]:
    return _index(block.nets(), lambda n: n.name)


def index_pins(block: BlockView) -> dict[str, list[PinView]]:
    return _index(block.pins(), lambda p: p.name)


def _connections(block: BlockView) -> Iterator[tuple[PinRef, str | None, SourceLoc]]:
    for pin in block.pins():
        yield PinRef(None, pin.name), pin.net, pin.loc
    for inst in block.instances():
        for pin in inst.pins():
            yield PinRef(inst.name, pin.name), pin.net, pin.loc
    for conn in block.orphan_connections():
        yield conn.ref, conn.net, conn.loc


def pins_of_net(block: BlockView) -> dict[str, list[tuple[PinRef, SourceLoc]]]:
    """Все подключения, включая повторы и orphan; None исключается."""
    result: dict[str, list[tuple[PinRef, SourceLoc]]] = {}
    for ref, net, loc in _connections(block):
        if net is not None:
            result.setdefault(net, []).append((ref, loc))
    return result


def net_of_pin(block: BlockView, ref: PinRef) -> str | None:
    for candidate, net, _ in _connections(block):
        if candidate == ref:
            return net
    return None


def block_pin_names(block: BlockView) -> list[str]:
    return [pin.name for pin in block.pins()]


def instance_pin_names(inst: InstanceView) -> list[str]:
    return [pin.name for pin in inst.pins()]


def resolve_type(view: NetlistView, inst: InstanceView) -> BlockView | PrimitiveView | None:
    """Первое определение в пространстве имён, соответствующем kind."""
    if inst.kind == "sub":
        return view.find_block(inst.type_name)
    if inst.kind == "prim":
        return view.find_primitive(inst.type_name)
    return None


def iter_instances(view: NetlistView) -> Iterator[tuple[BlockView, InstanceView]]:
    for block in view.blocks():
        for inst in block.instances():
            yield block, inst


class ModelIndex:
    """Кэш одного представления; после изменений создаётся новый индекс.

    init оставлен для совместимости с приложенной версией; основной API —
    ModelIndex(view). Ссылки на блоки предотвращают повторное использование
    id временных BlockView, пока их индексы находятся в кэше.
    """

    def __init__(self, view: NetlistView | None = None) -> None:
        self._view = view
        self._blocks: dict[str, list[BlockView]] | None = None
        self._primitives: dict[str, list[PrimitiveView]] | None = None
        self._nets_cache: dict[int, dict[str, list[NetView]]] = {}
        self._pins_cache: dict[int, dict[str, list[tuple[PinRef, SourceLoc]]]] = {}
        self._owners: dict[int, BlockView] = {}

    def init(self, view: NetlistView) -> None:
        self.__init__(view)

    @property
    def blocks(self) -> dict[str, list[BlockView]]:
        if self._blocks is None:
            self._blocks = index_blocks(self._view) if self._view is not None else {}
        return self._blocks

    @property
    def primitives(self) -> dict[str, list[PrimitiveView]]:
        if self._primitives is None:
            self._primitives = index_primitives(self._view) if self._view is not None else {}
        return self._primitives

    def nets(self, block: BlockView) -> dict[str, list[NetView]]:
        key = id(block)
        if key not in self._nets_cache:
            self._owners[key] = block
            self._nets_cache[key] = index_nets(block)
        return self._nets_cache[key]

    def pins_of_net(self, block: BlockView) -> dict[str, list[tuple[PinRef, SourceLoc]]]:
        key = id(block)
        if key not in self._pins_cache:
            self._owners[key] = block
            self._pins_cache[key] = pins_of_net(block)
        return self._pins_cache[key]
