#!/bin/bash
# kora-runtime container dispatch — final exec logic for the entrypoint.
# Extracted from docker/entrypoint.sh (KR-D-DEPLOY ST1) so the dispatch
# semantics can be unit-tested in isolation with controlled PATH + env.
#
# Called from entrypoint.sh as: ``exec /opt/hermes/docker/dispatch.sh "$@"``
#
# Decision tree (in order):
#
#   1. NO args                       -> default to ``daemon`` (KR-D-DAEMON
#                                       ST1's `kora daemon` subcommand).
#   2. ``$1`` is a non-hermes/non-kora binary on PATH
#                                    -> exec verbatim (sleep / bash / gosu).
#   3. KORA_DEPLOY_ENV set + != "dev" + doppler on PATH
#                                    -> wrap with 3-project Doppler nested
#                                       run, then exec ``hermes "$@"``.
#   4. Fallback                      -> exec ``hermes "$@"`` (local dev,
#                                       no doppler, or KORA_DEPLOY_ENV unset).
#
# IMPORTANT: ``hermes`` and ``kora`` are EXCLUDED from step (2)'s escape
# hatch — they must always flow through the Doppler-wrap branch when in
# a deploy env. Otherwise ``docker run kora-runtime hermes daemon`` would
# silently bypass secret injection. Other binaries (bash, sleep, gosu)
# keep the direct-exec semantics they had before this refactor.
#
# Dry-run mode for tests: setting ``KORA_DISPATCH_DRY_RUN=1`` prints
# the resolved argv on stdout instead of exec'ing. Used by the pytest
# coverage in ``tests/docker/test_entrypoint_dispatch.py``.

set -e

# Step 1: default-to-daemon when no args.
if [ $# -eq 0 ]; then
    set -- "daemon"
fi

_do_exec() {
    if [ "${KORA_DISPATCH_DRY_RUN:-0}" = "1" ]; then
        # Quoted with %q so the test driver can reconstruct argv exactly.
        printf 'EXEC:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    exec "$@"
}

# Step 2: direct-exec escape hatch for non-hermes/non-kora PATH binaries.
if [ "$1" != "hermes" ] && [ "$1" != "kora" ] && command -v "$1" >/dev/null 2>&1; then
    _do_exec "$@"
    exit 0
fi

# Step 3: Doppler-wrap branch.
#
# Inner command varies: if the operator already typed `hermes` or `kora` as
# the first arg (e.g. `docker run kora-runtime hermes daemon`), don't prepend
# another `hermes` — exec the argv verbatim under the wrap. Otherwise (e.g.
# `docker run kora-runtime daemon` or no-args-defaults-to-daemon), prepend
# `hermes` so the args are interpreted as a hermes subcommand.
if [ -n "${KORA_DEPLOY_ENV:-}" ] \
   && [ "${KORA_DEPLOY_ENV}" != "dev" ] \
   && command -v doppler >/dev/null 2>&1; then
    if [ "$1" = "hermes" ] || [ "$1" = "kora" ]; then
        _do_exec doppler run -p kora-runtime-substrate -c "$KORA_DEPLOY_ENV" -- \
                 doppler run -p kora-runtime-anthropic -c "$KORA_DEPLOY_ENV" -- \
                 doppler run -p kora-runtime-gateways -c "$KORA_DEPLOY_ENV" -- \
                 "$@"
    else
        _do_exec doppler run -p kora-runtime-substrate -c "$KORA_DEPLOY_ENV" -- \
                 doppler run -p kora-runtime-anthropic -c "$KORA_DEPLOY_ENV" -- \
                 doppler run -p kora-runtime-gateways -c "$KORA_DEPLOY_ENV" -- \
                 hermes "$@"
    fi
    exit 0
fi

# Step 4: fallback — local dev or doppler-unavailable.
# Same hermes-prefix avoidance as step 3.
if [ "$1" = "hermes" ] || [ "$1" = "kora" ]; then
    _do_exec "$@"
else
    _do_exec hermes "$@"
fi
