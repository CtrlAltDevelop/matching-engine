"""Fixtures that drive an engine one command at a time."""

from __future__ import annotations

import pytest
from helpers import Submit

from matching_engine.domain import Command, SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.events import Event


@pytest.fixture
def engine() -> MatchingEngine:
    return MatchingEngine()


@pytest.fixture
def submit(engine: MatchingEngine) -> Submit:
    """Feed commands with consecutive seqs; ``ts`` is ``seq * 1000`` for readability."""

    def _submit(command: Command) -> list[Event]:
        seq = engine.last_seq + 1
        return engine.process(SequencedCommand(seq, seq * 1000, command))

    return _submit
