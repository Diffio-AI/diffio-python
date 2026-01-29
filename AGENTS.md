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
* Keep public API names in `camelCase` to match the service contract, for example `create_project` arguments like `fileName`.
* Do not add Python typing, type hints, or `typing` imports in this SDK.
* Avoid `from __future__ import annotations` in SDK files.

## Testing Guidelines
* Tests are written with pytest and live under `tests`.
* Name tests with the `test_*.py` pattern, for example `tests/test_client.py`.
* Prefer adding or updating fixtures in `tests/fixtures` when new API flows need stable inputs.

## Commit and Pull Request Guidelines
* There is no enforced commit message format, use short imperative summaries that describe the change.
* Pull requests should include a concise summary, any relevant issue links, and the exact test command used.
* Include before and after notes when API behavior changes, plus doc updates in `README.md` when needed.

## SDK Documentation Sync
* Whenever you change the SDK, update the website API documentation in `app/components/docs/` to match the new behavior.
* Before finishing any SDK update, use a sub agent to review and apply the documentation updates in `app/components/docs/`, and do not exit until that review is complete.
* Before finishing any SDK update, use a sub agent to review `README.md`, apply any updates needed to match the code, and do not exit until that review is complete.

## Configuration and Environment
* Set `DIFFIO_API_KEY` for authenticated requests.
* Override `DIFFIO_API_BASE_URL` when pointing at the Functions emulator or a non default endpoint.
* Keep emulator base URLs consistent with the examples in `README.md`.
