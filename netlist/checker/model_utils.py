"""
MODEL_UTILS

Вспомогательные утилиты для работы с нетлистом.

Модуль содержит функции для поиска дубликатов, построения индексов
и разрешения типов экземпляров. Функции не изменяют исходную модель
(NetlistView) и не создают диагностик (Diagnostic).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable, TypeVar

if TYPE_CHECKING:
    from netlist.core.model import (
        BlockView,
        InstanceView,
        NetView,
        NetlistView,
        PinRef,
        PrimitiveView,
        SourceLoc,
    )

T = TypeVar("T")


# Поиск дубликатов


def find_duplicates(
        items: Iterable[T],
        key: Callable[[T], str]
) -> dict[str, list[T]]:
    """
    Находит элементы с одинаковыми ключами.

    Проходит по коллекции ровно один раз. Возвращает словарь, где
    ключ — это значение, полученное через `key(item)`, а значение —
    список всех элементов с этим ключом. Если ключ уникален, он не
    попадает в результат.

    Сложность: O(n) по времени, O(n) по памяти.
    Порядок элементов в списках сохраняется исходным.
    """
    key_to_items: dict[str, list[T]] = {}
    for item in items:
        k = key(item)
        if k not in key_to_items:
            key_to_items[k] = []
        key_to_items[k].append(item)

    # Оставляем только те ключи, где элементов больше одного.
    return {k: v for k, v in key_to_items.items() if len(v) > 1}


# Индексация блоков и сетей


def index_blocks(view: NetlistView) -> dict[str, list[BlockView]]:
    """
    Строит индекс блоков по их именам.

    Возвращает словарь, где ключ — имя блока, а значение — список
    всех блоков с этим именем. Дубликаты имен не отбрасываются,
    чтобы проверки могли их обнаружить.

    Сложность: O(B), где B — количество блоков в модели.
    """
    name_to_blocks: dict[str, list[BlockView]] = {}
    for block in view.blocks():
        name = block.name
        if name not in name_to_blocks:
            name_to_blocks[name] = []
        name_to_blocks[name].append(block)
    return name_to_blocks


def index_primitives(view: NetlistView) -> dict[str, list[PrimitiveView]]:
    """Строит индекс примитивов, сохраняя определения с одинаковыми именами."""
    name_to_primitives: dict[str, list[PrimitiveView]] = {}
    for primitive in view.primitives():
        name_to_primitives.setdefault(primitive.name, []).append(primitive)
    return name_to_primitives


def block_pin_names(block: BlockView) -> list[str]:
    """Вернуть имена внешних пинов блока в порядке объявления."""
    return [pin.name for pin in block.pins()]


def index_nets(block: BlockView) -> dict[str, list[NetView]]:
    """
    Строит индекс сетей внутри одного блока.

    Возвращает словарь, где ключ — имя сети, а значение — список
    всех сетей с этим именем в данном блоке. Дубликаты сохраняются.

    Сложность: O(N), где N — количество сетей в блоке.
    """
    name_to_nets: dict[str, list[NetView]] = {}
    for net in block.nets():
        name = net.name
        if name not in name_to_nets:
            name_to_nets[name] = []
        name_to_nets[name].append(net)
    return name_to_nets


# Индексация подключений пинов


def pins_of_net(
        block: BlockView
) -> dict[str, list[tuple[PinRef, SourceLoc]]]:
    """
    Строит индекс подключений пинов к сетям в блоке.

    Возвращает словарь: имя сети -> список пар (PinRef, SourceLoc).
    Где PinRef — ссылка на вывод, а SourceLoc — место в файле, где
    это подключение было описано.

    Подключения с net=None (явно неподключенные) в индекс не входят,
    так как они не привязаны к имени сети.

    Сложность: O(C), где C — количество подключений в блоке.
    """
    net_name_to_pins: dict[str, list[tuple[PinRef, SourceLoc]]] = {}

    # Предполагаем, что у блока есть метод для перебора подключений.
    # В зависимости от модели это могут быть инстансы и их пины.
    for conn in block.connections():
        net_name = conn.net_name
        if net_name is None:
            continue

        if net_name not in net_name_to_pins:
            net_name_to_pins[net_name] = []

        net_name_to_pins[net_name].append((conn.pin_ref, conn.loc))

    return net_name_to_pins


# Разрешение типов экземпляров


def resolve_type(
        view: NetlistView,
        inst: InstanceView
) -> BlockView | PrimitiveView | None:
    """
    Находит определение типа для конкретного экземпляра.

    Экземпляр может ссылаться либо на блок (подсхему), либо на
    примитив. Функция ищет совпадение по имени типа среди блоков
    и примитивов.

    Возвращает:
      - BlockView, если найден блок.
      - PrimitiveView, если найден примитив.
      - None, если тип не найден.

    Если найдено несколько определений с одинаковым именем (что
    является ошибкой модели), возвращается None, чтобы не выбирать
    произвольный вариант. Такие случаи должна обрабатывать проверка
    иерархии (HierarchyCheck).
    """
    type_name = inst.type_name

    # Ищем среди блоков. Используем генератор, чтобы не создавать
    # промежуточный список и сразу остановиться при нахождении.
    blocks = [b for b in view.blocks() if b.name == type_name]

    # Аналогично для примитивов.
    primitives = [p for p in view.primitives() if p.name == type_name]

    # Если совпадений нет ни там, ни там — тип не найден.
    if not blocks and not primitives:
        return None

    # Если найдено ровно одно определение блока — возвращаем его.
    if len(blocks) == 1 and not primitives:
        return blocks[0]

    # Если найден ровно один примитив — возвращаем его.
    if len(primitives) == 1 and not blocks:
        return primitives[0]

    # Любая неоднозначность (несколько блоков, несколько примитивов,
    # или блок и примитив с одинаковым именем) считается ошибкой.
    return None


# Кэш индексов


class ModelIndex:
    """
    Кэш для хранения индексов модели.

    Позволяет проверкам не пересчитывать индексы блоков, сетей и
    подключений при каждом вызове. Кэш привязан к конкретной модели
    (NetlistView) и должен инициализироваться через `init`.
    """

    def __init__(self, view: NetlistView | None = None) -> None:
        """
        Создает пустой экземпляр кэша.

        До вызова `init` методы `nets` и `pins_of_net` будут
        возвращать пустые словари или выбрасывать исключение.
        (В текущей реализации — возвращать пустые словари).
        """
        self._view: NetlistView | None = None
        self._blocks_index: dict[str, list[BlockView]] = {}
        self._primitives_index: dict[str, list[PrimitiveView]] = {}
        self._nets_cache: dict[int, dict[str, list[NetView]]] = {}
        self._pins_cache: dict[int, dict[str, list[tuple[PinRef, SourceLoc]]]] = {}
        if view is not None:
            self.init(view)

    def init(self, view: NetlistView) -> None:
        """
        Инициализирует кэш для указанной модели.

        Если кэш уже был инициализирован для другой модели, он
        полностью заменяется новым. Это гарантирует, что старые
        данные не будут использованы с новой моделью.
        """
        self._view = view
        self._blocks_index = index_blocks(view)
        self._primitives_index = index_primitives(view)

        # Очищаем кэши для блоков, так как они относятся к старой модели.
        self._nets_cache.clear()
        self._pins_cache.clear()

    @property
    def blocks(self) -> dict[str, list[BlockView]]:
        """Индекс блоков, построенный последним вызовом ``init``."""
        return self._blocks_index

    @property
    def primitives(self) -> dict[str, list[PrimitiveView]]:
        """Индекс примитивов, построенный последним вызовом ``init``."""
        return self._primitives_index

    def nets(self, block: BlockView) -> dict[str, list[NetView]]:
        """
        Возвращает индекс сетей для указанного блока.

        Если индекс для этого блока уже был построен ранее, он
        возвращается из кэша. Если нет — строится и сохраняется.

        Ключом кэша служит `id(block)`, чтобы даже блоки с
        одинаковыми именами не перезаписывали друг друга.
        """
        block_id = id(block)
        if block_id not in self._nets_cache:
            self._nets_cache[block_id] = index_nets(block)
        return self._nets_cache[block_id]

    def pins_of_net(
            self,
            block: BlockView
    ) -> dict[str, list[tuple[PinRef, SourceLoc]]]:
        """
        Возвращает индекс подключений пинов для указанного блока.

        Работает аналогично методу `nets`: при первом вызове для
        блока строит индекс и кэширует его, при повторном — отдает
        готовый результат.
        """
        block_id = id(block)
        if block_id not in self._pins_cache:
            self._pins_cache[block_id] = pins_of_net(block)
        return self._pins_cache[block_id]
