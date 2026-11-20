"""HierarchyCheck: определения, выбранный корень и достижимость.

Исправленная версия приложенного hierarchycheck.py. Диагностика общая
с моделью; обход делегируется HierarchyWalker, циклы проверяет CycleCheck.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.checker.model_utils import ModelIndex
from src.core.model import CheckOptions, Diagnostic, NetlistView, Severity, SourceLoc

if TYPE_CHECKING:
    from src.core.hierarchy import HierarchyWalker

NAME = "hierarchy"
CODES: dict[str, str] = {
    "E201": "неизвестный блок",
    "E202": "неизвестный примитив",
    "W202": "неизвестный примитив (предупреждение)",
    "E203": "повторное определение блока",
    "E204": "корневой блок не найден",
    "E205": "повторное определение примитива",
    "W206": "недостижимый блок",
}


def check(view: NetlistView, opts: CheckOptions | None = None,
          walker: HierarchyWalker | None = None,
          index: ModelIndex | None = None) -> list[Diagnostic]:
    if opts is None:
        opts = CheckOptions()
    if index is None:
        index = ModelIndex(view)
    if walker is None:
        from src.core.hierarchy import HierarchyWalker

        walker = HierarchyWalker(view)
    diags: list[Diagnostic] = []

    def emit(code: str, message: str, loc: SourceLoc,
             block: str | None = None, obj: str | None = None) -> None:
        level = Severity.WARNING if code.startswith("W") else Severity.ERROR
        diags.append(Diagnostic(code, level, NAME, message, loc, block, obj))

    for name, blocks in index.blocks.items():
        for duplicate in blocks[1:]:
            emit("E203", f"повторное определение блока '{name}'",
                 duplicate.loc, name, name)
    for name, primitives in index.primitives.items():
        for duplicate in primitives[1:]:
            emit("E205", f"повторное определение примитива '{name}'",
                 duplicate.loc, obj=name)

    for edge in walker.missing():
        emit("E201", f"неизвестный блок '{edge.child}'",
             edge.loc, edge.parent, edge.instance)
    for blocks in index.blocks.values():
        for block in blocks:
            for inst in block.instances():
                if inst.kind == "prim" and inst.type_name not in index.primitives:
                    code = "E202" if opts.strict_primitives else "W202"
                    emit(code, f"неизвестный примитив '{inst.type_name}'",
                         inst.loc, block.name, inst.name)

    if opts.top not in index.blocks:
        emit("E204", f"корневой блок '{opts.top}' не найден",
             SourceLoc(view.source_file, None), obj=opts.top)
    else:
        reachable = walker.reachable(opts.top)
        for name, blocks in index.blocks.items():
            if name not in reachable:
                emit("W206", f"блок '{name}' недостижим из '{opts.top}'",
                     blocks[0].loc, name, name)

    # При одинаковом положении сохраняется порядок формирования сообщений.
    return sorted(diags, key=lambda d: (
        d.loc.file or "", d.loc.line is None, d.loc.line or 0,
    ))
