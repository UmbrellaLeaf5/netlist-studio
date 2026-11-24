import unittest

from src.checker import syntax_check
from src.checker.syntax_check import LineKind, SyntaxCheck
from src.core.model import Severity, SourceLoc


class SyntaxCheckTest(unittest.TestCase):
    def test_tokenize_and_classify(self):
        cases = {
            "  # note": LineKind.BLANK,
            "primitive D p": LineKind.PRIMITIVE,
            "block main": LineKind.BLOCK,
            "end": LineKind.END,
            "pin p n": LineKind.PIN,
            "net n": LineKind.NET,
            "inst x sub leaf": LineKind.INST,
            "x.p n": LineKind.CONN,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                tokens = syntax_check.tokenize(line)
                self.assertEqual(syntax_check.classify(tokens), expected)

    def test_valid_document(self):
        lines = [
            "primitive D p",
            "block main",
            "net n",
            "pin out n",
            "inst d prim D",
            "d.p n",
            "end",
        ]
        self.assertEqual(SyntaxCheck().check_lines(lines, "ok.net"), [])
        self.assertEqual(syntax_check.check(object()), [])

    def test_lexical_errors(self):
        cases = [
            ("bad", "E101"),
            ("net", "E103"),
            ("block 7main", "E102"),
            ("block end", "E107"),
            ("# комментарий", "E108"),
        ]
        for line, code in cases:
            with self.subTest(line=line):
                diag = SyntaxCheck().check_lines([line], "bad.net")[0]
                self.assertEqual((diag.code, diag.severity, diag.loc),
                                 (code, Severity.ERROR, SourceLoc("bad.net", 1)))

    def test_block_context_errors(self):
        cases = [
            (["net n"], ["E104"]),
            (["end"], ["E106"]),
            (["block main", "block child", "end"], ["E105"]),
            (["block main", "primitive D p", "end"], ["E109"]),
            (["block main"], ["E105"]),
        ]
        for lines, codes in cases:
            with self.subTest(lines=lines):
                result = SyntaxCheck().check_lines(lines)
                self.assertEqual([diag.code for diag in result], codes)


if __name__ == "__main__":
    unittest.main()
