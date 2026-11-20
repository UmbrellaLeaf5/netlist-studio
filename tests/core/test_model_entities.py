"""Unit-тесты сущностей принятой модели NetlistModel."""

import copy
import unittest

from src.core.model import Block, InstanceKind, ModelError, Net, Pin, Primitive, SourceLoc


class DataEntityTests(unittest.TestCase):
    def test_pin_defaults_and_value_equality(self):
        self.assertEqual(Pin("p"), Pin("p", None, SourceLoc(None, None)))
        self.assertNotEqual(Pin("p", "n1"), Pin("p", "n2"))

    def test_net_value_equality(self):
        self.assertEqual(Net("n1"), Net("n1"))
        self.assertNotEqual(Net("n1"), Net("n2"))

    def test_primitive_defaults_and_value_equality(self):
        self.assertEqual(Primitive("r"), Primitive("r", [], SourceLoc(None, None)))
        self.assertNotEqual(Primitive("r", ["a"]), Primitive("r", ["b"]))

    def test_data_entities_support_independent_deep_copies(self):
        original_pin = Pin("p", "n", SourceLoc("x.net", 1))
        original_net = Net("n", SourceLoc("x.net", 2))
        original_primitive = Primitive("r", ["a", "b"], SourceLoc("x.net", 3))

        pin_copy = copy.deepcopy(original_pin)
        net_copy = copy.deepcopy(original_net)
        primitive_copy = copy.deepcopy(original_primitive)

        self.assertEqual(pin_copy, original_pin)
        self.assertIsNot(pin_copy, original_pin)
        self.assertEqual(net_copy, original_net)
        self.assertIsNot(net_copy, original_net)
        self.assertEqual(primitive_copy, original_primitive)
        self.assertIsNot(primitive_copy, original_primitive)
        self.assertIsNot(primitive_copy.pin_names, original_primitive.pin_names)

        primitive_copy.pin_names.append("c")
        self.assertEqual(original_primitive.pin_names, ["a", "b"])


class BlockPinTests(unittest.TestCase):
    def setUp(self):
        self.block = Block("main")
        self.block.add_net("n1")
        self.block.add_net("n2")

    def test_add_pin_returns_pin_and_preserves_order(self):
        first = self.block.add_pin("a", "n1")
        second = self.block.add_pin("b", "n2")
        self.assertIsInstance(first, Pin)
        self.assertEqual(self.block.pins(), [first, second])

    def test_add_unconnected_pin(self):
        pin = self.block.add_pin("a")
        self.assertIsNone(pin.net)

    def test_duplicate_pin_is_rejected_without_mutation(self):
        original = self.block.add_pin("a", "n1")
        with self.assertRaises(ModelError):
            self.block.add_pin("a", "n2")
        self.assertEqual(self.block.pins(), [original])
        self.assertEqual(original.net, "n1")

    def test_unknown_net_is_rejected_without_adding_pin(self):
        with self.assertRaises(ModelError):
            self.block.add_pin("a", "missing")
        self.assertEqual(self.block.pins(), [])

    def test_remove_pin_and_unknown_pin(self):
        self.block.add_pin("a", "n1")
        self.block.add_pin("b", "n2")
        self.block.remove_pin("a")
        self.assertEqual([p.name for p in self.block.pins()], ["b"])
        with self.assertRaises(KeyError):
            self.block.remove_pin("absent")

    def test_rename_pin_keeps_order_and_connection(self):
        pin = self.block.add_pin("a", "n1")
        self.block.add_pin("b", "n2")
        self.block.rename_pin("a", "renamed")
        self.assertIs(self.block.pins()[0], pin)
        self.assertEqual((pin.name, pin.net), ("renamed", "n1"))
        self.assertEqual([p.name for p in self.block.pins()], ["renamed", "b"])

    def test_rename_pin_to_same_name_is_allowed(self):
        pin = self.block.add_pin("a", "n1")
        self.block.rename_pin("a", "a")
        self.assertEqual(pin.name, "a")

    def test_rename_missing_or_occupied_pin_is_atomic(self):
        a = self.block.add_pin("a", "n1")
        b = self.block.add_pin("b", "n2")
        with self.assertRaises(KeyError):
            self.block.rename_pin("missing", "c")
        with self.assertRaises(ModelError):
            self.block.rename_pin("a", "b")
        self.assertEqual(self.block.pins(), [a, b])
        self.assertEqual([p.name for p in self.block.pins()], ["a", "b"])

    def test_connect_pin_replaces_connection(self):
        pin = self.block.add_pin("a", "n1")
        self.block.connect_pin("a", "n2")
        self.assertEqual(pin.net, "n2")
        self.block.connect_pin("a", None)
        self.assertIsNone(pin.net)

    def test_connect_unknown_net_preserves_old_connection(self):
        pin = self.block.add_pin("a", "n1")
        with self.assertRaises(ModelError):
            self.block.connect_pin("a", "missing")
        self.assertEqual(pin.net, "n1")

    def test_connect_missing_pin_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.block.connect_pin("missing", "n1")

    def test_pins_returns_new_list_with_live_pin_objects(self):
        pin = self.block.add_pin("a", "n1")
        result = self.block.pins()
        result.clear()
        self.assertEqual(self.block.pins(), [pin])
        self.assertIs(self.block.pins()[0], pin)


class BlockNetTests(unittest.TestCase):
    def setUp(self):
        self.block = Block("main")

    def test_add_net_returns_net_and_preserves_order(self):
        first = self.block.add_net("n1")
        second = self.block.add_net("n2")
        self.assertIsInstance(first, Net)
        self.assertEqual(self.block.nets(), [first, second])

    def test_duplicate_net_is_rejected_without_mutation(self):
        first = self.block.add_net("n1")
        with self.assertRaises(ModelError):
            self.block.add_net("n1")
        self.assertEqual(self.block.nets(), [first])

    def test_remove_net_disconnects_block_and_instance_pins(self):
        self.block.add_net("n1")
        self.block.add_net("n2")
        external = self.block.add_pin("a", "n1")
        inst = self.block.add_instance("x1", "prim", "r")
        internal = inst.add_pin("p", "n1")
        untouched = inst.add_pin("q", "n2")
        self.block.remove_net("n1")
        self.assertEqual([n.name for n in self.block.nets()], ["n2"])
        self.assertIsNone(external.net)
        self.assertIsNone(internal.net)
        self.assertEqual(untouched.net, "n2")

    def test_remove_missing_net_does_not_change_connections(self):
        self.block.add_net("n1")
        pin = self.block.add_pin("a", "n1")
        with self.assertRaises(KeyError):
            self.block.remove_net("missing")
        self.assertEqual(pin.net, "n1")
        self.assertEqual([n.name for n in self.block.nets()], ["n1"])

    def test_rename_net_updates_all_owner_pins_and_keeps_order(self):
        self.block.add_net("n1")
        self.block.add_net("n2")
        external = self.block.add_pin("a", "n1")
        inst = self.block.add_instance("x1", "prim", "r")
        internal = inst.add_pin("p", "n1")
        self.block.rename_net("n1", "renamed")
        self.assertEqual([n.name for n in self.block.nets()], ["renamed", "n2"])
        self.assertEqual(external.net, "renamed")
        self.assertEqual(internal.net, "renamed")

    def test_rename_net_conflict_is_atomic(self):
        self.block.add_net("n1")
        self.block.add_net("n2")
        pin = self.block.add_pin("a", "n1")
        with self.assertRaises(ModelError):
            self.block.rename_net("n1", "n2")
        self.assertEqual([n.name for n in self.block.nets()], ["n1", "n2"])
        self.assertEqual(pin.net, "n1")

    def test_rename_missing_net_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.block.rename_net("missing", "new")

    def test_rename_net_to_same_name_is_allowed(self):
        net = self.block.add_net("n1")
        pin = self.block.add_pin("a", "n1")
        self.block.rename_net("n1", "n1")
        self.assertIs(self.block.nets()[0], net)
        self.assertEqual(pin.net, "n1")

    def test_nets_returns_new_list(self):
        net = self.block.add_net("n1")
        result = self.block.nets()
        result.clear()
        self.assertEqual(self.block.nets(), [net])


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.block = Block("main")
        self.block.add_net("n1")
        self.block.add_net("n2")
        self.instance = self.block.add_instance("x1", "sub", "amp")

    def test_creation_from_string_and_enum_kinds(self):
        self.assertEqual(self.instance.kind, InstanceKind.SUB)
        other = self.block.add_instance("x2", InstanceKind.PRIM, "r")
        self.assertEqual(other.kind, InstanceKind.PRIM)
        self.assertEqual(other.type_name, "r")

    def test_invalid_kind_does_not_create_instance(self):
        with self.assertRaises(ModelError):
            self.block.add_instance("bad", "unknown", "type")
        self.assertEqual([i.name for i in self.block.instances()], ["x1"])

    def test_set_type_updates_kind_and_type_name(self):
        self.instance.set_type("prim", "resistor")
        self.assertEqual(self.instance.kind, InstanceKind.PRIM)
        self.assertEqual(self.instance.type_name, "resistor")

    def test_set_type_invalid_kind_preserves_previous_values(self):
        before = (self.instance.kind, self.instance.type_name)
        with self.assertRaises(ModelError):
            self.instance.set_type("bad", "other")
        self.assertEqual((self.instance.kind, self.instance.type_name), before)

    def test_instance_pin_operations(self):
        p = self.instance.add_pin("a", "n1")
        self.instance.add_pin("b", "n2")
        self.instance.connect_pin("a", "n2")
        self.assertEqual(p.net, "n2")
        self.instance.rename_pin("a", "renamed")
        self.assertEqual([pin.name for pin in self.instance.pins()],
                         ["renamed", "b"])
        self.instance.remove_pin("b")
        self.assertEqual([pin.name for pin in self.instance.pins()], ["renamed"])

    def test_instance_rejects_unknown_net(self):
        with self.assertRaises(ModelError):
            self.instance.add_pin("a", "missing")
        self.assertEqual(self.instance.pins(), [])

    def test_instance_pins_use_containing_block_nets(self):
        other = Block("other")
        other.add_net("same")
        with self.assertRaises(ModelError):
            self.instance.add_pin("a", "same")

    def test_instance_pin_duplicate_and_missing_errors(self):
        self.instance.add_pin("a", "n1")
        with self.assertRaises(ModelError):
            self.instance.add_pin("a", "n2")
        with self.assertRaises(KeyError):
            self.instance.remove_pin("missing")
        with self.assertRaises(KeyError):
            self.instance.rename_pin("missing", "x")
        with self.assertRaises(KeyError):
            self.instance.connect_pin("missing", "n1")


class BlockInstanceCollectionTests(unittest.TestCase):
    def setUp(self):
        self.block = Block("main")

    def test_add_instance_preserves_order(self):
        a = self.block.add_instance("a", "sub", "A")
        b = self.block.add_instance("b", "prim", "R")
        self.assertEqual(self.block.instances(), [a, b])

    def test_duplicate_instance_name_is_rejected(self):
        self.block.add_instance("x", "sub", "A")
        with self.assertRaises(ModelError):
            self.block.add_instance("x", "prim", "R")
        self.assertEqual([i.name for i in self.block.instances()], ["x"])

    def test_remove_instance_removes_its_pins_only(self):
        self.block.add_net("n")
        first = self.block.add_instance("x", "sub", "A")
        first.add_pin("p", "n")
        second = self.block.add_instance("y", "prim", "R")
        second.add_pin("q", "n")
        self.block.remove_instance("x")
        self.assertEqual(self.block.instances(), [second])
        self.assertEqual([pin.name for pin in second.pins()], ["q"])
        self.assertEqual([net.name for net in self.block.nets()], ["n"])

    def test_remove_missing_instance_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.block.remove_instance("missing")

    def test_rename_instance_keeps_type_pins_and_order(self):
        first = self.block.add_instance("x", "sub", "A")
        first.add_pin("p")
        second = self.block.add_instance("y", "prim", "R")
        self.block.rename_instance("x", "renamed")
        self.assertIs(self.block.instances()[0], first)
        self.assertEqual((first.name, first.type_name), ("renamed", "A"))
        self.assertEqual([i.name for i in self.block.instances()],
                         ["renamed", "y"])
        self.assertEqual([p.name for p in first.pins()], ["p"])

    def test_rename_instance_conflict_is_atomic(self):
        first = self.block.add_instance("x", "sub", "A")
        second = self.block.add_instance("y", "prim", "R")
        with self.assertRaises(ModelError):
            self.block.rename_instance("x", "y")
        self.assertEqual(self.block.instances(), [first, second])
        self.assertEqual(first.name, "x")

    def test_rename_missing_instance_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.block.rename_instance("missing", "new")

    def test_instance_lookup(self):
        instance = self.block.add_instance("x", "sub", "A")
        self.assertIs(self.block.instance("x"), instance)
        with self.assertRaises(KeyError):
            self.block.instance("missing")

    def test_instances_returns_new_list(self):
        instance = self.block.add_instance("x", "sub", "A")
        result = self.block.instances()
        result.clear()
        self.assertEqual(self.block.instances(), [instance])

    def test_orphan_connections_are_always_empty_in_accepted_model(self):
        self.assertEqual(self.block.orphan_connections(), [])


class EqualityTests(unittest.TestCase):
    def test_instances_compare_by_value_without_owner_identity(self):
        left = Block("left")
        right = Block("right")
        a = left.add_instance("x", "sub", "amp")
        b = right.add_instance("x", "sub", "amp")
        a.loc = b.loc = SourceLoc("x.net", 2)
        self.assertEqual(a, b)

    def test_instances_differ_when_pin_order_or_content_differs(self):
        left = Block("b")
        right = Block("b")
        a = left.add_instance("x", "sub", "amp")
        b = right.add_instance("x", "sub", "amp")
        a.add_pin("p")
        a.add_pin("q")
        b.add_pin("q")
        b.add_pin("p")
        self.assertNotEqual(a, b)

    def test_blocks_compare_contents_and_order(self):
        a = Block("b")
        b = Block("b")
        a.add_net("n1")
        a.add_net("n2")
        b.add_net("n2")
        b.add_net("n1")
        self.assertNotEqual(a, b)

    def test_blocks_with_same_content_are_equal(self):
        a = Block("b")
        b = Block("b")
        for block in (a, b):
            block.add_net("n")
            block.add_pin("p", "n")
            block.add_instance("x", "prim", "r").add_pin("a", "n")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
