"""NetCheck, § 4.8. Запуск: python -m unittest tests.test_shifrin_net"""
import copy
import gc
import unittest
import weakref
from types import SimpleNamespace as Row
from unittest.mock import Mock, patch

from src.checker import net_check as nc
from src.core.model import CheckOptions, PinRef, Severity, SourceLoc


def loc(line):
    return SourceLoc("a.net", line)


def pin(name, net, line):
    return Row(name=name, net=net, loc=loc(line))


def inst(name, target, pins=(), kind="sub"):
    return Row(name=name, type_name=target, pins=lambda: pins, kind=kind, loc=loc(2))


def block(name, pins=(), instances=(), orphans=()):
    return Row(name=name, pins=lambda: pins, instances=lambda: instances,
               orphan_connections=lambda: orphans)


class NetCheckTest(unittest.TestCase):
    def test_local_errors_and_first_connection(self):
        orphan = Row(ref=PinRef("missing", "q"), net="pair", loc=loc(10))
        b = block("B", [pin("p", "solo", 5), pin("p", "unknown", 6),
                        pin("free", None, 7)], [inst("i", "T", [pin("q", "pair", 8)])],
                  [orphan])
        index = Mock()
        index.nets.return_value = {"empty": [Row(loc=loc(1)), Row(loc=loc(3))],
                                   "solo": [Row(loc=loc(2))], "pair": [Row(loc=loc(4))]}
        view = Row(blocks=lambda: [b])
        diags = nc.check(view, index=index)
        self.assertEqual([(d.code, d.obj, d.loc.line) for d in diags],
                         [("E603", "empty", 1), ("W604", "solo", 2),
                          ("E602", "empty", 3), ("E601", "p", 6)])
        self.assertTrue(all(d.block == "B" and d.check == "net" for d in diags))
        self.assertEqual([d.severity for d in diags],
                         [Severity.ERROR, Severity.WARNING, Severity.ERROR, Severity.ERROR])
        self.assertIn("1", diags[2].message)
        self.assertIn("3", diags[2].message)
        self.assertEqual(nc.check(view, CheckOptions(max_errors=2), index), diags[:3])

    def test_flat_nets_only_dangling_reachable_instances(self):
        child = block("B", [pin("a", "n", 3), pin("b", "n", 4)])
        parent = block("top", instances=[inst("open", "B", [pin("a", None, 5)]),
                                         inst("ok", "B", [pin("b", "s", 6)]),
                                         inst("absent", "unknown"), inst("prim", "B", kind="prim"),
                                         inst("loop", "top")])
        unused = block("unused", instances=[inst("hidden", "B")])
        index = Mock()
        index.nets.side_effect = lambda b: {"n": [Row(loc=loc(1))]} if b is child else {}
        view = Row(blocks=lambda: [unused, parent, child])
        for flat, top, expected in [(False, "top", []), (True, "missing", []),
                                    (True, "top", ["open"]), (True, "unused", ["hidden"])]:
            with self.subTest(flat=flat, top=top):
                ds = nc.check(view, CheckOptions(flat_nets=flat, top=top), index)
                warnings = [d for d in ds if d.code == "W605"]
                self.assertEqual([d.obj for d in warnings], expected)
                for d in warnings:
                    self.assertEqual((d.block, d.loc, d.severity), (top, loc(2), Severity.WARNING))
                    for part in (f"{top}/{d.obj}", "'n'", "'B'"):
                        self.assertIn(part, d.message)

    @patch.object(nc, "ModelIndex", autospec=True)
    def test_empty_and_default_index(self, constructor):
        view = Row(blocks=lambda: [])
        self.assertEqual(nc.check(view), [])
        constructor.assert_called_once_with(view)

    def test_boundary_cache_and_lifetime(self):
        b = block("B", [pin("p", "n", 1)])
        index = Mock()
        index.nets.return_value = {"n": [Row(loc=loc(2))]}
        cache = nc._BoundaryNets(index)
        self.assertEqual(cache.get(b), {"n": ["p"]})
        self.assertEqual(cache.get(b), {"n": ["p"]})
        index.nets.assert_called_once_with(b)
        record = nc._Conn(PinRef(None, "p"), "n", loc(1))
        self.assertEqual(copy.copy(record), record)
        refs = [weakref.ref(cache), weakref.ref(record)]
        del cache, record
        gc.collect()
        self.assertEqual([ref() for ref in refs], [None, None])


if __name__ == "__main__":
    unittest.main()
