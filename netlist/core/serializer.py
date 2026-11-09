"""Детерминированная сериализация принятой модели. Автор: Н. Бучилкин."""

from netlist.core.model import Block, Netlist


class NetlistSerializer:
    """Формирует текст; сохранением на диск занимается FileManager."""

    def __init__(self, indent: str = "  ") -> None:
        self.indent = indent

    def serialize(self, model: Netlist, header: str | None = None) -> str:
        sections: list[str] = []
        if header:
            sections.append("\n".join(f"# {line}" for line in header.splitlines()))
        primitives = [
            " ".join(["primitive", prim.name, *prim.pin_names])
            for prim in model.primitives()
        ]
        if primitives:
            sections.append("\n".join(primitives))
        sections.extend("\n".join(self.serialize_block(b)) for b in model.blocks())
        return "\n\n".join(sections) + "\n"

    def serialize_block(self, block: Block) -> list[str]:
        lines = [f"block {block.name}"]
        for pin in block.pins():
            net = "-" if pin.net is None else pin.net
            lines.append(f"{self.indent}pin {pin.name} {net}")
        for net in block.nets():
            lines.append(f"{self.indent}net {net.name}")
        for inst in block.instances():
            lines.append(f"{self.indent}inst {inst.name} {inst.kind} {inst.type_name}")
            for pin in inst.pins():
                net = "-" if pin.net is None else pin.net
                lines.append(f"{self.indent}{inst.name}.{pin.name} {net}")
        lines.append("end")
        return lines
