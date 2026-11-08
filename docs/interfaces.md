# Проект «Нетлист». Интерфейсы модулей

Содержание:

0. Общие соглашения
1. Формат файла нетлиста, файлы схематика и истории
2. Ядро редактора: NetlistModel, NetlistParser, NetlistSerializer, HierarchyWalker
3. Редактор: FileManager, UndoStack, Autosave, Canvas, BlockList, ToolPalette, EditorMainWindow
4. Чекер: ModelUtils, CheckerParser, SyntaxCheck, HierarchyCheck, CycleCheck, SubcircuitCheck, PinCheck, NetCheck, CheckerCLI
5. Оболочка: Settings, ProcessRunner, CheckerDialog, MainWindow
6. Пользовательские интерфейсы: GUI оболочки, GUI редактора, CLI чекера
7. Коды ошибок
8. Сквозной сценарий в вызовах интерфейсов
9. Сводная таблица согласований

---

## 0. Общие соглашения

**Язык и версии.** Python 3.11+, аннотации типов обязательны в публичных функциях. GUI на Qt.
Пока не выбрано между PySide6 и PyQt6, сигналы в документе записаны как `Signal(...)`
(в PyQt6 это `pyqtSignal`). Тесты: `pytest` для логики, `pytest-qt` для интерфейса.

**Структура пакетов.** Разбиение на файлы соответствует архитектуре и ответственности модулей:

```
netlist/
  core/      model.py  diagnostics.py  errors.py  views.py  entities.py
             netlist.py  parser.py  serializer.py  hierarchy.py                 # ядро редактора
  editor/    main_window.py  canvas.py  block_list.py  tool_palette.py
             file_manager.py  undo_stack.py  autosave.py
  checker/   cli.py  checker_parser.py  model_utils.py  syntax_check.py
             hierarchy_check.py  cycle_check.py  subcircuit_check.py
             pin_check.py  net_check.py
  shell/     main_window.py  checker_dialog.py  settings.py  process_runner.py
```

Запуск: `python -m netlist.shell`, `python -m netlist.editor`, `python -m netlist.checker`.

**Зависимости по коду (только стрелки финальной архитектуры):**

- `core.diagnostics` и `core.errors` не зависят от модулей проекта.
- `core.views` → `core.diagnostics`.
- `core.entities` → `core.diagnostics`, `core.errors`, `core.views`.
- `core.netlist` → `core.entities`, `core.errors`, `core.views`.
- `core.model` — публичный фасад, который экспортирует типы и классы из
  `diagnostics`, `errors`, `views`, `entities` и `netlist`.
- `core.parser` → `core.model`, `checker.syntax_check`.
- `core.serializer` → `core.model`.
- `core.hierarchy` → `core.model`, `checker.hierarchy_check`, `checker.cycle_check`.
- `checker.hierarchy_check` и `checker.cycle_check` → `core.hierarchy`. Это взаимная зависимость
  с HierarchyWalker. Цикл импорта разрываем так: HierarchyWalker импортирует проверки **внутри
  метода** `validate()`, а не на уровне модуля.
- Все проверки → `checker.model_utils`, `core.model` (только общие типы `NetlistView`, `Diagnostic`, `CheckOptions`).
- `checker.checker_parser` → `core.parser`.
- `checker.cli` → `checker.checker_parser`, все проверки, `editor.file_manager` (только его функции без Qt).
- Оболочка не импортирует ни редактор, ни чекер. Она запускает их как процессы через ProcessRunner.

**Общие типы.** Ошибки и предупреждения во всех модулях описываются одним классом `Diagnostic`.
Все проверки читают нетлист через протокол `NetlistView`. Оба типа и `CheckOptions` объявлены в
модуле NetlistModel (раздел 2.1). Отдельный модуль под них не заводится.

**Исключения.** Исключения бросаются при нарушении контракта вызова (неверный аргумент, нарушение
инварианта модели) и при ошибках ввода-вывода. Ошибки *содержимого* нетлиста исключениями не
считаются: они возвращаются списком `Diagnostic`.

**Масштаб.** Обход иерархии и индексы делаются итеративно, без рекурсии, за O(размер блока).
Глубина иерархии — до десятков уровней, в блоке — до сотен инстансов. Раскрытие иерархии
(flatten) в памяти нигде не выполняется: все проверки идут по определениям блоков.

---

## 1. Формат файла нетлиста

Автор: **Л. Сальникова**. Согласовано: Н. Бучилкин (NetlistParser, NetlistSerializer), М. Шифрин
(CheckerParser, CheckerCLI), Н. Чиркин (SyntaxCheck), Д. Криворучко (NetlistModel).

### 1.1. Общие правила

- Текстовый файл в кодировке ASCII. Рекомендуемое расширение `.net`. Конец строки LF или CRLF.
- Файл читается построчно. Каждая строка относится к одному из типов из п. 1.2.
- Элементы строки разделяются любым количеством пробелов и табуляций. Пробелы и отступы в начале строки ни на что не влияют.
- `#` начинает комментарий до конца строки, в том числе после элементов строки.
- **Идентификатор**: `[A-Za-z_][A-Za-z0-9_]*`, регистр учитывается.
- **Зарезервированные слова** нельзя использовать как идентификаторы: `block`, `end`, `pin`,
  `net`, `inst`, `sub`, `prim`, `primitive`.
- Способ записи — **перечисление пинов**: для каждого пина указана подключённая к нему цепь.
  Знак `-` вместо имени цепи означает, что пин явно не подключён.

### 1.2. Типы строк

| Строка | Где допустима | Смысл |
|---|---|---|
| *(пустая)* | везде | игнорируется |
| `# текст` | везде | комментарий |
| `primitive NAME PIN1 PIN2 ...` | вне блоков | объявление разрешённого примитива и его пинов (порядок не важен) |
| `block NAME` | вне блоков | начало блока |
| `end` | внутри блока | конец блока |
| `pin NAME NET` / `pin NAME -` | внутри блока | внешний пин блока и подключённая к нему цепь |
| `net NAME` | внутри блока | объявление цепи |
| `inst NAME sub BLOCK` | внутри блока | инстанс-подсхема блока `BLOCK` |
| `inst NAME prim PRIMITIVE` | внутри блока | инстанс-примитив |
| `INST.PIN NET` / `INST.PIN -` | внутри блока | пин `PIN` инстанса `INST` и подключённая к нему цепь |

Порядок строк внутри блока свободный. Рекомендуемый порядок (так пишет NetlistSerializer):
`pin`, `net`, затем каждый `inst` вместе со строками своих пинов. Порядок блоков в файле значим:
он совпадает с порядком в списке блоков редактора. Корневой блок называется `main`, должен быть
ровно один, стоять он может в любом месте файла.

### 1.3. Грамматика (EBNF, на уровне строк)

```
file        = { line } ;
line        = [ statement ] [ comment ] EOL ;
comment     = "#" { any-char } ;
statement   = primitive | block-begin | block-end | pin | net | inst | conn ;
primitive   = "primitive" ID { ID } ;
block-begin = "block" ID ;
block-end   = "end" ;
pin         = "pin" ID netref ;
net         = "net" ID ;
inst        = "inst" ID ( "sub" | "prim" ) ID ;
conn        = ID "." ID netref ;          (* без пробелов вокруг точки *)
netref      = ID | "-" ;
ID          = letter_ { letter_ | digit } ;   (* не зарезервированное слово *)
```

Контекстные правила, которые тоже относятся к синтаксису (их проверяет SyntaxCheck):
блоки не вкладываются друг в друга; `end` закрывает открытый блок; строки `pin/net/inst/conn`
стоят только внутри блока; `primitive` стоит только вне блока; к концу файла все блоки закрыты.

### 1.4. Семантические правила (проверяются чекером, см. раздел 7)

1. Имена блоков в файле уникальны, блок `main` присутствует.
2. В пределах блока уникальны имена внешних пинов, цепей и инстансов. Пространства имён раздельные.
3. Всякая цепь, на которую ссылаются `pin` или `INST.PIN`, объявлена строкой `net` в этом же блоке.
4. Инстанс `sub` ссылается на существующий блок, инстанс `prim` — на объявленный примитив.
   Примитивы могут также браться из библиотеки (`--primitives` у чекера).
5. Набор пинов инстанса-подсхемы совпадает с набором внешних пинов её блока, набор пинов
   инстанса-примитива — с объявлением `primitive`.
6. К каждому пину подключено не больше одной цепи, то есть каждый пин упомянут не больше одного раза.
7. Иерархия не содержит циклов.
8. Пины и цепи не должны оставаться неподключёнными (на это выдаются ошибки или предупреждения).

### 1.5. Примеры из ТЗ в этом формате

```
# Пример 1: плоский нетлист
primitive transistor1 p1 p2 p3

block main
  pin a1 n1
  pin a2 n2
  pin a3 n4
  pin a4 n5
  net n1
  net n2
  net n3
  net n4
  net n5
  inst t1 prim transistor1
  t1.p1 n1
  t1.p2 n3
  t1.p3 n4
  inst t2 prim transistor1
  t2.p1 n3
  t2.p2 n2
  t2.p3 n5
end
```

```
# Пример 2: иерархический нетлист
primitive diode1 p1 p2

block main
  pin a1 n1
  pin a2 n2
  net n1
  net n2
  inst b1i1 sub block1
  b1i1.a1 n1
  b1i1.a2 n2
end

block block1
  pin a1 n1
  pin a2 n2
  net n1
  net n2
  inst d1 prim diode1
  d1.p1 n1
  d1.p2 n2
end
```

### 1.6. Файл схематика `<имя>.sch.json`

Пишет и читает редактор: FileManager отвечает за файл, Canvas — за содержимое. Чекер этот файл
не использует. Формат — JSON:

```json
{
  "version": 1,
  "blocks": {
    "main": {
      "frame":     [x, y, w, h],
      "pins":      { "a1": {"side": "left", "offset": 40} },
      "instances": { "t1": {"pos": [x, y], "size": [w, h],
                            "pins": {"p1": {"side": "left", "offset": 10}}} },
      "nets":      { "n1": {"segments": [[x1, y1, x2, y2], ...],
                            "junctions": [[x, y], ...]} }
    }
  }
}
```

`side` принимает значения `left|right|top|bottom`. Сегменты цепей строго горизонтальные или
вертикальные. `junctions` — точки соединения: в них рисуется жирная точка, пересечение без
такой точки считается пересечением без соединения. Если схематика нет или в нём не хватает
объектов, Canvas раскладывает недостающее автоматически.

### 1.7. Файл истории `<имя>.history.json`

Пишет и читает UndoStack. Путь выдаёт FileManager.

```json
{ "version": 1, "netlist_sha256": "<хэш .net на момент записи>",
  "index": 17, "commands": [ {"type": "add_instance", ...}, ... ] }
```

Если `netlist_sha256` не совпадает с хэшем текущего `.net` (файл меняли вне редактора), история
отбрасывается, а пользователь получает предупреждение.

### 1.8. Файл блокировки `<имя>.net.lock`

Создаёт FileManager. Содержит одну строку: `pid=<pid> host=<hostname> time=<ISO-8601>`.

---

## 2. Ядро редактора

### 2.1. NetlistModel — `netlist/core/model.py`

Автор: **Д. Криворучко**. Согласовано: Н. Бучилкин, А. Пронин, Н. Акрамов, а также
М. Шифрин, Н. Чиркин, Л. Сальникова, М. Большаков (они пользуются `NetlistView`, `Diagnostic`,
`CheckOptions`).

Публичный фасад хранит API **принятой** модели нетлиста и общих типов проекта. Реализация
разделена по ответственности: `diagnostics.py` содержит общие типы сообщений и настроек,
`views.py` — протоколы чтения, `entities.py` — блоки, экземпляры, пины, цепи и примитивы,
`errors.py` — `ModelError`, а `netlist.py` — контейнер определений и принятие черновиков.
Старые импорты из `netlist.core.model` сохраняются. Интерфейс, парсер и сериализатор в модели
не используются.

#### Общие типы

```python
class InstanceKind(str, enum.Enum):
    SUB = "sub"
    PRIM = "prim"

class Severity(enum.Enum):
    ERROR = "error"
    WARNING = "warning"
```

```python
@dataclass(frozen=True)
class SourceLoc:
    file: str | None      # путь к файлу; None, если объект создан в редакторе
    line: int | None      # номер строки, начиная с 1; None, если строки нет
```

```python
@dataclass(frozen=True)
class Diagnostic:
    code: str             # "E501", "W604", ... (раздел 7)
    severity: Severity
    check: str            # имя проверки: "syntax", "hierarchy", "cycle", "subckt", "pin", "net", "editor"
    message: str          # текст для человека, по-русски
    loc: SourceLoc
    block: str | None = None   # блок, в котором найдена ошибка
    obj: str | None = None     # объект: "a1", "t1.p2", "n3", ...
    def format(self) -> str
    def to_dict(self) -> dict
```

Одна найденная ошибка или одно предупреждение. Объект неизменяемый. `format()` возвращает строку
вида `file:line: error E501 [pin] текст`. Если `loc.line` равен `None`, номер строки не выводится,
если `loc.file` равен `None`, вместо пути пишется `<editor>`. `to_dict()` возвращает словарь
с теми же полями (severity — строкой). Его используют для JSON-вывода и для тестов.

```python
@dataclass(frozen=True)
class PinRef:
    instance: str | None   # None, если это внешний пин блока
    pin: str
    def __str__(self) -> str   # "a1" или "t1.p2"
```

Ссылка на пин внутри блока.

```python
@dataclass
class CheckOptions:
    top: str = "main"                  # корневой блок
    strict_primitives: bool = True     # необъявленный примитив — ошибка (False — предупреждение)
    flat_nets: bool = False            # искать цепи, неподключённые после раскрытия иерархии
    max_errors: int | None = None      # после стольких ошибок проверка останавливается
```

Параметры проверок. Объект создают CheckerCLI (из аргументов командной строки) и
HierarchyWalker.validate (там используются значения по умолчанию).

#### Протокол чтения `NetlistView`

Единый интерфейс чтения нетлиста. Его реализуют три класса: `Netlist` (принятая модель),
`EditorDraft` (черновик Canvas) и `CheckerDraft` (черновик чекера). Проверки и HierarchyWalker
работают только через этот протокол. **Черновики могут содержать некорректные данные**: дубликаты
имён, ссылки на необъявленные объекты. Поэтому все методы возвращают списки, а не словари, и
порядок соответствует порядку в файле (или порядку создания в редакторе).

```python
class PrimitiveView(Protocol):
    name: str
    pin_names: list[str]
    loc: SourceLoc

class PinView(Protocol):
    name: str
    net: str | None        # None означает «-», пин не подключён
    loc: SourceLoc

class NetView(Protocol):
    name: str
    loc: SourceLoc

class InstanceView(Protocol):
    name: str
    kind: InstanceKind
    type_name: str         # имя блока или примитива
    loc: SourceLoc
    def pins(self) -> list[PinView]        # пины, упомянутые в строках INST.PIN

class OrphanConnection(Protocol):          # строка INST.PIN с необъявленным INST
    ref: PinRef
    net: str | None
    loc: SourceLoc

class BlockView(Protocol):
    name: str
    loc: SourceLoc
    def pins(self) -> list[PinView]                 # внешние пины
    def nets(self) -> list[NetView]
    def instances(self) -> list[InstanceView]
    def orphan_connections(self) -> list[OrphanConnection]

class NetlistView(Protocol):
    source_file: str | None
    def primitives(self) -> list[PrimitiveView]
    def blocks(self) -> list[BlockView]             # в порядке файла
    def find_block(self, name: str) -> BlockView | None      # первый с таким именем
    def find_primitive(self, name: str) -> PrimitiveView | None
```

`find_block` и `find_primitive` возвращают первое вхождение имени. Дубликаты ищут проверки, для
этого у ModelUtils есть индексы со списками.

#### Классы принятой модели

`Netlist`, `Block`, `Instance`, `Pin`, `Net`, `Primitive` реализуют протоколы выше. В отличие
от черновиков, модель всегда **непротиворечива**: имена уникальны, все цепи объявлены, у инстанса
нет повторяющихся пинов, `orphan_connections()` всегда пуст. Если операция нарушает инвариант,
бросается `ModelError(ValueError)`. Поэтому модель сама по себе гарантирует, что принятое
состояние корректно на уровне структуры.

```python
class ModelError(ValueError): ...
```

```python
class Netlist:
    def __init__(self) -> None
    @classmethod
    def from_view(cls, view: NetlistView) -> "Netlist"
    def replace_with(self, view: NetlistView) -> None
    def is_empty(self) -> bool
    # чтение — по протоколу NetlistView
    # изменение:
    def add_primitive(self, name: str, pin_names: list[str]) -> Primitive
    def remove_primitive(self, name: str) -> None
    def add_block(self, name: str, index: int | None = None) -> Block
    def remove_block(self, name: str) -> None
    def rename_block(self, old: str, new: str) -> None
    def move_block(self, name: str, index: int) -> None
    def copy_block(self, src: str, new_name: str, index: int | None = None) -> Block
    def block(self, name: str) -> Block                       # KeyError, если блока нет
```

- `from_view(view)` строит новую модель по любому представлению, обычно по черновику.
  Если в представлении есть нарушения инвариантов, бросает `ModelError`, и в сообщении указан
  первый проблемный объект.
- `replace_with(view)` атомарно заменяет содержимое модели содержимым `view`. Именно так
  черновик **принимается**. Если возникла `ModelError`, модель остаётся прежней.
- `rename_block` переименовывает блок и вместе с ним `type_name` у всех инстансов `sub`,
  которые на него ссылаются.
- `move_block` переставляет блок в порядке файла.
- `copy_block` делает глубокую копию определения блока под новым именем.
- `remove_block` удаляет определение блока. Инстансы, которые на него ссылаются, **не удаляются**:
  принятая модель может временно содержать неполную иерархию, а сообщает об этом HierarchyCheck.

```python
class Block:
    name: str
    loc: SourceLoc
    def add_pin(self, name: str, net: str | None = None) -> Pin
    def remove_pin(self, name: str) -> None
    def rename_pin(self, old: str, new: str) -> None
    def connect_pin(self, name: str, net: str | None) -> None
    def add_net(self, name: str) -> Net
    def remove_net(self, name: str) -> None        # отключает все пины этой цепи
    def rename_net(self, old: str, new: str) -> None   # обновляет ссылки во всех пинах
    def add_instance(self, name: str, kind: InstanceKind | str, type_name: str) -> Instance
    def remove_instance(self, name: str) -> None
    def rename_instance(self, old: str, new: str) -> None
    def instance(self, name: str) -> Instance
```

```python
class Instance:
    name: str; kind: InstanceKind; type_name: str; loc: SourceLoc
    def set_type(self, kind: InstanceKind | str, type_name: str) -> None
    def add_pin(self, name: str, net: str | None = None) -> Pin
    def remove_pin(self, name: str) -> None
    def rename_pin(self, old: str, new: str) -> None
    def connect_pin(self, name: str, net: str | None) -> None
```

`connect_pin` с цепью, которая не объявлена в блоке, бросает `ModelError`.
`Pin`, `Net`, `Primitive` — простые классы данных с полями из протоколов.

---

### 2.2. NetlistParser — `netlist/core/parser.py`

Автор: **Н. Бучилкин**. Согласовано: Н. Акрамов (FileManager), М. Шифрин (CheckerParser),
Н. Чиркин (SyntaxCheck).

Разбирает текст файла и возвращает «сырые» данные. Модель он не строит: из сырых данных
черновик делают Canvas (`EditorDraft`) и CheckerParser (`CheckerDraft`). Синтаксис проверяется
через SyntaxCheck. Строки с синтаксическими ошибками пропускаются, а остальные разбираются,
чтобы чекер мог найти как можно больше ошибок за один запуск.

#### Сырые данные

```python
@dataclass
class RawPrimitive:  name: str; pin_names: list[str]; loc: SourceLoc
@dataclass
class RawPin:        name: str; net: str | None; loc: SourceLoc
@dataclass
class RawNet:        name: str; loc: SourceLoc
@dataclass
class RawInstance:   name: str; kind: str; type_name: str; loc: SourceLoc
@dataclass
class RawConnection: instance: str; pin: str; net: str | None; loc: SourceLoc
@dataclass
class RawBlock:
    name: str; loc: SourceLoc
    pins: list[RawPin]; nets: list[RawNet]
    instances: list[RawInstance]; connections: list[RawConnection]
@dataclass
class RawNetlist:
    source_file: str | None
    primitives: list[RawPrimitive]
    blocks: list[RawBlock]
```

Сырые данные — это записи строк файла, сгруппированные по блокам. Порядок и дубликаты
сохраняются, ссылки не разрешаются.

```python
@dataclass
class ParseResult:
    raw: RawNetlist
    diagnostics: list[Diagnostic]       # синтаксические, check == "syntax"
    @property
    def ok(self) -> bool                # нет диагностик с Severity.ERROR
```

#### Класс

```python
class NetlistParser:
    def __init__(self, syntax_check: "SyntaxCheck | None" = None) -> None
    def parse(self, text: str, filename: str | None = None) -> ParseResult
    def parse_library(self, text: str, filename: str | None = None) -> ParseResult
```

- `__init__`: если `syntax_check` не передан, используется `checker.syntax_check.SyntaxCheck()`.
  Параметр нужен, чтобы в тестах подставлять заглушку.
- `parse(text, filename)`: разбивает текст на строки и вызывает `syntax_check.check_lines(lines, filename)`.
  Номера строк с ошибками запоминает и такие строки пропускает. Остальные строки разбирает с помощью
  `SyntaxCheck.tokenize` и `SyntaxCheck.classify`, поэтому лексика у парсера и проверки одна и та же.
  Каждой записи проставляется `SourceLoc(filename, номер строки)`. Если блок не закрыт, он всё
  равно попадает в результат. Исключений из-за содержимого не бросает. Сложность O(длина текста).
- `parse_library(text, filename)`: то же самое, но в файле допустимы только строки `primitive`,
  комментарии и пустые строки. Используется для библиотеки примитивов (`--primitives`).

---

### 2.3. NetlistSerializer — `netlist/core/serializer.py`

Автор: **Н. Бучилкин**. Согласовано: Н. Акрамов (FileManager), Л. Сальникова (формат).

```python
class NetlistSerializer:
    def __init__(self, indent: str = "  ") -> None
    def serialize(self, model: Netlist, header: str | None = None) -> str
    def serialize_block(self, block: Block) -> list[str]
```

- `serialize(model, header)` переводит **принятую** модель в текст по формату раздела 1. Если
  задан `header`, он выводится строками комментария `# ...` в начале файла. Порядок вывода:
  примитивы, пустая строка, затем блоки в порядке модели, разделённые пустой строкой. Внутри
  блока: `pin`, `net`, затем каждый `inst` со строками своих пинов, в порядке модели. Перевод
  строки — `\n`, в конце файла `\n`. Результат детерминирован. Гарантия обратимости: для любой
  модели `m` выполняется `Netlist.from_view(CheckerDraft(parse(serialize(m)).raw)) == m`, проверяется тестом.
- `serialize_block(block)` возвращает строки одного блока от `block` до `end`. Пригодится для
  предпросмотра и тестов.

Файлы сериализатор не пишет, этим занимается FileManager.

---

### 2.4. HierarchyWalker — `netlist/core/hierarchy.py`

Автор: **Н. Бучилкин**. Согласовано: А. Пронин (редактор), Н. Чиркин (HierarchyCheck),
М. Большаков (CycleCheck).

Обходит иерархию блоков любого `NetlistView` и запускает проверки иерархии. Его используют
редактор (EditorMainWindow при загрузке и Canvas при принятии черновика) и чекер (HierarchyCheck,
CycleCheck).

```python
@dataclass(frozen=True)
class HierEdge:
    parent: str          # имя блока-владельца
    instance: str        # имя инстанса
    child: str           # имя блока-подсхемы (type_name)
    loc: SourceLoc

class HierarchyCycleError(Exception):
    cycle: list[str]     # ["a", "b", "a"]

class HierarchyWalker:
    def __init__(self, view: NetlistView) -> None
    def children(self, block: str) -> list[HierEdge]
    def parents(self, block: str) -> list[HierEdge]
    def missing(self) -> list[HierEdge]
    def iter_dfs(self, root: str = "main") -> Iterator[tuple[HierEdge | None, int]]
    def reachable(self, root: str = "main") -> set[str]
    def find_cycles(self) -> list[list[str]]
    def topological_order(self) -> list[str]
    def validate(self, opts: CheckOptions | None = None) -> list[Diagnostic]
```

- `__init__(view)`: один раз строит граф «блок → инстансы-подсхемы» за O(число инстансов).
  Учитываются только инстансы `kind == "sub"`. Если у блока есть дубликаты, берётся первое
  определение. После изменения `view` нужно создать новый walker.
- `children(block)` — рёбра к подсхемам блока в порядке инстансов. Если блока нет, возвращает пустой список.
- `parents(block)` — все рёбра, у которых `child == block`.
- `missing()` — рёбра на несуществующие блоки (неполнота иерархии).
- `iter_dfs(root)` — итеративный обход в глубину от `root`. Выдаёт пары `(ребро, глубина)`,
  первым идёт `(None, 0)` для самого корня. Блок, уже стоящий на текущем пути, повторно не
  раскрывается, так что на циклической иерархии обход конечен. Уже посещённые в другой ветке
  блоки тоже не раскрываются: обходится каждое **определение**, а не каждый экземпляр.
- `reachable(root)` — множество блоков, достижимых из `root`, включая сам `root`.
- `find_cycles()` — для каждого найденного обратного ребра возвращает цикл как список имён,
  первое и последнее имя совпадают. Цикл нормализуется: начинается с лексикографически
  минимального блока, так что повторов нет. O(V + E).
- `topological_order()` — блоки так, что подсхема всегда идёт раньше блока, который её использует.
  При цикле бросает `HierarchyCycleError`.
- `validate(opts)` — возвращает `HierarchyCheck.check(view, opts, walker=self) + CycleCheck.check(view, opts, walker=self)`.
  Проверки импортируются внутри метода (см. раздел 0). Так редактор при загрузке и сохранении
  проверяет иерархию тем же кодом, что и чекер.

---

## 3. Редактор

### 3.1. FileManager — `netlist/editor/file_manager.py`

Автор: **Н. Акрамов**. Согласовано: А. Пронин (EditorMainWindow, Canvas), М. Шифрин (CheckerCLI).

Отвечает за создание, открытие, сохранение и блокировку файлов. **Модуль на верхнем уровне не
импортирует Qt**, потому что его использует чекер (CheckerCLI) для чтения.

```python
class FileAccessError(OSError): ...          # файл не найден, нет прав, ошибка записи

@dataclass(frozen=True)
class LockInfo:  pid: int; host: str; time: datetime; stale: bool

class FileLockedError(FileAccessError):
    info: LockInfo

@dataclass
class OpenResult:
    path: str
    parse: ParseResult            # сырые данные и синтаксические диагностики
    schematic: dict | None        # содержимое .sch.json или None
    @property
    def ok(self) -> bool          # == parse.ok

@dataclass(frozen=True)
class SaveResult:  path: str; sha256: str
```

#### Функции без состояния (их использует и чекер)

```python
def read_text(path: str) -> str
def write_text_atomic(path: str, text: str) -> str      # возвращает sha256 записанного
def schematic_path(path: str) -> str                     # a/b.net -> a/b.sch.json
def history_path(path: str) -> str                       # a/b.net -> a/b.history.json
def lock_path(path: str) -> str                          # a/b.net -> a/b.net.lock
def file_sha256(path: str) -> str
```

- `read_text(path)` читает файл целиком. Байты декодируются как UTF-8 с `errors="replace"`,
  чтобы не-ASCII символы дошли до SyntaxCheck и попали в отчёт как ошибка E108 с номером строки.
  Если файла нет или нет прав, бросает `FileAccessError`. Блокировки не учитывает: читать
  разрешено всегда.
- `write_text_atomic(path, text)` пишет текст во временный файл рядом с целевым и заменяет
  целевой через `os.replace`. Так при сбое старый файл остаётся целым.

#### Класс `FileManager`

```python
class FileManager:
    def __init__(self, parser: NetlistParser, serializer: NetlistSerializer) -> None
    current_path: str | None          # только чтение
    @property
    def is_open(self) -> bool
    def new(self) -> None
    def open(self, path: str, force_unlock: bool = False) -> OpenResult
    def save(self, model: Netlist, schematic: dict, path: str | None = None) -> SaveResult
    def close(self) -> None
    def check_lock(self, path: str) -> LockInfo | None
```

- `new()` закрывает текущий файл (`close()`) и начинает документ без пути (`current_path = None`).
- `open(path, force_unlock)`:
  1. захватывает блокировку: атомарно создаёт файл `lock_path(path)` (`O_CREAT|O_EXCL`) и
     открывает `.net` с блокировкой ОС (`fcntl.lockf` в Linux, `msvcrt.locking` в Windows),
     чтобы другие программы не открыли его на запись;
  2. читает текст и вызывает `parser.parse`;
  3. читает схематик, если он есть. Повреждённый JSON даёт `schematic = None` и предупреждение
     W701 в `parse.diagnostics`.

  Если файл уже заблокирован, бросает `FileLockedError`. Если блокировку оставил процесс,
  который уже не работает (`stale`), её можно снять, вызвав метод с `force_unlock=True`.
  Если `result.ok == False`, блокировка снимается, а `current_path` не меняется. Решение о
  принятии документа принимает EditorMainWindow.
- `save(model, schematic, path)` сериализует модель и атомарно пишет `.net` и `.sch.json`.
  Если передан `path`, это «сохранить как»: новый файл блокируется, старая блокировка снимается,
  `current_path` меняется. Для нового документа `path` обязателен, иначе `ValueError`. Возвращает
  `SaveResult` с хэшем `.net`, он нужен UndoStack для привязки истории.
- `close()` снимает блокировку и удаляет lock-файл. Можно вызывать повторно.
- `check_lock(path)` — кто держит блокировку файла, или `None`.

---

### 3.2. UndoStack — `netlist/editor/undo_stack.py`

Автор: **Н. Акрамов**. Согласовано: А. Пронин (Canvas, EditorMainWindow).

Неограниченная история изменений объектов Canvas, которая сохраняется между запусками
редактора. Построена на паттерне «команда». Базовый класс и реестр команд объявлены здесь,
конкретные команды пишет автор Canvas. Qt не используется: уведомления идут через обратные вызовы.

```python
class CanvasCommand(abc.ABC):
    type_name: ClassVar[str]                  # ключ в реестре, например "add_instance"
    text: str                                 # для меню: "Добавить инстанс t1"
    @abc.abstractmethod
    def redo(self, canvas: "Canvas") -> None
    @abc.abstractmethod
    def undo(self, canvas: "Canvas") -> None
    @abc.abstractmethod
    def to_dict(self) -> dict
    @classmethod
    @abc.abstractmethod
    def from_dict(cls, data: dict) -> "CanvasCommand"
    def merge_with(self, other: "CanvasCommand") -> bool   # по умолчанию False

def register_command(cls: type[CanvasCommand]) -> type[CanvasCommand]   # декоратор
def command_from_dict(data: dict) -> CanvasCommand                      # по data["type"]
```

Команда хранит всё, что нужно для повтора и отмены: имена, координаты, прежние значения.
Ссылок на живые объекты Qt в ней нет, поэтому она сериализуется в JSON. `merge_with` склеивает
однотипные последовательные команды (например, несколько шагов перетаскивания одного объекта
превращаются в одно «Переместить»).

```python
class UndoStack:
    def __init__(self, canvas: "Canvas") -> None
    def push(self, cmd: CanvasCommand) -> None
    def undo(self) -> bool
    def redo(self) -> bool
    def can_undo(self) -> bool
    def can_redo(self) -> bool
    def undo_text(self) -> str | None
    def redo_text(self) -> str | None
    def begin_macro(self, text: str) -> None
    def end_macro(self) -> None
    def clear(self) -> None
    index: int; count: int                        # только чтение
    def add_listener(self, fn: Callable[[], None]) -> None
    def save(self, path: str, netlist_sha256: str) -> None
    def load(self, path: str, netlist_sha256: str) -> bool
```

- `push(cmd)` выполняет `cmd.redo(canvas)`, отбрасывает ветку redo, кладёт команду в стек (или
  склеивает её с предыдущей) и оповещает слушателей.
- `undo()` и `redo()` возвращают `False`, если делать нечего.
- `begin_macro` и `end_macro` объединяют несколько команд в одну составную, которая отменяется
  целиком. Пример — «Копировать блок».
- `clear()` очищает историю. Вызывается при создании нового документа: история идёт «до момента
  создания файла».
- `add_listener(fn)` — `fn()` вызывается после любого изменения стека. Так ToolPalette
  обновляет доступность кнопок.
- `save(path, sha)` атомарно записывает `.history.json` (формат 1.7).
- `load(path, sha)` загружает историю, только если хэш в файле совпадает с `sha` и все команды
  известны реестру. Тогда восстанавливается и `index`, и redo-хвост. Состояние Canvas при этом
  **не меняется**: считается, что оно уже соответствует `index`. Если что-то не совпало,
  возвращает `False`, а история пустая.

---

### 3.3. Autosave — `netlist/editor/autosave.py`

Автор: **Н. Акрамов**. Согласовано: А. Пронин (EditorMainWindow).

Периодически записывает **последнее принятое** состояние документа через FileManager.
Включается только после успешной загрузки или первого сохранения файла. Для нового документа
без пути выключен.

```python
@dataclass
class DocumentState:
    model: Netlist          # принятая модель
    schematic: dict
    undo_stack: UndoStack

class Autosave(QObject):
    saved = Signal(str)            # путь
    failed = Signal(str)           # текст ошибки
    def __init__(self, file_manager: FileManager,
                 get_state: Callable[[], DocumentState],
                 interval_ms: int = 30_000, parent: QObject | None = None) -> None
    def enable(self) -> None
    def disable(self) -> None
    @property
    def enabled(self) -> bool
    def set_interval(self, ms: int) -> None
    def mark_dirty(self) -> None
    def flush(self) -> bool
```

- `enable()` запускает QTimer. Если `file_manager.current_path is None`, бросает `RuntimeError`.
- `mark_dirty()` вызывается, когда принятая модель изменилась (после `Canvas.commit`).
- По таймеру, а также при вызове `flush()`, если состояние «грязное», выполняется
  `file_manager.save(state.model, state.schematic)` и затем `state.undo_stack.save(history_path, sha)`.
  После успеха испускается `saved`, после ошибки — `failed`, а флаг «грязно» остаётся.
  `flush()` возвращает признак успеха и вызывается при закрытии окна.

---

### 3.4. Canvas — `netlist/editor/canvas.py`

Автор: **А. Пронин**. Согласовано: Н. Акрамов (UndoStack, Autosave), Д. Криворучко (NetlistModel),
Н. Бучилкин (RawNetlist, HierarchyWalker).

Область графического редактирования. **Canvas владеет черновиком** (`EditorDraft`) и графическими
объектами. Изменения пользователя сначала попадают в черновик, а в NetlistModel переносятся
только после проверок (`commit`). Для выбора элементов Canvas использует BlockList, для
инструментов — ToolPalette.

#### `EditorDraft`

Изменяемый черновик, который реализует `NetlistView` (раздел 2.1). В отличие от модели, он
допускает промежуточно некорректные состояния: дубликаты имён после вставки, цепь без пинов
во время рисования. Состоит из `DraftBlock`, `DraftInstance`, `DraftPin`, `DraftNet` с теми же
методами изменения, что и у `Block`/`Instance` из модели, но без проверки инвариантов.

```python
class EditorDraft:                      # implements NetlistView
    @classmethod
    def from_raw(cls, raw: RawNetlist) -> "EditorDraft"
    @classmethod
    def from_model(cls, model: Netlist) -> "EditorDraft"
    @classmethod
    def new_document(cls) -> "EditorDraft"     # пустой блок main
```

#### Класс `Canvas`

```python
ItemId = tuple[str, ...]     # ("main", "inst", "t1"), ("main", "pin", "a1"),
                             # ("main", "ipin", "t1", "p1"), ("main", "net", "n1")

class Tool(enum.Enum):
    SELECT = "select"
    NET = "net"

class Canvas(QGraphicsView):
    draftChanged = Signal()
    currentBlockChanged = Signal(str)
    selectionChanged = Signal(list)          # list[ItemId]
    committed = Signal(bool)                 # удалось ли принять черновик
    statusMessage = Signal(str)

    def __init__(self, model: Netlist, undo_stack: UndoStack,
                 block_list: BlockList, palette: ToolPalette,
                 validator: Callable[[NetlistView], list[Diagnostic]],
                 parent: QWidget | None = None) -> None
```

`validator` передаёт EditorMainWindow: `lambda v: HierarchyWalker(v).validate()`.

**Документ:**

```python
    def new_document(self) -> None
    def load_draft(self, raw: RawNetlist, schematic: dict | None) -> None
    @property
    def draft(self) -> EditorDraft
    def validate(self) -> list[Diagnostic]
    def commit(self) -> list[Diagnostic]
    def schematic(self) -> dict
    def reset_to_model(self) -> None
```

- `new_document()` создаёт черновик с пустым блоком `main` и открывает его. Модель не трогает.
- `load_draft(raw, schematic)` строит черновик из сырых данных. Положение объектов берёт из
  схематика, недостающие объекты раскладывает автоматически. Открывает блок `main`.
- `validate()` возвращает `validator(draft)`, к которому добавлены локальные проверки редактора
  с `check="editor"`: дубликаты имён, повторное подключение пина, ссылка на необъявленную цепь
  (используются те же коды, что у чекера: E404, E503, E601, E602).
  Черновик и модель при этом не меняются. Этим методом пользуется кнопка «Проверить».
- `commit()` вызывает `validate()`. Если ошибок (`ERROR`) нет, вызывает `model.replace_with(draft)`
  и испускает `committed(True)`. Иначе модель остаётся прежней («последнее принятое состояние
  сохраняется») и испускается `committed(False)`. Возвращает список диагностик. Вызывается при
  сохранении и автоматически после каждой завершённой команды, чтобы Autosave писал свежие данные.
- `schematic()` возвращает словарь формата 1.6 по текущему черновику.
- `reset_to_model()` откатывает черновик к принятой модели (`EditorDraft.from_model`).

**Навигация и инструменты:**

```python
    def open_block(self, name: str) -> None
    @property
    def current_block(self) -> str
    def set_tool(self, tool: Tool) -> None
    def selected(self) -> list[ItemId]
    def select(self, ids: list[ItemId]) -> None
```

`open_block` показывает блок и выделяет его в BlockList (`block_list.select(name)`).

**Элементарные операции.** Их вызывают **только** команды из `redo`/`undo`. UI-обработчики сами
черновик не меняют: они создают команду и отправляют её в `undo_stack.push`. Каждая операция
меняет черновик и графику и испускает `draftChanged`.

```python
    # блоки
    def op_add_block(self, name: str, index: int) -> None
    def op_remove_block(self, name: str) -> dict          # снимок для undo
    def op_restore_block(self, snapshot: dict, index: int) -> None
    def op_rename_block(self, old: str, new: str) -> None
    def op_move_block(self, name: str, index: int) -> None
    def op_copy_block(self, src: str, new_name: str, index: int) -> None
    # внешние пины блока
    def op_add_pin(self, block: str, name: str, side: str, offset: float) -> None
    def op_remove_pin(self, block: str, name: str) -> dict
    def op_rename_pin(self, block: str, old: str, new: str) -> None
    def op_move_pin(self, block: str, name: str, side: str, offset: float) -> None
    # инстансы и их пины
    def op_add_instance(self, block: str, name: str, kind: str, type_name: str,
                        pos: tuple[float, float]) -> None
    def op_remove_instance(self, block: str, name: str) -> dict
    def op_restore_instance(self, block: str, snapshot: dict) -> None
    def op_move_instance(self, block: str, name: str, pos: tuple[float, float]) -> None
    def op_set_instance_props(self, block: str, name: str, *, new_name: str | None = None,
                              kind: str | None = None, type_name: str | None = None) -> dict
    def op_add_instance_pin(self, block: str, inst: str, pin: str, side: str, offset: float) -> None
    def op_remove_instance_pin(self, block: str, inst: str, pin: str) -> dict
    def op_rename_instance_pin(self, block: str, inst: str, old: str, new: str) -> None
    # цепи
    def op_add_net(self, block: str, name: str, geometry: dict, pins: list[PinRef]) -> None
    def op_remove_net(self, block: str, name: str) -> dict
    def op_rename_net(self, block: str, old: str, new: str) -> None
    def op_set_net_geometry(self, block: str, name: str, geometry: dict,
                            pins: list[PinRef]) -> dict    # возвращает прежнее состояние
```

Операции удаления возвращают снимок (JSON-совместимый словарь), который команда хранит для
отмены. Конкретные команды (`AddBlockCommand`, `MoveInstanceCommand`, `EditNetCommand` и т. д.)
лежат в этом же модуле и регистрируются через `@register_command`.

**Отрисовка (требования ТЗ):** блок рисуется прямоугольником, внешние пины лежат на его
границе. Инстанс — прямоугольник с пинами на границе. Все именованные объекты подписаны.
Цепь — ветвящаяся ломаная из горизонтальных и вертикальных отрезков. Соединение в точке
пересечения отмечается точкой (`junctions`), пересечение без точки соединением не является.
После редактирования цепь должна соединять несколько пинов. Если она соединяет меньше двух,
выдаётся предупреждение W604.

---

### 3.5. BlockList — `netlist/editor/block_list.py`

Автор: **А. Пронин**. Согласовано: внутри редактора (Canvas, EditorMainWindow).

Подокно, состоящее из двух частей:
1. **Блоки файла.** Список блоков в порядке файла. Блок, открытый в Canvas, всегда выделен.
   Здесь создают, удаляют, копируют, переставляют блоки и редактируют их свойства.
2. **Элементы.** Источник элементов, которые перетаскиваются на поле: «Внешний пин», «Пин
   инстанса», примитивы из файла, блоки файла как подсхемы.

```python
ELEMENT_MIME = "application/x-netlist-element"
# содержимое: JSON {"kind": "pin" | "ipin" | "prim" | "sub", "type": "<имя>"}

class BlockList(QWidget):
    blockActivated = Signal(str)               # пользователь выбрал блок
    createRequested = Signal()
    deleteRequested = Signal(str)
    copyRequested = Signal(str)
    moveRequested = Signal(str, int)           # имя, новый индекс
    propertiesRequested = Signal(str)
    def set_blocks(self, names: list[str]) -> None
    def set_primitives(self, names: list[str]) -> None
    def select(self, name: str) -> None        # без испускания blockActivated
    def current(self) -> str | None
```

BlockList черновик не меняет: он только испускает запросы. Canvas подписан на них и превращает
их в команды UndoStack. После каждого `draftChanged` Canvas вызывает `set_blocks`/`set_primitives`.

---

### 3.6. ToolPalette — `netlist/editor/tool_palette.py`

Автор: **А. Пронин**. Согласовано: внутри редактора.

Панель кнопок действий: «Новый», «Загрузить», «Сохранить», «Проверить», «Отменить»,
«Повторить», а также переключатель режима «Выделение / Цепь».

```python
class ToolPalette(QToolBar):
    newRequested = Signal()
    openRequested = Signal()
    saveRequested = Signal()
    checkRequested = Signal()
    undoRequested = Signal()
    redoRequested = Signal()
    toolChanged = Signal(object)              # Tool
    def set_undo_state(self, enabled: bool, text: str | None) -> None
    def set_redo_state(self, enabled: bool, text: str | None) -> None
    def set_save_enabled(self, enabled: bool) -> None
    def set_tool(self, tool: Tool) -> None     # без испускания toolChanged
```

Canvas подписан на `undoRequested`, `redoRequested` и `toolChanged`. На файловые сигналы
(`new/open/save/check`) подписан EditorMainWindow. В подсказке кнопок Undo/Redo выводится текст
команды.

---

### 3.7. EditorMainWindow — `netlist/editor/main_window.py`

Автор: **А. Пронин**. Согласовано: К. Транович (запуск из оболочки), Н. Акрамов.

Главное окно редактора. Связывает Canvas, FileManager, UndoStack, Autosave и HierarchyWalker.
Состоит из строки меню, ToolPalette, BlockList (dock слева), Canvas (центр) и панели сообщений
(dock снизу, туда выводятся `Diagnostic`).

```python
def main(argv: list[str] | None = None) -> int

class EditorMainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None
    def new_document(self) -> None
    def open_document(self, path: str) -> bool
    def save_document(self) -> bool
    def save_document_as(self, path: str) -> bool
    def check_document(self) -> list[Diagnostic]
    def show_diagnostics(self, diags: list[Diagnostic]) -> None
    def closeEvent(self, event: QCloseEvent) -> None
```

- `main(argv)` — точка входа `python -m netlist.editor [FILE]`. Если `FILE` передан, сразу
  вызывается `open_document`. Возвращает код выхода Qt-приложения.
- `new_document()` вызывает `fm.new()`, `canvas.new_document()`, `undo_stack.clear()`,
  `autosave.disable()`.
- `open_document(path)`: вызывает `fm.open(path)`, при `FileLockedError` показывает диалог.
  Если `not result.ok`, показывает диагностики и возвращает `False`. Иначе вызывает
  `canvas.load_draft(result.parse.raw, result.schematic)` и `canvas.commit()`. Если при
  `commit` нашлись ошибки иерархии или циклов, выполняет `fm.close()` и возвращает `False`:
  документ не принят. При успехе — `undo_stack.load(history_path, file_sha256(path))`,
  `autosave.enable()`, файл добавляется в «Недавние», возвращается `True`.
- `save_document()`: для документа без пути вызывает `save_document_as` через диалог.
  Иначе: `canvas.commit()`. При ошибках они показываются, возвращается `False`. Затем
  `res = fm.save(model, canvas.schematic())` и `undo_stack.save(history_path, res.sha256)`.
- `save_document_as(path)` делает то же с `fm.save(..., path=path)`, после чего вызывает
  `autosave.enable()`.
- `check_document()` вызывает `canvas.validate()` и `show_diagnostics`.
- `closeEvent` при несохранённых изменениях спрашивает пользователя, затем вызывает
  `autosave.flush()`, `fm.close()` и сохраняет настройки.

**Настройки редактора** хранятся в `QSettings("Netlist", "Editor")` и сохраняются между запусками:
`window/geometry`, `window/state`, `files/recent` (до 10), `autosave/interval_ms`,
`canvas/grid_step`, `canvas/snap`.

---

## 4. Чекер

### Общий контракт модулей проверок

Каждый из модулей SyntaxCheck, HierarchyCheck, CycleCheck, SubcircuitCheck, PinCheck, NetCheck
экспортирует:

```python
NAME: str                       # "syntax", "hierarchy", "cycle", "subckt", "pin", "net"
CODES: dict[str, str]           # код -> краткое описание (для --list-codes и документации)
def check(view: NetlistView, opts: CheckOptions = CheckOptions(), **kw) -> list[Diagnostic]
```

Требования к `check`: чистая функция, без ввода-вывода, `view` не меняет. На черновике с любыми
дефектами не падает: если объект не найден, это либо пропуск, либо диагностика. Порядок
результата: по порядку блоков, внутри блока — по номеру строки. Дубликаты одной и той же
ошибки не выдаются. Каждая проверка ищет **только свой** класс ошибок (раздел 7), поэтому
наборы ошибок разных проверок не пересекаются. Пример: инстанс на несуществующий блок даёт
только E201 в HierarchyCheck, а SubcircuitCheck такой инстанс пропускает.

---

### 4.1. ModelUtils — `netlist/checker/model_utils.py`

Автор: **Н. Чиркин**. Согласовано: М. Шифрин, Л. Сальникова, М. Большаков.

Содержит только вспомогательные функции поиска, построения индексов и работы со связями.
Структур данных нетлиста здесь нет: они описаны протоколом `NetlistView`.

```python
def find_duplicates(items: Iterable[T], key: Callable[[T], str]) -> dict[str, list[T]]
```
Возвращает только те ключи, которые встречаются больше одного раза, со всеми элементами в
исходном порядке.

```python
def index_blocks(view: NetlistView) -> dict[str, list[BlockView]]
def index_primitives(view: NetlistView) -> dict[str, list[PrimitiveView]]
def index_instances(block: BlockView) -> dict[str, list[InstanceView]]
def index_nets(block: BlockView) -> dict[str, list[NetView]]
def index_pins(block: BlockView) -> dict[str, list[PinView]]
```
Индексы «имя → все объекты с этим именем». Списки нужны, чтобы видеть дубликаты.
Сложность O(n) по размеру блока.

```python
def pins_of_net(block: BlockView) -> dict[str, list[tuple[PinRef, SourceLoc]]]
```
Для каждого имени цепи, упомянутого в блоке, возвращает подключённые пины: внешние, пины
инстансов и пины из `orphan_connections`. Цепи без пинов в результат не попадают, их находят
сравнением с `index_nets`.

```python
def net_of_pin(block: BlockView, ref: PinRef) -> str | None
def block_pin_names(block: BlockView) -> list[str]
def instance_pin_names(inst: InstanceView) -> list[str]
```

```python
def resolve_type(view: NetlistView, inst: InstanceView) -> BlockView | PrimitiveView | None
```
Находит определение типа инстанса с учётом `kind`. Возвращает `None`, если определения нет.

```python
def iter_instances(view: NetlistView) -> Iterator[tuple[BlockView, InstanceView]]
```

```python
class ModelIndex:
    def __init__(self, view: NetlistView) -> None
    blocks: dict[str, list[BlockView]]               # ленивые, кэшируются
    primitives: dict[str, list[PrimitiveView]]
    def nets(self, block: BlockView) -> dict[str, list[NetView]]
    def pins_of_net(self, block: BlockView) -> dict[str, list[tuple[PinRef, SourceLoc]]]
```
Кэш тех же индексов. CheckerCLI создаёт его один раз и передаёт в проверки аргументом
`index=` (из `**kw`), чтобы индексы не строились заново в каждой проверке.

---

### 4.2. CheckerParser — `netlist/checker/checker_parser.py`

Автор: **М. Шифрин**. Согласовано: Н. Бучилкин (NetlistParser), М. Шифрин (CheckerCLI).

Готовит черновик чекера с помощью общего NetlistParser.

```python
class CheckerDraft:                 # implements NetlistView
    def __init__(self, raw: RawNetlist, library: list[RawPrimitive] | None = None) -> None

@dataclass
class LoadResult:
    draft: CheckerDraft
    diagnostics: list[Diagnostic]   # синтаксические ошибки и ошибки библиотеки

def load(text: str, filename: str | None = None,
         library_text: str | None = None, library_name: str | None = None) -> LoadResult
```

- `CheckerDraft(raw, library)` — неизменяемая обёртка над сырыми данными. Строки `INST.PIN`
  привязываются к первому инстансу с именем `INST` в блоке. Строки с необъявленным `INST`
  попадают в `orphan_connections()`. Примитивы библиотеки добавляются **после** примитивов файла.
- `load(...)` вызывает `NetlistParser().parse(text, filename)` и, если передана библиотека,
  `parse_library(library_text, library_name)`. Затем строит `CheckerDraft`. Возвращает черновик
  даже при синтаксических ошибках: в нём есть всё, что удалось разобрать.

---

### 4.3. SyntaxCheck — `netlist/checker/syntax_check.py`

Автор: **Н. Чиркин**. Согласовано: Н. Бучилкин (NetlistParser), М. Шифрин (CheckerCLI), Л. Сальникова (формат).

Проверяет соответствие текста грамматике из п. 1.3. Его вызывает NetlistParser, так что
синтаксис проверяется при каждой загрузке, и в редакторе, и в чекере. Кроме того, модуль
служит **единственным источником лексических правил**.

```python
NAME = "syntax"
KEYWORDS: frozenset[str]
ID_RE: re.Pattern

class LineKind(enum.Enum):
    BLANK; PRIMITIVE; BLOCK; END; PIN; NET; INST; CONN

def tokenize(line: str) -> list[str]
def classify(tokens: list[str]) -> LineKind | None

class SyntaxCheck:
    def check_lines(self, lines: Sequence[str], filename: str | None = None) -> list[Diagnostic]

def check(view: NetlistView, opts: CheckOptions = CheckOptions(), **kw) -> list[Diagnostic]
```

- `tokenize(line)` отрезает комментарий и делит строку по пробельным символам.
  `"t1.p1 n1"` превращается в `["t1.p1", "n1"]`, точку разбирает уже `classify` и парсер.
- `classify(tokens)` определяет тип строки по первому токену и шаблону `ID.ID`, количество
  элементов не проверяет. Возвращает `None`, если тип не распознан.
- `SyntaxCheck.check_lines(lines, filename)` проходит по строкам, отслеживая, открыт ли блок,
  и выдаёт ошибки E101–E108. O(длина текста).
- Модульная функция `check(view, ...)` выполнена для единообразия контракта и всегда возвращает
  `[]`: синтаксические ошибки уже получены при разборе.

---

### 4.4. HierarchyCheck — `netlist/checker/hierarchy_check.py`

Автор: **Н. Чиркин**. Согласовано: М. Шифрин, Н. Бучилкин (HierarchyWalker).

```python
NAME = "hierarchy"
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          walker: HierarchyWalker | None = None, index: ModelIndex | None = None) -> list[Diagnostic]
```

Ищет неполноту иерархии и смежные ошибки. Список кодов — в разделе 7: E201 (подсхема без блока),
E202 (необъявленный примитив; при `strict_primitives=False` это W202), E203 (дубликат блока),
E204 (нет `opts.top`), E205 (дубликат примитива), W206 (блок недостижим из `top`). Использует
`walker.missing()` и `walker.reachable(opts.top)`. Если `walker` не передан, создаёт
`HierarchyWalker(view)`, импортируя его внутри функции.

### 4.5. CycleCheck — `netlist/checker/cycle_check.py`

Автор: **М. Большаков**. Согласовано: М. Шифрин, Н. Бучилкин (HierarchyWalker).

```python
NAME = "cycle"
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          walker: HierarchyWalker | None = None, index: ModelIndex | None = None) -> list[Diagnostic]
```

Для каждого цикла из `walker.find_cycles()` выдаёт одну ошибку E301. В тексте путь цикла
расписан через инстансы: `main -> b1 (block1) -> x (block2) -> y (block1)`. `loc` указывает
на строку `inst`, которая замыкает цикл. Цикл через несуществующий блок невозможен:
такие рёбра в граф walker'а не попадают.

### 4.6. SubcircuitCheck — `netlist/checker/subcircuit_check.py`

Автор: **Л. Сальникова**. Согласовано: М. Шифрин (CheckerCLI), Н. Чиркин (ModelUtils).

```python
NAME = "subckt"
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None) -> list[Diagnostic]
```

Проверяет, что инстансы соответствуют определениям своих типов. Для каждого инстанса, у
которого `resolve_type` нашёл определение:
- E401: в инстансе нет пина, который есть в определении (`block_pin_names` для `sub`,
  `pin_names` для `prim`). Одна ошибка на каждый отсутствующий пин, `obj = "inst.pin"`.
- E402: у инстанса есть пин, которого нет в определении.
- E403: тип указан неверным видом (`sub` на имя примитива или `prim` на имя блока).
  Это ловится до `resolve_type` по индексам блоков и примитивов.

Кроме того, по блокам:
- E404: дубликат имени инстанса в блоке.
- E405: строка `INST.PIN` ссылается на необъявленный инстанс (`orphan_connections`).

Инстансы с неразрешённым типом пропускаются, их обрабатывает HierarchyCheck. Сложность —
O(суммарного числа пинов инстансов) при множествах имён пинов, которые строятся один раз на
каждое определение.

### 4.7. PinCheck — `netlist/checker/pin_check.py`

Автор: **Л. Сальникова**. Согласовано: М. Шифрин, Н. Чиркин.

```python
NAME = "pin"
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None) -> list[Diagnostic]
```

- E501: внешний пин блока не подключён (`pin a -`).
- E502: пин инстанса явно не подключён (`t1.p1 -`). Пин, который вообще не упомянут, — это E401
  в SubcircuitCheck, а не E502.
- E503: пин подключён больше одного раза: повтор `pin NAME` в блоке или повтор `INST.PIN` у
  одного инстанса. Сообщение указывает обе строки.
- W504 (предупреждение): внешний пин блока-подсхемы в определении подключён к цепи, а в конкретном
  инстансе этот пин явно `-`. Выдаётся на строке инстанса: внутренняя цепь
  блока в этом экземпляре висит в воздухе.

### 4.8. NetCheck — `netlist/checker/net_check.py`

Автор: **М. Шифрин**. Согласовано: Н. Чиркин (ModelUtils).

```python
NAME = "net"
def check(view: NetlistView, opts: CheckOptions = CheckOptions(),
          index: ModelIndex | None = None) -> list[Diagnostic]
```

Ошибки цепей, специфичные для выбранного формата:
- E601: ссылка на цепь, не объявленную в блоке.
- E602: цепь объявлена дважды.
- E603: неподключённая цепь — объявлена, но ни к одному пину не подключена.
- W604: цепь подключена ровно к одному пину.
- W605 (только при `opts.flat_nets`): цепь, которая после раскрытия иерархии окажется
  неподключённой. Такая цепь блока подключена только к внешним пинам блока, и хотя бы в одном
  экземпляре блока все эти пины не подключены или не упомянуты (пример на рис. 8 ТЗ). Ищется
  по определениям за один проход по рёбрам иерархии, без раскрытия. В отчёте указывается
  блок, цепь и путь экземпляра.

### 4.9. CheckerCLI — `netlist/checker/cli.py`

Автор: **М. Шифрин**. Согласовано: К. Транович (CheckerDialog, ProcessRunner), Н. Акрамов
(FileManager), все авторы проверок.

Принимает аргументы командной строки, читает файл через FileManager, вызывает CheckerParser,
последовательно запускает проверки, выводит ошибки и возвращает код завершения.

```python
CHECKS: dict[str, ModuleType]   # порядок: syntax, hierarchy, cycle, subckt, pin, net
EXIT_OK, EXIT_ERRORS, EXIT_USAGE, EXIT_IO = 0, 1, 2, 3

def build_arg_parser() -> argparse.ArgumentParser
def run(args: argparse.Namespace, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int
def report(diags: list[Diagnostic], fmt: str, out: TextIO, filename: str) -> None
def main(argv: list[str] | None = None) -> int     # sys.exit(main())
```

Алгоритм `run`:
1. `text = file_manager.read_text(args.file)`; библиотека (если задана `--primitives`) читается
   так же. При `FileAccessError` сообщение выводится в `err`, возвращается `EXIT_IO`.
2. `res = checker_parser.load(text, args.file, lib_text, lib_name)`. Синтаксические диагностики
   попадают в отчёт, если включена проверка `syntax`.
3. Строится `opts = CheckOptions(...)` и `index = ModelIndex(res.draft)`, создаётся один
   `walker = HierarchyWalker(res.draft)`.
4. Выбранные проверки выполняются по порядку `CHECKS`, каждая получает `opts`, `index` и, если
   ей нужен, `walker`. Синтаксические ошибки **не останавливают** остальные проверки: они
   работают по разобранной части файла. При `--max-errors N` прогон останавливается после N ошибок.
5. Фильтрация (`--no-warnings`), сортировка по номеру строки, `report`.
6. Код возврата: `EXIT_ERRORS`, если есть ошибки (или предупреждения при `-W`), иначе `EXIT_OK`.

Пользовательский интерфейс CLI описан в п. 6.3.

---

## 5. Оболочка

### 5.1. Settings — `netlist/shell/settings.py`

Автор: **К. Транович**. Согласовано: К. Транович (CheckerDialog, ProcessRunner).

Хранит настройки оболочки между запусками через `QSettings("Netlist", "Shell")`: в Linux это
ini-файл, в Windows — реестр.

```python
@dataclass
class CheckerDialogState:
    input_file: str = ""
    checks: list[str] = field(default_factory=lambda: list(ALL_CHECKS))
    primitives_file: str = ""
    top: str = "main"
    flat_nets: bool = False
    warnings_as_errors: bool = False
    no_warnings: bool = False
    output_format: str = "text"        # "text" | "json"
    max_errors: int = 0                # 0 — без ограничения
    extra_args: str = ""

class Settings:
    def __init__(self, qsettings: QSettings | None = None) -> None
    def get(self, key: str, default: Any = None) -> Any
    def set(self, key: str, value: Any) -> None
    def checker_dialog_state(self) -> CheckerDialogState
    def save_checker_dialog_state(self, state: CheckerDialogState) -> None
    def editor_command(self) -> list[str]
    def checker_command(self) -> list[str]
    def sync(self) -> None
```

- `editor_command()` и `checker_command()` возвращают префикс команды запуска. По умолчанию
  `[sys.executable, "-m", "netlist.editor"]` и `[sys.executable, "-m", "netlist.checker"]`.
  Их можно переопределить ключами `paths/editor` и `paths/checker`, например для собранного exe.
- Конструктор принимает `QSettings`, чтобы в тестах подставлять временный ini-файл.

### 5.2. ProcessRunner — `netlist/shell/process_runner.py`

Автор: **К. Транович**. Согласовано: К. Транович, А. Пронин (аргументы редактора), М. Шифрин (аргументы чекера).

Запускает редактор и чекер как отдельные процессы. Ведёт диалог запуска чекера и передаёт
ему вывод.

```python
class ProcessRunner(QObject):
    checkerOutput = Signal(str)
    checkerFinished = Signal(int)          # код выхода чекера
    failedToStart = Signal(str)
    def __init__(self, settings: Settings, parent: QObject | None = None) -> None
    def run_editor(self, file: str | None = None) -> bool
    def open_checker_dialog(self, parent: QWidget) -> None
    def run_checker(self, args: list[str]) -> bool
    def is_checker_running(self) -> bool
    def kill_checker(self) -> None
```

- `run_editor(file)` запускает `editor_command() + ([file] if file else [])` через
  `QProcess.startDetached`, так что редактор живёт независимо от оболочки. Возвращает успех,
  при ошибке испускает `failedToStart`.
- `open_checker_dialog(parent)` создаёт (или поднимает уже открытый) немодальный `CheckerDialog`
  и соединяет его `runRequested` с `run_checker`, а `checkerOutput` и `checkerFinished` — с
  выводом диалога.
- `run_checker(args)` запускает `checker_command() + args` через `QProcess`, объединив stdout
  и stderr. Вывод построчно испускается в `checkerOutput`. Пока чекер работает, повторный
  запуск возвращает `False`.

### 5.3. CheckerDialog — `netlist/shell/checker_dialog.py`

Автор: **К. Транович**. Согласовано: М. Шифрин (параметры CLI).

Немодальный диалог выбора входного файла и параметров командной строки чекера. Перечень
параметров соответствует CLI из п. 6.3.

```python
class CheckerDialog(QDialog):
    runRequested = Signal(list)            # list[str] — аргументы для CheckerCLI
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None
    def set_state(self, state: CheckerDialogState) -> None
    def state(self) -> CheckerDialogState
    def append_output(self, text: str) -> None
    def set_running(self, running: bool) -> None
    def show_exit_code(self, code: int) -> None

def build_args(state: CheckerDialogState) -> list[str]
```

- `build_args(state)` — чистая функция, тестируется без Qt. Пример результата:
  `[input_file, "--checks", "hierarchy,pin", "--primitives", lib, "--format", "text", "-W"]`.
  `extra_args` разбивается через `shlex.split` и дописывается в конец.
- По кнопке «Запустить» диалог проверяет, что файл задан, сохраняет состояние
  (`settings.save_checker_dialog_state`) и испускает `runRequested(build_args(state()))`.
- При открытии состояние берётся из `settings.checker_dialog_state()`, поэтому настройки диалога
  сохраняются между запусками оболочки.
- `show_exit_code` показывает итог: 0 — «ошибок нет», 1 — «найдены ошибки», 2 и 3 — «ошибка запуска».

### 5.4. MainWindow — `netlist/shell/main_window.py`

Автор: **К. Транович**. Согласовано: —.

```python
def main(argv: list[str] | None = None) -> int

class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, runner: ProcessRunner) -> None
```

Три кнопки: «Редактор» (`runner.run_editor()`), «Чекер» (`runner.open_checker_dialog(self)`)
и «Выход» (`close()`). Геометрия окна сохраняется в Settings. На `failedToStart` открывается
окно с сообщением об ошибке.

---

## 6. Пользовательские интерфейсы

### 6.1. GUI оболочки

```
┌ Нетлист ─────────────────┐
│   [  Редактор  ]         │
│   [  Чекер…    ]         │
│   [  Выход     ]         │
└──────────────────────────┘
```

Диалог «Запуск чекера» (немодальный):

| Элемент | Параметр CLI |
|---|---|
| Входной файл `[……] [Обзор…]` | `FILE` |
| Флажки проверок: Синтаксис, Иерархия, Циклы, Подсхемы, Пины, Цепи | `--checks` |
| Библиотека примитивов `[……] [Обзор…]` | `--primitives` |
| Корневой блок `[main]` | `--top` |
| ☐ Цепи после раскрытия иерархии | `--flat-nets` |
| ☐ Предупреждения как ошибки / ☐ Скрыть предупреждения | `-W` / `--no-warnings` |
| Формат вывода (text/json), Макс. ошибок (0 = без ограничения) | `--format`, `--max-errors` |
| Доп. аргументы `[……]` | дописываются как есть |
| Кнопки `[Запустить] [Остановить] [Закрыть]` | |
| Область вывода (моноширинный шрифт, только чтение) и строка итога с кодом выхода | |

Все значения сохраняются между запусками.

### 6.2. GUI редактора

Окно: строка меню, ToolPalette (сверху), BlockList (слева: «Блоки» и «Элементы»), Canvas (в
центре), панель «Сообщения» (снизу; двойной щелчок по сообщению открывает блок и выделяет объект).
В заголовке окна — имя файла, `*` при наличии несохранённых изменений, отметка «автосохранение
вкл/выкл».

**Меню:**

| Меню | Пункты (горячие клавиши) |
|---|---|
| Файл | Новый (Ctrl+N), Открыть… (Ctrl+O), Сохранить (Ctrl+S), Сохранить как… (Ctrl+Shift+S), Недавние ▸, Выход (Ctrl+Q) |
| Правка | Отменить (Ctrl+Z), Повторить (Ctrl+Y, Ctrl+Shift+Z), Копировать (Ctrl+C), Вставить (Ctrl+V), Удалить (Del), Переименовать (F2), Свойства… (Alt+Enter) |
| Блок | Новый блок…, Удалить, Копировать…, Переместить вверх (Ctrl+↑), вниз (Ctrl+↓), Свойства… |
| Инструменты | Выделение (S), Цепь (N) |
| Проверка | Проверить (F5) |
| Настройки | Интервал автосохранения…, Шаг сетки…, Привязка к сетке |
| Справка | Руководство, О программе |

**Действия мышью на Canvas:**
- Перетаскивание элемента из «Элементы» на поле: «Внешний пин» ставится на ближайшую границу
  блока, «Пин инстанса» — на границу инстанса под курсором, примитив или блок создают инстанс.
  Имя предлагается автоматически (`t1`, `t2`, …).
- Щелчок — выделение, Shift — добавить к выделению, рамка — групповое выделение,
  перетаскивание — перемещение (пины перемещаются вдоль границы).
- Двойной щелчок по имени — переименование, по инстансу — свойства (имя, вид sub/prim, тип).
  Двойной щелчок по инстансу-подсхеме с Ctrl открывает её блок.
- Режим «Цепь»: щелчок по пину или существующей цепи начинает цепь, щелчки ставят изломы
  (только горизонтальные и вертикальные отрезки), щелчок по пину или цепи соединяет, Esc или
  правая кнопка завершает. Ответвление от существующей цепи ставит точку соединения.
- Контекстное меню объекта: Переименовать, Свойства, Удалить, Копировать.

**Список блоков:** открытый блок всегда выделен; контекстное меню — Новый, Удалить, Копировать,
Вверх/Вниз, Свойства; переупорядочивание перетаскиванием.

### 6.3. CLI чекера

```
python -m netlist.checker FILE [опции]

  FILE                        входной файл нетлиста
  -c, --checks LIST           список проверок через запятую:
                              syntax,hierarchy,cycle,subckt,pin,net (по умолчанию все)
  -p, --primitives LIBFILE    библиотека примитивов (строки primitive)
      --top NAME              корневой блок (по умолчанию main)
      --flat-nets             искать цепи, неподключённые после раскрытия иерархии (W605)
      --lenient-primitives    необъявленный примитив — предупреждение, а не ошибка
  -W, --warnings-as-errors    предупреждения влияют на код выхода
      --no-warnings           не выводить предупреждения
      --max-errors N          остановиться после N ошибок
  -f, --format {text,json}    формат вывода (по умолчанию text)
  -o, --output FILE           писать отчёт в файл вместо stdout
  -q, --quiet                 только итоговая строка
      --list-codes            вывести таблицу кодов ошибок и выйти
      --version, -h/--help
```

**Вывод `text`** (stdout), одна строка на диагностику, затем итог:

```
design.net:12: error E501 [pin] внешний пин 'a3' блока 'main' не подключён
design.net:30: error E201 [hierarchy] инстанс 'x1' ссылается на несуществующий блок 'block7'
design.net:41: warning W604 [net] цепь 'n9' блока 'block1' подключена только к одному пину
Итого: 2 ошибки, 1 предупреждение
```

**Вывод `json`:** `{"file": "...", "errors": N, "warnings": M, "diagnostics": [Diagnostic.to_dict(), ...]}`.

**Коды выхода:** `0` — ошибок нет; `1` — найдены ошибки (или предупреждения при `-W`);
`2` — неверные аргументы (сообщение argparse в stderr); `3` — файл не читается.

---

## 7. Коды ошибок

Префикс `E` означает ошибку, `W` — предупреждение. Подробные тексты и примеры войдут в документ
«Описание ошибок чекера».

| Код | Проверка | Описание |
|---|---|---|
| E101 | syntax | нераспознанный тип строки |
| E102 | syntax | некорректный идентификатор |
| E103 | syntax | неверное число элементов строки |
| E104 | syntax | строка блока (`pin/net/inst/INST.PIN`) вне блока |
| E105 | syntax | `block` внутри незакрытого блока / блок не закрыт к концу файла |
| E106 | syntax | `end` без открытого блока |
| E107 | syntax | зарезервированное слово вместо идентификатора |
| E108 | syntax | недопустимый (не-ASCII) символ |
| E109 | syntax | `primitive` внутри блока |
| E201 | hierarchy | подсхема ссылается на несуществующий блок (неполнота иерархии) |
| E202 / W202 | hierarchy | примитив не объявлен (не входит в разрешённый набор) |
| E203 | hierarchy | повторное определение блока |
| E204 | hierarchy | нет корневого блока |
| E205 | hierarchy | повторное объявление примитива |
| W206 | hierarchy | блок недостижим из корневого |
| E301 | cycle | цикл в иерархии |
| E401 | subckt | у инстанса нет пина из определения |
| E402 | subckt | у инстанса лишний пин, которого нет в определении |
| E403 | subckt | вид инстанса не соответствует типу (`sub`↔`prim`) |
| E404 | subckt | повторное имя инстанса в блоке |
| E405 | subckt | подключение к необъявленному инстансу |
| E501 | pin | неподключённый внешний пин блока |
| E502 | pin | неподключённый пин инстанса |
| E503 | pin | пин подключён больше одного раза |
| W504 | pin | внутренняя цепь блока «висит» в экземпляре: пин подсхемы не подключён снаружи |
| E601 | net | ссылка на необъявленную цепь |
| E602 | net | повторное объявление цепи |
| E603 | net | неподключённая цепь |
| W604 | net | цепь подключена к единственному пину |
| W605 | net | цепь становится неподключённой после раскрытия иерархии |
| W701 | editor | файл схематика повреждён, используется авторазмещение |
| W702 | editor | история редактирования не соответствует файлу и отброшена |

---

## 8. Сквозной сценарий в вызовах интерфейсов

1. **Оболочка → редактор.** `MainWindow` → `ProcessRunner.run_editor()` → процесс `python -m netlist.editor`.
2. **Загрузка.** `EditorMainWindow.open_document(p)` → `FileManager.open(p)` → `NetlistParser.parse`
   → `SyntaxCheck.check_lines` → `Canvas.load_draft(raw, sch)` → `Canvas.commit()` →
   `validator` = `HierarchyWalker(draft).validate()` → `HierarchyCheck.check` + `CycleCheck.check`
   → `Netlist.replace_with(draft)` → `UndoStack.load` → `Autosave.enable()`.
3. **Редактирование.** Действие мышью → `undo_stack.push(AddInstanceCommand(...))` →
   `cmd.redo(canvas)` → `canvas.op_add_instance(...)` → `draftChanged` → `canvas.commit()` →
   `autosave.mark_dirty()`. Отмена: `ToolPalette.undoRequested` → `UndoStack.undo()` → `cmd.undo(canvas)`.
4. **Сохранение.** `save_document()` → `Canvas.commit()` → `FileManager.save(model, canvas.schematic())`
   → `NetlistSerializer.serialize(model)` → `write_text_atomic` → `UndoStack.save(history_path, sha)`.
5. **Проверка.** Оболочка: `ProcessRunner.open_checker_dialog` → `CheckerDialog.runRequested(build_args(state))`
   → `ProcessRunner.run_checker(args)` → `cli.main(argv)` → `file_manager.read_text` →
   `checker_parser.load` → `NetlistParser.parse` → проверки по `CHECKS` → `report` → код выхода →
   `checkerFinished(code)` → `CheckerDialog.show_exit_code`.

Каждая стрелка этого сценария соответствует ребру на [`images/overall-structure.png`](images/overall-structure.png).

---

## 9. Сводная таблица согласований

| Модуль / документ | Автор | Согласовано с |
|---|---|---|
| Формат файла нетлиста | Сальникова | Бучилкин, Шифрин, Чиркин, Криворучко |
| MainWindow | Транович | — |
| CheckerDialog | Транович | Транович, Шифрин |
| Settings | Транович | Транович |
| ProcessRunner | Транович | Транович, Пронин, Шифрин |
| NetlistModel (+NetlistView, Diagnostic, CheckOptions) | Криворучко | Бучилкин, Пронин, Акрамов, Шифрин, Чиркин, Сальникова, Большаков |
| NetlistParser | Бучилкин | Акрамов, Шифрин, Чиркин |
| NetlistSerializer | Бучилкин | Акрамов, Сальникова |
| HierarchyWalker | Бучилкин | Пронин, Чиркин, Большаков |
| EditorMainWindow | Пронин | Транович, Акрамов |
| ToolPalette | Пронин | Пронин |
| BlockList | Пронин | Пронин |
| Canvas (+EditorDraft, команды) | Пронин | Пронин, Акрамов, Криворучко, Бучилкин |
| FileManager | Акрамов | Пронин, Шифрин |
| UndoStack | Акрамов | Пронин |
| Autosave | Акрамов | Пронин |
| CheckerParser (+CheckerDraft) | Шифрин | Шифрин, Бучилкин |
| SyntaxCheck | Чиркин | Шифрин, Бучилкин, Сальникова |
| HierarchyCheck | Чиркин | Шифрин, Бучилкин |
| SubcircuitCheck | Сальникова | Шифрин, Чиркин |
| PinCheck | Сальникова | Шифрин, Чиркин |
| NetCheck | Шифрин | Шифрин, Чиркин |
| CycleCheck | Большаков | Шифрин, Бучилкин |
| ModelUtils | Чиркин | Чиркин, Шифрин, Сальникова, Большаков |
| CheckerCLI | Шифрин | Транович, Акрамов, Чиркин, Сальникова, Большаков |
