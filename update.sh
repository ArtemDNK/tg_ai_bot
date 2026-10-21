#!/usr/bin/env bash
# Обновление: свежий код из GitHub + последние версии зависимостей + перезапуск службы.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "Запусти через sudo: sudo $0" >&2; exit 1; }

git -C "$APP_DIR" pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q --upgrade -r "$APP_DIR/requirements.txt"
chown -R tgaibot:tgaibot "$APP_DIR/data"
systemctl restart tg-ai-bot
sleep 2
systemctl --no-pager --lines=5 status tg-ai-bot
