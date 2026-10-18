#!/bin/bash
# 重建早报专用的 Python 环境：~/.follow-builders/venv，装 requirements.txt 里固定版本的库。
# 电脑上的 python3 升级（比如 Homebrew 换成 3.15）不影响这个环境。环境坏了时运行一次即可。
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.follow-builders/venv"
PY="/opt/homebrew/opt/python@3.14/bin/python3.14"
[ -x "$PY" ] || PY="$(command -v python3.14 || command -v python3)"
echo "用 $PY 建环境：$VENV"
"$PY" -m venv --clear "$VENV"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r "$REPO/requirements.txt"
"$VENV/bin/python" -c "import requests, feedparser, youtube_transcript_api, markdown; print('环境可以用了')"
