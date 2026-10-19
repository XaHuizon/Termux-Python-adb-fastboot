#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
SHEBANG=${SHEBANG:-/data/data/com.termux/files/usr/bin/python3}

build_app() {
    name=$1
    source_dir=$2
    build_dir=${TMPDIR:-/tmp}/py-$name-build

    rm -rf "$build_dir"
    mkdir -p "$build_dir"
    cp "$source_dir/__main__.py" "$build_dir/__main__.py"
    cp -R "$source_dir/termuxadb" "$build_dir/termuxadb"
    find "$build_dir" -type d -name __pycache__ -exec rm -rf {} +

    "$PYTHON" -m zipapp "$build_dir" -p "$SHEBANG" -o "$ROOT/py-$name"
    chmod 700 "$ROOT/py-$name"
}

build_app adb "$ROOT/python-adb"
build_app fastboot "$ROOT/python-fastboot"

printf 'built %s and %s\n' "$ROOT/py-adb" "$ROOT/py-fastboot"
