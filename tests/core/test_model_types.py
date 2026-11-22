"""Unit-тесты общих типов NetlistModel"""

from dataclasses import FrozenInstanceError
import unittest

from src.core.model import (
    CheckOptions, Diagnostic, InstanceKind, ModelError, PinRef, Severity,
    SourceLoc,
)


class EnumTests(unittest.TestCase):
    def test_instance_kind_values_and_string_conversion(self):
        self.assertEqual(InstanceKind.SUB.value, "sub")
        self.assertEqual(InstanceKind.PRIM.value, "prim")
        self.assertEqual(str(InstanceKind.SUB), "sub")
        self.assertEqual(str(InstanceKind.PRIM), "prim")
        self.assertEqual(InstanceKind.SUB, "sub")

    def test_invalid_instance_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            InstanceKind("unknown")

    def test_severity_values(self):
        self.assertEqual(Severity.ERROR.value, "error")
        self.assertEqual(Severity.WARNING.value, "warning")


class SourceLocTests(unittest.TestCase):
    def test_known_and_unknown_locations(self):
        self.assertEqual(SourceLoc("design.net", 12),
                         SourceLoc(file="design.net", line=12))
        self.assertEqual(SourceLoc(None, None), SourceLoc(None, None))

    def test_source_location_is_immutable(self):
        loc = SourceLoc("design.net", 4)
        with self.assertRaises(FrozenInstanceError):
            loc.line = 5


class DiagnosticTests(unittest.TestCase):
    def make_diag(self, file="design.net", line=12):
        return Diagnostic(
            "E501", Severity.ERROR, "pin", "внешний пин не подключён",
            SourceLoc(file, line), block="main", obj="a",
        )

    def test_format_with_file_and_line(self):
        self.assertEqual(
            self.make_diag().format(),
            "design.net:12: error E501 [pin] внешний пин не подключён",
        )

    def test_format_without_line(self):
        self.assertEqual(
            self.make_diag(line=None).format(),
            "design.net: error E501 [pin] внешний пин не подключён",
        )

    def test_format_editor_location(self):
        self.assertEqual(
            self.make_diag(file=None, line=7).format(),
            "<editor>:7: error E501 [pin] внешний пин не подключён",
        )

    def test_format_editor_without_line(self):
        self.assertEqual(
            self.make_diag(file=None, line=None).format(),
            "<editor>: error E501 [pin] внешний пин не подключён",
        )

    def test_warning_format(self):
        diag = Diagnostic(
            "W604", Severity.WARNING, "net", "цепь подключена один раз",
            SourceLoc(None, None),
        )
        self.assertEqual(
            diag.format(),
            "<editor>: warning W604 [net] цепь подключена один раз",
        )

    def test_to_dict_has_all_fields_and_string_severity(self):
        diag = self.make_diag()
        self.assertEqual(diag.to_dict(), {
            "code": "E501",
            "severity": "error",
            "check": "pin",
            "message": "внешний пин не подключён",
            "loc": {"file": "design.net", "line": 12},
            "block": "main",
            "obj": "a",
        })

    def test_to_dict_preserves_optional_none_fields(self):
        diag = Diagnostic(
            "E201", Severity.ERROR, "hierarchy", "нет блока",
            SourceLoc(None, None),
        )
        self.assertEqual(diag.to_dict()["block"], None)
        self.assertEqual(diag.to_dict()["obj"], None)
        self.assertEqual(diag.to_dict()["loc"], {"file": None, "line": None})

    def test_to_dict_returns_independent_nested_data(self):
        diag = self.make_diag()
        first = diag.to_dict()
        first["loc"]["line"] = 999
        first["block"] = "changed"
        second = diag.to_dict()
        self.assertEqual(second["loc"]["line"], 12)
        self.assertEqual(second["block"], "main")

    def test_diagnostic_is_immutable(self):
        diag = self.make_diag()
        with self.assertRaises(FrozenInstanceError):
            diag.code = "E999"


class PinRefTests(unittest.TestCase):
    def test_external_pin_string(self):
        self.assertEqual(str(PinRef(None, "out")), "out")

    def test_instance_pin_string(self):
        self.assertEqual(str(PinRef("x1", "p2")), "x1.p2")

    def test_pin_ref_is_immutable(self):
        ref = PinRef("x1", "p1")
        with self.assertRaises(FrozenInstanceError):
            ref.pin = "p2"


class CheckOptionsTests(unittest.TestCase):
    def test_defaults(self):
        opts = CheckOptions()
        self.assertEqual(opts.top, "main")
        self.assertTrue(opts.strict_primitives)
        self.assertFalse(opts.flat_nets)
        self.assertIsNone(opts.max_errors)

    def test_custom_values(self):
        opts = CheckOptions(
            top="root", strict_primitives=False, flat_nets=True, max_errors=3
        )
        self.assertEqual(
            (opts.top, opts.strict_primitives, opts.flat_nets, opts.max_errors),
            ("root", False, True, 3),
        )

    def test_instances_are_independent(self):
        first = CheckOptions()
        second = CheckOptions()
        first.top = "other"
        self.assertEqual(second.top, "main")


class ModelErrorTests(unittest.TestCase):
    def test_model_error_is_value_error(self):
        self.assertTrue(issubclass(ModelError, ValueError))


if __name__ == "__main__":
    unittest.main()
