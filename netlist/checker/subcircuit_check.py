"""SubcircuitCheck: соответствие инстансов определениям их типов.

Модуль чекера проекта «Нетлист»: `netlist/checker/subcircuit_check.py`.
Автор: Л. Сальникова. Интерфейс согласовать с М. Шифриным (CheckerCLI)
и Н. Чиркиным (ModelUtils), см. interfaces.md, п. 4.6 и раздел 7.

Что проверяется
---------------
Инстанс в блоке является экземпляром либо другого блока
(`inst X sub B`), либо примитива (`inst X prim P`). У определения
типа есть свой набор пинов: внешние пины блока `B` (строки `pin`)
или пины из строки `primitive P ...`. Пины, упомянутые у инстанса в
строках `X.PIN NET`, должны совпадать с этим набором. Именно это в ТЗ
названо «некорректной подсхемой».

Коды ошибок (все с уровнем ERROR):

* E401: у инстанса не упомянут пин, который есть в определении.
  Одна ошибка на каждый отсутствующий пин, на строке `inst`.
* E402: у инстанса упомянут пин, которого в определении нет.
  Ошибка на строке `X.PIN`.
* E403: вид инстанса не совпадает с типом: `sub` указывает на имя
  примитива или `prim` указывает на имя блока.
* E404: в одном блоке два инстанса с одинаковым именем.
* E405: строка `X.PIN` ссылается на инстанс `X`, который в блоке
  не объявлен (`BlockView.orphan_connections()`).

Каких проверок здесь НЕТ
------------------------
* тип инстанса вообще не найден: это E201/E202 в HierarchyCheck,
  а такой инстанс здесь молча пропускается;
* пин упомянут дважды или подключён к `-`: это E503/E502 в PinCheck;
* цепь не объявлена: это E601 в NetCheck.

Как устроено
------------
Проход по блокам в порядке файла, внутри блока за один проход по
инстансам. Наборы пинов определений строятся один раз на каждый тип
(`_DefinitionPins`), поэтому общая сложность
O(суммарного числа пинов инстансов + размеров определений).
Иерархия не раскрывается: проверяются определения, а не экземпляры.

Проверка работает через протокол `NetlistView` и одинаково годится
для черновика чекера (CheckerDraft), черновика редактора (EditorDraft)
и принятой модели (Netlist). Черновики могут быть «грязными»: дубликаты
имён, ссылки в никуда. На таких данных функция не падает, а выдаёт
диагностику или пропускает объект.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from netlist.checker.model_utils import (
    ModelIndex,
    block_pin_names,
    resolve_type,
)
from netlist.core.model import CheckOptions, Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    # Протоколы нужны только для аннотаций, поэтому во время выполнения
    # модуль от них не зависит.
    from netlist.core.model import (
        BlockView,
        InstanceView,
        NetlistView,
        OrphanConnection,
        PinView,
    )


# Имя проверки. Попадает в поле Diagnostic.check и в ключ
# --checks у CheckerCLI.
NAME = "subckt"

# Код -> краткое описание. Читает CheckerCLI для --list-codes;
# тексты совпадают с таблицей раздела 7 interfaces.md.
CODES: dict[str, str] = {
    "E401": "у инстанса нет пина из определения",
    "E402": "у инстанса лишний пин, которого нет в определении",
    "E403": "вид инстанса не соответствует типу (sub<->prim)",
    "E404": "повторное имя инстанса в блоке",
    "E405": "подключение к необъявленному инстансу",
}

# Человекочитаемые названия видов инстанса для сообщений E403.
_KIND_WORD = {"sub": "подсхема (sub)", "prim": "примитив (prim)"}


# Публичная функция
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None,
          **_kw: object) -> list[Diagnostic]:
    """Проверить все инстансы нетлиста на соответствие своим типам.

    Аргументы:
        view:  любой объект с протоколом NetlistView. Не изменяется.
        opts:  параметры проверки. Здесь используется только
               `max_errors`. Объект по умолчанию общий для всех
               вызовов, но функция его не меняет, так что это безопасно.
        index: кэш индексов ModelUtils. CheckerCLI строит его один раз
               и раздаёт всем проверкам. Если не передан, строим свой.
        _kw:   прочие именованные аргументы общего контракта
               (например, `walker=`) принимаются и игнорируются:
               так CLI может звать все проверки одинаково.

    Возвращает список диагностик: блоки в порядке файла, внутри блока
    по номеру строки. Исключений из-за содержимого нетлиста не бросает.
    """
    if index is None:
        index = ModelIndex(view)
    definitions = _DefinitionPins(view)
    result: list[Diagnostic] = []
    for block in view.blocks():
        result.extend(_sorted_by_line(
            _check_block(block, index, definitions)))
        if _limit_reached(result, opts.max_errors):
            return _cut_after_errors(result, opts.max_errors)
    return result


# Проверка одного блока
def _check_block(block: BlockView, index: ModelIndex,
                 definitions: _DefinitionPins) -> list[Diagnostic]:
    """Все ошибки E401..E405 одного блока, пока без сортировки.

    Один проход по инстансам. Словарь `first` хранит первое
    объявление каждого имени. Повторное объявление даёт E404, а его
    пины не проверяются. Причина: CheckerDraft привязывает строки
    `X.PIN` к ПЕРВОМУ инстансу с именем `X`, так что у дубликата
    пинов нет, и проверка выдала бы ложные E401 на все пины.
    E403 же относится к самой строке `inst` и проверяется всегда.
    """
    diags: list[Diagnostic] = []
    first: dict[str, InstanceView] = {}
    for inst in block.instances():
        wrong_kind = _wrong_kind(block, inst, index)
        if wrong_kind is not None:
            diags.append(wrong_kind)
        if inst.name in first:
            diags.append(_duplicate_instance(block, inst, first))
            continue
        first[inst.name] = inst
        expected = definitions.get(inst)
        if expected is not None:
            diags.extend(_compare_pins(block, inst, expected))
    for orphan in block.orphan_connections():
        diags.append(_orphan_connection(block, orphan))
    return diags


def _wrong_kind(block: BlockView, inst: InstanceView,
                index: ModelIndex) -> Diagnostic | None:
    """E403: вид инстанса указывает не на тот сорт определений.

    Проверяем по индексам имён блоков и примитивов, до resolve_type.
    Ошибка выдаётся, только если имя есть среди определений ДРУГОГО
    вида и при этом его нет среди определений своего вида. Если имени
    нет нигде, это неполнота иерархии (E201/E202, HierarchyCheck).
    Если блок и примитив называются одинаково, `sub` и `prim`
    однозначно выбирают одно из них, и ошибки тоже нет.
    """
    if inst.kind == "sub":
        own, other, right = index.blocks, index.primitives, "prim"
    elif inst.kind == "prim":
        own, other, right = index.primitives, index.blocks, "sub"
    else:
        # Неизвестный вид может прийти только из «грязного» черновика.
        # Это синтаксическая ошибка, её выдаёт SyntaxCheck, не мы.
        return None
    if inst.type_name in own or inst.type_name not in other:
        return None
    return _error(
        "E403",
        f"инстанс '{inst.name}' объявлен как {_KIND_WORD[inst.kind]}, "
        f"но '{inst.type_name}' это {_KIND_WORD[right]}; "
        f"нужно 'inst {inst.name} {right} {inst.type_name}'",
        inst.loc, block.name, inst.name)


def _duplicate_instance(block: BlockView, inst: InstanceView,
                        first: dict[str, InstanceView]) -> Diagnostic:
    """E404 на повторном объявлении. В тексте указана строка первого."""
    original = first[inst.name]
    return _error(
        "E404",
        f"повторное имя инстанса '{inst.name}' в блоке '{block.name}' "
        f"(первое объявление: {_line_text(original.loc)})",
        inst.loc, block.name, inst.name)


def _orphan_connection(block: BlockView,
                       orphan: OrphanConnection) -> Diagnostic:
    """E405: строка `X.PIN` при необъявленном инстансе `X`.

    Ошибка выдаётся на каждую такую строку: исправлять придётся
    каждую, и в каждой может быть своя опечатка.
    """
    return _error(
        "E405",
        f"подключение '{orphan.ref}' к необъявленному инстансу "
        f"'{orphan.ref.instance}' в блоке '{block.name}'",
        orphan.loc, block.name, str(orphan.ref))


# Сравнение пинов инстанса с определением (E401, E402)
def _compare_pins(block: BlockView, inst: InstanceView,
                  expected: dict[str, None]) -> list[Diagnostic]:
    """E401 и E402 для одного инстанса с найденным определением.

    `expected` хранит имена пинов определения в виде упорядоченного
    словаря: он одновременно задаёт порядок сообщений (как в файле) и
    даёт проверку принадлежности за O(1).

    Повторно упомянутый пин учитывается один раз, по первой строке.
    Сам повтор является ошибкой E503 из PinCheck, и выдавать E402
    второй раз на ту же «лишнюю» ногу было бы дублированием.
    """
    actual: dict[str, PinView] = {}  # имя пина -> первое упоминание
    for pin in inst.pins():
        actual.setdefault(pin.name, pin)
    diags: list[Diagnostic] = []
    for name in expected:
        if name not in actual:
            diags.append(_error(
                "E401",
                f"у инстанса '{inst.name}' ({inst.type_name}) "
                f"нет пина '{name}' из определения",
                inst.loc, block.name, f"{inst.name}.{name}"))
    for name, pin in actual.items():
        if name not in expected:
            diags.append(_error(
                "E402",
                f"у инстанса '{inst.name}' лишний пин '{name}': "
                f"в определении '{inst.type_name}' его нет",
                pin.loc, block.name, f"{inst.name}.{name}"))
    return diags


class _DefinitionPins:
    """Кэш наборов пинов определений, по одному на каждый тип.

    Ключ: пара (вид, имя типа). Значение: упорядоченный словарь имён
    пинов определения или None, если resolve_type ничего не нашёл.
    Кэш нужен ради заявленной сложности: в блоке могут быть сотни
    инстансов одного типа, и строить набор пинов для каждого незачем.
    """

    def __init__(self, view: NetlistView) -> None:
        self._view = view
        self._cache: dict[tuple[str, str], dict[str, None] | None] = {}

    def get(self, inst: InstanceView) -> dict[str, None] | None:
        """Пины определения типа `inst` или None, если тип не найден.

        resolve_type зависит только от вида и имени типа (берётся
        первое определение с таким именем), поэтому кэшировать по
        этой паре корректно.
        """
        key = (inst.kind, inst.type_name)
        if key not in self._cache:
            self._cache[key] = self._load(inst)
        return self._cache[key]

    def _load(self, inst: InstanceView) -> dict[str, None] | None:
        definition = resolve_type(self._view, inst)
        if definition is None:
            return None  # неразрешённый тип: забота HierarchyCheck
        if inst.kind == "sub":
            names = block_pin_names(definition)
        else:
            names = list(definition.pin_names)
        # dict.fromkeys убирает повторы (повтор пина в определении
        # является ошибкой самого определения) и сохраняет порядок.
        return dict.fromkeys(names)


# Общие мелочи: создание диагностик, сортировка, лимит ошибок.
# Похожие функции есть в pin_check.py. Отдельный модуль под них не
# заводим, потому что архитектура запрещает добавлять модули, а
# ModelUtils принадлежит другому автору.

def _error(code: str, message: str, loc: SourceLoc,
           block: str, obj: str) -> Diagnostic:
    """Собрать Diagnostic уровня ERROR от имени этой проверки."""
    return Diagnostic(code=code, severity=Severity.ERROR, check=NAME,
                      message=message, loc=loc, block=block, obj=obj)


def _line_text(loc: SourceLoc) -> str:
    """'строка 12' для сообщений. У объектов редактора строки нет."""
    if loc.line is None:
        return "без номера строки"
    return f"строка {loc.line}"


def _sorted_by_line(diags: list[Diagnostic]) -> list[Diagnostic]:
    """Сортировка внутри блока по номеру строки (требование контракта).

    Диагностики без номера строки (объекты, созданные в редакторе)
    идут в конце. Сортировка устойчивая, поэтому несколько E401 на
    одной строке `inst` остаются в порядке пинов определения.
    """
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
