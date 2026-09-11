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

# Run the dashboard as an s6-supervised, loopback-only service. Caddy is the
# only public listener and retains the HTTP basic-auth boundary.
ENV HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=127.0.0.1 \
    HERMES_DASHBOARD_PORT=9120 \
    HERMES_GATEWAY_BOOTSTRAP_STATE=running \
    HERMES_PID_SHED_THRESHOLD=750 \
    HERMES_PID_ALERT_THRESHOLD=850

# secure-start.sh prepares the proxy and add-on binaries, then runs Caddy as
# the container's foreground command. The gateway, loopback dashboard,
# filebrowser, WebDAV, and stale-process reaper are supervised by s6.
#
# Credentials come from env: DASHBOARD_USER, DASHBOARD_PASSWORD.
# If DASHBOARD_PASSWORD is unset, a random 24-char password is generated
# and printed to logs (visible in `railway logs`).
#
# Caddy binary is downloaded on first boot to /opt/data/bin/caddy and
# cached there for subsequent restarts (the volume persists).
COPY secure-start.sh /opt/hermes/secure-start.sh
COPY patch_weekly_client_time_cron.py /opt/hermes/patch_weekly_client_time_cron.py
COPY process_reaper.py /opt/hermes/process_reaper.py
COPY railway_prepare.py /opt/hermes/railway_prepare.py
COPY Dockerfile /opt/hermes/railway-derived.Dockerfile
COPY test_process_reaper.py /opt/hermes/test_process_reaper.py
COPY test_cont_init.py /opt/hermes/test_cont_init.py
COPY test_pid_pressure.py /opt/hermes/test_pid_pressure.py
COPY test_boot_contract.py /opt/hermes/test_boot_contract.py
COPY test_fallback_supervision.py /opt/hermes/test_fallback_supervision.py
COPY pid_pressure.py /opt/hermes/pid_pressure.py
COPY pid_pressure.py /opt/hermes/tools/pid_pressure.py
COPY patch_pid_pressure_guard.py /opt/hermes/patch_pid_pressure_guard.py
COPY patch_stage2_safety.py /opt/hermes/patch_stage2_safety.py
COPY patch_runtime_contract.py /opt/hermes/patch_runtime_contract.py
COPY fallback_lifecycle.py /opt/hermes/fallback_lifecycle.py
COPY fallback_lifecycle.py /opt/hermes/hermes_cli/fallback_lifecycle.py
COPY fallback_gateway_supervisor.py /opt/hermes/fallback_gateway_supervisor.py
COPY railway-cont-init /opt/hermes/railway-cont-init
COPY railway-cont-init /etc/cont-init.d/01a-railway-limits
COPY entrypoint-dispatch /opt/hermes/docker/entrypoint-dispatch.sh
COPY entrypoint-dispatch /opt/hermes/entrypoint-dispatch
COPY dashboard-run /etc/s6-overlay/s6-rc.d/dashboard/run
COPY dashboard-run /opt/hermes/dashboard-run
COPY s6-rc.d/ /etc/s6-overlay/s6-rc.d/
COPY s6-rc.d/ /opt/hermes/s6-rc.d/
RUN chmod +x \
    /opt/hermes/secure-start.sh \
    /opt/hermes/patch_weekly_client_time_cron.py \
    /opt/hermes/process_reaper.py \
    /opt/hermes/railway_prepare.py \
    /opt/hermes/patch_pid_pressure_guard.py \
    /opt/hermes/patch_stage2_safety.py \
    /opt/hermes/patch_runtime_contract.py \
    /opt/hermes/fallback_gateway_supervisor.py \
    /opt/hermes/docker/entrypoint-dispatch.sh \
    /etc/cont-init.d/01a-railway-limits \
    /etc/s6-overlay/s6-rc.d/dashboard/run \
    /etc/s6-overlay/s6-rc.d/process-reaper/run \
    /etc/s6-overlay/s6-rc.d/filebrowser/run \
    /etc/s6-overlay/s6-rc.d/rclone-webdav/run
RUN python3 /opt/hermes/patch_runtime_contract.py && \
    bash -n /opt/hermes/docker/main-wrapper.sh \
        /opt/hermes/bin/hermes /opt/hermes/docker/hermes-exec-shim.sh && \
    python3 -m py_compile /opt/hermes/hermes_cli/service_manager.py \
        /opt/hermes/hermes_cli/gateway.py /opt/hermes/hermes_cli/fallback_lifecycle.py \
        /opt/hermes/fallback_gateway_supervisor.py
RUN python3 /opt/hermes/patch_pid_pressure_guard.py
RUN python3 /opt/hermes/patch_stage2_safety.py && \
    bash -n /opt/hermes/docker/stage2-hook.sh
RUN cd /opt/hermes && \
    python3 -m unittest -v \
        test_process_reaper.py test_cont_init.py test_pid_pressure.py \
        test_boot_contract.py test_fallback_supervision.py && \
    rm -rf railway-derived.Dockerfile \
        test_process_reaper.py test_cont_init.py test_pid_pressure.py \
        test_boot_contract.py test_fallback_supervision.py pid_pressure.py \
        fallback_lifecycle.py railway-cont-init entrypoint-dispatch \
        dashboard-run s6-rc.d __pycache__

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
