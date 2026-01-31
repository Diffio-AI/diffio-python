#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "diffio" / "__init__.py"

BUMP_KINDS = {"major", "minor", "patch"}


def read_project_name(text: str) -> str:
    match = re.search(r'^name\s*=\s*"([^"]+)"\s*$', text, flags=re.M)
    if not match:
        raise RuntimeError("Could not find project name in pyproject.toml")
    return match.group(1)


def parse_version(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Unsupported version format: {value}")
    return tuple(int(part) for part in parts)


def fetch_latest_version(name: str) -> str:
    base_url = os.environ.get("PYPI_BASE_URL", "https://pypi.org/pypi").rstrip("/")
    url = f"{base_url}/{name}/json"
    try:
        with urlopen(url, timeout=20) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
        return "0.0.0"

    versions = [
        version
        for version in data.get("releases", {}).keys()
        if re.fullmatch(r"\d+\.\d+\.\d+", version)
    ]
    if not versions:
        return "0.0.0"
    return max(versions, key=parse_version)


def bump_version(current: str, kind: str) -> str:
    major, minor, patch = parse_version(current)
    if kind == "major":
        major += 1
        minor = 0
        patch = 0
    elif kind == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def update_file(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    updated, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise RuntimeError(f"Expected one replacement in {path}, got {count}")
    path.write_text(updated)


def main() -> int:
    bump = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BUMP", "patch")).lower()
    if bump not in BUMP_KINDS:
        print("Usage: python scripts/bump_version.py [major|minor|patch]", file=sys.stderr)
        return 1

    pyproject_text = PYPROJECT.read_text()
    name = read_project_name(pyproject_text)
    latest = fetch_latest_version(name)
    next_version = bump_version(latest, bump)

    update_file(PYPROJECT, r'^version\s*=\s*".*"$', f'version = "{next_version}"')
    update_file(INIT, r'^__version__\s*=\s*".*"$', f'__version__ = "{next_version}"')

    print(f"Bumped {name} from {latest} to {next_version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
