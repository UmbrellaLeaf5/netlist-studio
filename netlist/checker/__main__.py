"""Запуск чекера командой ``python -m netlist.checker``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
