#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

command -v python3 >/dev/null || { echo 'python3 is required'; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p data/libs
if [[ ! -s data/libs/CoreADI.x86_64 ]]; then
  echo 'data/libs/CoreADI.x86_64 is missing.'
  echo 'Copy the verified CoreADI.x86_64 file into data/libs before setup.'
  exit 1
fi

# First start creates data/.vault-key if it does not already exist.
chmod 700 data
echo "Installed FindMy into $ROOT"
echo 'Run ./start-linux.sh, then use an SSH tunnel to open http://127.0.0.1:8765/.'
