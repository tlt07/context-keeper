#!/usr/bin/env bash
# Установка context_keeper: скрипт + хук SessionStart + ежедневный запуск.
# Повторный запуск безопасен (обновляет на месте).
#   ./install.sh                 — по умолчанию, обновление в 23:30
#   ./install.sh --time 08:00    — другое время
#   ./install.sh --now           — сразу сделать первую выжимку по всем проектам
set -euo pipefail

TIME="23:30"
RUN_NOW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --time) TIME="$2"; shift 2 ;;
    --now) RUN_NOW=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 1 ;;
  esac
done
HOUR=$((10#${TIME%%:*})); MINUTE=$((10#${TIME##*:}))

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPT="$CLAUDE_DIR/scripts/context_keeper.py"
SETTINGS="$CLAUDE_DIR/settings.json"
KEEPER_DIR="$CLAUDE_DIR/context-keeper"
LABEL="com.context-keeper.daily"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# 1. Проверки
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "Нужен python3 (3.8+). На Mac: brew install python" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Нужен python3 версии 3.8+" >&2; exit 1; }
CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then CLAUDE_BIN="$HOME/.local/bin/claude"; fi
if [ -z "$CLAUDE_BIN" ]; then
  echo "Не найден Claude Code CLI (claude). Установите: https://docs.claude.com/claude-code" >&2; exit 1
fi

# 2. Скрипт
mkdir -p "$CLAUDE_DIR/scripts" "$KEEPER_DIR"
cp "$SRC_DIR/context_keeper.py" "$SCRIPT"
chmod +x "$SCRIPT"
say "✓ Скрипт: $SCRIPT"

# 3. Хук SessionStart в ~/.claude/settings.json
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$KEEPER_DIR/settings.json.bak-$(date +%Y%m%d-%H%M%S)"
"$PYTHON" - "$SETTINGS" <<'EOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
cmd = 'python3 "$HOME/.claude/scripts/context_keeper.py" inject'
ss = s.setdefault("hooks", {}).setdefault("SessionStart", [])
if not any("context_keeper.py" in h.get("command", "") for e in ss for h in e.get("hooks", [])):
    ss.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
p.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n")
EOF
say "✓ Хук SessionStart добавлен в $SETTINGS (копия старого — в $KEEPER_DIR)"

# 4. Ежедневный запуск
PATH_FOR_JOB="$(dirname "$CLAUDE_BIN"):$(dirname "$PYTHON"):/usr/local/bin:/usr/bin:/bin"
if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT</string>
    <string>update</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH_FOR_JOB</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$KEEPER_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$KEEPER_DIR/launchd.err.log</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "✓ LaunchAgent $LABEL: каждый день в $(printf '%02d:%02d' "$HOUR" "$MINUTE") (если Mac спал — при пробуждении)"
else
  LINE="$MINUTE $HOUR * * * PATH=$PATH_FOR_JOB $PYTHON $SCRIPT update >> $KEEPER_DIR/cron.log 2>&1 # context-keeper"
  ( crontab -l 2>/dev/null | grep -v '# context-keeper' || true; echo "$LINE" ) | crontab -
  say "✓ cron: каждый день в $(printf '%02d:%02d' "$HOUR" "$MINUTE")"
fi

# 5. Пояснение для Claude в глобальном CLAUDE.md
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
touch "$CLAUDE_MD"
if ! grep -q "context-keeper:start" "$CLAUDE_MD"; then
  cat >> "$CLAUDE_MD" <<'EOF'

<!-- context-keeper:start -->
## Контекст проектов между сессиями (context_keeper)

- Скрипт `~/.claude/scripts/context_keeper.py` раз в день сводит свежие транскрипты каждого
  проекта в `<проект>/.claude/CONTEXT.md` (копия — `~/.claude/context-archive/`). Хук
  SessionStart подмешивает этот файл в начало каждой новой сессии.
- Это выжимка, а не истина: если расходится с кодом — верить коду. Раздел «Заметки вручную»
  в конце файла скрипт не трогает.
- Обновить сейчас: `python3 ~/.claude/scripts/context_keeper.py update --project .`;
  статус: `... status`; лог: `~/.claude/context-keeper/keeper.log`.
<!-- context-keeper:end -->
EOF
  say "✓ Пояснение добавлено в $CLAUDE_MD"
fi

# 6. Первая выжимка
if [ "$RUN_NOW" = 1 ]; then
  say "Делаю первую выжимку по всем проектам (≈30–90 с на проект)…"
  "$PYTHON" "$SCRIPT" update
  "$PYTHON" "$SCRIPT" status
else
  echo
  echo "Первая выжимка появится в $(printf '%02d:%02d' "$HOUR" "$MINUTE") или при открытии сессии в проекте."
  echo "Сделать сразу:  python3 ~/.claude/scripts/context_keeper.py update"
fi
say "Готово."
