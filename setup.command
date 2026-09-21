#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
print "설치 완료. run.command를 열면 실행 메뉴가 나옵니다."
read '?Enter를 누르면 닫힙니다: '
