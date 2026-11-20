"""Проверки черновика, принятия схемы и команд Canvas."""

import json

import pytest

from src.core.model import Diagnostic, Netlist, PinRef, Severity, SourceLoc
from src.core.parser import NetlistParser
from src.editor.block_list import BlockList
from src.editor.canvas import (
    AddInstanceCommand,
    AddNetCommand,
    Canvas,
    EditorDraft,
    MoveInstanceCommand,
    RemoveNetCommand,
)
from tests.core.fakes import make_syntax_module, replaced_modules

SCHEME = """\
primitive resistor p1 p2
block aux
end
block main
  net n1
  pin input n1
  inst r1 prim resistor
  r1.p1 n1
end
"""


def raw(text):
    with replaced_modules(
        {"src.checker.syntax_check": make_syntax_module()}
    ):
        result = NetlistParser().parse(text, filename="example.net")
    assert result.ok, result.diagnostics
    return result.raw


@pytest.fixture
def canvas(qtbot):
    model = Netlist()
    blocks = BlockList()
    qtbot.addWidget(blocks)
    widget = Canvas(model, None, blocks, None, lambda draft: [])
    qtbot.addWidget(widget)
    return widget, model, blocks


def test_draft_keeps_orphan_connection_for_editor_diagnostics():
    draft = EditorDraft.from_raw(raw("block main\nnet n1\nu1.p1 n1\nend\n"))

    [orphan] = draft.block("main").orphan_connections()
    assert (orphan.ref.instance, orphan.ref.pin, orphan.net) == (
        "u1",
        "p1",
        "n1",
    )


def test_draft_edits_do_not_modify_source_model():
    model = Netlist()
    model.add_block("main").add_net("n1")
    draft = EditorDraft.from_model(model)

    draft.block("main").add_net("draft_only")

    assert [net.name for net in model.block("main").nets()] == ["n1"]


def test_new_document_replaces_draft_but_leaves_accepted_model(canvas):
    widget, model, _ = canvas
    widget.load_draft(raw(SCHEME), None)
    model.add_block("accepted")

    widget.new_document()

    assert [b.name for b in widget.draft.blocks()] == ["main"]
    assert [b.name for b in model.blocks()] == ["accepted"]


def test_load_opens_main_even_when_another_block_comes_first(canvas):
    widget, _, blocks = canvas

    widget.load_draft(raw(SCHEME), None)

    assert widget.current_block == "main"
    assert blocks.current() == "main"
    assert widget.schematic()["blocks"]["main"]["instances"]["r1"]["pos"]


def test_open_block_synchronizes_selection_and_clears_previous_selection(
    canvas, qtbot
):
    widget, _, blocks = canvas
    widget.load_draft(raw(SCHEME), None)
    widget.select([("main", "inst", "r1")])
    assert widget.selected() == [("main", "inst", "r1")]

    with qtbot.waitSignal(widget.currentBlockChanged) as change:
        widget.open_block("aux")

    assert change.args == ["aux"]
    assert blocks.current() == "aux"
    assert widget.selected() == []


def test_schematic_restores_explicit_instance_position(canvas):
    widget, _, _ = canvas
    geometry = {
        "version": 1,
        "blocks": {
            "main": {"instances": {"r1": {"pos": [240, 160], "size": [90, 50]}}}
        },
    }

    widget.load_draft(raw(SCHEME), geometry)

    instance = widget.schematic()["blocks"]["main"]["instances"]["r1"]
    assert (instance["pos"], instance["size"]) == ([240, 160], [90, 50])


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("inst u1 sub aux\ninst u1 sub aux", "E404"),
        ("net n1\npin p1 n1\npin p1 n1", "E503"),
        ("net n1\nnet n1", "E602"),
        ("pin p1 missing", "E601"),
        ("u1.p1 -", "E405"),
    ],
    ids=[
        "duplicate-instance",
        "duplicate-pin",
        "duplicate-net",
        "unknown-net",
        "orphan",
    ],
)
def test_validate_reports_local_error_before_accepting_draft(
    canvas, body, code
):
    widget, model, _ = canvas
    widget.load_draft(raw(f"block main\n{body}\nend\n"), None)

    diagnostics = widget.validate()

    assert [diag.code for diag in diagnostics] == [code]
    assert diagnostics[0].severity is Severity.ERROR
    assert model.is_empty()


def test_invalid_commit_keeps_last_accepted_model(canvas, qtbot):
    widget, model, _ = canvas
    model.add_block("main").add_net("accepted")
    widget.load_draft(raw("block main\npin p1 missing\nend\n"), None)

    with qtbot.waitSignal(widget.committed) as committed:
        diagnostics = widget.commit()

    assert committed.args == [False]
    assert [diag.code for diag in diagnostics] == ["E601"]
    assert [net.name for net in model.block("main").nets()] == ["accepted"]


def test_valid_commit_preserves_connections_in_accepted_model(canvas, qtbot):
    widget, model, _ = canvas
    widget.load_draft(raw(SCHEME), None)

    with qtbot.waitSignal(widget.committed) as committed:
        assert widget.commit() == []

    assert committed.args == [True]
    assert model.block("main").instance("r1").pins()[0].net == "n1"


def test_warning_does_not_prevent_commit(qtbot):
    warning = Diagnostic(
        "W604", Severity.WARNING, "editor", "test", SourceLoc(None, None)
    )
    model = Netlist()
    widget = Canvas(model, None, None, None, lambda draft: [warning])
    qtbot.addWidget(widget)

    with qtbot.waitSignal(widget.committed) as committed:
        assert widget.commit() == [warning]

    assert committed.args == [True]
    assert model.find_block("main") is not None


def test_reset_replaces_unaccepted_changes_with_model_state(canvas):
    widget, model, _ = canvas
    model.add_block("main").add_net("accepted")
    widget.load_draft(raw("block main\nnet pending\nend\n"), None)

    widget.reset_to_model()

    assert [net.name for net in widget.draft.block("main").nets()] == [
        "accepted"
    ]


def test_add_instance_command_restores_position_after_undo_and_redo(
    canvas, qtbot
):
    widget, _, _ = canvas
    widget.load_draft(raw("primitive resistor p1 p2\nblock main\nend\n"), None)
    command = AddInstanceCommand("main", "r1", "prim", "resistor", (240, 160))

    with qtbot.waitSignal(widget.draftChanged):
        command.redo(widget)
    command.undo(widget)
    assert not widget.draft.block("main").instances()
    command.redo(widget)

    assert widget.schematic()["blocks"]["main"]["instances"]["r1"]["pos"] == [
        240,
        160,
    ]


def test_serialized_move_command_retains_both_positions(canvas):
    widget, _, _ = canvas
    widget.load_draft(raw(SCHEME), None)
    command = MoveInstanceCommand("main", "r1", (100, 100), (240, 160))
    restored = MoveInstanceCommand.from_dict(
        json.loads(json.dumps(command.to_dict()))
    )

    restored.redo(widget)
    assert widget.schematic()["blocks"]["main"]["instances"]["r1"]["pos"] == [
        240,
        160,
    ]
    restored.undo(widget)

    assert widget.schematic()["blocks"]["main"]["instances"]["r1"]["pos"] == [
        100,
        100,
    ]


def test_undo_remove_net_restores_connections_and_geometry(canvas):
    widget, _, _ = canvas
    widget.load_draft(raw("block main\npin p1 -\npin p2 -\nend\n"), None)
    geometry = {"segments": [[20, 20, 80, 20]], "junctions": []}
    AddNetCommand(
        "main", "n1", geometry, [PinRef(None, "p1"), PinRef(None, "p2")]
    ).redo(widget)
    command = RemoveNetCommand(widget.net_snapshot("main", "n1"))

    command.redo(widget)
    assert [p.net for p in widget.draft.block("main").pins()] == [None, None]
    command.undo(widget)

    assert [p.net for p in widget.draft.block("main").pins()] == ["n1", "n1"]
    assert widget.schematic()["blocks"]["main"]["nets"]["n1"] == geometry


@pytest.mark.parametrize(
    ("block", "instance"),
    [("aux", "r1"), ("main", "r2")],
    ids=["other-block", "other-instance"],
)
def test_moves_of_different_instances_are_not_merged(block, instance):
    first = MoveInstanceCommand("main", "r1", (20, 20), (40, 40))
    second = MoveInstanceCommand(block, instance, (40, 40), (80, 80))

    assert first.merge_with(second) is False


def test_merged_move_undo_returns_to_start_of_drag(canvas):
    widget, _, _ = canvas
    widget.load_draft(raw(SCHEME), None)
    start = tuple(
        widget.schematic()["blocks"]["main"]["instances"]["r1"]["pos"]
    )
    first = MoveInstanceCommand("main", "r1", start, (240, 160))
    second = MoveInstanceCommand("main", "r1", (240, 160), (280, 180))
    first.redo(widget)
    second.redo(widget)
    assert first.merge_with(second)

    first.undo(widget)

    assert widget.schematic()["blocks"]["main"]["instances"]["r1"][
        "pos"
    ] == list(start)
