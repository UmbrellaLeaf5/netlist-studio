"""Netlist: принятое состояние и атомарное принятие представлений.

Автор: Д. Криворучко. Контракт: docs/interfaces.md, раздел 2.1.

Этот модуль отвечает за коллекции определений верхнего уровня и их
изменение: порядок блоков, поиск, добавление, копирование и удаление.
Содержимое отдельного блока реализовано в entities.py, общие протоколы
и диагностические типы — в views.py и diagnostics.py.

Netlist не занимается вводом-выводом или семантической проверкой
иерархии. Он гарантирует только внутреннюю непротиворечивость принятого
состояния: уникальность имён внутри пространств модели и подключения
только к объявленным в своём блоке цепям.
"""

from __future__ import annotations

from netlist.core.entities import Block, Primitive, _rename, _unique
from netlist.core.errors import ModelError
from netlist.core.views import BlockView, NetlistView


def _copy_block(source: BlockView, name: str) -> Block:
    """Глубоко скопировать и проверить одно определение блока.

    Сначала объявляются все цепи: порядок строк источника свободный,
    поэтому пин может предшествовать своей цепи. Затем создаются пины
    блока и экземпляров с их подключениями. Простые изменяемые записи
    не разделяются с источником; неизменяемые SourceLoc сохраняются.

    Подключение к неизвестной цепи, повтор имени или orphan connection
    означает, что представление нельзя принять в Netlist: бросается
    ModelError. Существование типа instance здесь не проверяется, потому
    что это задача HierarchyCheck.
    """
    target = Block(name)
    target.loc = source.loc
    for net in source.nets():
        target.add_net(net.name).loc = net.loc
    for pin in source.pins():
        target.add_pin(pin.name, pin.net).loc = pin.loc
    for inst in source.instances():
        copied = target.add_instance(inst.name, inst.kind, inst.type_name)
        copied.loc = inst.loc
        for pin in inst.pins():
            copied.add_pin(pin.name, pin.net).loc = pin.loc
    orphans = source.orphan_connections()
    if orphans:
        raise ModelError(f"подключение '{orphans[0].ref}' без инстанса")
    return target


class Netlist:
    """Принятое состояние; определения блоков и примитивов независимы."""

    def __init__(self) -> None:
        """Создать пустую модель без неявного корневого блока.

        Блок main создаёт вызывающий компонент, например новый документ
        редактора. Отсутствие main допустимо в промежуточном состоянии;
        его наличие проверяет HierarchyCheck.
        """
        self.source_file: str | None = None
        self._blocks: dict[str, Block] = {}
        self._primitives: dict[str, Primitive] = {}

    @classmethod
    def from_view(cls, view: NetlistView) -> Netlist:
        """Создать независимую принятую модель из представления.

        Аргументы:
            view: любой объект с протоколом NetlistView. Не изменяется.

        Возвращает новый Netlist с порядком определений и SourceLoc
        источника. При структурном дефекте бросает ModelError с именем
        первого проблемного объекта. Повторы не скрываются словарём:
        каждое добавление проходит проверку уникальности.
        Иерархия не раскрывается. Сложность O(размера представления).
        """
        result = cls()
        result.source_file = view.source_file
        for prim in view.primitives():
            copied = result.add_primitive(prim.name, prim.pin_names)
            copied.loc = prim.loc
        for block in view.blocks():
            _unique(result._blocks, block.name)
            result._blocks[block.name] = _copy_block(block, block.name)
        return result

    def replace_with(self, view: NetlistView) -> None:
        """Принять представление целиком, сохранив старое при отказе.

        Аргументы:
            view: модель или черновик с протоколом NetlistView.

        Возвращаемого значения нет. При ModelError содержимое self,
        source_file и ранее выданные объекты остаются прежними.
        Временная модель полностью проверяется до первой записи в self.
        Это работает и для replace_with(self): источник не разрушается
        во время чтения. Старые внешние ссылки остаются старым снимком.
        """
        replacement = type(self).from_view(view)
        self.source_file = replacement.source_file
        self._blocks = replacement._blocks
        self._primitives = replacement._primitives

    def is_empty(self) -> bool:
        """Пусто, только если нет ни блоков, ни определений примитивов."""
        return not self._blocks and not self._primitives

    def primitives(self) -> list[Primitive]:
        """Определения примитивов в порядке документа."""
        return list(self._primitives.values())

    def blocks(self) -> list[Block]:
        """Определения блоков в порядке документа."""
        return list(self._blocks.values())

    def find_block(self, name: str) -> Block | None:
        """Найти блок, не считая его отсутствие ошибкой вызова."""
        return self._blocks.get(name)

    def find_primitive(self, name: str) -> Primitive | None:
        """Найти примитив либо вернуть None для отсутствующего имени."""
        return self._primitives.get(name)

    def add_primitive(self, name: str, pin_names: list[str]) -> Primitive:
        """Добавить определение примитива в конец списка определений.

        Аргументы:
            name:      уникальное имя среди примитивов.
            pin_names: имена предусмотренных пинов в нужном порядке.

        Возвращает Primitive. Повтор определения или пина — ModelError.
        Список копируется: дальнейшее изменение аргумента вызывающим
        кодом не должно менять уже принятое определение.
        Имя блока может совпадать с name: пространства имён раздельные.
        """
        _unique(self._primitives, name)
        seen: set[str] = set()
        for pin in pin_names:
            if pin in seen:
                raise ModelError(f"повторный пин '{pin}' примитива '{name}'")
            seen.add(pin)
        primitive = Primitive(name, list(pin_names))
        self._primitives[name] = primitive
        return primitive

    def remove_primitive(self, name: str) -> None:
        """Удалить определение, оставив экземпляры для HierarchyCheck."""
        del self._primitives[name]

    def _insert(self, block: Block, index: int | None) -> None:
        """Вставить блок; индекс использует правила стандартного list.insert."""
        items = list(self._blocks.items())
        position = len(items) if index is None else index
        items.insert(position, (block.name, block))
        self._blocks = dict(items)

    def add_block(self, name: str, index: int | None = None) -> Block:
        """Добавить пустое определение блока.

        Аргументы:
            name:  уникальное имя определения среди блоков.
            index: позиция вставки по правилам list.insert; None — конец.

        Возвращает Block для заполнения через его методы. Повтор имени
        даёт ModelError. main автоматически не создаётся: это решение
        редактора при создании документа, а не конструктора модели.
        """
        _unique(self._blocks, name)
        block = Block(name)
        self._insert(block, index)
        return block

    def remove_block(self, name: str) -> None:
        """Удалить определение name, сохранив ссылающиеся экземпляры.

        Возвращаемого значения нет; если определения нет — KeyError.
        Возникшую неполноту иерархии покажет HierarchyCheck. Удалять
        экземпляры автоматически нельзя: это потеряло бы их подключения.
        """
        del self._blocks[name]

    def rename_block(self, old: str, new: str) -> None:
        """Переименовать определение old в new и ссылки на него.

        Меняются только type_name экземпляров sub. Одноимённый примитив
        принадлежит другому пространству имён и не должен затрагиваться.
        Порядок блоков сохраняется. Возвращаемого значения нет.
        Неизвестное old — KeyError, занятое new — ModelError.
        """
        _rename(self._blocks, old, new)
        for block in self._blocks.values():
            for inst in block._instances.values():
                if inst.kind == "sub" and inst.type_name == old:
                    inst.type_name = new

    def move_block(self, name: str, index: int) -> None:
        """Переместить блок name на позицию index.

        Индекс применяется по правилам list.insert ПОСЛЕ извлечения
        блока. Сам объект остаётся тем же: ссылки редактора действуют.
        Новый порядок строится отдельно и принимается только после
        успешной вставки. Неизвестное имя — KeyError. Результата нет.
        """
        block = self._blocks[name]
        items = [(key, value) for key, value in self._blocks.items()
                 if key != name]
        items.insert(index, (name, block))
        self._blocks = dict(items)

    def copy_block(self, src: str, new_name: str,
                   index: int | None = None) -> Block:
        """Глубоко скопировать определение, сохранив имена ссылок типов.

        Аргументы:
            src:      имя исходного блока.
            new_name: уникальное имя создаваемой копии.
            index:    позиция вставки; None — конец списка блоков.

        Возвращает новый Block. Неизвестное src — KeyError, занятое
        new_name — ModelError. Исходное определение не изменяется.
        Подсхемы не копируются рекурсивно: копируется одно определение.
        Владельцы пинов копии привязаны к новому блоку, не к исходному.
        """
        source = self._blocks[src]
        _unique(self._blocks, new_name)
        copied = _copy_block(source, new_name)
        self._insert(copied, index)
        return copied

    def block(self, name: str) -> Block:
        """Получить изменяемый блок; отсутствие имени означает KeyError."""
        return self._blocks[name]
