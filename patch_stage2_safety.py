#!/usr/bin/env python3
"""Replace pathname-based volume ownership repair in the pinned stage2 hook."""

from __future__ import annotations

import sys
from pathlib import Path


OLD_MKDIR = 'mkdir -p "$HERMES_HOME"'
NEW_MKDIR = '"/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py --ensure-home'

OLD_TREE = '''chown_hermes_tree() {
    target="$1"
    if refuse_symlinked_path "recursive chown" "$target"; then
        return 0
    fi
    chown -R hermes:hermes "$target" 2>/dev/null || \\
        echo "[stage2] Warning: chown $target failed (rootless container?) \u2014 continuing"
}'''
NEW_TREE = '''chown_hermes_tree() {
    target="$1"
    case "$target" in
        "$HERMES_HOME"/*) relative="${target#"$HERMES_HOME"/}" ;;
        *) echo "[stage2] Refusing ownership repair outside HERMES_HOME: $target" >&2; return 1 ;;
    esac
    "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
        --safe-chown-subtree "$relative"
}'''

OLD_HOME = '''        chown hermes:hermes "$HERMES_HOME" 2>/dev/null || \\
            echo "[stage2] Warning: chown $HERMES_HOME failed (rootless container?) \u2014 continuing"'''
NEW_HOME = '''        "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
            --safe-chown-home'''

OLD_GATEWAY_LOG_DIR = '''        chown hermes:hermes "$HERMES_HOME/logs/gateways" 2>/dev/null || true'''
NEW_GATEWAY_LOG_DIR = '''        "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
            --safe-chown-directory "logs/gateways"'''

OLD_TOP_LEVEL_FILE = '''            chown hermes:hermes "$HERMES_HOME/$f" 2>/dev/null || true'''
NEW_TOP_LEVEL_FILE = '''            "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
                --safe-fix-file "$f"'''

OLD_CONFIG = '''        chown hermes:hermes "$HERMES_HOME/config.yaml" 2>/dev/null || true
        chmod 640 "$HERMES_HOME/config.yaml" 2>/dev/null || true'''
NEW_CONFIG = '''        "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
            --safe-fix-file "config.yaml" --mode 640'''

OLD_ENV = '''        chown hermes:hermes "$HERMES_HOME/.env" 2>/dev/null || true
        chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true'''
NEW_ENV = '''        "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
            --safe-fix-file ".env" --mode 600'''

OLD_AUTH = '''        printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" > "$HERMES_HOME/auth.json"
        chown hermes:hermes "$HERMES_HOME/auth.json" 2>/dev/null || true
        chmod 600 "$HERMES_HOME/auth.json"'''
NEW_AUTH = '''        printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" | \\
            "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
                --safe-create-file "auth.json" --mode 600'''

OLD_GATEWAY_STATE = '''        printf '{"gateway_state":"running"}\\n' > "$HERMES_HOME/gateway_state.json"
        chown hermes:hermes "$HERMES_HOME/gateway_state.json" 2>/dev/null || true
        chmod 644 "$HERMES_HOME/gateway_state.json"'''
NEW_GATEWAY_STATE = '''        printf '{"gateway_state":"running"}\\n' | \\
            "/opt/hermes/.venv/bin/python" /opt/hermes/railway_prepare.py \\
                --safe-create-file "gateway_state.json" --mode 644'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one {label} anchor, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/docker/stage2-hook.sh")
    text = target.read_text()
    text = replace_once(text, OLD_MKDIR, NEW_MKDIR, "home bootstrap")
    text = replace_once(text, OLD_TREE, NEW_TREE, "recursive ownership")
    text = replace_once(text, OLD_HOME, NEW_HOME, "home ownership")
    text = replace_once(text, OLD_GATEWAY_LOG_DIR, NEW_GATEWAY_LOG_DIR, "gateway log ownership")
    text = replace_once(text, OLD_TOP_LEVEL_FILE, NEW_TOP_LEVEL_FILE, "top-level file ownership")
    text = replace_once(text, OLD_CONFIG, NEW_CONFIG, "config ownership and mode")
    text = replace_once(text, OLD_ENV, NEW_ENV, "environment ownership and mode")
    text = replace_once(text, OLD_AUTH, NEW_AUTH, "auth ownership and mode")
    text = replace_once(text, OLD_GATEWAY_STATE, NEW_GATEWAY_STATE, "gateway state ownership and mode")
    target.write_text(text)
    print("patched stage2 volume ownership repair")


if __name__ == "__main__":
    main()
