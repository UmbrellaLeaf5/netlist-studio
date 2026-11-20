"""CheckerCLI: консольный чекер нетлиста.

Модуль чекера проекта «Нетлист»: `netlist/checker/cli.py`.
Автор: М. Шифрин. Интерфейс согласовать с К. Трановичем (CheckerDialog,
ProcessRunner), Н. Акрамовым (FileManager) и авторами проверок, см.
interfaces.md, п. 4.9 и п. 6.3.

Запуск: `python -m netlist.checker FILE [опции]` (список опций — в
build_arg_parser и в `--help`).

Как работает `run`
------------------
1. Читает файл нетлиста и, если задана `--primitives`, библиотеку
   примитивов через `file_manager.read_text`. Файл не читается —
   сообщение в `err` и код 3.
2. Строит черновик `checker_parser.load(...)`. Синтаксические ошибки
   (и ошибки библиотеки) попадают в отчёт, если включена проверка
   `syntax`, но остальные проверки не останавливают: те работают по
   разобранной части файла.
3. Создаёт один раз `CheckOptions`, `ModelIndex` и `HierarchyWalker` и
   раздаёт их всем проверкам.
4. Запускает выбранные проверки в порядке `CHECKS`. При `--max-errors N`
   прогон останавливается, как только набрано N ошибок.
5. Убирает предупреждения при `--no-warnings`, сортирует по файлу и
   номеру строки, печатает отчёт.
6. Возвращает код выхода (см. EXIT_*).

Предупреждения, скрытые `--no-warnings`, на код выхода не влияют даже
при `-W`: код всегда соответствует строке «Итого».
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from typing import TYPE_CHECKING, TextIO

from netlist.checker import (
    checker_parser,
    cycle_check,
    hierarchy_check,
    net_check,
    pin_check,
    subcircuit_check,
    syntax_check,
)
from netlist.checker.model_utils import ModelIndex
from netlist.core.hierarchy import HierarchyWalker
from netlist.core.model import CheckOptions, Diagnostic, Severity
from netlist.editor import file_manager

if TYPE_CHECKING:
    from types import ModuleType


__version__ = "0.1.0"

# Имя проверки -> модуль. Порядок словаря и есть порядок запуска.
CHECKS: dict[str, ModuleType] = {
    module.NAME: module
    for module in (syntax_check, hierarchy_check, cycle_check,
                   subcircuit_check, pin_check, net_check)
}

EXIT_OK, EXIT_ERRORS, EXIT_USAGE, EXIT_IO = 0, 1, 2, 3

PROG = "python -m netlist.checker"


# Аргументы командной строки
def build_arg_parser() -> argparse.ArgumentParser:
    """Парсер аргументов по п. 6.3 interfaces.md."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Проверка файла нетлиста на ошибки.",
        epilog="Коды выхода: 0 — ошибок нет; 1 — найдены ошибки (или "
               "предупреждения при -W); 2 — неверные аргументы; "
               "3 — файл не читается.")
    parser.add_argument(
        "file", metavar="FILE", nargs="?",
        help="входной файл нетлиста")
    parser.add_argument(
        "-c", "--checks", metavar="LIST", type=_check_list,
        default=list(CHECKS),
        help="список проверок через запятую: "
             f"{','.join(CHECKS)} (по умолчанию все)")
    parser.add_argument(
        "-p", "--primitives", metavar="LIBFILE",
        help="библиотека примитивов (строки primitive)")
    parser.add_argument(
        "--top", metavar="NAME", default="main",
        help="корневой блок (по умолчанию main)")
    parser.add_argument(
        "--flat-nets", action="store_true",
        help="искать цепи, неподключённые после раскрытия иерархии (W605)")
    parser.add_argument(
        "--lenient-primitives", action="store_true",
        help="необъявленный примитив — предупреждение, а не ошибка")
    parser.add_argument(
        "-W", "--warnings-as-errors", action="store_true",
        help="предупреждения влияют на код выхода")
    parser.add_argument(
        "--no-warnings", action="store_true",
        help="не выводить предупреждения")
    parser.add_argument(
        "--max-errors", metavar="N", type=_non_negative_int, default=0,
        help="остановиться после N ошибок (0 — без ограничения)")
    parser.add_argument(
        "-f", "--format", choices=("text", "json"), default="text",
        help="формат вывода (по умолчанию text)")
    parser.add_argument(
        "-o", "--output", metavar="FILE",
        help="писать отчёт в файл вместо stdout")
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="только итоговая строка (для формата text)")
    parser.add_argument(
        "--list-codes", action="store_true",
        help="вывести таблицу кодов ошибок и выйти")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _check_list(value: str) -> list[str]:
    """`--checks pin,net` -> ["pin", "net"] в порядке CHECKS."""
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"неизвестная проверка: {', '.join(unknown)} "
            f"(допустимы: {','.join(CHECKS)})")
    if not names:
        raise argparse.ArgumentTypeError("список проверок пуст")
    return [name for name in CHECKS if name in names]


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"ожидается целое число, получено '{value}'") from None
    if number < 0:
        raise argparse.ArgumentTypeError(
            f"ожидается число не меньше 0, получено {number}")
    return number


# Запуск
def main(argv: list[str] | None = None) -> int:
    """Точка входа: `sys.exit(main())`.

    Ошибки аргументов argparse печатает в stderr сам; здесь его
    SystemExit превращается в код возврата, чтобы main() можно было
    звать из тестов.
    """
    parser = build_arg_parser()
    try:
        args = parser.parse_args(argv)
        if args.file is None and not args.list_codes:
            parser.error("не указан входной файл FILE")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return run(args)


def run(args: argparse.Namespace, out: TextIO | None = None,
        err: TextIO | None = None) -> int:
    """Выполнить проверку по разобранным аргументам, вернуть код выхода.

    `out` и `err` по умолчанию — sys.stdout и sys.stderr. Они берутся в
    момент вызова, а не при импорте, чтобы их подмена (в тестах, при
    перенаправлении) работала.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if args.list_codes:
        out.write(_codes_table())
        return EXIT_OK
    if args.file is None:
        err.write(f"{PROG}: ошибка: не указан входной файл FILE\n")
        return EXIT_USAGE

    try:
        text = file_manager.read_text(args.file)
        lib_text = (file_manager.read_text(args.primitives)
                    if args.primitives else None)
    except OSError as exc:  # FileAccessError — наследник OSError
        err.write(f"{PROG}: ошибка: {exc}\n")
        return EXIT_IO

    loaded = checker_parser.load(text, args.file, lib_text, args.primitives)
    opts = CheckOptions(
        top=args.top,
        strict_primitives=not args.lenient_primitives,
        flat_nets=args.flat_nets,
        max_errors=args.max_errors or None)
    diags = _run_checks(loaded, args.checks, opts)

    if args.no_warnings:
        diags = [d for d in diags if d.severity is not Severity.WARNING]
    diags = _sorted(diags, args.file)

    buffer = io.StringIO()
    report(diags, args.format, buffer, args.file, quiet=args.quiet)
    try:
        if args.output:
            file_manager.write_text_atomic(args.output, buffer.getvalue())
        else:
            out.write(buffer.getvalue())
    except OSError as exc:
        err.write(f"{PROG}: ошибка: не удалось записать отчёт: {exc}\n")
        return EXIT_IO

    errors, warnings = _count(diags)
    if errors or (args.warnings_as_errors and warnings):
        return EXIT_ERRORS
    return EXIT_OK


def _run_checks(loaded: checker_parser.LoadResult, selected: list[str],
                opts: CheckOptions) -> list[Diagnostic]:
    """Выбранные проверки по порядку CHECKS с учётом opts.max_errors.

    Каждая проверка и сама обрезает свой результат по max_errors, но
    лимит общий на весь прогон, поэтому здесь он применяется ещё раз.
    """
    draft = loaded.draft
    index = ModelIndex(draft)
    walker = HierarchyWalker(draft)
    diags: list[Diagnostic] = []
    for name, module in CHECKS.items():
        if name not in selected:
            continue
        if name == syntax_check.NAME:
            # Синтаксис проверен при разборе; check() вызывается ради
            # единообразия и по контракту возвращает [].
            diags.extend(loaded.diagnostics)
        diags.extend(module.check(draft, opts, index=index, walker=walker))
        if opts.max_errors is not None and \
                _count(diags)[0] >= opts.max_errors:
            return _cut_after_errors(diags, opts.max_errors)
    return diags


# Отчёт
def report(diags: list[Diagnostic], fmt: str, out: TextIO, filename: str,
           *, quiet: bool = False) -> None:
    """Напечатать диагностики в формате `text` или `json`.

    text: одна строка `Diagnostic.format()` на диагностику, затем
    «Итого: ...». При `quiet` — только итоговая строка.
    json: `{"file", "errors", "warnings", "diagnostics"}`; `quiet` на
    него не влияет.
    """
    errors, warnings = _count(diags)
    if fmt == "json":
        json.dump({"file": filename, "errors": errors, "warnings": warnings,
                   "diagnostics": [d.to_dict() for d in diags]},
                  out, ensure_ascii=False, indent=2)
        out.write("\n")
        return
    if fmt != "text":
        raise ValueError(f"неизвестный формат отчёта: {fmt!r}")
    if not quiet:
        for diag in diags:
            out.write(diag.format() + "\n")
    out.write(f"Итого: {_summary(errors, warnings)}\n")


def _summary(errors: int, warnings: int) -> str:
    """'2 ошибки, 1 предупреждение'."""
    return (_plural(errors, "ошибка", "ошибки", "ошибок") + ", " +
            _plural(warnings, "предупреждение", "предупреждения",
                    "предупреждений"))


def _plural(n: int, one: str, few: str, many: str) -> str:
    """'1 ошибка', '2 ошибки', '5 ошибок', '11 ошибок', '21 ошибка'."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def _codes_table() -> str:
    """Таблица для --list-codes: код, проверка, описание."""
    rows = sorted((code, name, text)
                  for name, module in CHECKS.items()
                  for code, text in module.CODES.items())
    return "".join(f"{code:<6} {name:<10} {text}\n"
                   for code, name, text in rows)


# Мелочи
def _count(diags: list[Diagnostic]) -> tuple[int, int]:
    """(число ошибок, число предупреждений)."""
    errors = sum(d.severity is Severity.ERROR for d in diags)
    return errors, len(diags) - errors


def _sorted(diags: list[Diagnostic], filename: str) -> list[Diagnostic]:
    """Сначала входной файл, потом библиотека; внутри — по номеру строки.

    Сортировка устойчивая: на одной строке порядок проверок сохраняется
    (например, W504 остаётся сразу после своей E502).
    """
    return sorted(diags, key=lambda d: (d.loc.file != filename,
                                        d.loc.file or "",
                                        d.loc.line is None,
                                        d.loc.line or 0))


def _cut_after_errors(diags: list[Diagnostic],
                      limit: int) -> list[Diagnostic]:
    """Обрезать список сразу после `limit`-й ошибки."""
    errors = 0
    for i, diag in enumerate(diags):
        if errors >= limit:
            return diags[:i]
        if diag.severity is Severity.ERROR:
            errors += 1
    return diags
