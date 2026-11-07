"""Хук PreToolUse: не даёт агенту менять собственные настройки (CLAUDE.md и .claude/). Код выхода 2 = блокировка."""
import json
import os
import sys

path = json.load(sys.stdin).get("tool_input", {}).get("path", "")
parts = os.path.normpath(path).split(os.sep)
if parts[0] == ".claude" or parts[-1] == "CLAUDE.md":
    print(f"{path} is agent configuration and must not be changed by the agent. Ask the user instead.", file=sys.stderr)
    sys.exit(2)
