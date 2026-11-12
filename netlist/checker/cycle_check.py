"""CycleCheck: циклы в иерархии блоков.

Модуль чекера проекта «Нетлист»: `netlist/checker/cycle_check.py`.
Автор: М. Большаков. Интерфейс согласовать с М. Шифриным (CheckerCLI)
и Н. Бучилкиным (HierarchyWalker), см. interfaces.md, п. 4.5 и раздел 7.

Что проверяется
---------------
Блок может включать другие блоки как подсхемы (`inst X sub B`). Если,
переходя от блока к его подсхемам, можно вернуться в исходный блок,
иерархия зациклена: при раскрытии (flatten) схема стала бы бесконечной.
В ТЗ это называется «зацикленность иерархии».

Код:

* E301 (ошибка): цикл в иерархии. Ровно одна ошибка на каждый цикл
  из `HierarchyWalker.find_cycles()`.

Как выглядит сообщение
----------------------
Путь расписан через инстансы, в скобках указан тип инстанса:

    цикл в иерархии: main -> b1 (block1) -> x (block2) -> y (block1)

Если цикл достижим из корневого блока (`opts.top`), путь начинается
с корня, и видно, через какие инстансы схема «проваливается» в цикл.
Сам цикл тогда начинается с того своего блока, до которого обход из
корня добрался первым. Если из корня цикл недостижим, путь начинается
с первого блока цикла в нормализованном виде walker'а
(лексикографически минимального).

Ошибка стоит на строке `inst`, которая замыкает цикл (в примере это
`inst y sub block1` в блоке block2). Поле `block` указывает на блок с
этой строкой, поле `obj` хранит имя инстанса.

Если один блок включает другой через несколько инстансов, цикл всё
равно один (walker описывает его именами блоков), и в пути показан
первый из этих инстансов.

Каких проверок здесь НЕТ
------------------------
* Сам поиск циклов делает HierarchyWalker (Н. Бучилкин). Редактор и
  чекер находят циклы одним и тем же кодом, а здесь циклы только
  превращаются в диагностики.
* Подсхема на несуществующий блок: это E201 в HierarchyCheck. Через
  такие инстансы цикл не проходит, walker их в граф не включает.
* Инстансы-примитивы в иерархию блоков не входят вообще.

Как устроено
------------
1. Берём у walker'а список циклов (списки имён блоков).
2. Один обход в глубину `walker.iter_dfs(opts.top)` запоминает, через
   какое ребро каждый блок был достигнут впервые. Путь от корня до
   любого блока восстанавливается по этим рёбрам в обратную сторону.
3. Для каждого цикла выбираем точку входа, поворачиваем цикл, чтобы он
   с неё начинался, находим инстансы между соседними блоками и
   собираем сообщение.
4. Сортируем ошибки по блокам и строкам. Если строки нет, порядок
   инстансов берём из индекса блоков.

Сложность O(V + E) на обход плюс O(длина цикла * число подсхем блока)
на каждое сообщение. Рекурсии нет (требование раздела 0 interfaces.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from netlist.checker.model_utils import ModelIndex
from netlist.core.model import CheckOptions, Diagnostic, Severity

if TYPE_CHECKING:
    # Типы нужны только для аннотаций. HierarchyWalker во время
    # выполнения импортируется внутри check(), см. комментарий там.
    from netlist.core.hierarchy import HierarchyWalker, HierEdge
    from netlist.core.model import NetlistView

    # Блок -> (порядковый номер первого посещения, ребро, по которому
    # в него пришли; None для корня).
    Reach = dict[str, tuple[int, HierEdge | None]]


# Имя проверки: поле Diagnostic.check и ключ для --checks у CheckerCLI.
NAME = "cycle"

# Код -> краткое описание (для --list-codes), как в разделе 7.
CODES: dict[str, str] = {
    "E301": "Цикл в иерархии блоков",
}


# Публичная функция
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          walker: HierarchyWalker | None = None,
          index: ModelIndex | None = None,
          **_kw: object) -> list[Diagnostic]:
    """Выдать E301 на каждый цикл в иерархии блоков.

    Аргументы:
        view:   любой объект с протоколом NetlistView. Не изменяется.
        opts:   `top` для пути до цикла и `max_errors` для лимита.
                Общий объект настроек только читается.
        walker: граф для этого же `view`. Строится, если не передан.
        index:  индекс блоков для сортировки. Если его не передали,
                создаётся ModelIndex(view).
        _kw:    прочие именованные аргументы контракта игнорируются.

    Возвращает диагностики по порядку блоков, внутри блока по строкам
    или порядку инстансов. Ошибки нетлиста не вызывают исключений.
    """
    if walker is None:
        # Импорт здесь, а не наверху: core.hierarchy импортирует эту
        # проверку в validate(), и так нет цикла импорта (раздел 0).
        from netlist.core.hierarchy import HierarchyWalker
        walker = HierarchyWalker(view)
    if index is None:
        index = ModelIndex(view)
    cycles = walker.find_cycles()
    if not cycles:
        return []  # частый случай: обход из корня даже не нужен
    reach = _first_reach(walker, opts.top)
    diags: list[tuple[Diagnostic, HierEdge]] = []
    seen: set[str] = set()
    for cycle in cycles:
        diag, closing = _cycle_error(walker, cycle, reach)
        # Страховка: цикл, выданный дважды, даёт одинаковый текст.
        if diag.message not in seen:
            seen.add(diag.message)
            diags.append((diag, closing))
    return _cut_after_errors(_sorted(diags, view, index), opts.max_errors)


# Путь от корня
def _first_reach(walker: HierarchyWalker, top: str) -> Reach:
    """Для каждого блока, достижимого из `top`: когда и откуда пришли.

    iter_dfs выдаёт рёбра в порядке обхода в глубину, первым идёт
    `(None, 0)` для самого корня. Запоминаем только ПЕРВОЕ ребро в
    каждый блок. Его родитель к этому моменту уже посещён, поэтому
    цепочка «ребро -> блок-родитель -> его ребро -> ...» всегда
    доходит до корня и не зацикливается, даже если иерархия циклична.
    """
    reach: Reach = {}
    for edge, _depth in walker.iter_dfs(top):
        block = top if edge is None else edge.child
        if block not in reach:
            reach[block] = (len(reach), edge)
    return reach


def _path_from_top(reach: Reach, block: str) -> list[HierEdge]:
    """Рёбра от корня до `block` (пустой список, если это сам корень)."""
    path: list[HierEdge] = []
    edge = reach[block][1]
    while edge is not None:
        path.append(edge)
        edge = reach[edge.parent][1]
    path.reverse()
    return path


# Одна ошибка E301
def _cycle_error(walker: HierarchyWalker, cycle: list[str],
                 reach: Reach) -> tuple[Diagnostic, HierEdge]:
    """Вернуть диагностику E301 и замыкающее ребро для сортировки.

    `cycle` приходит в виде ["a", "b", "a"]: последнее имя повторяет
    первое. Цикл поворачивается так, чтобы начинаться с точки входа из
    корня, затем к нему приписывается путь от корня до этой точки.
    """
    nodes = cycle[:-1]
    entry = _entry_block(nodes, reach)
    start = nodes.index(entry)
    ring = nodes[start:] + nodes[:start] + [entry]
    loop = [_edge(walker, a, b) for a, b in zip(ring, ring[1:])]
    prefix = _path_from_top(reach, entry) if entry in reach else []
    head = prefix[0].parent if prefix else entry
    text = head + "".join(f" -> {e.instance} ({e.child})"
                          for e in prefix + loop)
    closing = loop[-1]  # инстанс, который возвращает нас в entry
    return Diagnostic(
        code="E301", severity=Severity.ERROR, check=NAME,
        message=f"цикл в иерархии: {text}", loc=closing.loc,
        block=closing.parent, obj=closing.instance), closing


def _entry_block(nodes: list[str], reach: Reach) -> str:
    """Блок цикла, до которого обход из корня добрался раньше всех.

    Путь от корня до него не проходит через другие блоки цикла (они
    посещены позже), поэтому в сообщении ничего не повторяется. Если
    цикл из корня недостижим, берём первый блок в порядке walker'а.
    """
    reached = [name for name in nodes if name in reach]
    if not reached:
        return nodes[0]
    return min(reached, key=lambda name: reach[name][0])


def _edge(walker: HierarchyWalker, parent: str, child: str) -> HierEdge:
    """Первый инстанс в `parent`, который ссылается на блок `child`.

    Такое ребро обязано существовать: walker сам нашёл цикл по своему
    графу. Если его нет, нарушен контракт HierarchyWalker, и по общим
    соглашениям (раздел 0) это исключение, а не диагностика.
    """
    for edge in walker.children(parent):
        if edge.child == child:
            return edge
    raise ValueError(f"HierarchyWalker вернул цикл с ребром "
                     f"{parent} -> {child}, которого нет в children()")


# Порядок и лимит
def _sorted(diags: list[tuple[Diagnostic, HierEdge]], view: NetlistView,
            index: ModelIndex) -> list[Diagnostic]:
    """По порядку блоков, внутри блока по строкам или инстансам."""
    order: dict[str, int] = {}
    for block in view.blocks():
        order.setdefault(block.name, len(order))

    def key(item: tuple[Diagnostic, HierEdge]) -> tuple[int, bool, int]:
        diag, edge = item
        block_pos = order.get(edge.parent, len(order))
        if diag.loc.line is not None:
            return block_pos, False, diag.loc.line

        # У черновика может не быть строки. Берём порядок инстансов из
        # первого определения блока, как при построении графа walker'а.
        blocks = index.blocks.get(edge.parent, [])
        instances = blocks[0].instances() if blocks else []
        for position, inst in enumerate(instances):
            if (inst.kind == "sub" and inst.name == edge.instance
                    and inst.type_name == edge.child):
                return block_pos, True, position
        return block_pos, True, len(instances)

    return [diag for diag, _closing in sorted(diags, key=key)]


def _cut_after_errors(diags: list[Diagnostic],
                      limit: int | None) -> list[Diagnostic]:
    """Оставить не больше `limit` ошибок (opts.max_errors).

    Здесь все диагностики являются ошибками, поэтому достаточно среза.
    """
    if limit is None:
        return diags
    return diags[:limit]
