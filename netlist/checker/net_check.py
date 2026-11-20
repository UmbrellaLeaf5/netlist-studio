"""NetCheck: ошибки цепей.

Модуль чекера проекта «Нетлист»: `netlist/checker/net_check.py`.
Автор: М. Шифрин. Интерфейс согласовать с Н. Чиркиным (ModelUtils),
см. interfaces.md, п. 4.8 и раздел 7.

Что проверяется
---------------
Цепь объявляется строкой `net NAME`, а подключается к пинам строками
`pin NAME NET` (внешний пин блока) и `INST.PIN NET` (пин инстанса).
Всё проверяется отдельно в каждом блоке, без раскрытия иерархии.

Коды:

* E601 (ошибка): строка `pin` или `INST.PIN` ссылается на цепь, которая
  не объявлена в этом блоке. Ошибка стоит на каждой такой строке.
* E602 (ошибка): цепь объявлена повторно. Ошибка стоит на повторной
  строке `net`, в сообщении названы обе строки.
* E603 (ошибка): цепь объявлена, но не подключена ни к одному пину.
* W604 (предупреждение): цепь подключена ровно к одному пину.
* W605 (предупреждение, только при `opts.flat_nets`): цепь окажется
  неподключённой после раскрытия иерархии (см. ниже).

E603 и W604 выдаются один раз на имя цепи, на первой строке `net`.

Какое подключение пина считается «настоящим»
--------------------------------------------
Если пин упомянут несколько раз, подключение определяет ПЕРВОЕ
упоминание (то же правило, что в PinCheck и CheckerDraft). Повторы —
это E503 в PinCheck, и для подсчёта пинов цепи они не учитываются.
А вот E601 выдаётся на любой строке со ссылкой на необъявленную цепь:
ссылка в тексте файла есть, и исправлять её нужно в любом случае.

Строки `INST.PIN` с необъявленным инстансом (E405 в SubcircuitCheck)
тоже считаются подключением: цепь в них упомянута, и выдавать ещё и
E603 на ту же опечатку было бы лишним.

W605: цепь, неподключённая после раскрытия иерархии
---------------------------------------------------
Пусть цепь `n` блока `B` подключена только к внешним пинам `B` (ни
одного пина инстансов внутри). Сама по себе она в порядке, сигнал
приходит снаружи через эти пины. Но если в каком-то экземпляре `B` все
эти пины снаружи не подключены (`x.a -`) или вообще не упомянуты, то
после раскрытия иерархии цепь не подключена ни к чему (рис. 8 ТЗ).

Ищется по определениям, за один проход по рёбрам иерархии:

1. Для каждого определения блока один раз находим такие цепи и их пины.
2. Обходом в ширину от `opts.top` проходим каждое ребро «блок ->
   инстанс-подсхема» один раз и для каждого инстанса смотрим, подключён
   ли снаружи хоть один пин цепи.

Предупреждение стоит на строке `inst` в родительском блоке (там видно,
какой экземпляр нужно исправить). В тексте указаны цепь, блок и путь
экземпляра от корня, например `main/b1/x`. Учитываются только блоки,
достижимые из корня: остальные в раскрытую схему не попадают (о них
говорит W206 в HierarchyCheck).

Каких проверок здесь НЕТ
------------------------
* Пин не подключён (`-`): это E501/E502 в PinCheck.
* Пин упомянут дважды: это E503 в PinCheck.
* Инстанс на несуществующий блок: это E201 в HierarchyCheck. Такие
  инстансы для W605 пропускаются.

Сложность O(числа строк блока) на блок и O(числа инстансов-подсхем)
на W605. Рекурсии нет (раздел 0 interfaces.md).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from netlist.checker.model_utils import ModelIndex
from netlist.core.model import (
    CheckOptions,
    Diagnostic,
    PinRef,
    Severity,
    SourceLoc,
)

if TYPE_CHECKING:
    # Протоколы нужны только для аннотаций.
    from collections.abc import Iterator

    from netlist.core.model import BlockView, InstanceView, NetlistView


# Имя проверки: поле Diagnostic.check и ключ для --checks у CheckerCLI.
NAME = "net"

# Код -> краткое описание (для --list-codes), как в разделе 7.
CODES: dict[str, str] = {
    "E601": "ссылка на необъявленную цепь",
    "E602": "повторное объявление цепи",
    "E603": "неподключённая цепь",
    "W604": "цепь подключена к единственному пину",
    "W605": "цепь становится неподключённой после раскрытия иерархии",
}


# Публичная функция
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None,
          **_kw: object) -> list[Diagnostic]:
    """Найти ошибки объявления и подключения цепей.

    Аргументы:
        view:  любой объект с протоколом NetlistView. Не изменяется.
        opts:  используются `flat_nets` (включает W605), `top` (корень
               для W605) и `max_errors`. Объект по умолчанию общий, но
               функция его не меняет.
        index: кэш индексов от CheckerCLI. Если не передан, строим свой.
        _kw:   прочие именованные аргументы контракта (`walker=`)
               принимаются и игнорируются.

    Возвращает диагностики: блоки в порядке файла, внутри блока по
    номеру строки. Исключений из-за содержимого нетлиста не бросает.
    """
    if index is None:
        index = ModelIndex(view)
    # Список блоков берём один раз: черновик вправе отдавать новые
    # объекты-обёртки при каждом вызове blocks(), а ниже блоки
    # сопоставляются по идентичности.
    blocks = view.blocks()
    per_block = [_check_block(block, index) for block in blocks]
    if opts.flat_nets:
        position = {id(block): i for i, block in enumerate(blocks)}
        for parent, diag in _flat_net_warnings(blocks, index, opts.top):
            per_block[position[id(parent)]].append(diag)

    result: list[Diagnostic] = []
    for diags in per_block:
        result.extend(_sorted_by_line(diags))
        if _limit_reached(result, opts.max_errors):
            return _cut_after_errors(result, opts.max_errors)
    return result


# Подключения пинов
@dataclass(frozen=True)
class _Conn:
    """Одна строка `pin` или `INST.PIN`: пин, его цепь и место."""
    ref: PinRef
    net: str | None
    loc: SourceLoc


def _all_connections(block: BlockView) -> Iterator[_Conn]:
    """Все строки подключения блока: внешние пины, пины инстансов, сироты.

    Пины инстансов-дубликатов (E404) сюда попадают тоже: у CheckerDraft
    их нет (строки привязаны к первому инстансу), а у черновика
    редактора могут быть, и они честно упомянуты в блоке.
    """
    for pin in block.pins():
        yield _Conn(PinRef(None, pin.name), pin.net, pin.loc)
    for inst in block.instances():
        for pin in inst.pins():
            yield _Conn(PinRef(inst.name, pin.name), pin.net, pin.loc)
    for orphan in block.orphan_connections():
        yield _Conn(orphan.ref, orphan.net, orphan.loc)


def _effective_connections(block: BlockView) -> dict[PinRef, _Conn]:
    """Пин -> его ПЕРВОЕ упоминание (см. docstring модуля)."""
    first: dict[PinRef, _Conn] = {}
    for conn in _all_connections(block):
        first.setdefault(conn.ref, conn)
    return first


def _pins_by_net(block: BlockView) -> dict[str, list[PinRef]]:
    """Цепь -> пины, которые к ней на самом деле подключены.

    Учитывается только первое упоминание каждого пина, неподключённые
    (`-`) пропускаются. Порядок — как в _all_connections.
    """
    pins: dict[str, list[PinRef]] = {}
    for conn in _effective_connections(block).values():
        if conn.net is not None:
            pins.setdefault(conn.net, []).append(conn.ref)
    return pins


# Проверка одного блока (E601-W604)
def _check_block(block: BlockView, index: ModelIndex) -> list[Diagnostic]:
    """E601, E602, E603 и W604 по одному блоку."""
    declared = index.nets(block)  # имя -> все строки `net` с ним
    diags: list[Diagnostic] = []

    for conn in _all_connections(block):
        if conn.net is not None and conn.net not in declared:
            diags.append(_diag(
                "E601", Severity.ERROR,
                f"цепь '{conn.net}' не объявлена в блоке '{block.name}' "
                f"(пин '{conn.ref}')",
                conn.loc, block.name, str(conn.ref)))

    for name, nets in declared.items():
        for repeat in nets[1:]:
            diags.append(_diag(
                "E602", Severity.ERROR,
                f"цепь '{name}' блока '{block.name}' объявлена повторно "
                f"({_line_text(nets[0].loc)} и {_line_text(repeat.loc)})",
                repeat.loc, block.name, name))

    connected = _pins_by_net(block)
    for name, nets in declared.items():
        pins = connected.get(name, [])
        if not pins:
            diags.append(_diag(
                "E603", Severity.ERROR,
                f"цепь '{name}' блока '{block.name}' не подключена "
                f"ни к одному пину",
                nets[0].loc, block.name, name))
        elif len(pins) == 1:
            diags.append(_diag(
                "W604", Severity.WARNING,
                f"цепь '{name}' блока '{block.name}' подключена только "
                f"к одному пину ('{pins[0]}')",
                nets[0].loc, block.name, name))
    return diags


# W605: цепи, неподключённые после раскрытия иерархии
class _BoundaryNets:
    """Кэш: блок -> {цепь: её внешние пины} для цепей «только на границе».

    Цепь «только на границе», если она объявлена в блоке, подключена
    хотя бы к одному пину и все её пины — внешние пины блока. Кэш
    строится по одному разу на определение, так что сотни экземпляров
    одного блока его не пересчитывают.
    """

    def __init__(self, index: ModelIndex) -> None:
        self._index = index
        self._cache: dict[int, dict[str, list[str]]] = {}

    def get(self, block: BlockView) -> dict[str, list[str]]:
        key = id(block)
        if key not in self._cache:
            self._cache[key] = self._load(block)
        return self._cache[key]

    def _load(self, block: BlockView) -> dict[str, list[str]]:
        declared = self._index.nets(block)
        result: dict[str, list[str]] = {}
        for net, pins in _pins_by_net(block).items():
            if net in declared and all(p.instance is None for p in pins):
                result[net] = [p.pin for p in pins]
        return result


def _flat_net_warnings(blocks: list[BlockView], index: ModelIndex,
                       top: str) -> Iterator[tuple[BlockView, Diagnostic]]:
    """W605 по всем рёбрам иерархии, достижимым из `top`.

    Обход в ширину по определениям: каждый блок раскрывается один раз,
    каждое ребро «родитель -> инстанс-подсхема» проверяется один раз.
    Путь до блока запоминается при первом посещении, так что он
    кратчайший и не зацикливается даже при циклической иерархии
    (цикл сам по себе — E301 в CycleCheck).

    Выдаёт пары (блок-родитель, диагностика), чтобы check() положил
    диагностику в список именно этого блока.
    """
    definitions: dict[str, BlockView] = {}
    for block in blocks:
        definitions.setdefault(block.name, block)  # дубликаты — E203
    root = definitions.get(top)
    if root is None:
        return  # нет корня — это E204 в HierarchyCheck
    boundary = _BoundaryNets(index)
    paths: dict[int, str] = {id(root): top}
    queue: deque[BlockView] = deque([root])
    while queue:
        parent = queue.popleft()
        for inst in parent.instances():
            if inst.kind != "sub":
                continue
            child = definitions.get(inst.type_name)
            if child is None:
                continue  # E201 (нет блока) или E403 (это примитив)
            path = f"{paths[id(parent)]}/{inst.name}"
            for net, pins in boundary.get(child).items():
                if not _connected_outside(inst, pins):
                    yield parent, _dangling(parent, inst, child.name,
                                            net, pins, path)
            if id(child) not in paths:
                paths[id(child)] = path
                queue.append(child)


def _connected_outside(inst: InstanceView, pins: list[str]) -> bool:
    """Подключён ли в этом инстансе снаружи хоть один из `pins`.

    Как и везде, смотрим первое упоминание пина. Ссылка на
    необъявленную цепь считается подключением: это уже E601 в
    родителе, и W605 сверху только запутает.
    """
    outside: dict[str, str | None] = {}
    for pin in inst.pins():
        outside.setdefault(pin.name, pin.net)
    return any(outside.get(pin) is not None for pin in pins)


def _dangling(parent: BlockView, inst: InstanceView, child: str,
              net: str, pins: list[str], path: str) -> Diagnostic:
    """W605 на строке `inst` в блоке-родителе."""
    names = ", ".join(f"'{inst.name}.{p}'" for p in pins)
    return _diag(
        "W605", Severity.WARNING,
        f"цепь '{net}' блока '{child}' после раскрытия иерархии не "
        f"подключена в экземпляре '{path}': снаружи не подключены "
        f"пины {names}",
        inst.loc, parent.name, inst.name)


# Общие мелочи. Те же функции есть в pin_check.py и subcircuit_check.py:
# архитектура не разрешает заводить под них отдельный модуль, а
# ModelUtils принадлежит другому автору.

def _diag(code: str, severity: Severity, message: str, loc: SourceLoc,
          block: str, obj: str) -> Diagnostic:
    """Собрать Diagnostic от имени этой проверки."""
    return Diagnostic(code=code, severity=severity, check=NAME,
                      message=message, loc=loc, block=block, obj=obj)


def _line_text(loc: SourceLoc) -> str:
    """'строка 12' для сообщений. У объектов редактора строки нет."""
    if loc.line is None:
        return "без номера строки"
    return f"строка {loc.line}"


def _sorted_by_line(diags: list[Diagnostic]) -> list[Diagnostic]:
    """Устойчивая сортировка по строке. Без номера строки — в конец."""
    return sorted(diags, key=lambda d: (d.loc.line is None,
                                        d.loc.line or 0))


def _limit_reached(diags: list[Diagnostic], limit: int | None) -> bool:
    """Набрано ли уже `limit` ошибок (предупреждения не считаются)."""
    if limit is None:
        return False
    errors = sum(d.severity is Severity.ERROR for d in diags)
    return errors >= limit


def _cut_after_errors(diags: list[Diagnostic],
                      limit: int | None) -> list[Diagnostic]:
    """Обрезать список сразу после `limit`-й ошибки (opts.max_errors)."""
    if limit is None:
        return diags
    errors = 0
    for i, diag in enumerate(diags):
        if errors >= limit:
            return diags[:i]
        if diag.severity is Severity.ERROR:
            errors += 1
    return diags
