# Netlist Studio

## Installation (English)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone the repository and enter its directory:

```bash
git clone <repository-url>
cd netlist-studio
```

Create the virtual environment and install the locked dependencies:

```bash
uv sync
```

Run the project with:

```bash
uv run main.py
```

Add a dependency with `uv add <package>`; remove one with `uv remove <package>`. Both commands update `pyproject.toml` and `uv.lock`.

## Documentation

- [Technical requirements (Markdown)](docs/technical-requirements.md)
- [Original technical requirements (PDF)](docs/technical-requirements.pdf)
- [Module interfaces and architecture](docs/interfaces.md)

## Установка (русский)

Установите [uv](https://docs.astral.sh/uv/getting-started/installation/), клонируйте репозиторий и перейдите в его директорию:

```bash
git clone <ссылка-на-репозиторий>
cd netlist-studio
```

Создайте виртуальное окружение и установите зависимости из lock-файла:

```bash
uv sync
```

Запустите проект:

```bash
uv run main.py
```

Добавить зависимость можно командой `uv add <пакет>`, удалить — `uv remove <пакет>`. Обе команды обновляют `pyproject.toml` и `uv.lock`.
