#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

backup_dir="$(mktemp -d)"
cp pyproject.toml "$backup_dir/pyproject.toml"
cp src/diffio/__init__.py "$backup_dir/__init__.py"

restore() {
  cp "$backup_dir/pyproject.toml" pyproject.toml
  cp "$backup_dir/__init__.py" src/diffio/__init__.py
  rm -rf "$backup_dir"
}

trap restore EXIT

python scripts/bump_version.py "${1:-patch}"
python -m build
python -m twine upload dist/*
