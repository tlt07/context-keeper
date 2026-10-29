#!/usr/bin/env bash
# Удаление context_keeper. Сами выжимки (.claude/CONTEXT.md в проектах и
# ~/.claude/context-archive/) остаются — удалите их вручную, если не нужны.
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"
LABEL="com.context-keeper.daily"

if [ "$(uname)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
else
  ( crontab -l 2>/dev/null | grep -v '# context-keeper' || true ) | crontab -
fi
echo "✓ Ежедневный запуск убран"

if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$CLAUDE_DIR/context-keeper/settings.json.bak-uninstall" 2>/dev/null || true
  python3 - "$SETTINGS" <<'EOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text())
hooks = s.get("hooks", {})
ss = [e for e in hooks.get("SessionStart", [])
      if not any("context_keeper.py" in h.get("command", "") for h in e.get("hooks", []))]
if ss:
    hooks["SessionStart"] = ss
else:
    hooks.pop("SessionStart", None)
if not hooks:
    s.pop("hooks", None)
p.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n")
EOF
  echo "✓ Хук SessionStart убран из $SETTINGS"
fi

if [ -f "$CLAUDE_DIR/CLAUDE.md" ]; then
  python3 - "$CLAUDE_DIR/CLAUDE.md" <<'EOF'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
t = p.read_text()
p.write_text(re.sub(r"\n*<!-- context-keeper:start -->.*?<!-- context-keeper:end -->\n?", "\n", t, flags=re.S))
EOF
  echo "✓ Пояснение убрано из CLAUDE.md"
fi

rm -f "$CLAUDE_DIR/scripts/context_keeper.py"
echo "✓ Скрипт удалён. Выжимки сохранены в проектах и в ~/.claude/context-archive/"
