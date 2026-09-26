"""Golden tests: a scripted command sequence against a checked-in expected output.

Each ``golden/*.json`` holds the commands, the events every command must
produce, and the L2 book left at the end. Command ``n`` runs with ``seq = n``
and ``ts = n * 1000``. After an intentional behaviour change, regenerate with
``UPDATE_GOLDEN=1 pytest tests/test_golden.py`` and review the diff like code.
"""

from __future__ import annotations

import os
from pathlib import Path

import msgspec
import pytest

from matching_engine.book import Depth
from matching_engine.domain import Command, SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.events import Event

GOLDEN_DIR = Path(__file__).parent / "golden"


class GoldenCase(msgspec.Struct, forbid_unknown_fields=True):
    description: str
    commands: list[Command]
    events: list[list[Event]]
    book: Depth


def run(commands: list[Command]) -> tuple[list[list[Event]], Depth]:
    engine = MatchingEngine()
    events = [
        engine.process(SequencedCommand(seq, seq * 1000, cmd))
        for seq, cmd in enumerate(commands, start=1)
    ]
    return events, engine.book.depth()


def render(case: GoldenCase) -> str:
    """One command or event per line, so a behaviour change reads as a small diff."""

    def line(obj: object) -> str:
        return msgspec.json.encode(obj).decode()

    events = ",\n".join(
        "    [\n" + ",\n".join(f"      {line(e)}" for e in per_cmd) + "\n    ]"
        for per_cmd in case.events
    )
    return (
        "{\n"
        f'  "description": {line(case.description)},\n'
        '  "commands": [\n' + ",\n".join(f"    {line(c)}" for c in case.commands) + "\n  ],\n"
        '  "events": [\n' + events + "\n  ],\n"
        f'  "book": {line(case.book)}\n'
        "}\n"
    )


@pytest.mark.parametrize("path", sorted(GOLDEN_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_golden(path: Path) -> None:
    case = msgspec.json.decode(path.read_bytes(), type=GoldenCase)
    events, book = run(case.commands)

    if os.environ.get("UPDATE_GOLDEN"):
        updated = GoldenCase(case.description, case.commands, events, book)
        path.write_text(render(updated), encoding="utf-8", newline="\n")
        return

    assert len(events) == len(case.commands)
    for seq, (actual, expected) in enumerate(zip(events, case.events, strict=True), start=1):
        assert actual == expected, f"command {seq} diverged"
    assert book == case.book
