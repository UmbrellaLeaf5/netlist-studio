"""Unit-тесты контейнера Netlist и принятия NetlistView."""

from dataclasses import dataclass
import unittest

from netlist.core.model import (
    Block, InstanceKind, ModelError, Netlist, SourceLoc,
)
from tests.core.fakes import (
    FakeBlock, FakeInstance, FakeNet, FakePin, FakePrimitive, FakeView, loc,
)


@dataclass
class FakeOrphan:
    ref: object
    net: str | None = None
    loc: SourceLoc = SourceLoc(None, None)


class FakeBlockWithOrphans(FakeBlock):
    def __init__(self, *args, orphans=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._orphans = list(orphans)

    def orphan_connections(self):
        return list(self._orphans)


def sample_view():
    primitive = FakePrimitive("res", ["a", "b"], loc(1))
    block = FakeBlock(
        "main",
        loc(2),
        pin_list=[FakePin("in", "n1", loc(3))],
        net_list=[FakeNet("n1", loc(4)), FakeNet("n2", loc(5))],
        instance_list=[
            FakeInstance(
                "r1", "prim", "res", loc(6),
                [FakePin("a", "n1", loc(7)), FakePin("b", "n2", loc(8))],
            )
        ],
    )
    return FakeView([block], [primitive], source_file="source.net")


class NetlistBasicsTests(unittest.TestCase):
    def test_empty_constructor_has_no_implicit_main(self):
        model = Netlist()
        self.assertIsNone(model.source_file)
        self.assertTrue(model.is_empty())
        self.assertEqual(model.blocks(), [])
        self.assertEqual(model.primitives(), [])
        self.assertIsNone(model.find_block("main"))

    def test_adding_primitive_copies_pin_names(self):
        model = Netlist()
        pins = ["a", "b"]
        primitive = model.add_primitive("res", pins)
        pins.append("c")
        self.assertEqual(primitive.pin_names, ["a", "b"])

    def test_primitive_order_and_lookup(self):
        model = Netlist()
        first = model.add_primitive("r", ["a", "b"])
        second = model.add_primitive("c", ["p", "n"])
        self.assertEqual(model.primitives(), [first, second])
        self.assertIs(model.find_primitive("r"), first)
        self.assertIsNone(model.find_primitive("missing"))

    def test_duplicate_primitive_name_is_rejected(self):
        model = Netlist()
        first = model.add_primitive("r", ["a"])
        with self.assertRaises(ModelError):
            model.add_primitive("r", ["b"])
        self.assertEqual(model.primitives(), [first])

    def test_duplicate_primitive_pin_name_is_rejected_atomically(self):
        model = Netlist()
        with self.assertRaises(ModelError):
            model.add_primitive("r", ["a", "b", "a"])
        self.assertEqual(model.primitives(), [])
        model.add_primitive("r", ["a"])

    def test_block_and_primitive_namespaces_are_independent(self):
        model = Netlist()
        primitive = model.add_primitive("same", ["p"])
        block = model.add_block("same")
        self.assertIs(model.find_primitive("same"), primitive)
        self.assertIs(model.find_block("same"), block)
        self.assertEqual(len(model.blocks()), 1)
        self.assertEqual(len(model.primitives()), 1)

    def test_remove_primitive_and_missing_name(self):
        model = Netlist()
        model.add_primitive("r", ["a"])
        model.remove_primitive("r")
        self.assertIsNone(model.find_primitive("r"))
        with self.assertRaises(KeyError):
            model.remove_primitive("r")

    def test_remove_primitive_keeps_existing_instances(self):
        model = Netlist()
        model.add_primitive("r", ["a"])
        block = model.add_block("main")
        instance = block.add_instance("r1", "prim", "r")
        model.remove_primitive("r")
        self.assertIsNone(model.find_primitive("r"))
        self.assertIs(block.instance("r1"), instance)
        self.assertEqual(instance.type_name, "r")

    def test_is_empty_requires_no_blocks_and_no_primitives(self):
        model = Netlist()
        model.add_primitive("r", [])
        self.assertFalse(model.is_empty())
        model.remove_primitive("r")
        model.add_block("main")
        self.assertFalse(model.is_empty())
        model.remove_block("main")
        self.assertTrue(model.is_empty())


class BlockCollectionTests(unittest.TestCase):
    def setUp(self):
        self.model = Netlist()

    def test_add_block_at_end_and_lookup(self):
        first = self.model.add_block("main")
        second = self.model.add_block("amp")
        self.assertEqual(self.model.blocks(), [first, second])
        self.assertIs(self.model.find_block("main"), first)
        self.assertIs(self.model.block("amp"), second)
        self.assertIsNone(self.model.find_block("missing"))

    def test_duplicate_block_name_is_rejected(self):
        first = self.model.add_block("main")
        with self.assertRaises(ModelError):
            self.model.add_block("main")
        self.assertEqual(self.model.blocks(), [first])

    def test_add_block_at_positive_index(self):
        a = self.model.add_block("a")
        c = self.model.add_block("c")
        b = self.model.add_block("b", 1)
        self.assertEqual(self.model.blocks(), [a, b, c])

    def test_add_block_uses_list_insert_negative_index_rules(self):
        self.model.add_block("a")
        self.model.add_block("b")
        c = self.model.add_block("c", -1)
        self.assertEqual([b.name for b in self.model.blocks()], ["a", "c", "b"])

    def test_add_block_index_beyond_end_appends(self):
        self.model.add_block("a")
        b = self.model.add_block("b", 100)
        self.assertEqual([x.name for x in self.model.blocks()], ["a", "b"])

    def test_remove_block_keeps_instances_that_reference_it(self):
        child = self.model.add_block("child")
        main = self.model.add_block("main")
        instance = main.add_instance("u1", "sub", "child")
        self.model.remove_block("child")
        self.assertIsNone(self.model.find_block("child"))
        self.assertIs(main.instance("u1"), instance)
        self.assertEqual(instance.type_name, "child")

    def test_remove_missing_block_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.model.remove_block("missing")

    def test_rename_block_updates_only_subcircuit_references(self):
        self.model.add_block("old")
        main = self.model.add_block("main")
        sub = main.add_instance("u1", "sub", "old")
        prim = main.add_instance("r1", "prim", "old")
        self.model.add_primitive("old", [])
        self.model.rename_block("old", "new")
        self.assertIsNone(self.model.find_block("old"))
        self.assertIsNotNone(self.model.find_block("new"))
        self.assertEqual(sub.type_name, "new")
        self.assertEqual(prim.type_name, "old")
        self.assertIsNotNone(self.model.find_primitive("old"))

    def test_rename_block_keeps_order_and_object_identity(self):
        a = self.model.add_block("a")
        b = self.model.add_block("b")
        c = self.model.add_block("c")
        self.model.rename_block("b", "renamed")
        self.assertEqual(self.model.blocks(), [a, b, c])
        self.assertIs(self.model.block("renamed"), b)
        self.assertEqual(b.name, "renamed")

    def test_rename_block_conflict_is_atomic(self):
        a = self.model.add_block("a")
        b = self.model.add_block("b")
        main = self.model.add_block("main")
        inst = main.add_instance("u", "sub", "a")
        with self.assertRaises(ModelError):
            self.model.rename_block("a", "b")
        self.assertEqual(self.model.blocks(), [a, b, main])
        self.assertEqual(inst.type_name, "a")

    def test_rename_missing_block_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.model.rename_block("missing", "new")

    def test_move_block_reorders_without_replacing_object(self):
        a = self.model.add_block("a")
        b = self.model.add_block("b")
        c = self.model.add_block("c")
        self.model.move_block("c", 0)
        self.assertEqual(self.model.blocks(), [c, a, b])
        self.assertIs(self.model.block("c"), c)

    def test_move_block_negative_index_uses_list_insert(self):
        a = self.model.add_block("a")
        b = self.model.add_block("b")
        c = self.model.add_block("c")
        self.model.move_block("a", -1)
        self.assertEqual(self.model.blocks(), [b, a, c])

    def test_move_block_large_index_appends(self):
        self.model.add_block("a")
        b = self.model.add_block("b")
        self.model.move_block("b", 100)
        self.assertEqual([x.name for x in self.model.blocks()], ["a", "b"])

    def test_move_missing_block_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.model.move_block("missing", 0)

    def test_block_collection_returns_new_list(self):
        block = self.model.add_block("main")
        result = self.model.blocks()
        result.clear()
        self.assertEqual(self.model.blocks(), [block])


class CopyBlockTests(unittest.TestCase):
    def make_model(self):
        model = Netlist()
        model.add_block("child")
        source = model.add_block("source")
        source.loc = loc(10)
        source.add_net("n1").loc = loc(11)
        source.add_net("n2").loc = loc(12)
        source.add_pin("external", "n1").loc = loc(13)
        inst = source.add_instance("u1", "sub", "child")
        inst.loc = loc(14)
        inst.add_pin("p", "n2").loc = loc(15)
        return model, source

    def test_copy_block_is_deep_and_preserves_order_and_locations(self):
        model, source = self.make_model()
        copied = model.copy_block("source", "copy", 1)
        self.assertEqual([b.name for b in model.blocks()],
                         ["child", "copy", "source"])
        self.assertEqual(copied.name, "copy")
        self.assertEqual(copied.loc, source.loc)
        self.assertEqual(
            [(n.name, n.loc) for n in copied.nets()],
            [(n.name, n.loc) for n in source.nets()],
        )
        self.assertEqual(
            [(pin.name, pin.net, pin.loc) for pin in copied.pins()],
            [(pin.name, pin.net, pin.loc) for pin in source.pins()],
        )
        self.assertEqual(
            [(i.name, i.kind, i.type_name, i.loc,
              [(p.name, p.net, p.loc) for p in i.pins()])
             for i in copied.instances()],
            [(i.name, i.kind, i.type_name, i.loc,
              [(p.name, p.net, p.loc) for p in i.pins()])
             for i in source.instances()],
        )
        self.assertIsNot(copied.nets()[0], source.nets()[0])
        self.assertIsNot(copied.pins()[0], source.pins()[0])
        self.assertIsNot(copied.instances()[0], source.instances()[0])
        self.assertIsNot(copied.instances()[0].pins()[0],
                         source.instances()[0].pins()[0])

    def test_copied_instance_pins_are_owned_by_copied_block(self):
        model, source = self.make_model()
        copied = model.copy_block("source", "copy")
        copied.add_net("copy_only")
        copied.instance("u1").add_pin("extra", "copy_only")
        with self.assertRaises(ModelError):
            source.instance("u1").add_pin("extra", "copy_only")

    def test_copy_block_preserves_type_references_without_recursive_copy(self):
        model, source = self.make_model()
        copied = model.copy_block("source", "copy")
        self.assertEqual(copied.instance("u1").type_name, "child")
        self.assertIs(model.block("child"), model.find_block("child"))

    def test_copy_block_duplicate_name_is_rejected(self):
        model, source = self.make_model()
        with self.assertRaises(ModelError):
            model.copy_block("source", "source")
        self.assertEqual([b.name for b in model.blocks()], ["child", "source"])

    def test_copy_block_missing_source_raises_key_error(self):
        model, _ = self.make_model()
        with self.assertRaises(KeyError):
            model.copy_block("missing", "copy")




class FromViewTests(unittest.TestCase):
    def test_from_view_copies_order_source_file_and_locations(self):
        view = sample_view()
        model = Netlist.from_view(view)
        self.assertEqual(model.source_file, "source.net")
        self.assertEqual([p.name for p in model.primitives()], ["res"])
        self.assertEqual([b.name for b in model.blocks()], ["main"])
        block = model.block("main")
        self.assertEqual(block.loc, loc(2))
        self.assertEqual(block.pins()[0].loc, loc(3))
        self.assertEqual(block.nets()[0].loc, loc(4))
        self.assertEqual(block.instance("r1").loc, loc(6))
        self.assertEqual(block.instance("r1").pins()[1].loc, loc(8))

    def test_from_view_does_not_mutate_source(self):
        view = sample_view()
        original_pin_names = list(view.primitive_list[0].pin_names)
        original_net = view.block_list[0].pin_list[0].net
        Netlist.from_view(view)
        self.assertEqual(view.primitive_list[0].pin_names, original_pin_names)
        self.assertEqual(view.block_list[0].pin_list[0].net, original_net)

    def test_from_view_copies_mutable_records(self):
        view = sample_view()
        model = Netlist.from_view(view)
        view.primitive_list[0].pin_names.append("extra")
        view.block_list[0].net_list[0].name = "changed"
        self.assertEqual(model.primitives()[0].pin_names, ["a", "b"])
        self.assertEqual(model.block("main").nets()[0].name, "n1")

    def test_from_view_accepts_forward_net_references(self):
        block = FakeBlock(
            "main",
            pin_list=[FakePin("p", "later", loc(2))],
            net_list=[FakeNet("later", loc(3))],
        )
        model = Netlist.from_view(FakeView([block]))
        self.assertEqual(model.block("main").pins()[0].net, "later")

    def test_from_view_rejects_duplicate_primitive_names(self):
        view = FakeView(primitives=[
            FakePrimitive("r", ["a"]), FakePrimitive("r", ["b"])
        ])
        with self.assertRaises(ModelError):
            Netlist.from_view(view)

    def test_from_view_rejects_duplicate_primitive_pins(self):
        view = FakeView(primitives=[FakePrimitive("r", ["a", "a"])])
        with self.assertRaises(ModelError):
            Netlist.from_view(view)

    def test_from_view_rejects_duplicate_block_names(self):
        view = FakeView(blocks=[FakeBlock("main"), FakeBlock("main")])
        with self.assertRaises(ModelError):
            Netlist.from_view(view)

    def test_from_view_rejects_duplicate_net_names(self):
        block = FakeBlock("main", net_list=[FakeNet("n"), FakeNet("n")])
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_duplicate_external_pin_names(self):
        block = FakeBlock(
            "main", net_list=[FakeNet("n")],
            pin_list=[FakePin("p", "n"), FakePin("p", "n")],
        )
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_duplicate_instance_names(self):
        block = FakeBlock("main", instance_list=[
            FakeInstance("x", "prim", "r"),
            FakeInstance("x", "prim", "c"),
        ])
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_duplicate_instance_pin_names(self):
        block = FakeBlock("main", instance_list=[
            FakeInstance("x", "prim", "r", pin_list=[
                FakePin("a"), FakePin("a")
            ])
        ])
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_pin_to_undeclared_net(self):
        block = FakeBlock("main", pin_list=[FakePin("p", "missing")])
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_instance_pin_to_undeclared_net(self):
        block = FakeBlock("main", instance_list=[
            FakeInstance("x", "prim", "r", pin_list=[FakePin("a", "missing")])
        ])
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_rejects_orphan_connections(self):
        block = FakeBlockWithOrphans(
            "main", orphans=[FakeOrphan(ref="ghost.p", net="n", loc=loc(4))]
        )
        with self.assertRaises(ModelError):
            Netlist.from_view(FakeView([block]))

    def test_from_view_does_not_check_type_definitions(self):
        block = FakeBlock("main", instance_list=[
            FakeInstance("x", "sub", "not_defined")
        ])
        model = Netlist.from_view(FakeView([block]))
        self.assertEqual(model.block("main").instance("x").type_name,
                         "not_defined")


class ReplaceWithTests(unittest.TestCase):
    def test_replace_with_replaces_all_data(self):
        target = Netlist()
        target.source_file = "old.net"
        target.add_primitive("old_prim", [])
        target.add_block("old_block")
        target.replace_with(sample_view())
        self.assertEqual(target.source_file, "source.net")
        self.assertEqual([p.name for p in target.primitives()], ["res"])
        self.assertEqual([b.name for b in target.blocks()], ["main"])

    def test_replace_with_failure_is_atomic(self):
        target = Netlist()
        target.source_file = "old.net"
        old_primitive = target.add_primitive("old_prim", ["p"])
        old_block = target.add_block("old_block")
        old_pin = old_block.add_pin("p")
        invalid = FakeView(
            [FakeBlock("new", pin_list=[FakePin("bad", "missing")])],
            [FakePrimitive("new_prim", [])],
            source_file="invalid.net",
        )
        with self.assertRaises(ModelError):
            target.replace_with(invalid)
        self.assertEqual(target.source_file, "old.net")
        self.assertEqual(target.primitives(), [old_primitive])
        self.assertEqual(target.blocks(), [old_block])
        self.assertIs(target.block("old_block").pins()[0], old_pin)
        self.assertIsNone(target.find_block("new"))

    def test_replace_with_self_is_safe(self):
        target = Netlist.from_view(sample_view())
        before = Netlist.from_view(target)
        target.replace_with(target)
        self.assertEqual(target.source_file, before.source_file)
        self.assertEqual(
            [(p.name, p.pin_names, p.loc) for p in target.primitives()],
            [(p.name, p.pin_names, p.loc) for p in before.primitives()],
        )
        self.assertEqual(
            [(b.name, b.loc,
              [(p.name, p.net, p.loc) for p in b.pins()],
              [(n.name, n.loc) for n in b.nets()],
              [(i.name, i.kind, i.type_name, i.loc,
                [(p.name, p.net, p.loc) for p in i.pins()])
               for i in b.instances()])
             for b in target.blocks()],
            [(b.name, b.loc,
              [(p.name, p.net, p.loc) for p in b.pins()],
              [(n.name, n.loc) for n in b.nets()],
              [(i.name, i.kind, i.type_name, i.loc,
                [(p.name, p.net, p.loc) for p in i.pins()])
               for i in b.instances()])
             for b in before.blocks()],
        )

    def test_replace_with_success_keeps_old_external_objects_as_old_snapshot(self):
        target = Netlist()
        old_block = target.add_block("old")
        target.replace_with(FakeView([FakeBlock("new")]))
        self.assertIsNone(target.find_block("old"))
        self.assertEqual(old_block.name, "old")
        self.assertEqual([b.name for b in target.blocks()], ["new"])


class EqualityTests(unittest.TestCase):
    def test_equal_models_with_same_order_and_contents(self):
        left = Netlist.from_view(sample_view())
        right = Netlist.from_view(sample_view())
        self.assertEqual(left, right)

    def test_models_differ_by_source_file(self):
        left = Netlist.from_view(sample_view())
        right = Netlist.from_view(sample_view())
        right.source_file = "other.net"
        self.assertNotEqual(left, right)

    def test_models_differ_by_definition_order(self):
        left = Netlist()
        left.add_block("a")
        left.add_block("b")
        right = Netlist()
        right.add_block("b")
        right.add_block("a")
        self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main()
