#!/bin/bash
# secure-start.sh — prepares Caddy and optional file-access binaries. Runtime
# services are supervised by s6; this script remains the foreground Caddy CMD.
# Caddy listens on $PORT and proxies the loopback dashboard at 127.0.0.1:9120.
#
# Credentials come from env (Railway): DASHBOARD_USER, DASHBOARD_PASSWORD.
# If unset, a random 24-char password is generated and printed in the
# logs (find it with `railway logs --service hermes-agent | grep -i
# 'dashboard credentials' -A 4`).
#
# Caddy binary is cached on the volume at /opt/data/bin/caddy, so cold
# starts after first deploy are fast.

set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
PROXY_PORT="${PORT:-9119}"
DASHBOARD_INTERNAL_PORT=9120

USER_NAME="${DASHBOARD_USER:-admin}"
PASSWORD_FILE="$HERMES_HOME/.dashboard-password"

# 1. Resolve the password (env var -> cached file -> generate).
if [ -n "$DASHBOARD_PASSWORD" ]; then
    printf '%s' "$DASHBOARD_PASSWORD" > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
fi
if [ ! -s "$PASSWORD_FILE" ]; then
    echo "[secure-start] DASHBOARD_PASSWORD not set; generating one." >&2
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    echo ""
    echo "============================================================"
    echo "  Dashboard credentials (SAVE THESE)"
    echo "    User:     $USER_NAME"
    echo "    Password: $(cat "$PASSWORD_FILE")"
    echo "============================================================"
    echo ""
fi
PASSWORD=$(cat "$PASSWORD_FILE")

# Apply the 2026-08-08 weekly client time report-format update through the
# supported Hermes cron CLI. The helper is idempotent and fail-closed if the
# target job is absent or ambiguous; a patch failure never blocks boot.
python3 /opt/hermes/patch_weekly_client_time_cron.py \
    || echo "[secure-start] WARN: weekly client time cron patch did not verify" >&2

# 2. Ensure Caddy is on the volume. Cached after first download.
CADDY="$HERMES_HOME/bin/caddy"
if [ ! -x "$CADDY" ]; then
    echo "[secure-start] Caddy not cached at $CADDY - downloading v2.8.4..." >&2
    mkdir -p "$HERMES_HOME/bin"
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz" \
        | tar -xz -C "$HERMES_HOME/bin" caddy
    chmod +x "$CADDY"
fi

# 2b. File-access tooling, cached on the volume like Caddy. Both are OPTIONAL:
#     every step here is guarded so a download/launch failure can never abort
#     boot (the gateway is the critical path; file access is an add-on).
#       - filebrowser → web UI on /files (browse/upload/download/delete)
#       - rclone      → WebDAV on /dav (mountable in macOS Finder)
FILEBROWSER="$HERMES_HOME/bin/filebrowser"
RCLONE="$HERMES_HOME/bin/rclone"
if [ ! -x "$FILEBROWSER" ]; then
    echo "[secure-start] Downloading filebrowser..." >&2
    curl -fsSL "https://github.com/filebrowser/filebrowser/releases/latest/download/linux-amd64-filebrowser.tar.gz" \
        | tar -xz -C "$HERMES_HOME/bin" filebrowser 2>/dev/null \
        && chmod +x "$FILEBROWSER" \
        || echo "[secure-start] WARN: filebrowser download failed; /files unavailable" >&2
fi
if [ ! -x "$RCLONE" ]; then
    echo "[secure-start] Downloading rclone..." >&2
    ( curl -fsSL "https://downloads.rclone.org/rclone-current-linux-amd64.zip" -o /tmp/rclone.zip \
        && python3 -m zipfile -e /tmp/rclone.zip /tmp/rclone-x \
        && cp "$(find /tmp/rclone-x -name rclone -type f | head -1)" "$RCLONE" \
        && chmod +x "$RCLONE" ) \
        || echo "[secure-start] WARN: rclone download failed; /dav unavailable" >&2
    rm -rf /tmp/rclone.zip /tmp/rclone-x 2>/dev/null || true
fi

# 3. Hash the password (bcrypt via Caddy's built-in hasher).
PASSWORD_HASH=$("$CADDY" hash-password --plaintext "$PASSWORD")

# 4. Write the Caddyfile.
cat > "$HERMES_HOME/Caddyfile" <<EOF
{
    auto_https off
    admin off
    log default {
        output stderr
        level INFO
    }
}

:$PROXY_PORT {
    log {
        output stderr
        level INFO
        format console
    }

    # Strip X-Forwarded-For globally. uvicorn (backing the Hermes dashboard)
    # trusts proxy headers from 127.0.0.1 and rewrites ws.client.host based
    # on X-Forwarded-For. The dashboard's _ws_client_is_allowed() then
    # rejects /api/pty because the rewritten client IP isn't loopback.
    request_header -X-Forwarded-For
    request_header -X-Forwarded-Host
    request_header -X-Real-Ip

    # Temporary, read-only verification artifact for the 2026-08-08 cron
    # update. It exposes only the selected job name/ID and marker status.
    @weekly_time_cron_patch path /ops/weekly-time-cron-verification-20260808-a9c3f7
    handle @weekly_time_cron_patch {
        root * /tmp
        rewrite * /weekly-client-time-cron-patch.json
        file_server
    }

    # Inbound webhooks (AgentMail message.received, etc).
    # Bypasses basic_auth so external services can POST without creds.
    # Hermes routes are gated by long-random path tokens — the URL is
    # the shared secret (Slack-style). Anything else returns 404 from
    # Hermes route-not-found.
    @webhooks path /webhooks/*
    handle @webhooks {
        reverse_proxy 127.0.0.1:8644 {
            flush_interval -1
        }
    }

    # WebSocket endpoints — gated by the dashboard's session token (in
    # query string). Bypass basic_auth (it interferes with WS upgrade).
    @ws path /api/pty /api/ws /api/events
    handle @ws {
        reverse_proxy 127.0.0.1:$DASHBOARD_INTERNAL_PORT {
            header_up Host 127.0.0.1:$DASHBOARD_INTERNAL_PORT
            header_up Origin http://127.0.0.1:$DASHBOARD_INTERNAL_PORT
        }
    }

    # File browser (web UI) on /files — basic-auth gated. filebrowser runs
    # with --baseURL /files so it owns the whole /files/* path; pass through
    # unchanged. Returns 502 if filebrowser didn't start (non-fatal add-on).
    @files path /files /files/*
    handle @files {
        basic_auth {
            $USER_NAME $PASSWORD_HASH
        }
        reverse_proxy 127.0.0.1:9121
    }

    # WebDAV on /dav (mount in macOS Finder) — basic-auth gated. rclone serves
    # with --baseurl /dav. WebDAV verbs (PROPFIND/MKCOL/MOVE/...) pass through
    # reverse_proxy by default.
    @dav path /dav /dav/*
    handle @dav {
        basic_auth {
            $USER_NAME $PASSWORD_HASH
        }
        reverse_proxy 127.0.0.1:9122
    }

    # Everything else — basic-auth gated.
    handle {
        basic_auth {
            $USER_NAME $PASSWORD_HASH
        }
        reverse_proxy 127.0.0.1:$DASHBOARD_INTERNAL_PORT {
            header_up Host 127.0.0.1:$DASHBOARD_INTERNAL_PORT
            header_up Origin http://127.0.0.1:$DASHBOARD_INTERNAL_PORT
        }
    }
}
EOF

# 4b. Optionally sync a private skills repo into $HERMES_HOME/skills/custom/.
#     Enable by setting BOTH:
#       SKILLS_REPO_URL   - https GitHub URL, e.g. https://github.com/you/skills.git
#       SKILLS_REPO_TOKEN - PAT with contents:read (fine-grained or classic)
#     The repo is expected to have a top-level skills/<name>/ layout; each
#     <name> is symlinked into the Hermes skills dir. If either var is unset
#     we skip — the skills bundled in the Hermes image still work.
SKILLS_REPO_DIR="$HERMES_HOME/skills/custom"
if [ -n "$SKILLS_REPO_URL" ] && [ -n "$SKILLS_REPO_TOKEN" ]; then
    # Build an authenticated URL (x-access-token form works for both classic
    # and fine-grained PATs). Strip any scheme the user included first.
    REPO_PATH="${SKILLS_REPO_URL#https://}"
    REPO_PATH="${REPO_PATH#http://}"
    AUTH_URL="https://x-access-token:${SKILLS_REPO_TOKEN}@${REPO_PATH}"
    CLEAN_URL="https://${REPO_PATH}"
    if [ -d "$SKILLS_REPO_DIR/.git" ]; then
        echo "[secure-start] Updating custom skills repo..." >&2
        (cd "$SKILLS_REPO_DIR" && \
         git remote set-url origin "$AUTH_URL" && \
         git pull --ff-only 2>&1 | head -5) >&2 || \
         echo "[secure-start] WARN: git pull failed, keeping existing checkout" >&2
    else
        echo "[secure-start] Cloning custom skills repo..." >&2
        mkdir -p "$HERMES_HOME/skills"
        rm -rf "$SKILLS_REPO_DIR"
        git clone --depth 1 "$AUTH_URL" "$SKILLS_REPO_DIR" 2>&1 | head -5 >&2 || \
            echo "[secure-start] WARN: clone failed, skipping custom skills" >&2
    fi
    # Strip token from remote URL so it isn't stored on disk in plain text.
    if [ -d "$SKILLS_REPO_DIR/.git" ]; then
        (cd "$SKILLS_REPO_DIR" && git remote set-url origin "$CLEAN_URL")
    fi
    if [ -d "$SKILLS_REPO_DIR/skills" ]; then
        echo "[secure-start] Linking custom skills into $HERMES_HOME/skills/ ..." >&2
        for skill in "$SKILLS_REPO_DIR"/skills/*/; do
            [ -d "$skill" ] || continue
            name=$(basename "$skill")
            # Prefix with "custom-" to avoid colliding with the skills
            # bundled in the Hermes image (notion, linear, airtable, etc.).
            target="$HERMES_HOME/skills/custom-$name"
            rm -rf "$target"
            ln -s "$skill" "$target"
        done
        echo "[secure-start] $(ls -d $HERMES_HOME/skills/custom-* 2>/dev/null | wc -l) custom skills linked" >&2
    fi
else
    echo "[secure-start] SKILLS_REPO_URL/SKILLS_REPO_TOKEN not set; skipping custom skills clone" >&2
fi

# 4c. Seed our agent identity (SOUL.md) from default-soul.md.
#     Hermes reads $HERMES_HOME/SOUL.md as the agent's system identity. The
#     content lives in /opt/hermes/default-soul.md (copied in by the Dockerfile,
#     source: default-soul.md in this repo) so it's easy to edit in one place;
#     {{HERMES_HOME}}/{{PUBLIC_HOST}} placeholders are filled in at boot.
#
#     We replace SOUL.md when it's MISSING or still a stock default - and there
#     are TWO defaults to match:
#       1. the s6 cont-init stage2 placeholder ("# Hermes Agent Persona"),
#          written before this CMD runs; and
#       2. the gateway's own default_soul.py persona ("...an intelligent AI
#          assistant created by Nous Research"), which the gateway writes during
#          first-run init AFTER this point - so we also re-assert it in step 5b
#          once the gateway is up.
#     Once you (or the agent) customize SOUL.md, neither marker matches and we
#     leave your version untouched.
SOUL_FILE="$HERMES_HOME/SOUL.md"
SOUL_TEMPLATE="/opt/hermes/default-soul.md"
PUBLIC_HOST="${RAILWAY_PUBLIC_DOMAIN:-your-app.up.railway.app}"
soul_is_default() {
    [ ! -f "$SOUL_FILE" ] && return 0
    head -1 "$SOUL_FILE" | grep -q '^# Hermes Agent Persona' && return 0
    head -3 "$SOUL_FILE" | grep -q 'intelligent AI assistant created by Nous Research' && return 0
    return 1
}
seed_soul() {
    sed -e "s|{{HERMES_HOME}}|$HERMES_HOME|g" \
        -e "s|{{PUBLIC_HOST}}|$PUBLIC_HOST|g" \
        "$SOUL_TEMPLATE" > "$SOUL_FILE"
}
if [ -f "$SOUL_TEMPLATE" ] && soul_is_default; then
    echo "[secure-start] Seeding Hermes SOUL.md from template..." >&2
    seed_soul
else
    echo "[secure-start] Custom SOUL.md present (or template missing); leaving it untouched." >&2
fi

# 4d. Seed a USER.md stub on first boot only, so the agent has a place to
#     record who it works for (and knows to ask). Never clobbered once written.
USER_FILE="$HERMES_HOME/USER.md"
if [ ! -f "$USER_FILE" ]; then
    echo "[secure-start] Seeding default USER.md (none present)..." >&2
    cat > "$USER_FILE" <<'USEREOF'
# USER.md

Operator: (unknown so far)

On your first conversation, ask who you are working for - their name and how they
want to be addressed - and replace the line above. Add anything else worth
remembering here over time: role, company, timezone, preferences, recurring
goals. This file is your memory of the person; keep it current.
USEREOF
fi

# 4e. Run the agent from the WEB-SERVED share folder so its files are shareable.
#     Hermes' terminal cwd defaults to "." (config.yaml: terminal.cwd), which
#     resolves to the process launch directory. The image launches from
#     /opt/hermes (read-only), so writes there fail with "permission denied".
#     We cd into $HERMES_HOME/share - writable AND the only directory exposed at
#     /files + /dav - so files the agent creates by default land somewhere it can
#     immediately hand out a download link for (no more "saved to the wrong
#     place"). Private/scratch work goes under $HERMES_HOME/internal instead.
/opt/hermes/.venv/bin/python /opt/hermes/railway_prepare.py
cd "$HERMES_HOME/share" || cd "$HERMES_HOME" || true

# 5. Normal PID-1 containers use s6. Wrapped runtimes cannot run s6-overlay,
# so provide equivalent direct supervision rather than leaving Caddy on 502.
if [ "${HERMES_DIRECT_FALLBACK:-0}" = 1 ]; then
    echo "[secure-start] Starting directly supervised fallback services." >&2
    fallback_pids=""

    fallback_terminate_group() {
        group_leader="$1"
        kill -TERM "-$group_leader" 2>/dev/null || true
        attempts=0
        while kill -0 "-$group_leader" 2>/dev/null && [ "$attempts" -lt 50 ]; do
            sleep 0.1
            attempts=$((attempts + 1))
        done
        kill -KILL "-$group_leader" 2>/dev/null || true
        wait "$group_leader" 2>/dev/null || true
    }

    fallback_supervise() {
        label="$1"
        shift
        child=""
        trap '[ -z "$child" ] || fallback_terminate_group "$child"; exit 0' TERM INT HUP
        while :; do
            setsid "$@" &
            child=$!
            if wait "$child"; then status=0; else status=$?; fi
            fallback_terminate_group "$child"
            child=""
            echo "[secure-start] Fallback $label exited ($status); restarting in 2s." >&2
            sleep 2
        done
    }

    fallback_start() {
        label="$1"
        shift
        fallback_supervise "$label" "$@" &
        fallback_pids="$fallback_pids $!"
    }

    fallback_start gateway-supervisor \
        /opt/hermes/.venv/bin/python /opt/hermes/fallback_gateway_supervisor.py

    fallback_start dashboard \
        /opt/hermes/.venv/bin/hermes dashboard \
        --host 127.0.0.1 --port "$DASHBOARD_INTERNAL_PORT" --no-open --tui
    fallback_start process-reaper \
        /opt/hermes/.venv/bin/python /opt/hermes/process_reaper.py
    fallback_start filebrowser \
        "$HERMES_HOME/bin/filebrowser" -r "$HERMES_HOME/share" \
        -a 127.0.0.1 -p 9121 -b /files \
        -d "$HERMES_HOME/.filebrowser.db" --noauth
    fallback_start rclone-webdav \
        "$HERMES_HOME/bin/rclone" serve webdav "$HERMES_HOME/share" \
        --addr 127.0.0.1:9122 --baseurl /dav

    fallback_stop() {
        trap - TERM INT HUP EXIT
        # fallback_pids contains only shell-generated decimal child PIDs.
        set -- $fallback_pids
        [ "$#" -eq 0 ] || kill "$@" 2>/dev/null || true
        attempts=0
        while [ "$#" -gt 0 ] && [ "$attempts" -lt 80 ]; do
            survivors=""
            for supervisor in "$@"; do
                if kill -0 "$supervisor" 2>/dev/null; then
                    survivors="$survivors $supervisor"
                fi
            done
            set -- $survivors
            [ "$#" -eq 0 ] || sleep 0.1
            attempts=$((attempts + 1))
        done
        [ "$#" -eq 0 ] || kill -KILL "$@" 2>/dev/null || true
        [ "$#" -eq 0 ] || wait "$@" 2>/dev/null || true
        fallback_pids=""
        if [ -n "${fallback_caddy_pid:-}" ]; then
            fallback_terminate_group "$fallback_caddy_pid"
            fallback_caddy_pid=""
        fi
    }
    trap fallback_stop TERM INT HUP EXIT
else
    echo "[secure-start] Gateway, dashboard, file access, and process reaper are s6-supervised." >&2
fi

# Give the dashboard a moment to bind before Caddy tries to proxy to it.
sleep 3

# 7. Caddy auth proxy on the public port.
echo "[secure-start] Starting Caddy auth proxy on :$PROXY_PORT (user '$USER_NAME')..." >&2
if [ "${HERMES_DIRECT_FALLBACK:-0}" = 1 ]; then
    setsid "$CADDY" run --config "$HERMES_HOME/Caddyfile" --adapter caddyfile &
    fallback_caddy_pid=$!
    if wait "$fallback_caddy_pid"; then fallback_status=0; else fallback_status=$?; fi
    fallback_caddy_pid=""
    fallback_stop
    exit "$fallback_status"
fi
exec "$CADDY" run --config "$HERMES_HOME/Caddyfile" --adapter caddyfile
