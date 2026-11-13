# Repository Guidelines

## Project Structure and Module Organization
* Source code lives in `src/diffio`, with the public SDK surface in `src/diffio/__init__.py`.
* Tests live in `tests`, with shared fixtures in `tests/fixtures` and `tests/conftest.py`.
* Tutorials and sample flows live in `tutorials`, for example `tutorials/audio-restoration-cli/README.md`.

## Build, Test, and Development Commands
* `pip install -e .` installs the SDK in editable mode for local development.
* `python -m pip install -r requirements-dev.txt` installs dev dependencies, including pytest.
* `python -m pytest` runs the full test suite under `tests`.

## Coding Style and Naming Conventions
* Use 4 space indentation and follow standard Python style conventions.
* Request and response field names are `snake_case`, matching the v2 service contract exactly as on the wire, for example `client.projects.create(file_name=..., size_bytes=...)` and `project.latest_generation.status`.
* Do not add Python typing, type hints, or `typing` imports in this SDK.
* Avoid `from __future__ import annotations` in SDK files.

## Testing Guidelines
* Tests are written with pytest and live under `tests`.
* Name tests with the `test_*.py` pattern, for example `tests/test_client.py`.
* Prefer extending the fake v2 API in `tests/fake_api.py` when new API flows need test coverage.

## Commit and Pull Request Guidelines
* There is no enforced commit message format, use short imperative summaries that describe the change.
* Pull requests should include a concise summary, any relevant issue links, and the exact test command used.
* Include before and after notes when API behavior changes, plus doc updates in `README.md` when needed.

## Hosted Docs (Required)
* The public, hosted docs live under `app/pages/docs` (routes/pages) and `app/components/docs` (docs content/components).
* Any SDK behavior change MUST be reflected in these hosted docs before finishing.
* Use a subagent to perform the hosted docs updates and review; do not finish the task until the subagent reports back.
* If you only update SDK code without updating `app/pages/docs` + `app/components/docs`, the task is incomplete.

## Configuration and Environment
* Set `DIFFIO_API_KEY` for authenticated requests.
* Override `DIFFIO_API_BASE_URL` with another API host (for example `https://diffio-api-dev.web.app` for dev); the SDK adds `/v2`.
* Unit tests use the in-memory fake in `tests/fake_api.py`; never point tests at production.
