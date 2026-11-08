"""Сущности принятого нетлиста: пины, цепи, примитивы и блоки.

Автор: Д. Криворучко. Контракт: docs/interfaces.md, раздел 2.1.

Классы этого модуля описывают содержимое одного принятого определения.
Операции над пинами проверяют имя цепи в содержащем блоке; сами типы
инстансов при этом не разрешаются. Этим разделением можно временно
сохранить неполную иерархию для диагностик Checker.

Имена хранятся в упорядоченных словарях: они одновременно обеспечивают
уникальность, быстрый поиск и порядок сериализации. Публичные методы
возвращают новые списки представлений, чтобы внешний код не менял состав
коллекции в обход проверок; возвращённые объекты сущностей остаются живыми.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeVar, cast

from netlist.core.diagnostics import InstanceKind, SourceLoc
from netlist.core.errors import ModelError
from netlist.core.views import (
    InstanceView,
    NetView,
    OrphanConnection,
    PinView,
)


# Неизменяемое положение можно безопасно разделять объектами без источника.
_UNKNOWN = SourceLoc(None, None)


@dataclass
class Pin:
    """Логический пин; подключение меняется через владельца пина."""

    name: str
    net: str | None = None
    loc: SourceLoc = _UNKNOWN


@dataclass
class Net:
    """Объявление цепи; связи хранятся в полях Pin.net."""

    name: str
    loc: SourceLoc = _UNKNOWN


@dataclass
class Primitive:
    """Тип примитивного элемента без внутренних цепей и экземпляров."""

    name: str
    pin_names: list[str] = field(default_factory=list)
    loc: SourceLoc = _UNKNOWN


# Общие проверки ключей применяются перед записью, чтобы не менять модель
# при неуспешной операции. Имена сущностей имеют отдельные пространства.
class _Named(Protocol):
    """Минимальный контракт объекта, ключ которого совпадает с name."""

    name: str


T = TypeVar("T", bound=_Named)


def _unique(items: dict[str, T], name: str) -> None:
    """Отклонить повтор до изменения словаря, сохранив прежнее состояние."""
    if name in items:
        raise ModelError(f"повторное имя '{name}'")


def _rename(items: dict[str, T], old: str, new: str) -> None:
    """Заменить ключ без перемещения объекта в конец порядка документа."""
    obj = items[old]  # Отсутствующий объект — KeyError, как при поиске.
    if old == new:
        return
    _unique(items, new)
    ordered = [(new if key == old else key, value)
               for key, value in items.items()]
    obj.name = new
    items.clear()
    items.update(ordered)


class _Pins:
    """Общие операции внешних пинов блока и пинов инстанса.

    Оба вида подключаются к цепям содержащего блока. Ссылка на блок
    нужна именно для этой проверки, а не для обхода иерархии типов.
    """

    def __init__(self, block: Block) -> None:
        self._block = block
        self._pins: dict[str, Pin] = {}

    def pins(self) -> list[PinView]:
        """Вернуть отдельный список живых пинов в порядке создания."""
        # list invariant: конкретные Pin реализуют PinView структурно.
        # Список новый, поэтому вызывающий код не меняет внутренний реестр.
        return cast(list[PinView], list(self._pins.values()))

    def _check_net(self, net: str | None) -> None:
        """None допустим; имя должно относиться к цепи этого блока."""
        if net is not None and net not in self._block._nets:
            raise ModelError(
                f"цепь '{net}' не объявлена в блоке '{self._block.name}'")

    def add_pin(self, name: str, net: str | None = None) -> Pin:
        """Добавить пин в конец списка пинов владельца.

        Аргументы:
            name: собственное имя пина, уникальное у этого владельца.
            net:  имя цепи содержащего блока; None — не подключён.

        Возвращает созданный Pin. При повторном имени или неизвестной
        цепи бросает ModelError. Проверки выполняются ДО добавления,
        чтобы не оставить частично созданный пин после отказа.
        """
        _unique(self._pins, name)
        self._check_net(net)
        pin = Pin(name, net)
        self._pins[name] = pin
        return pin

    def remove_pin(self, name: str) -> None:
        """Удалить пин по собственному имени name.

        Цепь и остальные пины сохраняются: удаление одного подключения
        не означает удаления всей цепи. Если пина нет, бросает KeyError.
        Возвращаемого значения нет.
        """
        del self._pins[name]

    def rename_pin(self, old: str, new: str) -> None:
        """Изменить имя old на new, сохранив подключение и позицию.

        Возвращаемого значения нет. Отсутствующее old даёт KeyError,
        занятое new — ModelError. Переименование в то же имя допустимо.
        Состав пинов экземпляров этого типа проверяет SubcircuitCheck:
        здесь меняется только пин непосредственного владельца.
        """
        _rename(self._pins, old, new)

    def connect_pin(self, name: str, net: str | None) -> None:
        """Изменить подключение существующего пина.

        Аргументы:
            name: имя пина у текущего владельца.
            net:  объявленная цепь содержащего блока либо None.

        Не возвращает значения. Если пина нет — KeyError; если цепь
        неизвестна — ModelError, причём старое подключение сохраняется.
        Повторный вызов заменяет подключение, а не добавляет вторую цепь.
        """
        pin = self._pins[name]
        self._check_net(net)
        pin.net = net


class Instance(_Pins):
    """Экземпляр типа; создаётся через Block.add_instance."""

    def __init__(self, name: str, kind: str | InstanceKind, type_name: str,
                 block: Block) -> None:
        super().__init__(block)
        self.name = name
        self.loc = _UNKNOWN
        self.set_type(kind, type_name)

    def set_type(self, kind: str | InstanceKind, type_name: str) -> None:
        """Изменить тип, сохранив пины для последующей проверки чекером.

        Аргументы:
            kind:      InstanceKind.SUB/PRIM или соответствующая строка.
            type_name: имя определения, а не имя данного экземпляра.

        Не возвращает значения. Неизвестный kind даёт ModelError;
        прежние kind и type_name в этом случае не меняются.
        Определение может отсутствовать. Проверяем только вид ссылки;
        существование определения и состав пинов относятся к чекеру.
        """
        try:
            self.kind = InstanceKind(kind)
        except ValueError as exc:
            raise ModelError(f"неизвестный вид инстанса '{kind}'") from exc
        self.type_name = type_name


class Block(_Pins):
    """Определение блока с раздельными пространствами имён объектов."""

    def __init__(self, name: str) -> None:
        super().__init__(self)
        self.name = name
        self.loc = _UNKNOWN
        self._nets: dict[str, Net] = {}
        self._instances: dict[str, Instance] = {}

    def nets(self) -> list[NetView]:
        """Получить объявления цепей в порядке создания."""
        return cast(list[NetView], list(self._nets.values()))

    def instances(self) -> list[InstanceView]:
        """Получить размещённые экземпляры в порядке создания."""
        # list invariant: Instance реализует InstanceView структурно.
        # Наружу отдаётся новая копия, внутренний порядок защищён.
        return cast(list[InstanceView], list(self._instances.values()))

    def orphan_connections(self) -> list[OrphanConnection]:
        """В принятой модели подключение без владельца невозможно."""
        return []

    def add_net(self, name: str) -> Net:
        """Объявить цепь name в конце списка цепей блока.

        Возвращает созданный Net; повтор имени даёт ModelError.
        Цепь сначала не подключена: пины присоединяет вызывающий код.
        Неподключённость проверяет NetCheck, это не отказ в создании.
        """
        _unique(self._nets, name)
        net = Net(name)
        self._nets[name] = net
        return net

    def _replace_net(self, old: str, new: str | None) -> None:
        """Обновить и внешние пины, и пины всех экземпляров блока."""
        for owner in [self, *self._instances.values()]:
            for pin in owner._pins.values():
                if pin.net == old:
                    pin.net = new

    def remove_net(self, name: str) -> None:
        """Удалить цепь name и отключить все её пины.

        Обрабатываются внешние пины и пины экземпляров. Сами пины
        сохраняются: None показывает, что их требуется переподключить.
        Если цепи нет — KeyError, никаких подключений не меняется.
        Сложность O(числа пинов блока). Возвращаемого значения нет.
        """
        del self._nets[name]
        self._replace_net(name, None)

    def rename_net(self, old: str, new: str) -> None:
        """Переименовать цепь old в new и обновить ссылки пинов.

        Сначала проверяется новое имя, затем меняются подключения:
        при конфликте имён прежняя цепь и все ссылки остаются целыми.
        Порядок цепей сохраняется. Сложность O(числа цепей и пинов).
        Отсутствующее old — KeyError, занятое new — ModelError.
        """
        _rename(self._nets, old, new)
        self._replace_net(old, new)

    def add_instance(self, name: str, kind: str | InstanceKind,
                     type_name: str) -> Instance:
        """Создать экземпляр без пинов в конце списка экземпляров.

        Аргументы:
            name:      уникальное собственное имя внутри блока.
            kind:      "sub" или "prim".
            type_name: имя используемого определения.

        Возвращает Instance, через который вызывающий модуль добавляет
        пины. При повторе имени или неизвестном kind — ModelError.
        Существование определения здесь не проверяется (HierarchyCheck).
        """
        _unique(self._instances, name)
        inst = Instance(name, kind, type_name, self)
        self._instances[name] = inst
        return inst

    def remove_instance(self, name: str) -> None:
        """Удалить экземпляр name вместе с его пинами из блока.

        Определение типа и цепи блока сохраняются: их могут использовать
        другие экземпляры. Если имени нет, бросает KeyError.
        Возвращаемого значения нет.
        """
        del self._instances[name]

    def rename_instance(self, old: str, new: str) -> None:
        """Изменить собственное имя old на new, сохранив тип и пины.

        Ссылка type_name не меняется: это имя определения, а не данного
        экземпляра. Позиция в списке также сохраняется. Неизвестное old
        даёт KeyError, занятое new — ModelError без изменения объекта.
        """
        _rename(self._instances, old, new)

    def instance(self, name: str) -> Instance:
        """Найти изменяемый экземпляр; если его нет, возникает KeyError."""
        return self._instances[name]
