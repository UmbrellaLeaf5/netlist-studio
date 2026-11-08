"""Публичный фасад NetlistModel с сохранением прежних импортов.

Автор: Д. Криворучко. Контракт: docs/interfaces.md, раздел 2.1.

Реализация разделена по ответственности:

* diagnostics.py — общие значения Severity, InstanceKind, SourceLoc,
  Diagnostic, PinRef и CheckOptions;
* views.py — протоколы NetlistView и входящих в него представлений;
* entities.py — Primitive, Block, Instance, Pin, Net и их операции;
* netlist.py — коллекция определений, копирование и принятие состояния.

Этот модуль оставлен как стабильная точка импорта: существующие модули
могут продолжать писать `from netlist.core.model import Netlist`, не зная
внутреннего разбиения. Новую реализацию следует помещать в профильный
модуль, а публичные имена экспортировать отсюда. Это позволяет читать
каждый компонент отдельно и избежать обратных импортов между слоями.
"""

from netlist.core.diagnostics import (
    CheckOptions,
    Diagnostic,
    InstanceKind,
    PinRef,
    Severity,
    SourceLoc,
)
from netlist.core.entities import Block, Instance, Net, Pin, Primitive
from netlist.core.errors import ModelError
from netlist.core.netlist import Netlist
from netlist.core.views import (
    BlockView,
    InstanceView,
    NetlistView,
    NetView,
    OrphanConnection,
    PinView,
    PrimitiveView,
)

__all__ = [
    "Block",
    "BlockView",
    "CheckOptions",
    "Diagnostic",
    "Instance",
    "InstanceKind",
    "InstanceView",
    "ModelError",
    "Net",
    "Netlist",
    "NetlistView",
    "NetView",
    "OrphanConnection",
    "Pin",
    "PinRef",
    "PinView",
    "Primitive",
    "PrimitiveView",
    "Severity",
    "SourceLoc",
]
