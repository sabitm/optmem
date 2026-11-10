#!/bin/sh
# Install one independent OptMem instance and an optional manual-only skill.
# Re-run with the same paths to update. Existing memories stay in place.

set -eu
DIR="${HOME:-}/.optmem"
SKILL_DIR=""
HAVE_DIR=0
HAVE_SKILL=0

usage() {
  printf '%s\n' 'usage: install.sh [INSTALL_DIR] [--skill-dir SKILL_DIR]'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --skill-dir)
      if [ "$#" -lt 2 ] || [ -z "$2" ] || [ "$HAVE_SKILL" -eq 1 ]; then
        usage >&2
        exit 1
      fi
      SKILL_DIR=$2
      HAVE_SKILL=1
      shift 2
      ;;
    -*)
      usage >&2
      exit 1
      ;;
    *)
      if [ "$HAVE_DIR" -eq 1 ] || [ -z "$1" ]; then
        usage >&2
        exit 1
      fi
      DIR=$1
      HAVE_DIR=1
      shift
      ;;
  esac
done

if [ "$HAVE_DIR" -eq 0 ] && [ -z "${HOME:-}" ]; then
  printf '%s\n' 'HOME is not set. Pass an installation directory.' >&2
  exit 1
fi

command -v python3 >/dev/null || {
  printf '%s\n' 'Python 3 is required to install OptMem.' >&2
  exit 1
}

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
trap 'exit 1' HUP INT TERM
if [ -n "${OPTMEM_SOURCE:-}" ]; then
  case "$OPTMEM_SOURCE" in
    /*) cp "$OPTMEM_SOURCE" "$TMP" ;;
    *) cp "./$OPTMEM_SOURCE" "$TMP" ;;
  esac
else
  curl -fsSL https://raw.githubusercontent.com/VictorTaelin/OptMem/main/memo -o "$TMP"
fi

PYTHONDONTWRITEBYTECODE=1 python3 -B - "$TMP" "$DIR" "$SKILL_DIR" <<'PY'
import ast
import os
import runpy
import subprocess
import sys

source, directory, skill_dir = sys.argv[1:]
try:
    with open(source, "rb") as f:
        ast.parse(f.read(), filename="downloaded memo")
    module = runpy.run_path(source, run_name="optmem_installer")
    installer = module.get("install_tool")
    if not callable(installer):
        raise ValueError("Downloaded memo does not provide install_tool")
    installer(source, directory, skill_dir or None)
    executable = os.path.join(os.path.realpath(os.path.expanduser(directory)), "memo")
    sys.stdout.flush()
    result = subprocess.run([sys.executable, executable, "init"])
    sys.exit(result.returncode)
except Exception as e:
    print("Install failed: %s" % e, file=sys.stderr)
    sys.exit(1)
PY
