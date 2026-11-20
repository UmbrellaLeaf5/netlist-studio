import unittest
from types import SimpleNamespace

from netlist.checker import model_utils
from netlist.core.model import PinRef, SourceLoc
from tests.core.fakes import (
    FakeBlock,
    FakeInstance,
    FakeNet,
    FakePin,
    FakePrimitive,
    FakeView,
    loc,
)


class ModelUtilsTest(unittest.TestCase):
    def test_indexes_and_duplicates(self):
        first = FakeBlock("main")
        second = FakeBlock("main")
        view = FakeView([first, second, FakeBlock("other")])

        self.assertEqual(model_utils.index_blocks(view),
                         {"main": [first, second], "other": [view.block_list[2]]})
        self.assertEqual(model_utils.find_duplicates(view.blocks(), lambda b: b.name),
                         {"main": [first, second]})

    def test_connections_and_pin_lookup(self):
        pin_loc = loc(2)
        inst_loc = loc(3)
        orphan_loc = loc(4)
        instance = FakeInstance(
            "x", "prim", "D", pin_list=[FakePin("a", "n", inst_loc), FakePin("b")]
        )
        orphan = SimpleNamespace(ref=PinRef("missing", "q"), net="n", loc=orphan_loc)
        block = SimpleNamespace(
            pins=lambda: [FakePin("out", "n", pin_loc), FakePin("free")],
            instances=lambda: [instance],
            orphan_connections=lambda: [orphan],
        )

        self.assertEqual(
            model_utils.pins_of_net(block),
            {"n": [(PinRef(None, "out"), pin_loc),
                   (PinRef("x", "a"), inst_loc),
                   (PinRef("missing", "q"), orphan_loc)]},
        )
        self.assertEqual(model_utils.net_of_pin(block, PinRef("x", "a")), "n")
        self.assertIsNone(model_utils.net_of_pin(block, PinRef("x", "b")))

    def test_names_types_and_iteration(self):
        child = FakeBlock("child", pin_list=[FakePin("a"), FakePin("b")])
        primitive = FakePrimitive("D", ["p"])
        sub = FakeInstance("s", "sub", "child", pin_list=[FakePin("a")])
        prim = FakeInstance("d", "prim", "D")
        unknown = FakeInstance("u", "other", "D")
        main = FakeBlock("main", instance_list=[sub, prim, unknown])
        view = FakeView([main, child], [primitive])

        self.assertEqual(model_utils.block_pin_names(child), ["a", "b"])
        self.assertEqual(model_utils.instance_pin_names(sub), ["a"])
        self.assertIs(model_utils.resolve_type(view, sub), child)
        self.assertIs(model_utils.resolve_type(view, prim), primitive)
        self.assertIsNone(model_utils.resolve_type(view, unknown))
        self.assertEqual(list(model_utils.iter_instances(view)),
                         [(main, sub), (main, prim), (main, unknown)])

    def test_model_index_cache_and_reset(self):
        block = FakeBlock("main", net_list=[FakeNet("a")], pin_list=[FakePin("p", "a")])
        view = FakeView([block], [FakePrimitive("D")])
        index = model_utils.ModelIndex(view)

        nets = index.nets(block)
        pins = index.pins_of_net(block)
        block.net_list.append(FakeNet("b"))
        self.assertIs(index.nets(block), nets)
        self.assertIs(index.pins_of_net(block), pins)
        self.assertEqual(set(nets), {"a"})
        self.assertEqual(set(index.blocks), {"main"})
        self.assertEqual(set(index.primitives), {"D"})

        index.init(view)
        self.assertEqual(set(index.nets(block)), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
