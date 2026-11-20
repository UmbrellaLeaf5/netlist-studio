"""Графический редактор нетлиста: черновик, холст и команды истории.

Автор: А. Пронин. Контракт: docs/interfaces.md, разделы 3.1--3.6.

Холст владеет ``EditorDraft`` и графическими объектами. Любое изменение
сначала попадает в черновик, а затем, после проверки, через ``commit``
переносится в принятую ``Netlist``. Элементарные операции ``op_*``
вызываются только командами ``UndoStack``, поэтому графика и черновик
не расходятся.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal as Signal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)
from PyQt5.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QDialog,
    QInputDialog,
    QMenu,
    QWidget,
)

from netlist.core.model import (
    Diagnostic,
    ModelError,
    Netlist,
    NetlistView,
    PinRef,
    Severity,
    SourceLoc,
)
from netlist.core.parser import RawNetlist
from netlist.editor.undo_stack import (
    CanvasCommand,
    UndoStack,
    register_command,
)

if TYPE_CHECKING:
    from netlist.editor.block_list import BlockList
    from netlist.editor.tool_palette import ToolPalette

CheckFn = Callable[[NetlistView], list[Diagnostic]]
ItemId = tuple[str, ...]

_UNKNOWN = SourceLoc(None, None)
ELEMENT_MIME = "application/x-netlist-element"

GRID = 20.0
DEFAULT_SIZE = (170.0, 110.0)
EXTERNAL_PIN_SIZE = (76.0, 30.0)


class Tool(Enum):
    """Инструмент холста."""

    SELECT = "select"
    PLACE = "place"
    WIRE = "wire"
    # Совместимый синоним из ранней версии интерфейса.
    NET = "wire"


def _tool_name(tool: Tool) -> str:
    """Русское название инструмента для строки состояния."""

    return {
        Tool.SELECT: "Выделение",
        Tool.PLACE: "Размещение",
        Tool.WIRE: "Соединение",
    }.get(tool, "Выделение")


# ----------------------------------------------------------------------
# Черновик
# ----------------------------------------------------------------------
@dataclass
class DraftPin:
    name: str
    net: str | None = None
    loc: SourceLoc = _UNKNOWN


@dataclass
class DraftNet:
    name: str
    loc: SourceLoc = _UNKNOWN


@dataclass
class DraftPrimitive:
    name: str
    pin_names: list[str] = field(default_factory=list)
    loc: SourceLoc = _UNKNOWN


@dataclass
class DraftOrphan:
    ref: PinRef
    net: str | None
    loc: SourceLoc = _UNKNOWN


class DraftInstance:
    """Инстанс в черновике; повторы имён и цепей допустимы."""

    def __init__(
        self,
        name: str,
        kind: str,
        type_name: str,
        loc: SourceLoc = _UNKNOWN,
    ) -> None:
        self.name = name
        self.kind = kind
        self.type_name = type_name
        self.loc = loc
        self._pins: list[DraftPin] = []

    def pins(self) -> list[DraftPin]:
        return list(self._pins)

    def add_pin(self, name: str, net: str | None = None) -> DraftPin:
        pin = DraftPin(name, net)
        self._pins.append(pin)
        return pin

    def remove_pin(self, name: str) -> DraftPin | None:
        for index, pin in enumerate(self._pins):
            if pin.name == name:
                return self._pins.pop(index)
        return None

    def rename_pin(self, old: str, new: str) -> None:
        for pin in self._pins:
            if pin.name == old:
                pin.name = new
                return

    def connect_pin(self, name: str, net: str | None) -> None:
        for pin in self._pins:
            if pin.name == name:
                pin.net = net
                return

    def set_type(self, kind: str, type_name: str) -> None:
        self.kind = kind
        self.type_name = type_name

    def copy(self, name: str | None = None) -> "DraftInstance":
        result = DraftInstance(name or self.name, self.kind, self.type_name,
                               self.loc)
        result._pins = [DraftPin(p.name, p.net, p.loc) for p in self._pins]
        return result


class DraftBlock:
    """Определение блока в черновике."""

    def __init__(self, name: str, loc: SourceLoc = _UNKNOWN) -> None:
        self.name = name
        self.loc = loc
        self._pins: list[DraftPin] = []
        self._nets: list[DraftNet] = []
        self._instances: list[DraftInstance] = []
        self._orphans: list[DraftOrphan] = []

    def pins(self) -> list[DraftPin]:
        return list(self._pins)

    def nets(self) -> list[DraftNet]:
        return list(self._nets)

    def instances(self) -> list[DraftInstance]:
        return list(self._instances)

    def orphan_connections(self) -> list[DraftOrphan]:
        return list(self._orphans)

    def add_pin(self, name: str, net: str | None = None) -> DraftPin:
        pin = DraftPin(name, net)
        self._pins.append(pin)
        return pin

    def remove_pin(self, name: str) -> DraftPin | None:
        for index, pin in enumerate(self._pins):
            if pin.name == name:
                return self._pins.pop(index)
        return None

    def rename_pin(self, old: str, new: str) -> None:
        for pin in self._pins:
            if pin.name == old:
                pin.name = new
                return

    def connect_pin(self, name: str, net: str | None) -> None:
        for pin in self._pins:
            if pin.name == name:
                pin.net = net
                return

    def add_net(self, name: str) -> DraftNet:
        net = DraftNet(name)
        self._nets.append(net)
        return net

    def remove_net(self, name: str) -> DraftNet | None:
        for index, net in enumerate(self._nets):
            if net.name == name:
                return self._nets.pop(index)
        return None

    def rename_net(self, old: str, new: str) -> None:
        for net in self._nets:
            if net.name == old:
                net.name = new
                break
        for pin in self._pins:
            if pin.net == old:
                pin.net = new
        for instance in self._instances:
            for pin in instance._pins:
                if pin.net == old:
                    pin.net = new

    def add_instance(
        self, name: str, kind: str, type_name: str
    ) -> DraftInstance:
        instance = DraftInstance(name, kind, type_name)
        self._instances.append(instance)
        return instance

    def remove_instance(self, name: str) -> DraftInstance | None:
        for index, instance in enumerate(self._instances):
            if instance.name == name:
                return self._instances.pop(index)
        return None

    def rename_instance(self, old: str, new: str) -> None:
        for instance in self._instances:
            if instance.name == old:
                instance.name = new
                return

    def instance(self, name: str) -> DraftInstance:
        for instance in self._instances:
            if instance.name == name:
                return instance
        raise KeyError(name)

    def copy(self, new_name: str) -> "DraftBlock":
        result = DraftBlock(new_name, self.loc)
        result._nets = [DraftNet(n.name, n.loc) for n in self._nets]
        result._pins = [DraftPin(p.name, p.net, p.loc) for p in self._pins]
        result._instances = [i.copy() for i in self._instances]
        result._orphans = [
            DraftOrphan(o.ref, o.net, o.loc) for o in self._orphans
        ]
        return result


class EditorDraft:
    """Изменяемое представление ``NetlistView`` с мягкими инвариантами."""

    def __init__(self) -> None:
        self.source_file: str | None = None
        self._primitives: list[DraftPrimitive] = []
        self._blocks: list[DraftBlock] = []

    # ---- построение -------------------------------------------------
    @classmethod
    def from_raw(cls, raw: RawNetlist) -> "EditorDraft":
        draft = cls()
        draft.source_file = raw.source_file
        for primitive in raw.primitives:
            item = draft.add_primitive(
                primitive.name, list(primitive.pin_names)
            )
            item.loc = primitive.loc
        for raw_block in raw.blocks:
            block = draft.add_block(raw_block.name)
            block.loc = raw_block.loc
            for raw_net in raw_block.nets:
                block.add_net(raw_net.name).loc = raw_net.loc
            for raw_pin in raw_block.pins:
                block.add_pin(raw_pin.name, raw_pin.net).loc = raw_pin.loc
            first: dict[str, DraftInstance] = {}
            for raw_instance in raw_block.instances:
                instance = block.add_instance(
                    raw_instance.name,
                    raw_instance.kind,
                    raw_instance.type_name,
                )
                instance.loc = raw_instance.loc
                first.setdefault(raw_instance.name, instance)
            for connection in raw_block.connections:
                instance = first.get(connection.instance)
                if instance is None:
                    block._orphans.append(DraftOrphan(
                        PinRef(connection.instance, connection.pin),
                        connection.net,
                        connection.loc,
                    ))
                else:
                    instance.add_pin(
                        connection.pin, connection.net
                    ).loc = connection.loc
        return draft

    @classmethod
    def from_model(cls, model: Netlist) -> "EditorDraft":
        draft = cls()
        draft.source_file = model.source_file
        for primitive in model.primitives():
            item = draft.add_primitive(
                primitive.name, list(primitive.pin_names)
            )
            item.loc = primitive.loc
        for model_block in model.blocks():
            block = draft.add_block(model_block.name)
            block.loc = model_block.loc
            for net in model_block.nets():
                block.add_net(net.name).loc = net.loc
            for pin in model_block.pins():
                block.add_pin(pin.name, pin.net).loc = pin.loc
            for model_instance in model_block.instances():
                instance = block.add_instance(
                    model_instance.name,
                    str(model_instance.kind),
                    model_instance.type_name,
                )
                instance.loc = model_instance.loc
                for pin in model_instance.pins():
                    instance.add_pin(pin.name, pin.net).loc = pin.loc
        return draft

    @classmethod
    def new_document(cls) -> "EditorDraft":
        draft = cls()
        draft.add_block("main")
        return draft

    # ---- чтение -----------------------------------------------------
    def primitives(self) -> list[DraftPrimitive]:
        return list(self._primitives)

    def blocks(self) -> list[DraftBlock]:
        return list(self._blocks)

    def find_block(self, name: str) -> DraftBlock | None:
        for block in self._blocks:
            if block.name == name:
                return block
        return None

    def find_primitive(self, name: str) -> DraftPrimitive | None:
        for primitive in self._primitives:
            if primitive.name == name:
                return primitive
        return None

    def block(self, name: str) -> DraftBlock:
        block = self.find_block(name)
        if block is None:
            raise KeyError(name)
        return block

    def add_primitive(
        self, name: str, pin_names: list[str]
    ) -> DraftPrimitive:
        primitive = DraftPrimitive(name, list(pin_names))
        self._primitives.append(primitive)
        return primitive

    def remove_primitive(self, name: str) -> DraftPrimitive | None:
        for index, primitive in enumerate(self._primitives):
            if primitive.name == name:
                return self._primitives.pop(index)
        return None

    def add_block(
        self, name: str, index: int | None = None
    ) -> DraftBlock:
        block = DraftBlock(name)
        if index is None:
            self._blocks.append(block)
        else:
            self._blocks.insert(index, block)
        return block

    def remove_block(self, name: str) -> DraftBlock | None:
        for index, block in enumerate(self._blocks):
            if block.name == name:
                return self._blocks.pop(index)
        return None

    def move_block(self, name: str, index: int) -> None:
        block = self.block(name)
        self._blocks.remove(block)
        self._blocks.insert(index, block)

    def rename_block(self, old: str, new: str) -> None:
        block = self.find_block(old)
        if block is None:
            return
        block.name = new
        for candidate in self._blocks:
            for instance in candidate._instances:
                if instance.kind == "sub" and instance.type_name == old:
                    instance.type_name = new

    def copy_block(
        self, src: str, new_name: str, index: int | None = None
    ) -> DraftBlock:
        source = self.block(src)
        copied = source.copy(new_name)
        if index is None:
            self._blocks.append(copied)
        else:
            self._blocks.insert(index, copied)
        return copied

    def definition_pins(self, kind: str, type_name: str) -> list[str]:
        if kind == "sub":
            block = self.find_block(type_name)
            if block is not None:
                return [pin.name for pin in block.pins()]
        else:
            primitive = self.find_primitive(type_name)
            if primitive is not None:
                return list(primitive.pin_names)
        return []


def _loc_to_list(loc: SourceLoc) -> list[object]:
    return [loc.file, loc.line]


def _loc_from_list(value: object) -> SourceLoc:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return SourceLoc(value[0], value[1])
    return _UNKNOWN


def block_to_dict(block: DraftBlock) -> dict:
    """JSON-совместимый снимок блока для команды удаления."""

    return {
        "name": block.name,
        "loc": _loc_to_list(block.loc),
        "pins": [
            {"name": p.name, "net": p.net, "loc": _loc_to_list(p.loc)}
            for p in block._pins
        ],
        "nets": [
            {"name": n.name, "loc": _loc_to_list(n.loc)}
            for n in block._nets
        ],
        "instances": [
            {
                "name": i.name,
                "kind": i.kind,
                "type_name": i.type_name,
                "loc": _loc_to_list(i.loc),
                "pins": [
                    {"name": p.name, "net": p.net,
                     "loc": _loc_to_list(p.loc)}
                    for p in i._pins
                ],
            }
            for i in block._instances
        ],
        "orphans": [
            {
                "instance": o.ref.instance,
                "pin": o.ref.pin,
                "net": o.net,
                "loc": _loc_to_list(o.loc),
            }
            for o in block._orphans
        ],
    }


def block_from_dict(draft: EditorDraft, data: dict) -> DraftBlock:
    """Восстановить блок из снимка ``block_to_dict``."""

    block = DraftBlock(str(data["name"]), _loc_from_list(data.get("loc")))
    for net_data in data.get("nets", []):
        net = DraftNet(str(net_data["name"]),
                       _loc_from_list(net_data.get("loc")))
        block._nets.append(net)
    for pin_data in data.get("pins", []):
        block._pins.append(DraftPin(
            str(pin_data["name"]),
            pin_data.get("net"),
            _loc_from_list(pin_data.get("loc")),
        ))
    for inst_data in data.get("instances", []):
        instance = DraftInstance(
            str(inst_data["name"]),
            str(inst_data["kind"]),
            str(inst_data["type_name"]),
            _loc_from_list(inst_data.get("loc")),
        )
        for pin_data in inst_data.get("pins", []):
            instance._pins.append(DraftPin(
                str(pin_data["name"]),
                pin_data.get("net"),
                _loc_from_list(pin_data.get("loc")),
            ))
        block._instances.append(instance)
    for orphan_data in data.get("orphans", []):
        block._orphans.append(DraftOrphan(
            PinRef(orphan_data.get("instance"), str(orphan_data.get("pin"))),
            orphan_data.get("net"),
            _loc_from_list(orphan_data.get("loc")),
        ))
    return block


def instance_snapshot(
    block: DraftBlock, instance: DraftInstance, pos: Iterable[float] | None
) -> dict:
    """Снимок инстанса вместе с его подключениями и координатой."""

    pins = [
        {"name": pin.name, "net": pin.net} for pin in instance.pins()
    ]
    connections = {pin.name: pin.net for pin in instance.pins()}
    return {
        "block": block.name,
        "name": instance.name,
        "kind": instance.kind,
        "type_name": instance.type_name,
        "pos": list(pos) if pos is not None else [0.0, 0.0],
        "connections": connections,
        "pins": pins,
    }


def _diag(code: str, message: str, block: str | None,
          obj: str | None = None) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=Severity.ERROR,
        check="editor",
        message=message,
        loc=SourceLoc(None, None),
        block=block,
        obj=obj,
    )


# ----------------------------------------------------------------------
# Графические объекты
# ----------------------------------------------------------------------
class InstanceItem(QGraphicsObject):
    """Прямоугольник инстанса с подписью и пинами."""

    def __init__(
        self,
        canvas: "Canvas",
        block: str,
        name: str,
        kind: str,
        type_name: str,
        pos: QPointF,
        size: tuple[float, float],
    ) -> None:
        super().__init__()
        self.canvas = canvas
        self.block_name = block
        self.instance_name = name
        self.kind = kind
        self.type_name = type_name
        self._size = size
        self._pin_geometry: dict[str, tuple[str, float]] = {}
        self.setPos(pos)
        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setZValue(2)
        self.setToolTip(f"{kind} {type_name}")

    def size(self) -> tuple[float, float]:
        return self._size

    def set_pin_geometry(self, pins: dict[str, dict]) -> None:
        self._pin_geometry = {
            name: (str(data.get("side", "left")),
                   float(data.get("offset", 20.0)))
            for name, data in pins.items()
        }
        self.update()

    def pin_local(self, name: str) -> QPointF:
        side, offset = self._pin_geometry.get(name, ("left", 20.0))
        width, height = self._size
        if side == "left":
            return QPointF(0.0, offset)
        if side == "right":
            return QPointF(width, offset)
        if side == "top":
            return QPointF(offset, 0.0)
        return QPointF(offset, height)

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._size[0], self._size[1])

    def paint(self, painter: QPainter, option, widget=None) -> None:
        width, height = self._size
        rect = QRectF(0.0, 0.0, width, height)
        painter.setRenderHint(QPainter.Antialiasing, True)
        fill = QColor("#e8f2fc") if self.kind == "sub" else QColor("#f7ecec")
        border = QColor("#2f6699") if self.kind == "sub" else QColor("#8b4a4a")
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.6))
        painter.drawRect(rect)

        if self.isSelected():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#e58a1f"), 2.0, Qt.DashLine))
            painter.drawRect(rect.adjusted(-5, -5, 5, 5))

        painter.setPen(QColor("#1f2933"))
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(0.0, height / 2 - 24.0, width, 22.0),
            Qt.AlignCenter,
            self.instance_name,
        )
        painter.setFont(QFont("Sans", 10))
        painter.setPen(QColor("#39424e"))
        painter.drawText(
            QRectF(0.0, height / 2 - 2.0, width, 20.0),
            Qt.AlignCenter,
            f"{self.kind} {self.type_name}",
        )

        # Подписи пинов внутри прямоугольника рядом с их стороной.
        painter.setFont(QFont("Sans", 8))
        painter.setPen(QColor("#243b53"))
        for name, (side, offset) in self._pin_geometry.items():
            if side == "left":
                painter.drawText(
                    QRectF(5.0, offset - 9.0, width * 0.4, 18.0),
                    Qt.AlignLeft | Qt.AlignVCenter, name,
                )
            elif side == "right":
                painter.drawText(
                    QRectF(width * 0.6 - 5.0, offset - 9.0,
                           width * 0.4, 18.0),
                    Qt.AlignRight | Qt.AlignVCenter, name,
                )


class InstancePinItem(QGraphicsObject):
    """Кружок пина на границе инстанса."""

    def __init__(
        self,
        canvas: "Canvas",
        block: str,
        instance: str,
        pin: str,
        parent: InstanceItem,
    ) -> None:
        super().__init__(parent)
        self.canvas = canvas
        self.block_name = block
        self.instance_name = instance
        self.pin_name = pin
        self.setPos(parent.pin_local(pin))
        self.setZValue(3)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setToolTip(f"{instance}.{pin}")

    def boundingRect(self) -> QRectF:
        return QRectF(-6.0, -6.0, 12.0, 12.0)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(QColor("#2f6699"), 1.5))
        painter.drawEllipse(QPointF(0.0, 0.0), 4.5, 4.5)


class ExternalPinItem(QGraphicsObject):
    """Внешний пин блока в форме стрелки."""

    def __init__(
        self,
        canvas: "Canvas",
        block: str,
        name: str,
        side: str,
        anchor: QPointF,
    ) -> None:
        super().__init__()
        self.canvas = canvas
        self.block_name = block
        self.pin_name = name
        self.side = side
        self._width, self._height = EXTERNAL_PIN_SIZE
        if side == "right":
            self.setPos(anchor - QPointF(0.0, self._height / 2.0))
        elif side == "left":
            self.setPos(
                anchor - QPointF(self._width, self._height / 2.0)
            )
        elif side == "top":
            self.setRotation(90)
            self.setPos(anchor - QPointF(self._height / 2.0, self._width))
        else:
            self.setRotation(90)
            self.setPos(anchor)
        self.setFlags(
            QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable
        )
        self.setZValue(2)
        self.setToolTip(name)

    def anchor_scene(self) -> QPointF:
        if self.side == "left":
            return self.mapToScene(
                QPointF(self._width, self._height / 2.0)
            )
        if self.side == "right":
            return self.mapToScene(QPointF(0.0, self._height / 2.0))
        return self.mapToScene(
            QPointF(self._width / 2.0, self._height)
        )

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._width, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        width, height = self._width, self._height
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        if self.side == "right":
            path.moveTo(0.0, 0.0)
            path.lineTo(width - 10.0, 0.0)
            path.lineTo(width, height / 2.0)
            path.lineTo(width - 10.0, height)
            path.lineTo(0.0, height)
        else:
            path.moveTo(10.0, 0.0)
            path.lineTo(width, 0.0)
            path.lineTo(width, height)
            path.lineTo(10.0, height)
            path.lineTo(0.0, height / 2.0)
        path.closeSubpath()
        painter.setBrush(QColor("#f2e8fb"))
        painter.setPen(QPen(QColor("#7b3fa0"), 1.6))
        painter.drawPath(path)
        if self.isSelected():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#e58a1f"), 2.0, Qt.DashLine))
            painter.drawRect(self.boundingRect().adjusted(-3, -3, 3, 3))
        painter.setPen(QColor("#4a245f"))
        painter.setFont(QFont("Sans", 10))
        painter.drawText(self.boundingRect(), Qt.AlignCenter, self.pin_name)


class NetItem(QGraphicsPathItem):
    """Ломаная цепи из горизонтальных и вертикальных отрезков."""

    def __init__(
        self,
        canvas: "Canvas",
        block: str,
        name: str,
        segments: list[list[float]],
        junctions: list[list[float]],
    ) -> None:
        super().__init__()
        self.canvas = canvas
        self.block_name = block
        self.net_name = name
        self.segments = segments
        self.junctions = junctions
        self.setZValue(1)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setPen(QPen(QColor("#2f9e44"), 2.4))
        self.setToolTip(f"net {name}")
        self._update_path()

    def _update_path(self) -> None:
        path = QPainterPath()
        for x1, y1, x2, y2 in self.segments:
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
        self.setPath(path)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        return super().boundingRect().adjusted(-8, -8, 8, 8)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#2f9e44"), 2.4)
        if self.isSelected():
            pen = QPen(QColor("#e58a1f"), 3.4)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        painter.setBrush(QColor("#2f9e44"))
        painter.setPen(Qt.NoPen)
        for x, y in self.junctions:
            painter.drawEllipse(QPointF(x, y), 4.0, 4.0)
        if self.segments:
            x1, y1, x2, y2 = self.segments[0]
            mid = QPointF((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            painter.setPen(QColor("#2b7a36"))
            painter.setFont(QFont("Sans", 8))
            painter.drawText(
                QRectF(mid.x() - 40.0, mid.y() - 20.0, 80.0, 16.0),
                Qt.AlignCenter,
                self.net_name,
            )


# ----------------------------------------------------------------------
# Холст
# ----------------------------------------------------------------------
class Canvas(QGraphicsView):
    """Виджет схемы, владеющий черновиком и графическими объектами."""

    draftChanged = Signal()
    currentBlockChanged = Signal(str)
    selectionChanged = Signal(list)
    committed = Signal(bool)
    statusMessage = Signal(str)

    def __init__(
        self,
        model: Netlist,
        undo_stack: UndoStack | None,
        block_list: "BlockList" | None,
        palette: "ToolPalette" | None,
        validator: CheckFn,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._undo_stack = undo_stack
        self._block_list = block_list
        self._palette = palette
        self._validator = validator
        self._draft = EditorDraft.new_document()
        self._geometry: dict[str, dict] = {}
        self._current = "main"
        self._tool = Tool.SELECT
        self.grid_step = GRID
        self.snap_enabled = True
        self._item_ids: dict[QGraphicsItem, ItemId] = {}
        self._id_items: dict[ItemId, QGraphicsItem] = {}
        self._drag_start: dict[InstanceItem, QPointF] = {}
        self._pin_drag: dict | None = None
        self._wire_points: list[QPointF] = []
        self._wire_start: tuple[str, PinRef | None] | None = None
        self._wire_start_net: str | None = None
        self._preview_item: QGraphicsPathItem | None = None

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#fcfcfc"))
        self._scene.selectionChanged.connect(self._on_scene_selection)
        self._scene.setSceneRect(QRectF(0.0, 0.0, 1200.0, 800.0))
        self.setCursor(Qt.ArrowCursor)

        self._ensure_block_geometry(self._draft.block("main"))
        self._rebuild_scene(preserve_selection=False)

    # ---- базовые свойства -------------------------------------------
    @property
    def draft(self) -> EditorDraft:
        return self._draft

    @property
    def current_block(self) -> str:
        return self._current

    def set_undo_stack(self, undo_stack: UndoStack) -> None:
        self._undo_stack = undo_stack

    def _stack(self) -> UndoStack | None:
        return self._undo_stack

    def undo(self) -> None:
        if self._undo_stack is not None:
            self._undo_stack.undo()

    def redo(self) -> None:
        if self._undo_stack is not None:
            self._undo_stack.redo()

    # ---- сетка -------------------------------------------------------
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor("#fcfcfc"))
        step = max(4.0, float(getattr(self, "grid_step", GRID)))
        left = int(rect.left()) - int(rect.left()) % int(step)
        top = int(rect.top()) - int(rect.top()) % int(step)
        minor = QPen(QColor("#ececec"), 0.0)
        major = QPen(QColor("#dedede"), 0.0)
        x = left
        while x < rect.right():
            painter.setPen(major if int(x) % 100 == 0 else minor)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            painter.setPen(major if int(y) % 100 == 0 else minor)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step

    # ---- работа с документом ----------------------------------------
    def new_document(self) -> None:
        self._draft = EditorDraft.new_document()
        self._geometry = {}
        self._current = "main"
        self._cancel_wire()
        self._ensure_block_geometry(self._draft.block("main"))
        self._rebuild_scene(preserve_selection=False)
        self._refresh_lists()
        self.currentBlockChanged.emit(self._current)
        self.draftChanged.emit()

    def load_draft(
        self, raw: RawNetlist, schematic: dict | None
    ) -> None:
        self._draft = EditorDraft.from_raw(raw)
        self._geometry = {}
        if isinstance(schematic, dict):
            blocks = schematic.get("blocks")
            if not isinstance(blocks, dict):
                # Совместимость с ранней формой схематика без обёртки.
                blocks = schematic
            if isinstance(blocks, dict):
                self._geometry = {
                    str(name): dict(data)
                    for name, data in blocks.items()
                    if isinstance(data, dict)
                    and ("frame" in data or "instances" in data
                         or "pins" in data or "nets" in data)
                }
        if self._draft.find_block("main") is not None:
            self._current = "main"
        elif self._draft.blocks():
            self._current = self._draft.blocks()[0].name
        else:
            self._draft.add_block("main")
            self._current = "main"
        self._cancel_wire()
        block = self._draft.find_block(self._current)
        if block is not None:
            self._ensure_block_geometry(block)
        self._rebuild_scene(preserve_selection=False)
        self._refresh_lists()
        self.currentBlockChanged.emit(self._current)
        self.draftChanged.emit()

    def reset_to_model(self) -> None:
        self._draft = EditorDraft.from_model(self._model)
        if self._draft.find_block(self._current) is None:
            self._current = (
                "main" if self._draft.find_block("main") is not None
                else (self._draft.blocks()[0].name
                      if self._draft.blocks() else "main")
            )
        block = self._draft.find_block(self._current)
        if block is not None:
            self._ensure_block_geometry(block)
        self._cancel_wire()
        self._rebuild_scene(preserve_selection=False)
        self._refresh_lists()
        self.currentBlockChanged.emit(self._current)
        self.draftChanged.emit()

    def schematic(self) -> dict:
        """Собрать геометрию всех блоков в формате ``.sch.json``."""

        blocks: dict[str, dict] = {}
        for block in self._draft.blocks():
            self._ensure_block_geometry(block)
            geometry = self._geometry.get(block.name, {})
            instances_geometry = geometry.get("instances", {})
            nets_geometry = geometry.get("nets", {})
            pins_geometry = geometry.get("pins", {})
            blocks[block.name] = {
                "frame": list(geometry.get("frame", [0, 0, 1200, 800])),
                "pins": {
                    pin.name: dict(pins_geometry[pin.name])
                    for pin in block.pins()
                    if pin.name in pins_geometry
                },
                "instances": {
                    inst.name: {
                        "pos": list(
                            instances_geometry.get(
                                inst.name, {}
                            ).get("pos", [0, 0])
                        ),
                        "size": list(
                            instances_geometry.get(
                                inst.name, {}
                            ).get("size", DEFAULT_SIZE)
                        ),
                        "pins": {
                            pin.name: dict(
                                instances_geometry.get(
                                    inst.name, {}
                                ).get("pins", {}).get(pin.name, {})
                            )
                            for pin in inst.pins()
                        },
                    }
                    for inst in block.instances()
                },
                "nets": {
                    net.name: {
                        "segments": [
                            list(segment)
                            for segment in nets_geometry.get(
                                net.name, {}
                            ).get("segments", [])
                        ],
                        "junctions": [
                            list(point)
                            for point in nets_geometry.get(
                                net.name, {}
                            ).get("junctions", [])
                        ],
                    }
                    for net in block.nets()
                },
            }
        return {"version": 1, "blocks": blocks}

    # ---- проверка и принятие -----------------------------------------
    def validate(self) -> list[Diagnostic]:
        diags: list[Diagnostic] = []
        try:
            diags.extend(self._validator(self._draft))
        except Exception as exc:  # проверка не должна ломать редактор
            diags.append(Diagnostic(
                code="E000",
                severity=Severity.ERROR,
                check="editor",
                message=f"проверка завершилась ошибкой: {exc}",
                loc=SourceLoc(None, None),
            ))
        diags.extend(self._local_diagnostics())
        return diags

    def commit(self) -> list[Diagnostic]:
        diags = self.validate()
        if any(d.severity is Severity.ERROR for d in diags):
            self.committed.emit(False)
            return diags
        try:
            self._model.replace_with(self._draft)
        except ModelError as exc:
            diag = Diagnostic(
                code="E000",
                severity=Severity.ERROR,
                check="editor",
                message=f"модель не приняла изменения: {exc}",
                loc=SourceLoc(None, None),
            )
            self.committed.emit(False)
            return diags + [diag]
        self.committed.emit(True)
        return diags

    def _local_diagnostics(self) -> list[Diagnostic]:
        """Локальные проверки редактора: E404, E503, E601, E602."""

        result: list[Diagnostic] = []
        for block in self._draft.blocks():
            seen_instances: dict[str, DraftInstance] = {}
            for instance in block.instances():
                if instance.name in seen_instances:
                    result.append(_diag(
                        "E404",
                        f"повторное имя инстанса '{instance.name}' "
                        f"в блоке '{block.name}'",
                        block.name, instance.name,
                    ))
                else:
                    seen_instances[instance.name] = instance

            seen_pins: dict[str, DraftPin] = {}
            for pin in block.pins():
                if pin.name in seen_pins:
                    result.append(_diag(
                        "E503",
                        f"внешний пин '{pin.name}' блока '{block.name}' "
                        f"подключён больше одного раза",
                        block.name, pin.name,
                    ))
                else:
                    seen_pins[pin.name] = pin

            for instance in block.instances():
                seen: dict[str, DraftPin] = {}
                for pin in instance.pins():
                    if pin.name in seen:
                        result.append(_diag(
                            "E503",
                            f"пин '{instance.name}.{pin.name}' подключён "
                            f"больше одного раза",
                            block.name, f"{instance.name}.{pin.name}",
                        ))
                    else:
                        seen[pin.name] = pin

            nets: dict[str, DraftNet] = {}
            for net in block.nets():
                if net.name in nets:
                    result.append(_diag(
                        "E602",
                        f"повторное объявление цепи '{net.name}' "
                        f"в блоке '{block.name}'",
                        block.name, net.name,
                    ))
                else:
                    nets[net.name] = net

            for orphan in block.orphan_connections():
                result.append(_diag(
                    "E405",
                    f"подключение '{orphan.ref}' в блоке '{block.name}' "
                    f"ссылается на необъявленный инстанс "
                    f"'{orphan.ref.instance}'",
                    block.name, str(orphan.ref),
                ))

            reported: set[tuple[str, str]] = set()
            for pin in block.pins():
                if pin.net is not None and pin.net not in nets:
                    marker = (pin.name, pin.net)
                    if marker not in reported:
                        reported.add(marker)
                        result.append(_diag(
                            "E601",
                            f"внешний пин '{pin.name}' блока "
                            f"'{block.name}' ссылается на необъявленную "
                            f"цепь '{pin.net}'",
                            block.name, pin.name,
                        ))
            for instance in block.instances():
                for pin in instance.pins():
                    if pin.net is not None and pin.net not in nets:
                        ref = f"{instance.name}.{pin.name}"
                        marker = (ref, pin.net)
                        if marker not in reported:
                            reported.add(marker)
                            result.append(_diag(
                                "E601",
                                f"пин '{ref}' блока '{block.name}' "
                                f"ссылается на необъявленную цепь "
                                f"'{pin.net}'",
                                block.name, ref,
                            ))
        return result

    # ---- навигация ----------------------------------------------------
    def open_block(self, name: str) -> None:
        if self._draft.find_block(name) is None:
            return
        self._current = name
        self._cancel_wire()
        self._ensure_block_geometry(self._draft.block(name))
        self._rebuild_scene(preserve_selection=False)
        if self._block_list is not None:
            self._block_list.select(name)
        self.currentBlockChanged.emit(name)

    def set_tool(self, tool: Tool | str) -> None:
        if not isinstance(tool, Tool):
            try:
                tool = Tool(tool)
            except ValueError:
                return
        self._tool = tool
        if self._palette is not None:
            self._palette.set_tool(tool)
        if tool is Tool.WIRE:
            self.setCursor(Qt.CrossCursor)
            self.setDragMode(QGraphicsView.NoDrag)
        elif tool is Tool.PLACE:
            self.setCursor(Qt.OpenHandCursor)
            self.setDragMode(QGraphicsView.NoDrag)
        else:
            self.setCursor(Qt.ArrowCursor)
            self.setDragMode(QGraphicsView.RubberBandDrag)
        self.statusMessage.emit(f"инструмент: {_tool_name(tool)}")

    def selected(self) -> list[ItemId]:
        return [self._item_ids[item] for item in self._scene.selectedItems()
                if item in self._item_ids]

    def select(self, ids: list[ItemId]) -> None:
        self._scene.clearSelection()
        for item_id in ids:
            item = self._id_items.get(tuple(item_id))
            if item is not None:
                item.setSelected(True)

    # ---- геометрия -----------------------------------------------------
    @staticmethod
    def _default_frame() -> list[float]:
        return [0.0, 0.0, 1200.0, 800.0]

    def _geometry_for(self, block_name: str) -> dict:
        return self._geometry.setdefault(block_name, {
            "frame": self._default_frame(),
            "pins": {},
            "instances": {},
            "nets": {},
        })

    def _ensure_block_geometry(self, block: DraftBlock) -> None:
        geometry = self._geometry_for(block.name)
        geometry.setdefault("frame", self._default_frame())
        pins = geometry.setdefault("pins", {})
        instances = geometry.setdefault("instances", {})
        nets = geometry.setdefault("nets", {})
        frame = geometry["frame"]

        for index, pin in enumerate(block.pins()):
            if pin.name not in pins:
                pins[pin.name] = {
                    "side": "left",
                    "offset": 80.0 + index * 70.0,
                }

        width, height = DEFAULT_SIZE
        for index, instance in enumerate(block.instances()):
            entry = instances.setdefault(instance.name, {})
            if "pos" not in entry:
                column = index % 3
                row = index // 3
                entry["pos"] = [
                    260.0 + column * (width + 90.0),
                    100.0 + row * (height + 80.0),
                ]
            entry.setdefault("size", [width, height])
            pin_geometry = entry.setdefault("pins", {})
            self._ensure_instance_pin_geometry(instance, entry, pin_geometry)

        for net in block.nets():
            if net.name not in nets:
                self._rebuild_net_geometry(block, net.name)
        # Удалить устаревшие записи, чтобы схематик не разрастался.
        pins_to_remove = [name for name in pins
                          if all(pin.name != name for pin in block.pins())]
        for name in pins_to_remove:
            pins.pop(name, None)
        inst_to_remove = [
            name for name in instances
            if all(inst.name != name for inst in block.instances())
        ]
        for name in inst_to_remove:
            instances.pop(name, None)
        net_to_remove = [
            name for name in nets
            if all(net.name != name for net in block.nets())
        ]
        for name in net_to_remove:
            nets.pop(name, None)
        _ = frame

    def _ensure_instance_pin_geometry(
        self, instance: DraftInstance, entry: dict, pin_geometry: dict
    ) -> None:
        pins = instance.pins()
        size = entry.get("size", list(DEFAULT_SIZE))
        height = float(size[1])
        width = float(size[0])
        sides: list[str] = []
        if instance.kind == "sub":
            sides = ["left"] * len(pins)
        else:
            sides = ["left" if index % 2 == 0 else "right"
                     for index in range(len(pins))]
        counts = {side: sides.count(side) for side in ("left", "right")}
        used = {"left": 0, "right": 0}
        for pin, side in zip(pins, sides):
            if pin.name in pin_geometry:
                continue
            used[side] += 1
            offset = height * used[side] / (counts[side] + 1)
            pin_geometry[pin.name] = {"side": side, "offset": offset}
        _ = width

    def _pin_anchor(self, block: DraftBlock, ref: PinRef) -> QPointF | None:
        geometry = self._geometry_for(block.name)
        frame = geometry.get("frame", self._default_frame())
        if ref.instance is None:
            data = geometry.get("pins", {}).get(ref.pin)
            if data is None:
                return None
            side = data.get("side", "left")
            offset = float(data.get("offset", 40.0))
            if side == "left":
                return QPointF(frame[0] + 80.0, frame[1] + offset)
            if side == "right":
                return QPointF(frame[0] + frame[2] - 80.0,
                               frame[1] + offset)
            if side == "top":
                return QPointF(frame[0] + offset, frame[1] + 40.0)
            return QPointF(frame[0] + offset,
                           frame[1] + frame[3] - 40.0)
        entry = geometry.get("instances", {}).get(ref.instance)
        if entry is None:
            return None
        pin_data = entry.get("pins", {}).get(ref.pin)
        if pin_data is None:
            return None
        pos = entry.get("pos", [0.0, 0.0])
        size = entry.get("size", list(DEFAULT_SIZE))
        side = pin_data.get("side", "left")
        offset = float(pin_data.get("offset", 20.0))
        x, y = float(pos[0]), float(pos[1])
        width, height = float(size[0]), float(size[1])
        if side == "left":
            return QPointF(x, y + offset)
        if side == "right":
            return QPointF(x + width, y + offset)
        if side == "top":
            return QPointF(x + offset, y)
        return QPointF(x + offset, y + height)

    @staticmethod
    def _route_pair(first: QPointF, second: QPointF) -> list[list[float]]:
        if abs(first.x() - second.x()) < 0.01:
            return [[first.x(), first.y(), second.x(), second.y()]]
        if abs(first.y() - second.y()) < 0.01:
            return [[first.x(), first.y(), second.x(), second.y()]]
        return [
            [first.x(), first.y(), second.x(), first.y()],
            [second.x(), first.y(), second.x(), second.y()],
        ]

    @staticmethod
    def _point_on_segment(
        point: QPointF, segment: list[float]
    ) -> bool:
        x1, y1, x2, y2 = segment
        if abs(y1 - y2) < 0.01 and abs(point.y() - y1) < 0.01:
            return min(x1, x2) + 0.01 < point.x() < max(x1, x2) - 0.01
        if abs(x1 - x2) < 0.01 and abs(point.x() - x1) < 0.01:
            return min(y1, y2) + 0.01 < point.y() < max(y1, y2) - 0.01
        return False

    def _net_pin_refs(
        self, block: DraftBlock, name: str
    ) -> list[PinRef]:
        refs: list[PinRef] = []
        for pin in block.pins():
            if pin.net == name:
                refs.append(PinRef(None, pin.name))
        for instance in block.instances():
            for pin in instance.pins():
                if pin.net == name:
                    refs.append(PinRef(instance.name, pin.name))
        for orphan in block.orphan_connections():
            if orphan.net == name:
                refs.append(orphan.ref)
        return refs

    def _route_refs(
        self, block: DraftBlock, refs: list[PinRef]
    ) -> tuple[list[list[float]], list[list[float]]]:
        """Построить ортогональную ломаную по пинам цепи.

        Основная линия идёт горизонтально на уровне первого пина, а
        остальные пины подключаются вертикальными ветками. Точка
        соединения ставится там, где ветка врезается в линию не на её
        конце. Так цепь с тремя и более пинами выглядит как дерево.
        """

        anchors: list[QPointF] = []
        for ref in refs:
            point = self._pin_anchor(block, ref)
            if point is None:
                continue
            if all(abs(point.x() - other.x()) > 0.01
                   or abs(point.y() - other.y()) > 0.01
                   for other in anchors):
                anchors.append(point)
        if len(anchors) < 2:
            return [], []
        base = anchors[0]
        xs = [point.x() for point in anchors]
        x_min, x_max = min(xs), max(xs)
        if abs(x_min - x_max) < 0.01:
            ys = [point.y() for point in anchors]
            return [[base.x(), min(ys), base.x(), max(ys)]], []

        segments: list[list[float]] = [
            [x_min, base.y(), x_max, base.y()]
        ]
        junctions: list[list[float]] = []
        branches: set[float] = set()
        for point in anchors[1:]:
            if abs(point.y() - base.y()) < 0.01:
                continue
            segments.append([
                point.x(), base.y(), point.x(), point.y()
            ])
            branches.add(round(point.x(), 3))
        for x in sorted(branches):
            if x_min + 0.01 < x < x_max - 0.01:
                junctions.append([x, round(base.y(), 3)])
        return segments, junctions

    def _rebuild_net_geometry(
        self, block: DraftBlock, name: str
    ) -> None:
        refs = self._net_pin_refs(block, name)
        segments, junctions = self._route_refs(block, refs)
        geometry = self._geometry_for(block.name)
        geometry.setdefault("nets", {})[name] = {
            "segments": segments,
            "junctions": junctions,
        }

    @staticmethod
    def _orthogonalize(points: list[QPointF]) -> tuple[
        list[list[float]], list[list[float]]
    ]:
        segments: list[list[float]] = []
        junctions: list[list[float]] = []
        for first, second in zip(points, points[1:]):
            if abs(first.x() - second.x()) < 0.01:
                segments.append([first.x(), first.y(), second.x(), second.y()])
            elif abs(first.y() - second.y()) < 0.01:
                segments.append([first.x(), first.y(), second.x(), second.y()])
            else:
                corner = QPointF(second.x(), first.y())
                segments.append(
                    [first.x(), first.y(), corner.x(), corner.y()]
                )
                segments.append(
                    [corner.x(), corner.y(), second.x(), second.y()]
                )
                junctions.append([corner.x(), corner.y()])
        return segments, junctions

    def _snap(self, point: QPointF) -> QPointF:
        if not getattr(self, "snap_enabled", True):
            return point
        step = max(1.0, float(getattr(self, "grid_step", GRID)))
        return QPointF(
            round(point.x() / step) * step,
            round(point.y() / step) * step,
        )

    def set_grid_step(self, step: float) -> None:
        self.grid_step = max(4.0, float(step))
        self.viewport().update()

    def set_snap_enabled(self, enabled: bool) -> None:
        self.snap_enabled = bool(enabled)

    # ---- построение сцены ---------------------------------------------
    def _rebuild_scene(self, preserve_selection: bool = True) -> None:
        selected = self.selected() if preserve_selection else []
        self._scene.blockSignals(True)
        self._preview_item = None
        self._scene.clear()
        self._item_ids.clear()
        self._id_items.clear()

        block = self._draft.find_block(self._current)
        if block is None:
            self._scene.blockSignals(False)
            return
        self._ensure_block_geometry(block)
        geometry = self._geometry_for(block.name)
        frame = geometry.get("frame", self._default_frame())
        self._scene.setSceneRect(
            QRectF(frame[0], frame[1], frame[2], frame[3])
        )

        title = QGraphicsSimpleTextItem(block.name)
        title.setFont(QFont("Sans", 12, QFont.Bold))
        title.setBrush(QColor("#1f2933"))
        title.setPos(frame[0] + 20.0, frame[1] + 16.0)
        title.setZValue(5)
        self._scene.addItem(title)

        for net in block.nets():
            net_data = geometry.get("nets", {}).get(net.name, {})
            item = NetItem(
                self, block.name, net.name,
                [list(s) for s in net_data.get("segments", [])],
                [list(j) for j in net_data.get("junctions", [])],
            )
            self._scene.addItem(item)
            self._register(item, (block.name, "net", net.name))

        for instance in block.instances():
            entry = geometry.get("instances", {}).get(instance.name, {})
            item = InstanceItem(
                self,
                block.name,
                instance.name,
                instance.kind,
                instance.type_name,
                QPointF(*entry.get("pos", [0.0, 0.0])),
                tuple(entry.get("size", DEFAULT_SIZE)),
            )
            item.set_pin_geometry(
                entry.get("pins", {})
            )
            self._scene.addItem(item)
            self._register(
                item, (block.name, "inst", instance.name)
            )
            for pin in instance.pins():
                pin_item = InstancePinItem(
                    self, block.name, instance.name, pin.name, item
                )
                self._register(
                    pin_item,
                    (block.name, "ipin", instance.name, pin.name),
                )

        for pin in block.pins():
            data = geometry.get("pins", {}).get(pin.name, {})
            anchor = self._pin_anchor(block, PinRef(None, pin.name))
            if anchor is None:
                continue
            item = ExternalPinItem(
                self, block.name, pin.name,
                str(data.get("side", "left")), anchor,
            )
            self._scene.addItem(item)
            self._register(item, (block.name, "pin", pin.name))

        self._scene.blockSignals(False)
        for item_id in selected:
            item = self._id_items.get(tuple(item_id))
            if item is not None:
                item.setSelected(True)
        self._on_scene_selection()

    def _register(self, item: QGraphicsItem, item_id: ItemId) -> None:
        self._item_ids[item] = item_id
        self._id_items[item_id] = item

    def _refresh(self) -> None:
        self._rebuild_scene(preserve_selection=True)
        self._refresh_lists()
        self.draftChanged.emit()

    def _refresh_lists(self) -> None:
        if self._block_list is None:
            return
        self._block_list.set_blocks(
            [block.name for block in self._draft.blocks()]
        )
        self._block_list.set_primitives(
            [primitive.name for primitive in self._draft.primitives()]
        )
        self._block_list.select(self._current)

    # ---- элементарные операции ----------------------------------------
    def op_add_block(self, name: str, index: int) -> None:
        self._draft.add_block(name, index)
        self._ensure_block_geometry(self._draft.block(name))
        self._refresh()

    def op_remove_block(self, name: str) -> dict:
        index = next(
            (i for i, block in enumerate(self._draft.blocks())
             if block.name == name),
            0,
        )
        block = self._draft.remove_block(name)
        if block is None:
            return {}
        snapshot = block_to_dict(block)
        snapshot["geometry"] = self._geometry.pop(name, {})
        snapshot["index"] = index
        if self._current == name:
            remaining = self._draft.blocks()
            self._current = (
                "main" if self._draft.find_block("main") is not None
                else (remaining[0].name if remaining else "main")
            )
            if not remaining:
                self._draft.add_block("main")
                self._current = "main"
            self._ensure_block_geometry(self._draft.block(self._current))
            self.currentBlockChanged.emit(self._current)
        self._refresh()
        return snapshot

    def op_restore_block(self, snapshot: dict, index: int) -> None:
        block = block_from_dict(self._draft, snapshot)
        self._draft._blocks.insert(index, block)
        geometry = snapshot.get("geometry")
        if isinstance(geometry, dict):
            self._geometry[block.name] = dict(geometry)
        self._ensure_block_geometry(block)
        self._refresh()

    def op_rename_block(self, old: str, new: str) -> None:
        if old == new or self._draft.find_block(new) is not None:
            return
        self._draft.rename_block(old, new)
        if old in self._geometry:
            self._geometry[new] = self._geometry.pop(old)
        if self._current == old:
            self._current = new
            self.currentBlockChanged.emit(new)
        self._refresh()

    def op_move_block(self, name: str, index: int) -> None:
        self._draft.move_block(name, index)
        self._refresh()

    def op_copy_block(self, src: str, new_name: str, index: int) -> None:
        self._draft.copy_block(src, new_name, index)
        if src in self._geometry:
            self._geometry[new_name] = json.loads(
                json.dumps(self._geometry[src])
            )
        self._ensure_block_geometry(self._draft.block(new_name))
        self._refresh()

    def op_add_pin(
        self, block: str, name: str, side: str, offset: float
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        target.add_pin(name, None)
        geometry = self._geometry_for(block)
        geometry.setdefault("pins", {})[name] = {
            "side": side, "offset": float(offset)
        }
        self._refresh()

    def op_remove_pin(self, block: str, name: str) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        geometry = self._geometry_for(block).get("pins", {})
        snapshot = {
            "block": block,
            "name": name,
            "side": geometry.get(name, {}).get("side", "left"),
            "offset": geometry.get(name, {}).get("offset", 40.0),
            "net": next(
                (pin.net for pin in target.pins() if pin.name == name),
                None,
            ),
        }
        target.remove_pin(name)
        geometry.pop(name, None)
        affected = {snapshot["net"]} if snapshot["net"] else set()
        self._recompute_nets(target, affected)
        self._refresh()
        return snapshot

    def op_restore_pin(self, snapshot: dict) -> None:
        target = self._draft.find_block(snapshot["block"])
        if target is None:
            return
        target.add_pin(snapshot["name"], snapshot.get("net"))
        self._geometry_for(snapshot["block"]).setdefault(
            "pins", {}
        )[snapshot["name"]] = {
            "side": snapshot.get("side", "left"),
            "offset": float(snapshot.get("offset", 40.0)),
        }
        affected = {snapshot["net"]} if snapshot.get("net") else set()
        self._recompute_nets(target, affected)
        self._refresh()

    def op_rename_pin(self, block: str, old: str, new: str) -> None:
        target = self._draft.find_block(block)
        if target is None or old == new:
            return
        target.rename_pin(old, new)
        pins = self._geometry_for(block).setdefault("pins", {})
        if old in pins:
            pins[new] = pins.pop(old)
        self._refresh()

    def op_move_pin(
        self, block: str, name: str, side: str, offset: float
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        self._geometry_for(block).setdefault("pins", {})[name] = {
            "side": side, "offset": float(offset)
        }
        affected = {
            pin.net for pin in target.pins()
            if pin.name == name and pin.net is not None
        }
        self._recompute_nets(target, affected)
        self._refresh()

    def op_move_instance_pin(
        self, block: str, inst: str, pin: str,
        side: str, offset: float,
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        try:
            target.instance(inst)
        except KeyError:
            return
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(inst, {}).setdefault("pins", {})
        entry[pin] = {"side": side, "offset": float(offset)}
        affected = {
            item.net for item in target.instance(inst).pins()
            if item.name == pin and item.net is not None
        }
        self._recompute_nets(target, affected)
        self._refresh()

    def op_add_instance(
        self,
        block: str,
        name: str,
        kind: str,
        type_name: str,
        pos: tuple[float, float],
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        instance = target.add_instance(name, kind, type_name)
        for pin_name in self._draft.definition_pins(kind, type_name):
            instance.add_pin(pin_name, None)
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(name, {})
        entry["pos"] = [float(pos[0]), float(pos[1])]
        entry.setdefault("size", list(DEFAULT_SIZE))
        pin_geometry = entry.setdefault("pins", {})
        self._ensure_instance_pin_geometry(instance, entry, pin_geometry)
        self._refresh()

    def op_remove_instance(self, block: str, name: str) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        instance = next(
            (item for item in target.instances() if item.name == name), None
        )
        if instance is None:
            return {}
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).get(name, {})
        snapshot = instance_snapshot(
            target, instance, entry.get("pos")
        )
        snapshot["size"] = list(entry.get("size", DEFAULT_SIZE))
        snapshot["pin_geometry"] = dict(entry.get("pins", {}))
        affected = self._nets_of_instance(instance)
        target.remove_instance(name)
        self._geometry_for(block).setdefault(
            "instances", {}
        ).pop(name, None)
        self._recompute_nets(target, affected)
        self._refresh()
        return snapshot

    def op_restore_instance(self, block: str, snapshot: dict) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        instance = target.add_instance(
            snapshot["name"], snapshot["kind"], snapshot["type_name"]
        )
        pins = snapshot.get("pins")
        if not pins:
            pins = [
                {"name": name, "net": net}
                for name, net in snapshot.get("connections", {}).items()
            ]
        for pin_data in pins:
            instance.add_pin(pin_data["name"], pin_data.get("net"))
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(snapshot["name"], {})
        entry["pos"] = list(snapshot.get("pos", [0.0, 0.0]))
        entry["size"] = list(snapshot.get("size", DEFAULT_SIZE))
        entry["pins"] = dict(snapshot.get("pin_geometry", {}))
        self._ensure_instance_pin_geometry(
            instance, entry, entry["pins"]
        )
        self._recompute_nets(target, self._nets_of_instance(instance))
        self._refresh()

    def op_move_instance(
        self, block: str, name: str, pos: tuple[float, float]
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(name, {})
        entry["pos"] = [float(pos[0]), float(pos[1])]
        affected: set[str] = set()
        for item in target.instances():
            if item.name == name:
                affected = self._nets_of_instance(item)
                break
        self._recompute_nets(target, affected)
        self._refresh()

    def op_set_instance_props(
        self,
        block: str,
        name: str,
        *,
        new_name: str | None = None,
        kind: str | None = None,
        type_name: str | None = None,
    ) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        instance = next(
            (item for item in target.instances() if item.name == name), None
        )
        if instance is None:
            return {}
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).get(name, {})
        old = instance_snapshot(target, instance, entry.get("pos"))
        old["size"] = list(entry.get("size", DEFAULT_SIZE))
        old["pin_geometry"] = dict(entry.get("pins", {}))
        affected = self._nets_of_instance(instance)
        before_pins = [
            (pin.name, pin.net) for pin in instance.pins()
        ]

        if new_name and new_name != name:
            target.rename_instance(name, new_name)
            instances = self._geometry_for(block).setdefault(
                "instances", {}
            )
            if name in instances:
                instances[new_name] = instances.pop(name)
        instance = next(
            (item for item in target.instances()
             if item.name == (new_name or name)), None
        )
        if instance is None:
            return old
        if kind is not None or type_name is not None:
            instance.set_type(
                kind if kind is not None else instance.kind,
                type_name if type_name is not None else instance.type_name,
            )
            self._reconcile_instance_pins(instance)
        after_pins = [
            (pin.name, pin.net) for pin in instance.pins()
        ]
        if after_pins != before_pins:
            affected |= self._nets_of_instance(instance)
            self._recompute_nets(target, affected)
        self._refresh()
        return old

    def op_restore_instance_snapshot(self, snapshot: dict) -> None:
        block = self._draft.find_block(snapshot.get("block", ""))
        if block is None:
            return
        name = snapshot["name"]
        instance = next(
            (item for item in block.instances() if item.name == name), None
        )
        if instance is None:
            self.op_restore_instance(block.name, snapshot)
            return
        affected = self._nets_of_instance(instance)
        instance.kind = snapshot["kind"]
        instance.type_name = snapshot["type_name"]
        instance._pins = [
            DraftPin(pin["name"], pin.get("net"))
            for pin in snapshot.get("pins", [])
        ]
        entry = self._geometry_for(block.name).setdefault(
            "instances", {}
        ).setdefault(name, {})
        entry["pins"] = dict(snapshot.get("pin_geometry", {}))
        self._ensure_instance_pin_geometry(
            instance, entry, entry["pins"]
        )
        affected |= self._nets_of_instance(instance)
        self._recompute_nets(block, affected)
        self._refresh()

    def _reconcile_instance_pins(self, instance: DraftInstance) -> None:
        desired = self._draft.definition_pins(
            instance.kind, instance.type_name
        )
        if not desired:
            return
        existing = {pin.name: pin for pin in instance.pins()}
        instance._pins = [
            existing.get(name, DraftPin(name, None)) for name in desired
        ]

    def op_add_instance_pin(
        self, block: str, inst: str, pin: str, side: str, offset: float
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        try:
            instance = target.instance(inst)
        except KeyError:
            return
        instance.add_pin(pin, None)
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(inst, {})
        entry.setdefault("pins", {})[pin] = {
            "side": side, "offset": float(offset)
        }
        self._refresh()

    def op_remove_instance_pin(
        self, block: str, inst: str, pin: str
    ) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        try:
            instance = target.instance(inst)
        except KeyError:
            return {}
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(inst, {})
        data = entry.get("pins", {}).get(pin, {})
        snapshot = {
            "block": block,
            "inst": inst,
            "pin": pin,
            "side": data.get("side", "left"),
            "offset": float(data.get("offset", 20.0)),
            "net": next(
                (p.net for p in instance.pins() if p.name == pin), None
            ),
        }
        instance.remove_pin(pin)
        entry.get("pins", {}).pop(pin, None)
        affected = {snapshot["net"]} if snapshot["net"] else set()
        self._recompute_nets(target, affected)
        self._refresh()
        return snapshot

    def op_restore_instance_pin(self, snapshot: dict) -> None:
        target = self._draft.find_block(snapshot["block"])
        if target is None:
            return
        try:
            instance = target.instance(snapshot["inst"])
        except KeyError:
            return
        instance.add_pin(snapshot["pin"], snapshot.get("net"))
        entry = self._geometry_for(snapshot["block"]).setdefault(
            "instances", {}
        ).setdefault(snapshot["inst"], {})
        entry.setdefault("pins", {})[snapshot["pin"]] = {
            "side": snapshot.get("side", "left"),
            "offset": float(snapshot.get("offset", 20.0)),
        }
        affected = {snapshot["net"]} if snapshot.get("net") else set()
        self._recompute_nets(target, affected)
        self._refresh()

    def op_rename_instance_pin(
        self, block: str, inst: str, old: str, new: str
    ) -> None:
        target = self._draft.find_block(block)
        if target is None or old == new:
            return
        try:
            instance = target.instance(inst)
        except KeyError:
            return
        instance.rename_pin(old, new)
        pins = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(inst, {}).setdefault("pins", {})
        if old in pins:
            pins[new] = pins.pop(old)
        self._refresh()

    def op_add_net(
        self,
        block: str,
        name: str,
        geometry: dict,
        pins: list[PinRef],
    ) -> None:
        target = self._draft.find_block(block)
        if target is None:
            return
        if all(net.name != name for net in target.nets()):
            target.add_net(name)
        self._geometry_for(block).setdefault("nets", {})[name] = {
            "segments": [list(s) for s in geometry.get("segments", [])],
            "junctions": [list(j) for j in geometry.get("junctions", [])],
        }
        for ref in pins:
            self._connect_ref(target, ref, name)
        self._refresh()

    def _connect_ref(
        self, block: DraftBlock, ref: PinRef, net: str | None
    ) -> None:
        if ref.instance is None:
            block.connect_pin(ref.pin, net)
            return
        try:
            instance = block.instance(ref.instance)
        except KeyError:
            return
        instance.connect_pin(ref.pin, net)

    def op_remove_net(self, block: str, name: str) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        geometry = self._geometry_for(block).setdefault(
            "nets", {}
        ).get(name, {})
        snapshot = {
            "block": block,
            "name": name,
            "geometry": {
                "segments": [list(s) for s in geometry.get("segments", [])],
                "junctions": [list(j) for j in geometry.get("junctions", [])],
            },
            "connections": [
                {"instance": ref.instance, "pin": ref.pin}
                for ref in self._net_pin_refs(target, name)
            ],
        }
        target.remove_net(name)
        for pin in target.pins():
            if pin.net == name:
                pin.net = None
        for instance in target.instances():
            for pin in instance.pins():
                if pin.net == name:
                    pin.net = None
        self._geometry_for(block).setdefault(
            "nets", {}
        ).pop(name, None)
        self._refresh()
        return snapshot

    def op_restore_net(self, snapshot: dict) -> None:
        target = self._draft.find_block(snapshot["block"])
        if target is None:
            return
        self.op_add_net(
            snapshot["block"],
            snapshot["name"],
            snapshot.get("geometry", {}),
            [PinRef(item.get("instance"), item["pin"])
             for item in snapshot.get("connections", [])],
        )

    def op_rename_net(self, block: str, old: str, new: str) -> None:
        target = self._draft.find_block(block)
        if target is None or old == new:
            return
        target.rename_net(old, new)
        nets = self._geometry_for(block).setdefault("nets", {})
        if old in nets:
            nets[new] = nets.pop(old)
        self._refresh()

    def op_set_net_geometry(
        self,
        block: str,
        name: str,
        geometry: dict,
        pins: list[PinRef],
    ) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        old = self.net_snapshot(block, name, pins)
        self._geometry_for(block).setdefault("nets", {})[name] = {
            "segments": [list(s) for s in geometry.get("segments", [])],
            "junctions": [list(j) for j in geometry.get("junctions", [])],
        }
        for ref in pins:
            self._connect_ref(target, ref, name)
        self._refresh()
        return old

    def net_snapshot(
        self, block: str, name: str, pins: list[PinRef] | None = None
    ) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        geometry = self._geometry_for(block).setdefault(
            "nets", {}
        ).get(name, {})
        refs = pins if pins is not None else self._net_pin_refs(target, name)
        return {
            "block": block,
            "name": name,
            "geometry": {
                "segments": [list(s) for s in geometry.get("segments", [])],
                "junctions": [list(j) for j in geometry.get("junctions", [])],
            },
            "connections": [
                {
                    "instance": ref.instance,
                    "pin": ref.pin,
                    "net": (
                        next((p.net for p in target.pins()
                              if ref.instance is None and p.name == ref.pin),
                              None)
                        if ref.instance is None
                        else self._instance_net(target, ref)
                    ),
                }
                for ref in refs
            ],
        }

    @staticmethod
    def _instance_net(block: DraftBlock, ref: PinRef) -> str | None:
        try:
            instance = block.instance(ref.instance or "")
        except KeyError:
            return None
        return next(
            (pin.net for pin in instance.pins() if pin.name == ref.pin), None
        )

    def instance_snapshot(self, block: str, name: str) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        instance = next(
            (item for item in target.instances() if item.name == name), None
        )
        if instance is None:
            return {}
        entry = self._geometry_for(block).setdefault(
            "instances", {}
        ).get(name, {})
        snapshot = instance_snapshot(
            target, instance, entry.get("pos")
        )
        snapshot["size"] = list(entry.get("size", DEFAULT_SIZE))
        snapshot["pin_geometry"] = dict(entry.get("pins", {}))
        return snapshot

    def pin_snapshot(self, block: str, name: str) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        data = self._geometry_for(block).setdefault(
            "pins", {}
        ).get(name, {})
        return {
            "block": block,
            "name": name,
            "side": data.get("side", "left"),
            "offset": float(data.get("offset", 40.0)),
            "net": next(
                (pin.net for pin in target.pins() if pin.name == name), None
            ),
        }

    def instance_pin_snapshot(
        self, block: str, inst: str, pin: str
    ) -> dict:
        target = self._draft.find_block(block)
        if target is None:
            return {}
        try:
            instance = target.instance(inst)
        except KeyError:
            return {}
        data = self._geometry_for(block).setdefault(
            "instances", {}
        ).setdefault(inst, {}).setdefault("pins", {}).get(pin, {})
        return {
            "block": block,
            "inst": inst,
            "pin": pin,
            "side": data.get("side", "left"),
            "offset": float(data.get("offset", 20.0)),
            "net": next(
                (p.net for p in instance.pins() if p.name == pin), None
            ),
        }

    @staticmethod
    def _nets_of_instance(instance: DraftInstance) -> set[str]:
        """Имена цепей, к которым подключён инстанс."""

        return {
            pin.net for pin in instance.pins() if pin.net is not None
        }

    def _recompute_nets(
        self, block: DraftBlock, names: set[str] | None = None
    ) -> None:
        """Перестроить только указанные цепи, сохранив остальную графику.

        Если ``names`` не задан, перестраиваются все цепи блока. Операции
        передают только затронутые цепи, поэтому ручная трассировка
        остальных цепей из ``.sch.json`` не теряется.
        """

        targets = (
            {net.name for net in block.nets()}
            if names is None else set(names)
        )
        for name in targets:
            if any(net.name == name for net in block.nets()):
                self._rebuild_net_geometry(block, name)

    # ---- выделение и мышь ---------------------------------------------
    def _on_scene_selection(self) -> None:
        ids = self.selected()
        self.selectionChanged.emit(ids)

    def _id_at(self, view_pos) -> ItemId | None:
        item = self.itemAt(view_pos)
        while item is not None:
            if item in self._item_ids:
                return self._item_ids[item]
            item = item.parentItem()
        return None

    def _pin_ref_for(self, item: QGraphicsItem) -> PinRef | None:
        if isinstance(item, InstancePinItem):
            return PinRef(item.instance_name, item.pin_name)
        if isinstance(item, ExternalPinItem):
            return PinRef(None, item.pin_name)
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._tool is Tool.WIRE:
            item = self.itemAt(event.pos())
            self._wire_click(self.mapToScene(event.pos()), item)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._tool is Tool.SELECT:
            item = self.itemAt(event.pos())
            pin = self._pin_ref_for(item) if item is not None else None
            if pin is not None:
                self._scene.clearSelection()
                item.setSelected(True)
                if isinstance(item, ExternalPinItem):
                    self._pin_drag = {
                        "kind": "pin",
                        "item": item,
                        "block": item.block_name,
                        "name": item.pin_name,
                        "old": self.pin_snapshot(
                            item.block_name, item.pin_name
                        ),
                        "new": None,
                    }
                elif isinstance(item, InstancePinItem):
                    self._pin_drag = {
                        "kind": "ipin",
                        "item": item,
                        "block": item.block_name,
                        "inst": item.instance_name,
                        "name": item.pin_name,
                        "old": self.instance_pin_snapshot(
                            item.block_name, item.instance_name,
                            item.pin_name,
                        ),
                        "new": None,
                    }
                event.accept()
                return
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self._drag_start = {
                item: item.pos()
                for item in self._scene.selectedItems()
                if isinstance(item, InstanceItem)
            }

    def mouseReleaseEvent(self, event) -> None:
        if self._pin_drag is not None and event.button() == Qt.LeftButton:
            self._finish_pin_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if event.button() != Qt.LeftButton or not self._drag_start:
            return
        moves: list[tuple[str, str, tuple[float, float],
                          tuple[float, float]]] = []
        for item, old in self._drag_start.items():
            if item.scene() is not self._scene:
                continue
            new = self._snap(item.pos())
            if abs(new.x() - old.x()) < 0.01 and \
                    abs(new.y() - old.y()) < 0.01:
                continue
            moves.append((
                item.block_name,
                item.instance_name,
                (old.x(), old.y()),
                (new.x(), new.y()),
            ))
        self._drag_start = {}
        if not moves or self._undo_stack is None:
            return
        if len(moves) > 1:
            self._undo_stack.begin_macro("Переместить объекты")
        for block, name, old, new in moves:
            self._undo_stack.push(
                MoveInstanceCommand(block, name, old, new)
            )
        if len(moves) > 1:
            self._undo_stack.end_macro()

    def mouseMoveEvent(self, event) -> None:
        if self._pin_drag is not None:
            self._update_pin_drag(self.mapToScene(event.pos()))
            event.accept()
            return
        if self._wire_points:
            position = self._snap(self.mapToScene(event.pos()))
            self._update_preview(position)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        item_id = self._id_at(event.pos())
        if item_id is None:
            super().mouseDoubleClickEvent(event)
            return
        item = self._id_items.get(item_id)
        if isinstance(item, InstanceItem):
            if event.modifiers() & Qt.ControlModifier and item.kind == "sub":
                self.open_block(item.type_name)
            else:
                self._edit_instance(item_id)
            event.accept()
            return
        if item_id[1] in ("pin", "net", "ipin"):
            self._rename_id(item_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self._cancel_wire()
            event.accept()
            return
        if event.key() == Qt.Key_Delete:
            self._delete_selected()
            event.accept()
            return
        if event.key() == Qt.Key_F2:
            selected = self.selected()
            if selected:
                self._rename_id(selected[0])
            event.accept()
            return
        super().keyPressEvent(event)

    def _update_pin_drag(self, scene_pos: QPointF) -> None:
        drag = self._pin_drag
        if drag is None:
            return
        item = drag["item"]
        if drag["kind"] == "pin":
            frame = self._geometry_for(drag["block"]).get(
                "frame", self._default_frame()
            )
            side = drag["old"].get("side", "left")
            if side not in ("left", "right"):
                side = "left"
            offset = min(max(scene_pos.y() - frame[1], 12.0),
                         frame[3] - 12.0)
            width, height = EXTERNAL_PIN_SIZE
            if side == "left":
                anchor = QPointF(frame[0] + 80.0, frame[1] + offset)
                item.setPos(anchor - QPointF(width, height / 2.0))
            else:
                anchor = QPointF(frame[0] + frame[2] - 80.0,
                                 frame[1] + offset)
                item.setPos(anchor - QPointF(0.0, height / 2.0))
            drag["new"] = {"side": side, "offset": offset}
            return

        parent = item.parentItem()
        if not isinstance(parent, InstanceItem):
            return
        local = parent.mapFromScene(scene_pos)
        width, height = parent.size()
        x = min(max(local.x(), 0.0), width)
        y = min(max(local.y(), 0.0), height)
        distances = {
            "left": abs(x),
            "right": abs(width - x),
            "top": abs(y),
            "bottom": abs(height - y),
        }
        side = min(distances, key=distances.get)
        offset = y if side in ("left", "right") else x
        if side == "left":
            item.setPos(QPointF(0.0, offset))
        elif side == "right":
            item.setPos(QPointF(width, offset))
        elif side == "top":
            item.setPos(QPointF(offset, 0.0))
        else:
            item.setPos(QPointF(offset, height))
        drag["new"] = {"side": side, "offset": offset}

    def _finish_pin_drag(self) -> None:
        drag = self._pin_drag
        self._pin_drag = None
        if drag is None or self._undo_stack is None:
            return
        new = drag.get("new")
        old = drag["old"]
        if not new:
            self._rebuild_scene(preserve_selection=True)
            return
        if abs(float(new["offset"]) - float(
            old.get("offset", 0.0)
        )) < 0.01 and new["side"] == old.get("side"):
            self._rebuild_scene(preserve_selection=True)
            return
        if drag["kind"] == "pin":
            self._undo_stack.push(MovePinCommand(
                drag["block"], drag["name"],
                old.get("side", "left"), float(old.get("offset", 0.0)),
                new["side"], float(new["offset"]),
            ))
        else:
            self._undo_stack.push(MoveInstancePinCommand(
                drag["block"], drag["inst"], drag["name"],
                old.get("side", "left"), float(old.get("offset", 0.0)),
                new["side"], float(new["offset"]),
            ))

    def contextMenuEvent(self, event) -> None:
        self._cancel_wire()
        item_id = self._id_at(event.pos())
        if item_id is None:
            menu = QMenu(self)
            add_pin = menu.addAction("Добавить внешний пин")
            action = menu.exec_(event.globalPos())
            if action is add_pin and self._undo_stack is not None:
                point = self._snap(self.mapToScene(event.pos()))
                frame = self._geometry_for(self._current).get(
                    "frame", self._default_frame()
                )
                side = ("left" if point.x() < frame[0] + frame[2] / 2
                        else "right")
                offset = point.y() - frame[1]
                self._undo_stack.push(AddPinCommand(
                    self._current, self._unique_pin_name(self._current),
                    side, offset,
                ))
            event.accept()
            return
        if not self._id_items[item_id].isSelected():
            self.select([item_id])
        menu = QMenu(self)
        rename = menu.addAction("Переименовать")
        properties = menu.addAction("Свойства…")
        delete = menu.addAction("Удалить")
        menu.addSeparator()
        copy = menu.addAction("Копировать")
        action = menu.exec_(event.globalPos())
        if action is rename:
            self._rename_id(item_id)
        elif action is properties:
            if item_id[1] == "inst":
                self._edit_instance(item_id)
        elif action is delete:
            self._delete_selected()
        elif action is copy:
            self.copy_selected()
        event.accept()

    # ---- операции интерфейса ------------------------------------------
    def _unique_instance_name(self, block_name: str) -> str:
        block = self._draft.find_block(block_name)
        existing = {inst.name for inst in block.instances()} if block else set()
        index = 1
        while f"t{index}" in existing:
            index += 1
        return f"t{index}"

    def _unique_pin_name(self, block_name: str) -> str:
        block = self._draft.find_block(block_name)
        existing = {pin.name for pin in block.pins()} if block else set()
        index = 1
        while f"a{index}" in existing:
            index += 1
        return f"a{index}"

    def place_element(
        self, kind: str, type_name: str, pos: QPointF
    ) -> None:
        if self._undo_stack is None:
            return
        point = self._snap(pos)
        if kind in ("sub", "prim"):
            name = self._unique_instance_name(self._current)
            self._undo_stack.push(AddInstanceCommand(
                self._current, name, kind, type_name,
                (point.x(), point.y()),
            ))
            return
        if kind == "pin":
            name = self._unique_pin_name(self._current)
            block = self._draft.find_block(self._current)
            if block is None:
                return
            frame = self._geometry_for(self._current).get(
                "frame", self._default_frame()
            )
            side = "left" if point.x() < frame[0] + frame[2] / 2 else "right"
            offset = (point.y() - frame[1] if side == "left"
                      else point.y() - frame[1])
            self._undo_stack.push(AddPinCommand(
                self._current, name, side, offset
            ))
            return
        if kind == "ipin":
            item = self.itemAt(self.mapFromScene(point))
            while item is not None and not isinstance(item, InstanceItem):
                item = item.parentItem()
            if not isinstance(item, InstanceItem):
                return
            existing = {
                pin.pin_name for pin in self._id_items.values()
                if isinstance(pin, InstancePinItem)
                and pin.instance_name == item.instance_name
            }
            index = 1
            while f"p{index}" in existing:
                index += 1
            pin_name = f"p{index}"
            local = item.mapFromScene(point)
            x = min(max(local.x(), 0.0), item.size()[0])
            y = min(max(local.y(), 0.0), item.size()[1])
            distances = {
                "left": abs(x),
                "right": abs(item.size()[0] - x),
                "top": abs(y),
                "bottom": abs(item.size()[1] - y),
            }
            side = min(distances, key=distances.get)
            offset = y if side in ("left", "right") else x
            self._undo_stack.push(AddInstancePinCommand(
                self._current, item.instance_name, pin_name, side, offset
            ))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(ELEMENT_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(ELEMENT_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(ELEMENT_MIME):
            super().dropEvent(event)
            return
        try:
            data = json.loads(
                bytes(event.mimeData().data(ELEMENT_MIME)).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        self.place_element(
            str(data.get("kind", "prim")),
            str(data.get("type", "")),
            self.mapToScene(event.pos()),
        )
        event.acceptProposedAction()

    # ---- рисование цепи ------------------------------------------------
    def _wire_click(self, point: QPointF, item: QGraphicsItem | None) -> None:
        pin = self._pin_ref_for(item) if item is not None else None
        net_item = item if isinstance(item, NetItem) else None
        if self._wire_start is None:
            if pin is not None:
                anchor = self._pin_anchor(
                    self._draft.block(self._current), pin
                )
                if anchor is None:
                    return
                self._wire_start = (self._current, pin)
                self._wire_start_net = self._net_of_ref(pin)
                self._wire_points = [anchor]
                self.statusMessage.emit("цепь: выберите второй пин или щёлкните для излома")
            elif net_item is not None:
                self._wire_start = (self._current, None)
                self._wire_start_net = net_item.net_name
                self._wire_points = [self._snap(point)]
            else:
                self._wire_points = [self._snap(point)]
            self._update_preview(self._snap(point))
            return

        block_name, start_ref = self._wire_start
        if pin is not None:
            anchor = self._pin_anchor(self._draft.block(block_name), pin)
            if anchor is None:
                return
            target_net = self._net_of_ref(pin)
            net_name = (
                self._wire_start_net
                if self._wire_start_net is not None
                else target_net
            )
            if net_name is None and start_ref is not None:
                net_name = self._net_of_ref(start_ref)
            points = self._wire_points + [anchor]
            self._finish_wire(block_name, net_name, points, [start_ref, pin])
            return

        if net_item is not None:
            point = self._nearest_point(net_item, point)
            net_name = (
                self._wire_start_net or net_item.net_name
            )
            points = self._wire_points + [point]
            self._finish_wire(
                block_name, net_name, points,
                [start_ref] if start_ref is not None else [],
            )
            return

        self._wire_points.append(self._snap(point))
        self._update_preview(self._snap(point))

    def _net_of_ref(self, ref: PinRef | None) -> str | None:
        if ref is None:
            return None
        block = self._draft.find_block(self._current)
        if block is None:
            return None
        if ref.instance is None:
            for pin in block.pins():
                if pin.name == ref.pin:
                    return pin.net
            return None
        try:
            instance = block.instance(ref.instance)
        except KeyError:
            return None
        for pin in instance.pins():
            if pin.name == ref.pin:
                return pin.net
        return None

    @staticmethod
    def _nearest_point(item: NetItem, point: QPointF) -> QPointF:
        best = point
        best_distance = float("inf")
        for x1, y1, x2, y2 in item.segments:
            if abs(y1 - y2) < 0.01:
                x = min(max(point.x(), min(x1, x2)), max(x1, x2))
                candidate = QPointF(x, y1)
            else:
                y = min(max(point.y(), min(y1, y2)), max(y1, y2))
                candidate = QPointF(x1, y)
            distance = (
                (candidate.x() - point.x()) ** 2
                + (candidate.y() - point.y()) ** 2
            )
            if distance < best_distance:
                best_distance = distance
                best = candidate
        return best

    def _finish_wire(
        self,
        block: str,
        net_name: str | None,
        points: list[QPointF],
        refs: list[PinRef | None],
    ) -> None:
        if self._undo_stack is None:
            self._cancel_wire()
            return
        target = self._draft.find_block(block)
        if target is None:
            self._cancel_wire()
            return
        if net_name is None:
            existing = {net.name for net in target.nets()}
            index = 1
            while f"n{index}" in existing:
                index += 1
            net_name = f"n{index}"
        pin_refs = [ref for ref in refs if ref is not None]
        exists = any(net.name == net_name for net in target.nets())
        if exists:
            all_refs = self._net_pin_refs(target, net_name)
            for ref in pin_refs:
                if ref not in all_refs:
                    all_refs.append(ref)
            segments, junctions = self._route_refs(target, all_refs)
            geometry = {"segments": segments, "junctions": junctions}
            self._undo_stack.push(SetNetGeometryCommand(
                block, net_name, geometry, pin_refs,
                self.net_snapshot(block, net_name, pin_refs),
            ))
        else:
            segments, junctions = self._orthogonalize(points)
            geometry = {"segments": segments, "junctions": junctions}
            self._undo_stack.push(AddNetCommand(
                block, net_name, geometry, pin_refs
            ))
        self._cancel_wire()
        self.statusMessage.emit(f"цепь '{net_name}' создана")

    def _update_preview(self, cursor: QPointF) -> None:
        if not self._wire_points:
            return
        points = self._wire_points + [cursor]
        segments, _junctions = self._orthogonalize(points)
        path = QPainterPath()
        for x1, y1, x2, y2 in segments:
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
        if self._preview_item is None:
            self._preview_item = QGraphicsPathItem()
            self._preview_item.setPen(
                QPen(QColor("#2f9e44"), 1.6, Qt.DashLine)
            )
            self._preview_item.setZValue(20)
            self._scene.addItem(self._preview_item)
        self._preview_item.setPath(path)

    def _cancel_wire(self) -> None:
        self._wire_points = []
        self._wire_start = None
        self._wire_start_net = None
        if self._preview_item is not None:
            if self._preview_item.scene() is not None:
                self._scene.removeItem(self._preview_item)
            self._preview_item = None

    # ---- команды интерфейса -------------------------------------------
    def set_model(self, model: Netlist) -> None:
        self._model = model

    def delete_selected(self) -> None:
        self._delete_selected()

    def rename_selected(self) -> None:
        selected = self.selected()
        if selected:
            self._rename_id(selected[0])

    def edit_selected(self) -> None:
        selected = self.selected()
        for item_id in selected:
            if item_id[1] == "inst":
                self._edit_instance(item_id)
                return
        if selected:
            self._rename_id(selected[0])

    def block_snapshot(self, name: str) -> dict:
        block = self._draft.find_block(name)
        if block is None:
            return {}
        index = next(
            (i for i, item in enumerate(self._draft.blocks())
             if item.name == name), 0)
        snapshot = block_to_dict(block)
        snapshot["geometry"] = json.loads(
            json.dumps(self._geometry.get(name, {}))
        )
        snapshot["index"] = index
        return snapshot

    def _delete_selected(self) -> None:
        if self._undo_stack is None:
            return
        selected = self.selected()
        if not selected:
            return
        commands: list[CanvasCommand] = []
        seen: set[tuple[str, ...]] = set()
        for item_id in selected:
            if item_id[1] == "inst":
                key = ("inst", item_id[0], item_id[2])
                if key in seen:
                    continue
                seen.add(key)
                commands.append(RemoveInstanceCommand(
                    self.instance_snapshot(item_id[0], item_id[2])
                ))
            elif item_id[1] == "pin":
                commands.append(RemovePinCommand(
                    self.pin_snapshot(item_id[0], item_id[2])
                ))
            elif item_id[1] == "ipin":
                commands.append(RemoveInstancePinCommand(
                    self.instance_pin_snapshot(
                        item_id[0], item_id[2], item_id[3]
                    )
                ))
            elif item_id[1] == "net":
                commands.append(RemoveNetCommand(
                    self.net_snapshot(item_id[0], item_id[2])
                ))
        if not commands:
            return
        if len(commands) > 1:
            self._undo_stack.begin_macro("Удалить")
        for command in commands:
            self._undo_stack.push(command)
        if len(commands) > 1:
            self._undo_stack.end_macro()
        self.statusMessage.emit("удалено объектов: %d" % len(commands))

    def _rename_id(self, item_id: ItemId) -> None:
        if self._undo_stack is None:
            return
        kind = item_id[1]
        if kind == "inst":
            current = item_id[2]
            title = "Новое имя инстанса"
        elif kind == "pin":
            current = item_id[2]
            title = "Новое имя внешнего пина"
        elif kind == "ipin":
            current = item_id[3]
            title = "Новое имя пина инстанса"
        elif kind == "net":
            current = item_id[2]
            title = "Новое имя цепи"
        else:
            return
        value, ok = QInputDialog.getText(self, title, "Имя:", text=current)
        value = value.strip()
        if not ok or not value or value == current:
            return
        if kind == "inst":
            self._undo_stack.push(RenameInstanceCommand(
                item_id[0], current, value
            ))
        elif kind == "pin":
            self._undo_stack.push(RenamePinCommand(
                item_id[0], current, value
            ))
        elif kind == "ipin":
            self._undo_stack.push(RenameInstancePinCommand(
                item_id[0], item_id[2], current, value
            ))
        elif kind == "net":
            self._undo_stack.push(RenameNetCommand(
                item_id[0], current, value
            ))

    def _edit_instance(self, item_id: ItemId) -> None:
        if self._undo_stack is None:
            return
        block = item_id[0]
        name = item_id[2]
        target = self._draft.find_block(block)
        if target is None:
            return
        instance = next(
            (item for item in target.instances() if item.name == name), None
        )
        if instance is None:
            return
        dialog = InstancePropertiesDialog(
            name, instance.kind, instance.type_name, self
        )
        if dialog.exec_() != dialog.Accepted:
            return
        new_name, kind, type_name = dialog.values()
        if (new_name, kind, type_name) == (
            name, instance.kind, instance.type_name
        ):
            return
        self._undo_stack.push(EditInstanceCommand(
            block, name,
            new_name=new_name,
            kind=kind,
            type_name=type_name,
            old=self.instance_snapshot(block, name),
        ))

    def copy_selected(self) -> None:
        if self._undo_stack is None:
            return
        selected = self.selected()
        if not selected:
            return
        block = self._draft.find_block(self._current)
        if block is None:
            return
        instances = [item for item in selected if item[1] == "inst"]
        if not instances:
            return
        copied = 0
        self._undo_stack.begin_macro("Копировать")
        try:
            for item_id in instances:
                snapshot = self.instance_snapshot(item_id[0], item_id[2])
                if not snapshot:
                    continue
                # Имя вычисляем внутри цикла: предыдущая копия уже
                # добавлена в черновик и заняла своё имя.
                name = self._unique_instance_name(item_id[0])
                pos = snapshot.get("pos", [0.0, 0.0])
                snapshot["block"] = item_id[0]
                snapshot["name"] = name
                snapshot["pos"] = [
                    float(pos[0]) + 40.0, float(pos[1]) + 40.0
                ]
                self._undo_stack.push(AddInstanceCommand(
                    item_id[0], name, snapshot["kind"],
                    snapshot["type_name"], tuple(snapshot["pos"]), snapshot,
                ))
                copied += 1
        finally:
            self._undo_stack.end_macro()
        if copied:
            self.statusMessage.emit(
                "скопировано объектов: %d" % copied
            )


    def op_rename_instance(
        self, block: str, old: str, new: str
    ) -> None:
        target = self._draft.find_block(block)
        if target is None or old == new:
            return
        target.rename_instance(old, new)
        instances = self._geometry_for(block).setdefault("instances", {})
        if old in instances:
            instances[new] = instances.pop(old)
        self._refresh()

    def op_restore_net_state(self, snapshot: dict) -> None:
        """Восстановить геометрию цепи и прежние подключения пинов."""

        target = self._draft.find_block(snapshot.get("block", ""))
        if target is None:
            return
        self._geometry_for(snapshot["block"]).setdefault(
            "nets", {}
        )[snapshot["name"]] = {
            "segments": [
                list(s) for s in snapshot.get("geometry", {}).get(
                    "segments", []
                )
            ],
            "junctions": [
                list(j) for j in snapshot.get("geometry", {}).get(
                    "junctions", []
                )
            ],
        }
        for connection in snapshot.get("connections", []):
            ref = PinRef(connection.get("instance"), connection["pin"])
            if ref.instance is None:
                target.connect_pin(ref.pin, connection.get("net"))
            else:
                try:
                    instance = target.instance(ref.instance)
                except KeyError:
                    continue
                instance.connect_pin(ref.pin, connection.get("net"))
        self._refresh()


class InstancePropertiesDialog(QDialog):
    """Диалог свойств инстанса: имя, вид и тип."""

    def __init__(
        self,
        name: str,
        kind: str,
        type_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        from PyQt5.QtWidgets import (
            QComboBox,
            QDialogButtonBox,
            QFormLayout,
            QLineEdit,
            QVBoxLayout,
        )

        self.setWindowTitle("Свойства инстанса")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["sub", "prim"])
        self.kind_combo.setCurrentText(kind)
        self.type_edit = QLineEdit(type_name)
        form.addRow("Имя:", self.name_edit)
        form.addRow("Вид:", self.kind_combo)
        form.addRow("Тип:", self.type_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return (
            self.name_edit.text().strip(),
            self.kind_combo.currentText(),
            self.type_edit.text().strip(),
        )


# ----------------------------------------------------------------------
# Конкретные команды истории
# ----------------------------------------------------------------------
def _pos(value) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    return (0.0, 0.0)


@register_command
class AddInstanceCommand(CanvasCommand):
    type_name = "add_instance"

    def __init__(
        self,
        block: str,
        name: str,
        kind: str,
        type_name: str,
        pos: tuple[float, float],
        snapshot: dict | None = None,
    ) -> None:
        self.block = block
        self.name = name
        self.kind = kind
        self.instance_type = type_name
        self.pos = [float(pos[0]), float(pos[1])]
        self.snapshot = snapshot
        self.text = f"Добавить инстанс {name}"

    def redo(self, canvas: Canvas) -> None:
        if self.snapshot:
            canvas.op_restore_instance(self.block, self.snapshot)
        else:
            canvas.op_add_instance(
                self.block, self.name, self.kind, self.instance_type,
                _pos(self.pos),
            )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_remove_instance(self.block, self.name)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name,
            "block": self.block,
            "name": self.name,
            "kind": self.kind,
            "type_name": self.instance_type,
            "pos": list(self.pos),
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddInstanceCommand":
        return cls(
            str(data["block"]),
            str(data["name"]),
            str(data["kind"]),
            str(data["type_name"]),
            _pos(data.get("pos")),
            data.get("snapshot"),
        )


@register_command
class RemoveInstanceCommand(CanvasCommand):
    type_name = "remove_instance"

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.text = f"Удалить инстанс {snapshot.get('name', '')}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_remove_instance(
            self.snapshot["block"], self.snapshot["name"]
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_restore_instance(
            self.snapshot["block"], self.snapshot
        )

    def to_dict(self) -> dict:
        return {"type": self.type_name, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoveInstanceCommand":
        return cls(dict(data["snapshot"]))


@register_command
class MoveInstanceCommand(CanvasCommand):
    type_name = "move_instance"

    def __init__(
        self,
        block: str,
        name: str,
        old_pos: tuple[float, float],
        new_pos: tuple[float, float],
    ) -> None:
        self.block = block
        self.name = name
        self.old_pos = [float(old_pos[0]), float(old_pos[1])]
        self.new_pos = [float(new_pos[0]), float(new_pos[1])]
        self.text = f"Переместить инстанс {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_move_instance(self.block, self.name, _pos(self.new_pos))

    def undo(self, canvas: Canvas) -> None:
        canvas.op_move_instance(self.block, self.name, _pos(self.old_pos))

    def merge_with(self, other: CanvasCommand) -> bool:
        if not isinstance(other, MoveInstanceCommand):
            return False
        if other.block != self.block or other.name != self.name:
            return False
        self.new_pos = list(other.new_pos)
        return True

    def to_dict(self) -> dict:
        return {
            "type": self.type_name,
            "block": self.block,
            "name": self.name,
            "old_pos": list(self.old_pos),
            "new_pos": list(self.new_pos),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MoveInstanceCommand":
        return cls(
            str(data["block"]), str(data["name"]),
            _pos(data.get("old_pos")), _pos(data.get("new_pos")),
        )


@register_command
class EditInstanceCommand(CanvasCommand):
    type_name = "edit_instance"

    def __init__(
        self,
        block: str,
        name: str,
        *,
        new_name: str | None = None,
        kind: str | None = None,
        type_name: str | None = None,
        old: dict | None = None,
    ) -> None:
        self.block = block
        self.name = name
        self.new_name = new_name
        self.kind = kind
        self.new_type = type_name
        self.old = old or {}
        self.text = f"Изменить инстанс {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_set_instance_props(
            self.block, self.name,
            new_name=self.new_name,
            kind=self.kind,
            type_name=self.new_type,
        )

    def undo(self, canvas: Canvas) -> None:
        if self.new_name and self.new_name != self.name:
            canvas.op_rename_instance(
                self.block, self.new_name, self.name
            )
        if self.old:
            canvas.op_restore_instance_snapshot(self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name,
            "block": self.block,
            "name": self.name,
            "new_name": self.new_name,
            "kind": self.kind,
            "type_name": self.new_type,
            "old": self.old,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EditInstanceCommand":
        return cls(
            str(data["block"]), str(data["name"]),
            new_name=data.get("new_name"),
            kind=data.get("kind"),
            type_name=data.get("type_name"),
            old=data.get("old"),
        )


@register_command
class RenameInstanceCommand(CanvasCommand):
    type_name = "rename_instance"

    def __init__(self, block: str, old: str, new: str) -> None:
        self.block = block
        self.old = old
        self.new = new
        self.text = f"Переименовать инстанс {old}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_rename_instance(self.block, self.old, self.new)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_rename_instance(self.block, self.new, self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "old": self.old, "new": self.new,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenameInstanceCommand":
        return cls(str(data["block"]), str(data["old"]), str(data["new"]))


@register_command
class AddBlockCommand(CanvasCommand):
    type_name = "add_block"

    def __init__(
        self, name: str, index: int, snapshot: dict | None = None
    ) -> None:
        self.name = name
        self.index = int(index)
        self.snapshot = snapshot
        self.text = f"Добавить блок {name}"

    def redo(self, canvas: Canvas) -> None:
        if self.snapshot:
            canvas.op_restore_block(self.snapshot, self.index)
        else:
            canvas.op_add_block(self.name, self.index)

    def undo(self, canvas: Canvas) -> None:
        self.snapshot = canvas.op_remove_block(self.name) or self.snapshot

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "name": self.name,
            "index": self.index, "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddBlockCommand":
        return cls(
            str(data["name"]), int(data.get("index", 0)),
            data.get("snapshot"),
        )


@register_command
class RemoveBlockCommand(CanvasCommand):
    type_name = "remove_block"

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.text = f"Удалить блок {snapshot.get('name', '')}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_remove_block(self.snapshot["name"])

    def undo(self, canvas: Canvas) -> None:
        canvas.op_restore_block(
            self.snapshot, int(self.snapshot.get("index", 0))
        )

    def to_dict(self) -> dict:
        return {"type": self.type_name, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoveBlockCommand":
        return cls(dict(data["snapshot"]))


@register_command
class RenameBlockCommand(CanvasCommand):
    type_name = "rename_block"

    def __init__(self, old: str, new: str) -> None:
        self.old = old
        self.new = new
        self.text = f"Переименовать блок {old}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_rename_block(self.old, self.new)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_rename_block(self.new, self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "old": self.old, "new": self.new
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenameBlockCommand":
        return cls(str(data["old"]), str(data["new"]))


@register_command
class MoveBlockCommand(CanvasCommand):
    type_name = "move_block"

    def __init__(self, name: str, old_index: int, new_index: int) -> None:
        self.name = name
        self.old_index = int(old_index)
        self.new_index = int(new_index)
        self.text = f"Переместить блок {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_move_block(self.name, self.new_index)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_move_block(self.name, self.old_index)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "name": self.name,
            "old_index": self.old_index, "new_index": self.new_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MoveBlockCommand":
        return cls(
            str(data["name"]), int(data["old_index"]),
            int(data["new_index"]),
        )


@register_command
class CopyBlockCommand(CanvasCommand):
    type_name = "copy_block"

    def __init__(self, src: str, new_name: str, index: int) -> None:
        self.src = src
        self.new_name = new_name
        self.index = int(index)
        self.text = f"Копировать блок {src}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_copy_block(self.src, self.new_name, self.index)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_remove_block(self.new_name)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "src": self.src,
            "new_name": self.new_name, "index": self.index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CopyBlockCommand":
        return cls(
            str(data["src"]), str(data["new_name"]),
            int(data.get("index", 0)),
        )


@register_command
class AddPinCommand(CanvasCommand):
    type_name = "add_pin"

    def __init__(
        self, block: str, name: str, side: str, offset: float
    ) -> None:
        self.block = block
        self.name = name
        self.side = side
        self.offset = float(offset)
        self.text = f"Добавить внешний пин {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_add_pin(
            self.block, self.name, self.side, self.offset
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_remove_pin(self.block, self.name)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "name": self.name, "side": self.side,
            "offset": self.offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddPinCommand":
        return cls(
            str(data["block"]), str(data["name"]),
            str(data.get("side", "left")),
            float(data.get("offset", 40.0)),
        )


@register_command
class RemovePinCommand(CanvasCommand):
    type_name = "remove_pin"

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.text = f"Удалить внешний пин {snapshot.get('name', '')}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_remove_pin(self.snapshot["block"],
                             self.snapshot["name"])

    def undo(self, canvas: Canvas) -> None:
        canvas.op_restore_pin(self.snapshot)

    def to_dict(self) -> dict:
        return {"type": self.type_name, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict) -> "RemovePinCommand":
        return cls(dict(data["snapshot"]))


@register_command
class RenamePinCommand(CanvasCommand):
    type_name = "rename_pin"

    def __init__(self, block: str, old: str, new: str) -> None:
        self.block = block
        self.old = old
        self.new = new
        self.text = f"Переименовать внешний пин {old}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_rename_pin(self.block, self.old, self.new)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_rename_pin(self.block, self.new, self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "old": self.old, "new": self.new,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenamePinCommand":
        return cls(str(data["block"]), str(data["old"]), str(data["new"]))


@register_command
class MovePinCommand(CanvasCommand):
    type_name = "move_pin"

    def __init__(
        self, block: str, name: str,
        old_side: str, old_offset: float,
        new_side: str, new_offset: float,
    ) -> None:
        self.block = block
        self.name = name
        self.old_side = old_side
        self.old_offset = float(old_offset)
        self.new_side = new_side
        self.new_offset = float(new_offset)
        self.text = f"Переместить пин {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_move_pin(
            self.block, self.name, self.new_side, self.new_offset
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_move_pin(
            self.block, self.name, self.old_side, self.old_offset
        )

    def merge_with(self, other: CanvasCommand) -> bool:
        if not isinstance(other, MovePinCommand):
            return False
        if other.block != self.block or other.name != self.name:
            return False
        self.new_side = other.new_side
        self.new_offset = other.new_offset
        return True

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "name": self.name, "old_side": self.old_side,
            "old_offset": self.old_offset, "new_side": self.new_side,
            "new_offset": self.new_offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MovePinCommand":
        return cls(
            str(data["block"]), str(data["name"]),
            str(data.get("old_side", "left")),
            float(data.get("old_offset", 40.0)),
            str(data.get("new_side", "left")),
            float(data.get("new_offset", 40.0)),
        )


@register_command
class MoveInstancePinCommand(CanvasCommand):
    type_name = "move_instance_pin"

    def __init__(
        self, block: str, inst: str, pin: str,
        old_side: str, old_offset: float,
        new_side: str, new_offset: float,
    ) -> None:
        self.block = block
        self.inst = inst
        self.pin = pin
        self.old_side = old_side
        self.old_offset = float(old_offset)
        self.new_side = new_side
        self.new_offset = float(new_offset)
        self.text = f"Переместить пин {inst}.{pin}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_move_instance_pin(
            self.block, self.inst, self.pin,
            self.new_side, self.new_offset,
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_move_instance_pin(
            self.block, self.inst, self.pin,
            self.old_side, self.old_offset,
        )

    def merge_with(self, other: CanvasCommand) -> bool:
        if not isinstance(other, MoveInstancePinCommand):
            return False
        if (other.block, other.inst, other.pin) != (
            self.block, self.inst, self.pin
        ):
            return False
        self.new_side = other.new_side
        self.new_offset = other.new_offset
        return True

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "inst": self.inst, "pin": self.pin,
            "old_side": self.old_side, "old_offset": self.old_offset,
            "new_side": self.new_side, "new_offset": self.new_offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MoveInstancePinCommand":
        return cls(
            str(data["block"]), str(data["inst"]), str(data["pin"]),
            str(data.get("old_side", "left")),
            float(data.get("old_offset", 20.0)),
            str(data.get("new_side", "left")),
            float(data.get("new_offset", 20.0)),
        )


@register_command
class AddInstancePinCommand(CanvasCommand):
    type_name = "add_instance_pin"

    def __init__(
        self, block: str, inst: str, pin: str, side: str, offset: float
    ) -> None:
        self.block = block
        self.inst = inst
        self.pin = pin
        self.side = side
        self.offset = float(offset)
        self.text = f"Добавить пин {inst}.{pin}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_add_instance_pin(
            self.block, self.inst, self.pin, self.side, self.offset
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_remove_instance_pin(self.block, self.inst, self.pin)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "inst": self.inst, "pin": self.pin,
            "side": self.side, "offset": self.offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddInstancePinCommand":
        return cls(
            str(data["block"]), str(data["inst"]), str(data["pin"]),
            str(data.get("side", "left")),
            float(data.get("offset", 20.0)),
        )


@register_command
class RemoveInstancePinCommand(CanvasCommand):
    type_name = "remove_instance_pin"

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.text = "Удалить пин %s.%s" % (
            snapshot.get("inst", ""), snapshot.get("pin", "")
        )

    def redo(self, canvas: Canvas) -> None:
        canvas.op_remove_instance_pin(
            self.snapshot["block"],
            self.snapshot["inst"],
            self.snapshot["pin"],
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_restore_instance_pin(self.snapshot)

    def to_dict(self) -> dict:
        return {"type": self.type_name, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoveInstancePinCommand":
        return cls(dict(data["snapshot"]))


@register_command
class RenameInstancePinCommand(CanvasCommand):
    type_name = "rename_instance_pin"

    def __init__(
        self, block: str, inst: str, old: str, new: str
    ) -> None:
        self.block = block
        self.inst = inst
        self.old = old
        self.new = new
        self.text = f"Переименовать пин {inst}.{old}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_rename_instance_pin(
            self.block, self.inst, self.old, self.new
        )

    def undo(self, canvas: Canvas) -> None:
        canvas.op_rename_instance_pin(
            self.block, self.inst, self.new, self.old
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "inst": self.inst, "old": self.old, "new": self.new,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenameInstancePinCommand":
        return cls(
            str(data["block"]), str(data["inst"]),
            str(data["old"]), str(data["new"]),
        )


@register_command
class AddNetCommand(CanvasCommand):
    type_name = "add_net"

    def __init__(
        self,
        block: str,
        name: str,
        geometry: dict,
        pins: list[PinRef],
    ) -> None:
        self.block = block
        self.name = name
        self.geometry = geometry
        self.pins = [
            {"instance": ref.instance, "pin": ref.pin} for ref in pins
        ]
        self.snapshot: dict | None = None
        self.text = f"Добавить цепь {name}"

    def redo(self, canvas: Canvas) -> None:
        if self.snapshot:
            canvas.op_restore_net(self.snapshot)
        else:
            canvas.op_add_net(
                self.block, self.name, self.geometry,
                [PinRef(item.get("instance"), item["pin"])
                 for item in self.pins],
            )

    def undo(self, canvas: Canvas) -> None:
        self.snapshot = canvas.op_remove_net(
            self.block, self.name
        ) or self.snapshot

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "name": self.name, "geometry": self.geometry,
            "pins": self.pins, "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddNetCommand":
        command = cls(
            str(data["block"]), str(data["name"]),
            dict(data.get("geometry", {})),
            [PinRef(item.get("instance"), item["pin"])
             for item in data.get("pins", [])],
        )
        command.snapshot = data.get("snapshot")
        return command


@register_command
class RemoveNetCommand(CanvasCommand):
    type_name = "remove_net"

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.text = f"Удалить цепь {snapshot.get('name', '')}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_remove_net(self.snapshot["block"],
                             self.snapshot["name"])

    def undo(self, canvas: Canvas) -> None:
        canvas.op_restore_net(self.snapshot)

    def to_dict(self) -> dict:
        return {"type": self.type_name, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoveNetCommand":
        return cls(dict(data["snapshot"]))


@register_command
class RenameNetCommand(CanvasCommand):
    type_name = "rename_net"

    def __init__(self, block: str, old: str, new: str) -> None:
        self.block = block
        self.old = old
        self.new = new
        self.text = f"Переименовать цепь {old}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_rename_net(self.block, self.old, self.new)

    def undo(self, canvas: Canvas) -> None:
        canvas.op_rename_net(self.block, self.new, self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "old": self.old, "new": self.new,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenameNetCommand":
        return cls(str(data["block"]), str(data["old"]), str(data["new"]))


@register_command
class SetNetGeometryCommand(CanvasCommand):
    type_name = "edit_net"

    def __init__(
        self,
        block: str,
        name: str,
        geometry: dict,
        pins: list[PinRef],
        old: dict | None = None,
    ) -> None:
        self.block = block
        self.name = name
        self.geometry = geometry
        self.pins = [
            {"instance": ref.instance, "pin": ref.pin} for ref in pins
        ]
        self.old = old or {}
        self.text = f"Изменить цепь {name}"

    def redo(self, canvas: Canvas) -> None:
        canvas.op_set_net_geometry(
            self.block, self.name, self.geometry,
            [PinRef(item.get("instance"), item["pin"])
             for item in self.pins],
        )

    def undo(self, canvas: Canvas) -> None:
        if self.old:
            canvas.op_restore_net_state(self.old)

    def to_dict(self) -> dict:
        return {
            "type": self.type_name, "block": self.block,
            "name": self.name, "geometry": self.geometry,
            "pins": self.pins, "old": self.old,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SetNetGeometryCommand":
        return cls(
            str(data["block"]), str(data["name"]),
            dict(data.get("geometry", {})),
            [PinRef(item.get("instance"), item["pin"])
             for item in data.get("pins", [])],
            data.get("old"),
        )
