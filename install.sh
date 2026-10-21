#!/usr/bin/env bash
# Установка tg_ai_bot как systemd-службы на Debian 12/13 (и Ubuntu).
# Запуск из папки репозитория:  sudo ./install.sh
# Повторный запуск безопасен: конфиг и данные не трогаются, зависимости обновляются.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="tg-ai-bot"
APP_USER="tgaibot"

c_ok() { echo -e "\e[32m✔ $*\e[0m"; }
c_info() { echo -e "\e[36m➜ $*\e[0m"; }
c_warn() { echo -e "\e[33m! $*\e[0m"; }
die() { echo -e "\e[31m✘ $*\e[0m" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запусти через sudo: sudo ./install.sh"
[[ -f "$APP_DIR/bot/main.py" ]] || die "Скрипт должен лежать в корне репозитория"

# ── 1. системные пакеты ───────────────────────────────────────
c_info "Ставлю системные пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null
c_ok "python3 $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Нужен Python 3.10+"

# ── 2. сервисный пользователь ────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    c_ok "Создан пользователь $APP_USER"
fi

# ── 3. виртуальное окружение ─────────────────────────────────
c_info "Ставлю Python-зависимости (последние версии)"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q --upgrade -r "$APP_DIR/requirements.txt"
c_ok "Зависимости установлены"

# ── 4. конфиг ────────────────────────────────────────────────
CONFIG="$APP_DIR/config.yaml"
if [[ -f "$CONFIG" ]]; then
    c_ok "config.yaml уже есть — оставляю как есть"
else
    echo
    c_info "Первичная настройка"
    read -rp "Токен бота от @BotFather: " TG_TOKEN
    [[ "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "Это не похоже на токен бота"

    echo "Telegram ID главного админа — узнать можно у @userinfobot."
    echo "Несколько ID — через пробел. Остальных пользователей добавишь прямо из бота."
    read -rp "ID админа: " ADMIN_IDS
    ADMIN_ARGS=()
    for id in $ADMIN_IDS; do
        [[ "$id" =~ ^[0-9]+$ ]] || die "ID должен быть числом: $id"
        ADMIN_ARGS+=(--admin "$id")
    done
    [[ ${#ADMIN_ARGS[@]} -gt 0 ]] || die "Нужен хотя бы один ID админа"

    "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/setup_config.py" \
        --example "$APP_DIR/config.example.yaml" --out "$CONFIG" \
        --token "$TG_TOKEN" "${ADMIN_ARGS[@]}"
fi

# ── 5. права ─────────────────────────────────────────────────
mkdir -p "$APP_DIR/data"
chown -R root:root "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chown "root:$APP_USER" "$CONFIG"
chmod 640 "$CONFIG"           # ключи читает только служба
chmod 750 "$APP_DIR/data"
c_ok "Права выставлены (код — root, data/ — $APP_USER, config.yaml 640)"

# ── 6. проверка конфига ──────────────────────────────────────
if ! (cd "$APP_DIR" && runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -c \
        "from bot.config import load_config; load_config('$CONFIG'); print('Конфиг в порядке')"); then
    c_warn "Конфиг пока невалиден — поправь $CONFIG и выполни: sudo systemctl restart $SERVICE"
fi

# ── 7. systemd ───────────────────────────────────────────────
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Telegram AI bot (tg_ai_bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=BOT_CONFIG=$CONFIG
Environment=BOT_DATA=$APP_DIR/data
ExecStart=$APP_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=5

# изоляция
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 3

echo
if systemctl is-active --quiet "$SERVICE"; then
    c_ok "Служба $SERVICE запущена."
    echo "  Напиши боту /keys и добавь API-ключи нейросетей — дальше /model и можно общаться."
else
    c_warn "Служба не запустилась. Последние строки лога:"
    journalctl -u "$SERVICE" -n 20 --no-pager || true
fi
cat <<EOF

  Логи:        journalctl -u $SERVICE -f
  Перезапуск:  sudo systemctl restart $SERVICE
  Конфиг:      sudo nano $CONFIG   (затем /reload в боте или перезапуск)
  Обновление:  sudo $APP_DIR/update.sh
EOF
