# Contributing

## Setup

```bash
uv sync --all-extras      # or: make install
make check                # ruff, ruff format --check, mypy --strict, pytest
```

Python 3.12 or newer. CI runs on 3.14.

## Rules of the house

- **The engine stays deterministic.** No clock, randomness, I/O or
  threading inside `engine.py` or `book.py`. Time and ordering come from the
  sequencer. If a change needs the time, it goes on the command, stamped
  before it is logged.
- **No floats or `Decimal` past the gateway.** Prices are ticks, quantities
  are lots, both `int`.
- **The WAL format is append-only.** New record kinds and new enum members
  may be added at the end; existing codes and layouts never change, or old
  logs stop replaying. A format change that cannot follow this rule bumps the
  segment magic and ships a reader for the old one.
- **Behaviour changes update the golden files on purpose.** Run
  `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff
  line by line, as you would code. A golden diff nobody can explain is a bug.
- **Benchmark numbers are measured, never estimated.** If a change affects
  performance, rerun `make bench-full` and update the README table with the
  machine it ran on.
- Typed code (`mypy --strict` passes), focused modules, tests next to the
  behaviour they pin.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), short and in the
imperative: `fix: reject a post-only order that would cross`. One coherent
change per commit, each one passing `make check`.

## Architecture decisions

Significant design changes get an ADR in `docs/adr/`, numbered in sequence.
Say what problem it solves, what was decided, and what it costs.
