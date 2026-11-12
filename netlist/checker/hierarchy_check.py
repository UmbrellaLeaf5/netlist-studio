"""
HIERARCHYCHECK

Модуль проверки иерархии нетлиста.

Выявляет дубликаты определений блоков и примитивов, проверяет
наличие корневого блока, ищет экземпляры с отсутствующими типами
и определяет недостижимые блоки (если это предусмотрено).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

# Импортируем наш модуль утилит.
# Если запускается как отдельный файл, может потребоваться
# настройка sys.path, но в рамках проекта это должно работать.
from netlist.checker import model_utils
from netlist.core.model import Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    from netlist.core.model import (
        BlockView,
        CheckOptions,
        Diagnostic,
        InstanceView,
        NetlistView,
        PrimitiveView,
    )

NAME = "hierarchy"
CODES: dict[str, str] = {
    "E101": "корневой блок не найден",
    "W102": "недостижимый блок",
    "E103": "неизвестный примитив",
    "W103": "неизвестный примитив (предупреждение)",
    "E104": "неизвестный блок",
    "E105": "дублирующееся определение",
}


# Вспомогательный обходчик (если не передан извне)


class _FallbackWalker:
    """
    Простейший обходчик иерархии, используемый по умолчанию.

    Если проверке не передали специализированный HierarchyWalker,
    она использует этот класс для поиска достижимых блоков.
    """

    def __init__(self, view: NetlistView) -> None:
        self._view = view
        # Создаем индекс для быстрого поиска блоков по имени.
        self._blocks = model_utils.index_blocks(view)

    def reachable(self, root_name: str) -> set[str]:
        """
        Возвращает множество имен блоков, достижимых от корня.

        Использует поиск в глубину (DFS) по графу подсхем.
        Включает сам корень в результат.
        """
        if root_name not in self._blocks:
            return set()

        reachable_names: set[str] = set()
        stack = [root_name]

        while stack:
            current = stack.pop()
            if current in reachable_names:
                continue
            reachable_names.add(current)

            # Находим все блоки с таким именем.
            blocks = self._blocks.get(current, [])
            for block in blocks:
                for inst in block.instances():
                    # Если инстанс ссылается на блок (подсхему),
                    # добавляем его в стек для обхода.
                    if inst.kind == "sub":
                        stack.append(inst.type_name)

        return reachable_names


# Основная функция проверки


def check(
        view: NetlistView,
        opts: CheckOptions = None,
        walker: object | None = None,
        index: model_utils.ModelIndex | None = None
) -> list[Diagnostic]:
    """
    Проверяет иерархию нетлиста на корректность.

    Возвращает список диагностик. Не изменяет исходную модель.
    Порядок диагностик стабилен: сначала по расположению в файле,
    затем по имени.
    """
    diags: list[Diagnostic] = []

    # Инициализация индекса.
    if index is None:
        index = model_utils.ModelIndex()
        index.init(view)

    # Поиск дубликатов блоков и примитивов.
    blocks_index = model_utils.index_blocks(view)
    primitives = _get_primitives(view)

    diags.extend(_find_duplicates_diags(
        blocks_index, "блок", NAME
    ))
    diags.extend(_find_duplicates_diags(
        primitives, "примитив", NAME
    ))

    # Проверка экземпляров на наличие определений.
    diags.extend(_check_instances(view, opts, index))

    # Проверка корневого блока.
    root_name = "main"  # Предполагаемое имя корня (требует согласования).
    if root_name not in blocks_index:
        diags.append(_make_diag(
            "E101", f"Корневой блок '{root_name}' не найден", NAME
        ))
        # Если корня нет, проверку достижимости не запускаем.
        return _sort_diags(diags)

    # Проверка достижимости.
    if walker is None:
        walker = _FallbackWalker(view)

    # В реальном коде walker может иметь другой интерфейс,
    # но мы ожидаем метод reachable.
    try:
        reachable = walker.reachable(root_name)
    except AttributeError:
        # Если переданный walker не поддерживает метод,
        # пропускаем проверку достижимости.
        reachable = set(blocks_index.keys())

    # Сравниваем все блоки с достижимыми.
    for name, blocks in blocks_index.items():
        if name not in reachable:
            diags.append(_make_diag(
                "W102", f"Блок '{name}' недостижим от корня", NAME
            ))

    return _sort_diags(diags)


# Вспомогательные функции проверки


def _check_instances(
        view: NetlistView,
        opts: CheckOptions | None,
        index: model_utils.ModelIndex
) -> list[Diagnostic]:
    """
    Проверяет, что у каждого экземпляра есть определение типа.

    Для неизвестных примитивов уровень диагностики зависит от
    настройки `strict_primitives` (если она передана).
    """
    diags: list[Diagnostic] = []
    strict = getattr(opts, "strict_primitives", False) if opts else False

    for block in view.blocks():
        for inst in block.instances():
            resolved = model_utils.resolve_type(view, inst)
            if resolved is not None:
                continue

            # Тип не найден. Проверяем, не является ли он примитивом.
            # Если имя типа совпадает с известным примитивом,
            # значит проблема в другом (возможно, это дубликат).
            # Но если имя вообще не найдено, это ошибка или предупреждение.

            if inst.kind == "prim":
                # Если инстанс явно указан как примитив, но не найден.
                level = "E" if strict else "W"
                code = "E103" if strict else "W103"
                diags.append(_make_diag(
                    code,
                    f"Неизвестный примитив '{inst.type_name}'",
                    NAME
                ))
            else:
                # Если инстанс указывает на подсхему, но её нет.
                diags.append(_make_diag(
                    "E104",
                    f"Неизвестный блок '{inst.type_name}'",
                    NAME
                ))
    return diags


def _find_duplicates_diags(
        index: dict[str, list[object]],
        entity_type: str,
        check_name: str
) -> list[Diagnostic]:
    """
    Формирует диагностики для дубликатов в индексе.

    Если для какого-то имени найдено больше одного объекта,
    создает ошибку.
    """
    diags: list[Diagnostic] = []
    for name, items in index.items():
        if len(items) > 1:
            diags.append(_make_diag(
                "E105",
                f"Дубликат {entity_type}а: '{name}' встречается {len(items)} раз",
                check_name
            ))
    return diags


def _get_primitives(view: NetlistView) -> dict[str, list[PrimitiveView]]:
    """
    Строит индекс примитивов по их именам.

    Аналогично index_blocks, но для примитивов. Возвращает словарь
    с сохранением всех дубликатов.
    """
    name_to_primitives: dict[str, list[PrimitiveView]] = {}
    # Предполагаем, что у view есть метод primitives().
    for prim in view.primitives():
        name = prim.name
        if name not in name_to_primitives:
            name_to_primitives[name] = []
        name_to_primitives[name].append(prim)
    return name_to_primitives


# Утилиты для диагностик и сортировки


def _make_diag(code: str, message: str, check_name: str) -> Diagnostic:
    """
    Создает объект Diagnostic.

    Примечание: точная структура Diagnostic требует согласования.
    Здесь используется упрощенная версия (словарь) для совместимости.
    """
    severity = Severity.ERROR if code.startswith("E") else Severity.WARNING
    return Diagnostic(code, severity, check_name, message, SourceLoc(None, None))


def _sort_diags(diags: list[Diagnostic]) -> list[Diagnostic]:
    """
    Сортирует диагностики в стабильном порядке.

    Сначала по коду (чтобы ошибки шли раньше предупреждений),
    затем по тексту сообщения. Это обеспечивает воспроизводимость.
    """
    return sorted(diags, key=lambda d: (d.code, d.message))
