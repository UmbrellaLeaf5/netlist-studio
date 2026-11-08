"""PinCheck: неподключённые и многократно подключённые пины.

Модуль чекера проекта «Нетлист»: `netlist/checker/pin_check.py`.
Автор: Л. Сальникова. Интерфейс согласовать с М. Шифриным (CheckerCLI)
и Н. Чиркиным (ModelUtils), см. docs/interfaces.md, п. 4.7 и раздел 7.

Что проверяется
---------------
В формате нетлиста каждый пин записан строкой вида «пин -> цепь»:
`pin NAME NET` для внешнего пина блока и `INST.PIN NET` для пина
инстанса. Вместо цепи можно написать `-`: пин явно не подключён.
По ТЗ к каждому пину подключено не больше одной цепи, и неподключённых
пинов быть не должно.

Коды:

* E501 (ошибка): внешний пин блока не подключён (`pin a -`).
* E502 (ошибка): пин инстанса явно не подключён (`t1.p1 -`).
* E503 (ошибка): пин упомянут больше одного раза: повтор `pin NAME`
  в блоке или повтор `INST.PIN` у одного инстанса. В сообщении обе
  строки, а сама ошибка стоит на повторной строке.
* W504 (предупреждение): в определении блока-подсхемы внешний пин
  подключён к цепи, а в данном инстансе этот пин явно `-`. Значит,
  внутренняя цепь блока в этом экземпляре «висит в воздухе». Выдаётся
  на строке `INST.PIN -` этого инстанса, то есть там, где её нужно
  исправлять. Всегда сопровождает E502 на той же строке и уточняет её.

Какое упоминание пина считается «настоящим»
-------------------------------------------
Если пин упомянут несколько раз, подключение определяет ПЕРВОЕ
упоминание (так же, как CheckerDraft привязывает строки к первому
инстансу с данным именем). E501/E502/W504 проверяются только по нему,
а все последующие упоминания дают только E503, независимо от цепи.
Так одна строка не получает две разные ошибки об одном и том же.

Каких проверок здесь НЕТ
------------------------
* Пин вообще не упомянут у инстанса: это E401 (SubcircuitCheck).
* Пина нет в определении: это E402 (SubcircuitCheck).
* Цепь, к которой подключён пин, не объявлена: это E601 (NetCheck).

Сложность O(числа строк pin/INST.PIN) плюс один разбор каждого
определения блока-подсхемы для W504. Иерархия не раскрывается.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from netlist.checker.model_utils import ModelIndex, resolve_type
from netlist.core.model import CheckOptions, Diagnostic, Severity, SourceLoc

if TYPE_CHECKING:
    # Протоколы нужны только для аннотаций.
    from netlist.core.model import (
        BlockView,
        InstanceView,
        NetlistView,
        PinView,
    )


# Имя проверки: поле Diagnostic.check и ключ для --checks у CheckerCLI.
NAME = "pin"

# Код -> краткое описание (для --list-codes), как в разделе 7.
CODES: dict[str, str] = {
    "E501": "неподключённый внешний пин блока",
    "E502": "неподключённый пин инстанса",
    "E503": "пин подключён больше одного раза",
    "W504": "внутренняя цепь блока «висит» в экземпляре: "
            "пин подсхемы не подключён снаружи",
}


# Публичная функция
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None,
          **_kw: object) -> list[Diagnostic]:
    """Найти неподключённые и повторно подключённые пины.

    Аргументы:
        view:  любой объект с протоколом NetlistView. Не изменяется.
        opts:  параметры. Используется только `max_errors`; объект по
               умолчанию общий, но функция его не меняет.
        index: кэш индексов от CheckerCLI. Этой проверке индексы не
               нужны (все данные берутся из самих блоков), параметр
               принимается для единообразия контракта.
        _kw:   прочие именованные аргументы контракта (`walker=`)
               принимаются и игнорируются.

    Возвращает диагностики: блоки в порядке файла, внутри блока по
    номеру строки. Исключений из-за содержимого нетлиста не бросает.
    """
    definitions = _DefinitionNets(view)
    result: list[Diagnostic] = []
    for block in view.blocks():
        diags = _check_block_pins(block)
        for inst in block.instances():
            diags.extend(_check_instance_pins(block, inst, definitions))
        result.extend(_sorted_by_line(diags))
        if _limit_reached(result, opts.max_errors):
            return _cut_after_errors(result, opts.max_errors)
    return result


# Внешние пины блока (E501, E503)
def _check_block_pins(block: BlockView) -> list[Diagnostic]:
    """E501 и E503 по строкам `pin` одного блока.

    Словарь `first` хранит первое упоминание каждого имени. Повтор
    сразу даёт E503, а «неподключённость» смотрится только у первого
    упоминания (см. docstring модуля).
    """
    diags: list[Diagnostic] = []
    first: dict[str, PinView] = {}
    for pin in block.pins():
        if pin.name in first:
            diags.append(_repeated(
                f"внешний пин '{pin.name}' блока '{block.name}'",
                first[pin.name], pin, block.name, pin.name))
            continue
        first[pin.name] = pin
        if pin.net is None:
            diags.append(_diag(
                "E501", Severity.ERROR,
                f"внешний пин '{pin.name}' блока '{block.name}' "
                f"не подключён",
                pin.loc, block.name, pin.name))
    return diags


# Пины инстанса (E502, E503, W504)
def _check_instance_pins(block: BlockView, inst: InstanceView,
                         definitions: _DefinitionNets
                         ) -> list[Diagnostic]:
    """E502, E503 и W504 по строкам `INST.PIN` одного инстанса.

    Устроено так же, как _check_block_pins. Дополнительно для
    неподключённого пина подсхемы смотрим, к какой цепи этот пин
    подключён внутри определения блока: если к настоящей цепи, то
    добавляем W504.
    """
    inner_nets = definitions.get(inst)  # None: не подсхема или нет блока
    diags: list[Diagnostic] = []
    first: dict[str, PinView] = {}
    for pin in inst.pins():
        ref = f"{inst.name}.{pin.name}"
        if pin.name in first:
            diags.append(_repeated(f"пин '{ref}'", first[pin.name],
                                   pin, block.name, ref))
            continue
        first[pin.name] = pin
        if pin.net is not None:
            continue
        diags.append(_diag(
            "E502", Severity.ERROR,
            f"пин '{ref}' инстанса '{inst.name}' не подключён",
            pin.loc, block.name, ref))
        inner = inner_nets.get(pin.name) if inner_nets else None
        if inner is not None:
            diags.append(_diag(
                "W504", Severity.WARNING,
                f"пин '{ref}' не подключён, поэтому цепь '{inner}' "
                f"блока '{inst.type_name}' в экземпляре '{inst.name}' "
                f"висит в воздухе",
                pin.loc, block.name, ref))
    return diags


def _repeated(what: str, original: PinView, repeat: PinView,
              block: str, obj: str) -> Diagnostic:
    """E503 на повторной строке. Сообщение называет обе строки."""
    return _diag(
        "E503", Severity.ERROR,
        f"{what} подключён больше одного раза "
        f"({_line_text(original.loc)} и {_line_text(repeat.loc)})",
        repeat.loc, block, obj)


class _DefinitionNets:
    """Кэш для W504: блок-подсхема -> {внешний пин: его цепь}.

    Строится лениво и по одному разу на каждый тип `sub`, чтобы сотни
    экземпляров одного блока не разбирали его определение заново.
    Для каждого пина берётся ПЕРВОЕ упоминание в определении, по тому же
    правилу, что и в _check_block_pins.
    """

    def __init__(self, view: NetlistView) -> None:
        self._view = view
        self._cache: dict[str, dict[str, str | None] | None] = {}

    def get(self, inst: InstanceView) -> dict[str, str | None] | None:
        """Цепи внешних пинов определения или None.

        None означает, что W504 для этого инстанса не применима: это
        примитив (внутренних цепей у него нет) или блок не найден
        (неполнота иерархии, её выдаёт HierarchyCheck).
        """
        if inst.kind != "sub":
            return None
        if inst.type_name not in self._cache:
            self._cache[inst.type_name] = self._load(inst)
        return self._cache[inst.type_name]

    def _load(self, inst: InstanceView) -> dict[str, str | None] | None:
        definition = resolve_type(self._view, inst)
        if definition is None:
            return None
        nets: dict[str, str | None] = {}
        for pin in definition.pins():
            nets.setdefault(pin.name, pin.net)
        return nets


# Общие мелочи. Те же функции есть в subcircuit_check.py: архитектура
# не разрешает заводить под них отдельный модуль, а ModelUtils
# принадлежит другому автору.

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
    """Устойчивая сортировка по строке. Без номера строки — в конец.

    Устойчивость важна: E502 и W504 стоят на одной строке, и W504
    должна идти сразу после своей E502.
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
