"""CheckerCLI, § 4.9 и 6.3. Запуск: python -m unittest tests.test_shifrin_cli"""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from netlist.checker import cli
from netlist.core.model import CheckOptions, Diagnostic, Severity, SourceLoc


def diag(code, line, file="design"):
    level = Severity.WARNING if code.startswith("W") else Severity.ERROR
    return Diagnostic(code, level, "net", "сообщение", SourceLoc(file, line))


class CLITest(unittest.TestCase):
    def setUp(self):
        def replace(obj, name, **kwargs):
            p = patch.object(obj, name, **kwargs)
            self.addCleanup(p.stop)
            return p.start()
        self.names = ["syntax", "hierarchy", "cycle", "subckt", "pin", "net"]
        self.assertEqual(list(cli.CHECKS), self.names)
        self.modules = {name: SimpleNamespace(check=Mock(return_value=[]), CODES={})
                        for name in self.names}
        replace(cli, "CHECKS", new=self.modules)
        self.read = replace(cli.file_manager, "read_text", return_value="source")
        self.write = replace(cli.file_manager, "write_text_atomic")
        self.loaded = SimpleNamespace(draft=object(), diagnostics=[])
        self.load = replace(cli.checker_parser, "load", return_value=self.loaded)
        self.index = replace(cli, "ModelIndex")
        self.walker = replace(cli, "HierarchyWalker")
        self.out, self.err = io.StringIO(), io.StringIO()

    def execute(self, *argv):
        args = cli.build_arg_parser().parse_args(argv)
        return cli.run(args, self.out, self.err)

    def test_argument_defaults_and_selection(self):
        parser = cli.build_arg_parser()
        args = parser.parse_args(["design"])
        self.assertEqual((args.checks, args.top, args.format, args.max_errors),
                         (self.names, "main", "text", 0))
        self.assertEqual(parser.parse_args(["-c", "net, pin,net"]).checks, ["pin", "net"])

    def test_main_usage_and_help(self):
        for argv, code in [([], 2), (["-c", ""], 2), (["-c", "xxx"], 2),
                           (["--max-errors", "-1"], 2), (["--max-errors", "n"], 2),
                           (["-f", "xml"], 2), (["--help"], 0), (["--version"], 0)]:
            with self.subTest(argv=argv), redirect_stdout(self.out), redirect_stderr(self.err):
                self.assertEqual(cli.main(argv), code)
        self.read.assert_not_called()
        with patch.object(cli, "run", return_value=3) as run:
            self.assertEqual(cli.main(["design"]), 3)
            self.assertEqual(run.call_args.args[0].file, "design")

    def test_no_input_and_code_table(self):
        self.assertEqual(self.execute(), 2)
        self.assertTrue(self.err.getvalue())
        self.modules["net"].CODES = {"W605": "dangling", "E601": "unknown"}
        self.assertEqual(self.execute("--list-codes"), 0)
        self.assertEqual([line.split() for line in self.out.getvalue().splitlines()],
                         [["E601", "net", "unknown"], ["W605", "net", "dangling"]])
        self.read.assert_not_called()

    def test_shared_dependencies_selection_order_and_partial_parse(self):
        self.read.side_effect = ["input", "library"]
        self.loaded.diagnostics = [diag("E101", 9, "lib")]
        calls = Mock()
        for name in self.names:
            calls.attach_mock(self.modules[name].check, name)
        self.modules["net"].check.return_value = [diag("W604", 3), diag("E601", 1)]
        self.assertEqual(self.execute("design", "-p", "lib", "--top", "top", "--flat-nets",
                                      "--lenient-primitives", "-c", "net,syntax", "-f", "json"), 1)
        self.assertEqual(self.read.call_args_list, [call("design"), call("lib")])
        self.load.assert_called_once_with("input", "design", "library", "lib")
        self.index.assert_called_once_with(self.loaded.draft)
        self.walker.assert_called_once_with(self.loaded.draft)
        self.assertEqual([entry[0] for entry in calls.mock_calls], ["syntax", "net"])
        opts = calls.mock_calls[0].args[1]
        self.assertEqual(opts, CheckOptions(top="top", flat_nets=True, strict_primitives=False))
        for entry in calls.mock_calls:
            self.assertIs(entry.args[0], self.loaded.draft)
            self.assertIs(entry.args[1], opts)
            self.assertEqual(entry.kwargs, dict(index=self.index.return_value,
                                                walker=self.walker.return_value))
        report = json.loads(self.out.getvalue())
        self.assertEqual([d["code"] for d in report["diagnostics"]], ["E601", "W604", "E101"])

    def test_warning_policy(self):
        self.loaded.diagnostics = [diag("E101", 1)]
        self.modules["net"].check.return_value = [diag("W604", 2)]
        for flags, code, warnings in [([], 0, 1), (["-W"], 1, 1),
                                      (["--no-warnings", "-W"], 0, 0)]:
            with self.subTest(flags=flags):
                self.out = io.StringIO()
                self.assertEqual(self.execute("design", "-c", "net", "-f", "json", *flags), code)
                data = json.loads(self.out.getvalue())
                self.assertEqual((data["errors"], data["warnings"]), (0, warnings))

    def test_global_limit_stops_before_later_check(self):
        self.loaded.diagnostics = [diag("E101", 1)]
        self.modules["pin"].check.return_value = [diag("W504", 2), diag("E501", 3), diag("E502", 4)]
        self.assertEqual(self.execute("design", "-c", "syntax,pin,net",
                                      "--max-errors", "2", "-f", "json"), 1)
        self.modules["net"].check.assert_not_called()
        self.assertEqual([d["code"] for d in json.loads(self.out.getvalue())["diagnostics"]],
                         ["E101", "W504", "E501"])

    def test_read_and_write_errors(self):
        for results in [[OSError("design")], ["source", OSError("library")]]:
            self.read.side_effect = results
            self.assertEqual(self.execute("design", "-p", "lib"), 3)
            self.assertIn(str(results[-1]), self.err.getvalue())
            self.load.assert_not_called()
        self.read.side_effect = None
        self.assertEqual(self.execute("design", "-o", "report"), 0)
        self.write.assert_called_once_with("report", "Итого: 0 ошибок, 0 предупреждений\n")
        self.assertEqual(self.out.getvalue(), "")
        self.write.side_effect = OSError("disk full")
        self.assertEqual(self.execute("design", "-o", "report"), 3)
        self.assertIn("disk full", self.err.getvalue())

    def test_report_formats_and_summary(self):
        d = diag("E601", 3)
        for quiet in (False, True):
            out = io.StringIO()
            cli.report([d], "text", out, "design", quiet=quiet)
            expected = [] if quiet else [d.format()]
            self.assertEqual(out.getvalue().splitlines(), expected + ["Итого: 1 ошибка, 0 предупреждений"])
            out = io.StringIO()
            cli.report([d], "json", out, "design", quiet=quiet)
            self.assertEqual(json.loads(out.getvalue()), dict(file="design", errors=1,
                             warnings=0, diagnostics=[d.to_dict()]))
        with self.assertRaises(ValueError):
            cli.report([], "xml", self.out, "design")
        for n, expected in [(2, "2 ошибки"), (11, "11 ошибок"), (21, "21 ошибка")]:
            out = io.StringIO()
            cli.report([d] * n, "text", out, "design", quiet=True)
            self.assertEqual(out.getvalue(), f"Итого: {expected}, 0 предупреждений\n")


if __name__ == "__main__":
    unittest.main()
