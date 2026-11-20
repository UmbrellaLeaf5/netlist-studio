# Netlist Studio

Netlist Studio — проект для работы с иерархическими нетлистами электрических и логических схем. Нетлист описывает блоки, экземпляры подсхем и примитивов, их пины и соединяющие их цепи независимо от графического изображения схемы.

По [техническому заданию](docs/technical-requirements.md) проект включает три компонента:

- **Оболочку** с графическим интерфейсом для запуска остальных компонентов.
- **Редактор** для создания схем, сохранения нетлиста, графического представления и истории изменений.
- **Чекер** — консольную программу для поиска ошибок в нетлисте.

Целевые платформы — Windows и Linux. Формат нетлиста и интерфейсы компонентов описаны в [документе по архитектуре](docs/interfaces.md).

## Текущее состояние

Проект находится в разработке. Код распределён между следующими частями:

| Компонент | Реализация |
| --- | --- |
| Ядро (`netlist/core/`) | Принятая модель, протоколы чтения, диагностики, разбор и сериализация нетлиста, обход иерархии. |
| Чекер (`netlist/checker/`) | Черновик для проверок, проверки синтаксиса, иерархии, циклов, подсхем, пинов и цепей, код консольного интерфейса. |
| Редактор (`netlist/editor/`) | Холст схемы, список блоков, панель инструментов, главное окно, управление файлами, автосохранение и история Undo/Redo. |
| Оболочка (`netlist/shell/`) | Главное окно, параметры запуска чекера, настройки и запуск отдельных процессов. |

Компоненты ещё интегрируются в единое приложение. Корневой `main.py` остаётся заготовкой: `uv run main.py` не открывает интерфейс и не запускает проверку файла. Ниже приведён самостоятельный пример использования ядра.

## Установка

Установите Python и [uv](https://docs.astral.sh/uv/getting-started/installation/). Для разработки ориентируйтесь на Python 3.11 или новее, как указано в архитектурном документе.

В корневой папке клонированного репозитория выполните:

```bash
uv sync --locked
```

`uv` создаст виртуальное окружение `.venv` и установит зависимости из `uv.lock`. Активировать окружение вручную не требуется: команды с префиксом `uv run` используют его автоматически.

## Пример работы с моделью

Запустите интерпретатор в корне проекта:

```bash
uv run python
```

В интерактивном сеансе можно создать блок с двумя цепями, подключить экземпляр примитива и получить текстовый нетлист:

```python
from netlist.core.model import InstanceKind, Netlist
from netlist.core.serializer import NetlistSerializer

model = Netlist()
model.add_primitive("diode", ["anode", "cathode"])

main = model.add_block("main")
main.add_net("signal")
main.add_net("return_net")
main.add_pin("input", "signal")
main.add_pin("output", "return_net")

diode = main.add_instance("d1", InstanceKind.PRIM, "diode")
diode.add_pin("anode", "signal")
diode.add_pin("cathode", "return_net")

print(NetlistSerializer().serialize(model))
```

В результате будет напечатан текст с объявлениями `primitive diode`, `block main`, `net signal`, `net return_net`, строкой `inst d1 prim diode` и подключениями пинов. Классы модели проверяют структурные ограничения, например уникальность имён и существование цепи перед подключением пина. Формат `.net` и правила проверок подробно описаны в [документации по интерфейсам](docs/interfaces.md).

## Структура репозитория

```text
netlist/
  core/
    model.py         # публичная точка импорта ядра
    netlist.py       # контейнер определений и принятие состояния
    entities.py      # блоки, экземпляры, пины, цепи и примитивы
    views.py         # протоколы чтения модели и черновиков
    diagnostics.py   # общие типы и диагностики
    errors.py        # ошибки структурных операций
    parser.py        # разбор текста нетлиста
    serializer.py    # текстовое представление нетлиста
    hierarchy.py     # обход иерархии
  checker/
    checker_parser.py  cli.py  model_utils.py
    syntax_check.py  hierarchy_check.py  cycle_check.py
    subcircuit_check.py  pin_check.py  net_check.py
  editor/
    main_window.py  canvas.py  block_list.py  tool_palette.py
    file_manager.py  undo_stack.py  autosave.py
  shell/
    main_window.py  checker_dialog.py  settings.py  process_runner.py
docs/
  technical-requirements.md
  technical-requirements.pdf
  interfaces.md
  images/
```

Новые зависимости добавляются командой `uv add <пакет>`, удаляются через `uv remove <пакет>`: эти команды обновляют `pyproject.toml` и `uv.lock`.

## Документация

- [Техническое задание в Markdown](docs/technical-requirements.md).
- [Исходное техническое задание в PDF](docs/technical-requirements.pdf).
- [Формат нетлиста, архитектура и интерфейсы модулей](docs/interfaces.md).
