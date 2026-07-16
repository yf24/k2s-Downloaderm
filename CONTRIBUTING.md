# Contributing

Thanks for looking at K2S Downloader. This document covers the local dev setup, how to run tests/lint, and the conventions this repo follows.

## Setup

```
uv sync --extra dev
```

or without `uv`:

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip install -e ".[dev]"
```

`ffmpeg` on your `PATH` is only needed to exercise the optional media-integrity check (`Downloader.download(..., ensure_media_check=True)`); it is not required to run the test suite.

## Running tests and lint

```
uv run pytest -q
uv run ruff check .
```

Both commands also run in CI (`.github/workflows/ci.yml`) on every push to `main` and on every pull request, against Python 3.9 (the floor declared in `pyproject.toml`'s `requires-python`) and 3.13. Please run both locally before opening a PR — a red CI run on an otherwise-mergeable PR just costs everyone a round trip.

## Code style

- Follow the conventions already in the file you're editing over any external style guide.
- Comments are English, even though this project's docs (`Readme.md`, `todolist.md`) are Traditional Chinese for the human-facing narrative. Code comments explain *why*, not *what* — skip a comment if the code is already self-explanatory.
- Prefer named constants over inline magic numbers, especially for timeouts (see `CHUNK_REQUEST_TIMEOUT`, `DEFAULT_TIMEOUT`, etc. in `src/k2s_downloader/core/`) so the reason for a specific value is discoverable and adjustable in one place.
- The `core/` package (`downloader.py`, `k2s_client.py`, `proxy.py`) has no PySide6 dependency and should stay that way — it's what the CLI and the tests exercise directly. UI-specific logic belongs in `gui/`.
- `core/` communicates with callers via constructor callbacks (`status_callback`, `progress_callback`, `proxy_state_callback`), not by printing or logging directly. This is what lets the same download logic drive both the CLI (`status_callback=print`) and the Qt GUI (callbacks wired to Qt signals in `gui/worker.py`) without core knowing which one it's talking to. Keep new caller-facing output going through these callbacks rather than adding new `print()`/`logging` calls inside `core/`.
- When fixing a bug, prefer a minimal, targeted diff over an opportunistic rewrite of surrounding code — this repo's history (see `todolist.md`) tracks issues by priority tier (P0 = correctness/hangs, P1 = infra, P2 = robustness, ...) precisely so unrelated cleanup doesn't get tangled into a bug fix.

## Testing conventions

- Tests live in `tests/` and mirror the module they exercise (e.g. `test_downloader_*.py` for `core/downloader.py`).
- Network calls are mocked at the `requests` call site (`unittest.mock.patch("k2s_downloader.core.downloader.requests.get", ...)` etc.) — no test should make a real network request.
- When a test targets concurrency (proxy-lock contention, thread scheduling), run it several times locally (`pytest -q` in a loop) before trusting it; a flaky pass is not a pass.
- Every bug fix should land with a regression test that fails against the pre-fix code and passes after. `gui/` is the one exception (`# pragma: no cover - GUI wiring`) since it requires a running Qt application to exercise meaningfully.

## Commit messages

This repo follows [Conventional Commits](https://www.conventionalcommits.org/) style: `type(scope): summary`, e.g. `fix(core): bound URL-generation retries and surface blocked-IP errors`. Common types used here: `fix`, `feat`, `test`, `ci`, `chore`, `docs`. `scope` is usually the touched module (`core`, `gui`) or area (`ci`).

## Tracking ongoing work

`todolist.md` is the living backlog for this project, organized by priority (P0 highest). If you're picking up an item from it: update its checkbox, completion date, and test location when you're done, and keep the original problem/location/suggestion text so the entry stays useful to whoever reads it next.

## Pull requests

- Keep PRs scoped to one priority-tier's worth of work where practical (see `todolist.md`'s tiers) rather than bundling unrelated fixes.
- Include the `pytest -q` / `ruff check .` results (or note that CI covers it) in the PR description.
- No CLA or sign-off is required; contributions are accepted under this repo's [MIT license](LICENSE).
