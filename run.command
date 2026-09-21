#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print "setup.command를 먼저 실행하세요."
  read '?Enter를 누르면 닫힙니다: '
  exit 1
fi
.venv/bin/python -m jobis_meals menu
