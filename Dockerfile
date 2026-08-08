# hermes-railway — minimal Railway deploy image for Hermes Agent
# with a Caddy HTTP basic-auth reverse proxy in front of the dashboard.
#
# Adapted from Shinyduo/hermes-agent (MIT) by adding auth.

# Pinned to nousresearch/hermes-agent:v2026.8.3 (v0.20.0 "The Herald Release"),
# published 2026-08-03 (digest below). We pin a versioned RELEASE tag by digest
# rather than the rolling :latest — both for a deterministic pull and because
# :latest has shipped broken boots before (the 2026-05-28 build crashed in
# tools/skills_sync.py on `import hermes_constants` and took the service down).
# A tagged release is the tested artifact; :latest just tracks main's HEAD.
# To update later: pick the newest release tag, grab its digest, re-pin below,
# and verify a clean boot (watch `railway logs` for the [secure-start] lines).
#   curl -s "https://hub.docker.com/v2/repositories/nousresearch/hermes-agent/tags/?ordering=last_updated&page_size=20" | jq -r '.results[] | "\(.name)\t\(.digest)"'
FROM nousresearch/hermes-agent@sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e

# secure-start.sh boots:
#   1) hermes gateway (background)
#   2) hermes dashboard on 127.0.0.1:9120 (background, no public binding)
#   3) Caddy on $PORT (foreground) with HTTP basic auth -> reverse_proxy
#
# Credentials come from env: DASHBOARD_USER, DASHBOARD_PASSWORD.
# If DASHBOARD_PASSWORD is unset, a random 24-char password is generated
# and printed to logs (visible in `railway logs`).
#
# Caddy binary is downloaded on first boot to /opt/data/bin/caddy and
# cached there for subsequent restarts (the volume persists).
COPY secure-start.sh /opt/hermes/secure-start.sh
COPY patch_weekly_client_time_cron.py /opt/hermes/patch_weekly_client_time_cron.py
RUN chmod +x /opt/hermes/secure-start.sh /opt/hermes/patch_weekly_client_time_cron.py

# Default agent identity. secure-start.sh renders this into $HERMES_HOME/SOUL.md
# on boot (replacing the stock "# Hermes Agent Persona" default), unless the
# operator has customized it. Edit default-soul.md to change the seeded persona.
COPY default-soul.md /opt/hermes/default-soul.md

# Safety net for the skills_sync boot crash seen in the 2026-05-28 image:
# tools/skills_sync.py runs at boot but its sys.path[0] is tools/, so the
# module-level `from hermes_constants import ...` (hermes_constants.py lives at
# /opt/hermes) raises ModuleNotFoundError and crash-loops the container. A
# runtime PYTHONPATH doesn't survive the entrypoint's privilege drop, so patch
# at build time. Tolerant: no-op if the file is gone or upstream already fixed
# the import (so this Dockerfile keeps working once a fixed image is pinned).
RUN python3 -c "import os; p='/opt/hermes/tools/skills_sync.py'; (os.path.exists(p) and (lambda L: (lambda m: (L.insert(m[0], L[m[0]][:len(L[m[0]])-len(L[m[0]].lstrip())]+'import sys; sys.path.insert(0, '+chr(34)+'/opt/hermes'+chr(34)+')'), open(p,'w').write(chr(10).join(L)), print('PATCHED skills_sync at line', m[0])) if m else print('skills_sync: import already OK, no patch'))([k for k,l in enumerate(L) if 'from hermes_constants import' in l]))(open(p).read().split(chr(10)))) or print('skills_sync: file absent, no patch')"

# Keep the upstream entrypoint dispatcher. It uses the full s6 supervision tree
# when the image owns PID 1 and safely falls back to stage2-hook.sh plus
# main-wrapper.sh on platforms that inject their own init. We only replace CMD;
# main-wrapper sees an executable first arg and runs secure-start.sh as hermes.
ENTRYPOINT [ "/opt/hermes/docker/entrypoint-dispatch.sh" ]
CMD [ "/opt/hermes/secure-start.sh" ]
