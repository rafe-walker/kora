#!/bin/bash
# Docker/Podman entrypoint: bootstrap config files into the mounted volume, then run hermes.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"

# --- Privilege dropping via gosu ---
# When started as root (the default for Docker, or fakeroot in rootless Podman),
# optionally remap the hermes user/group to match host-side ownership, fix volume
# permissions, then re-exec as hermes.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "$(id -u hermes)" ]; then
        echo "Changing hermes UID to $HERMES_UID"
        usermod -u "$HERMES_UID" hermes
    fi

    if [ -n "$HERMES_GID" ] && [ "$HERMES_GID" != "$(id -g hermes)" ]; then
        echo "Changing hermes GID to $HERMES_GID"
        # -o allows non-unique GID (e.g. macOS GID 20 "staff" may already exist
        # as "dialout" in the Debian-based container image)
        groupmod -o -g "$HERMES_GID" hermes 2>/dev/null || true
    fi

    # Fix ownership of the data volume. When HERMES_UID remaps the hermes user,
    # files created by previous runs (under the old UID) become inaccessible.
    # Always chown -R when UID was remapped; otherwise only if top-level is wrong.
    actual_hermes_uid=$(id -u hermes)
    needs_chown=false
    if [ -n "$HERMES_UID" ] && [ "$HERMES_UID" != "10000" ]; then
        needs_chown=true
    elif [ "$(stat -c %u "$HERMES_HOME" 2>/dev/null)" != "$actual_hermes_uid" ]; then
        needs_chown=true
    fi
    if [ "$needs_chown" = true ]; then
        echo "Fixing ownership of $HERMES_HOME to hermes ($actual_hermes_uid)"
        # In rootless Podman the container's "root" is mapped to an unprivileged
        # host UID — chown will fail.  That's fine: the volume is already owned
        # by the mapped user on the host side.
        chown -R hermes:hermes "$HERMES_HOME" 2>/dev/null || \
            echo "Warning: chown failed (rootless container?) — continuing anyway"
        # The .venv must also be re-chowned when UID is remapped, otherwise
        # lazy_deps.py cannot install platform packages (discord.py, etc.).
        chown -R hermes:hermes "$INSTALL_DIR/.venv" 2>/dev/null || \
            echo "Warning: chown .venv failed (rootless container?) — continuing anyway"
    fi

    # Ensure config.yaml is readable by the hermes runtime user even if it was
    # edited on the host after initial ownership setup. Must run here (as root)
    # rather than after the gosu drop, otherwise a non-root caller like
    # `docker run -u $(id -u):$(id -g)` hits "Operation not permitted" (#15865).
    if [ -f "$HERMES_HOME/config.yaml" ]; then
        chown hermes:hermes "$HERMES_HOME/config.yaml" 2>/dev/null || true
        chmod 640 "$HERMES_HOME/config.yaml" 2>/dev/null || true
    fi

    echo "Dropping root privileges"
    exec gosu hermes "$0" "$@"
fi

# --- Running as hermes from here ---

# ---------------------------------------------------------------------------
# R4.1 §9.2 gate 2 — fail closed if ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN
# is present. Kora authenticates via CLAUDE_CODE_OAUTH_TOKEN exclusively;
# a stray ANTHROPIC_* env var means a misconfigured deploy could silently
# bill against a different account. Fail loud, emit a diagnostic to stderr.
# ---------------------------------------------------------------------------
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: ANTHROPIC_API_KEY is set — Kora deploys must use" >&2
  echo "       CLAUDE_CODE_OAUTH_TOKEN exclusively (R4.1 §9.2 gate 2)." >&2
  echo "       Remove ANTHROPIC_API_KEY from the deploy env and retry." >&2
  exit 1
fi
if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "ERROR: ANTHROPIC_AUTH_TOKEN is set — Kora deploys must use" >&2
  echo "       CLAUDE_CODE_OAUTH_TOKEN exclusively (R4.1 §9.2 gate 2)." >&2
  echo "       Remove ANTHROPIC_AUTH_TOKEN from the deploy env and retry." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Required env vars (substrate connectivity — Doppler: kora-runtime-substrate)
# ---------------------------------------------------------------------------
REQUIRED_SUBSTRATE_VARS=(
  KORA_SERVICE_TOKEN
  KORA_ISOKRON_DSN
  KORA_DEFAULT_WORKSPACE_ID
  KORA_SEA_MCP_ENDPOINT
)

missing=()
for v in "${REQUIRED_SUBSTRATE_VARS[@]}"; do
  if [ -z "${!v:-}" ]; then
    missing+=("$v")
  fi
done

# ---------------------------------------------------------------------------
# Required env vars (Anthropic auth — Doppler: kora-runtime-anthropic)
# ---------------------------------------------------------------------------
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  missing+=("CLAUDE_CODE_OAUTH_TOKEN")
fi

if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: Kora startup blocked — missing required env vars:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo "" >&2
  echo "Source from 3 separate Doppler projects per R2 §5 / R4.1:" >&2
  echo "  kora-runtime-substrate  → wsk_* / DSN / workspace / MCP" >&2
  echo "  kora-runtime-anthropic  → CLAUDE_CODE_OAUTH_TOKEN" >&2
  echo "  kora-runtime-gateways   → Slack / email / etc." >&2
  echo "" >&2
  echo "Compose with: doppler run -p kora-runtime-substrate -c prd -- \\" >&2
  echo "              doppler run -p kora-runtime-anthropic -c prd -- \\" >&2
  echo "              doppler run -p kora-runtime-gateways -c prd -- \\" >&2
  echo "              /opt/hermes/docker/entrypoint.sh" >&2
  exit 1
fi

# Slack gateway env vars — only validated if Slack is enabled.
# Non-fatal: an operator may intentionally disable Slack while keeping
# the YAML config block in place. Warn so the cause is visible in logs.
if [ "${SLACK_GATEWAY_ENABLED:-true}" = "true" ]; then
  SLACK_VARS=(SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_SIGNING_SECRET)
  for v in "${SLACK_VARS[@]}"; do
    if [ -z "${!v:-}" ]; then
      echo "WARNING: Slack gateway enabled but $v missing — Slack will not start." >&2
    fi
  done
fi

source "${INSTALL_DIR}/.venv/bin/activate"

# Stamp install method for detect_install_method()
echo "docker" > "${HERMES_HOME:=/opt/data}/.install_method" 2>/dev/null || true

# Create essential directory structure.  Cache and platform directories
# (cache/images, cache/audio, platforms/whatsapp, etc.) are created on
# demand by the application — don't pre-create them here so new installs
# get the consolidated layout from get_hermes_dir().
# The "home/" subdirectory is a per-profile HOME for subprocesses (git,
# ssh, gh, npm …).  Without it those tools write to /root which is
# ephemeral and shared across profiles.  See issue #4426.
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home}

# .env
if [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
fi

# config.yaml
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
fi

# SOUL.md
if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
    cp "$INSTALL_DIR/docker/SOUL.md" "$HERMES_HOME/SOUL.md"
fi

# auth.json: bootstrap from env on first boot only.  Used by orchestrators
# (e.g. provisioning a Hermes VPS from an account-management service) that
# need to seed the OAuth refresh credential non-interactively, instead of
# walking the user through `hermes setup` + the device-flow login dance.
# Subsequent token rotations write back to the same file, which lives on a
# persistent volume — so this env var is consumed exactly once at first
# boot.  The `[ ! -f ... ]` guard is critical: without it, a container
# restart would clobber a rotated refresh token with the now-stale value
# the orchestrator originally seeded.
if [ ! -f "$HERMES_HOME/auth.json" ] && [ -n "$HERMES_AUTH_JSON_BOOTSTRAP" ]; then
    printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" > "$HERMES_HOME/auth.json"
    chmod 600 "$HERMES_HOME/auth.json"
fi

# Sync bundled skills (manifest-based so user edits are preserved)
if [ -d "$INSTALL_DIR/skills" ]; then
    python3 "$INSTALL_DIR/tools/skills_sync.py"
fi

# NOTE (KR-D-DEPLOY ST1): the legacy HERMES_DASHBOARD background-launch
# branch was removed here. The daemon's web listener (kora daemon ->
# WebListener uvicorn) serves the admin UI on 9119 by default; the
# duplicate background launch would have raced with the daemon for the
# port. The `hermes dashboard` subcommand remains available for ad-hoc
# localhost invocation outside the daemon — set HERMES_DASHBOARD locally
# only if you're not running the daemon.

# Final dispatch (KR-D-DEPLOY ST1): the exec-decision logic moved to
# docker/dispatch.sh so it can be unit-tested in isolation. The dispatch
# script handles:
#
#   docker run <image>                 -> exec `hermes daemon` (NEW default)
#   docker run <image> chat -q "..."   -> exec `hermes chat -q "..."`
#   docker run <image> sleep infinity  -> exec `sleep infinity` directly
#   docker run <image> bash            -> exec `bash` directly
#   docker run <image> hermes daemon   -> exec `hermes daemon` (wrapped in
#                                         Doppler in deploy envs)
#
# In a deploy env (KORA_DEPLOY_ENV set + != "dev", `doppler` on PATH) any
# hermes/kora invocation is wrapped with the 3-project Doppler nested run
# so secrets from kora-runtime-substrate / -anthropic / -gateways are
# injected. See docker/dispatch.sh for the full decision tree.
exec /opt/hermes/docker/dispatch.sh "$@"
