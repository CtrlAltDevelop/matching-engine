.PHONY: install lint format typecheck test test-fast check bench bench-full requirements serve docker profile

install:  ## Create .venv with every extra and the dev tools
	uv sync --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

test-fast:  ## Skip the subprocess crash test
	uv run pytest -m "not slow"

check: lint typecheck test  ## Everything CI runs, locally

bench:  ## Quick benchmark pass
	uv run python bench/bench_engine.py --orders 200000
	uv run python bench/bench_recovery.py --events 200000 --tail 20000

bench-full:  ## The numbers published in the README
	uv run python bench/bench_engine.py --orders 1000000 --runs 3
	uv run python bench/bench_recovery.py --events 1000000 --tail 100000
	uv run python bench/bench_gateway.py

profile:  ## cProfile the engine benchmark; open profile.pstats with snakeviz or similar
	uv run python -m cProfile -o profile.pstats bench/bench_engine.py --orders 200000 --runs 1

requirements:  ## Regenerate the pinned runtime requirements CI installs and audits
	uv export --no-dev --all-extras --no-hashes --no-emit-project --format requirements-txt -o requirements.txt

serve:
	uv run matching-engine serve

docker:
	docker compose up --build
