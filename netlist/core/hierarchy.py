"""Граф определений и итеративные обходы иерархии. Автор: Н. Бучилкин."""

from collections.abc import Iterator
from dataclasses import dataclass

from netlist.core.model import CheckOptions, Diagnostic, NetlistView, SourceLoc


@dataclass(frozen=True)
class HierEdge:
    parent: str
    instance: str
    child: str
    loc: SourceLoc


class HierarchyCycleError(Exception):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("цикл в иерархии: " + " -> ".join(self.cycle))


class HierarchyWalker:
    """Снимок графа; после изменения view создаётся новый walker."""

    def __init__(self, view: NetlistView) -> None:
        self._view = view
        blocks = {}
        for block in view.blocks():
            blocks.setdefault(block.name, block)
        self._adj: dict[str, list[HierEdge]] = {name: [] for name in blocks}
        self._reverse: dict[str, list[HierEdge]] = {}
        self._missing: list[HierEdge] = []
        for name, block in blocks.items():
            for inst in block.instances():
                if inst.kind != "sub":
                    continue
                edge = HierEdge(name, inst.name, inst.type_name, inst.loc)
                self._adj[name].append(edge)
                self._reverse.setdefault(edge.child, []).append(edge)
                if edge.child not in blocks:
                    self._missing.append(edge)

    def children(self, block: str) -> list[HierEdge]:
        return list(self._adj.get(block, ()))

    def parents(self, block: str) -> list[HierEdge]:
        return list(self._reverse.get(block, ()))

    def missing(self) -> list[HierEdge]:
        return list(self._missing)

    def iter_dfs(self, root: str = "main") -> Iterator[tuple[HierEdge | None, int]]:
        """Раскрыть каждое определение один раз; неизвестный корень даёт пусто."""
        if root not in self._adj:
            return
        visited = {root}
        yield None, 0
        stack = [iter(self._adj[root])]
        while stack:
            edge = next(stack[-1], None)
            if edge is None:
                stack.pop()
            elif edge.child in self._adj and edge.child not in visited:
                visited.add(edge.child)
                yield edge, len(stack)
                stack.append(iter(self._adj[edge.child]))

    def reachable(self, root: str = "main") -> set[str]:
        return {root if edge is None else edge.child for edge, _ in self.iter_dfs(root)}

    def _walk_all(self) -> Iterator[tuple[str, str | list[str]]]:
        """Цветной DFS: события завершения вершин и обратных рёбер.

        active содержит позиции GRAY-вершин в пути, visited — все
        обнаруженные вершины. Стек Python не зависит от глубины схемы.
        """
        visited: set[str] = set()
        for root in self._adj:
            if root in visited:
                continue
            visited.add(root)
            path = [root]
            active = {root: 0}
            stack = [iter(self._adj[root])]
            while stack:
                edge = next(stack[-1], None)
                if edge is None:
                    stack.pop()
                    name = path.pop()
                    del active[name]
                    yield "finish", name
                elif edge.child in active:
                    cycle = path[active[edge.child]:]
                    start = min(range(len(cycle)), key=cycle.__getitem__)
                    cycle = cycle[start:] + cycle[:start]
                    yield "cycle", cycle + [cycle[0]]
                elif edge.child in self._adj and edge.child not in visited:
                    visited.add(edge.child)
                    active[edge.child] = len(path)
                    path.append(edge.child)
                    stack.append(iter(self._adj[edge.child]))

    def find_cycles(self) -> list[list[str]]:
        """Нормализованные циклы обратных рёбер, включая недостижимые компоненты."""
        cycles: dict[tuple[str, ...], list[str]] = {}
        for event, value in self._walk_all():
            if event == "cycle":
                assert isinstance(value, list)
                cycles.setdefault(tuple(value), value)
        return list(cycles.values())

    def topological_order(self) -> list[str]:
        order: list[str] = []
        for event, value in self._walk_all():
            if event == "cycle":
                assert isinstance(value, list)
                raise HierarchyCycleError(value)
            assert isinstance(value, str)
            order.append(value)
        return order

    def validate(self, opts: CheckOptions | None = None) -> list[Diagnostic]:
        from netlist.checker import cycle_check, hierarchy_check

        if opts is None:
            opts = CheckOptions()
        return (hierarchy_check.check(self._view, opts, walker=self)
                + cycle_check.check(self._view, opts, walker=self))
