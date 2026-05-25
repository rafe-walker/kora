"""Shared constants for the Kora runtime.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.

Kora inherits this resolver from upstream ``NousResearch/hermes-agent`` and
extends it with backwards-compat for the legacy ``HERMES_HOME`` env var
and ``~/.hermes`` install directory. KR-1 ST3 made the rename:

* ``KORA_HOME`` is the primary env var; ``HERMES_HOME`` is read as a
  fallback with a one-time stderr warning recommending migration.
* ``~/.kora`` is the default install dir; ``~/.hermes`` is honored as
  a fallback when ``~/.kora`` does not yet exist (also warns once).

See ``kora_cli/migrate_hermes_home.py`` for the operator-facing migration
script that copies/symlinks ``~/.hermes`` to ``~/.kora``.
"""

import os
import sys
import sysconfig
from contextvars import ContextVar, Token
from pathlib import Path


_profile_fallback_warned: bool = False
_hermes_env_var_bc_warned: bool = False
_hermes_home_dir_bc_warned: bool = False
_UNSET = object()
_KORA_HOME_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_KORA_HOME_OVERRIDE", default=_UNSET
)


def _warn_hermes_env_var_bc_once() -> None:
    """Warn (to stderr, once per process) that HERMES_HOME is being used.

    Triggered when the operator has not set KORA_HOME but HAS set
    HERMES_HOME. We honor the legacy value but tell them to migrate.
    Stderr-direct (not via ``logging``) because this resolver runs at
    module-import time across 30+ call sites; logging may not be wired
    yet.
    """
    global _hermes_env_var_bc_warned
    if _hermes_env_var_bc_warned:
        return
    _hermes_env_var_bc_warned = True
    try:
        sys.stderr.write(
            "[KORA_HOME bc] Using legacy HERMES_HOME env var. Migrate to "
            "KORA_HOME (see `kora migrate-hermes-home --help`). "
            "HERMES_HOME support will be removed after KR-2.\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _warn_hermes_home_dir_bc_once() -> None:
    """Warn (to stderr, once per process) that ~/.hermes is being used.

    Triggered when no KORA_HOME/HERMES_HOME env var is set, no ~/.kora
    directory exists, but ~/.hermes does. We honor it as a fallback
    install location and tell the operator to migrate.
    """
    global _hermes_home_dir_bc_warned
    if _hermes_home_dir_bc_warned:
        return
    _hermes_home_dir_bc_warned = True
    try:
        sys.stderr.write(
            "[KORA_HOME bc] Using legacy ~/.hermes install directory "
            "(~/.kora does not yet exist). Run `kora migrate-hermes-home` "
            "to copy/symlink ~/.hermes → ~/.kora. Legacy fallback will be "
            "removed after KR-2.\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def set_kora_home_override(path: str | Path | None) -> Token:
    """Set a context-local Kora home override and return its reset token.

    This is for in-process, per-task scoping.  It deliberately does not
    mutate ``os.environ`` because that is shared by every thread in the
    process.
    """
    value: str | object = _UNSET if path is None else str(path)
    return _KORA_HOME_OVERRIDE.set(value)


def reset_kora_home_override(token: Token) -> None:
    """Restore the previous context-local Kora home override."""
    _KORA_HOME_OVERRIDE.reset(token)


def get_kora_home_override() -> str | None:
    """Return the active context-local Kora home override, if any."""
    override = _KORA_HOME_OVERRIDE.get()
    if override is _UNSET or not override:
        return None
    return str(override)


def get_kora_home() -> Path:
    """Return the Kora home directory (default: ~/.kora).

    Resolution order:
        1. In-process ``KORA_HOME`` override (set via ``set_kora_home_override``).
        2. ``KORA_HOME`` env var.
        3. ``HERMES_HOME`` env var (BC; warns once and recommends migration).
        4. ``~/.kora`` if it exists on disk.
        5. ``~/.hermes`` if it exists on disk (BC; warns once and
           recommends migration via ``kora migrate-hermes-home``).
        6. Default to ``~/.kora`` (will be created on first write).

    This is the single source of truth — all other copies should import this.

    When ``KORA_HOME`` is unset but an ``active_profile`` file indicates
    a non-default profile is active, logs a loud one-shot warning to
    ``errors.log`` so cross-profile data corruption is diagnosable instead
    of silent.  Behavior is unchanged otherwise — we still return
    ``~/.kora`` — because raising here would brick 30+ module-level
    callers that import this at load time.  Subprocess spawners are
    expected to propagate ``KORA_HOME`` (and, for now, ``HERMES_HOME``)
    explicitly (see the systemd template in ``kora_cli/gateway.py`` and
    the kanban dispatcher in ``kora_cli/kanban_db.py``).  See upstream
    https://github.com/NousResearch/hermes-agent/issues/18594.
    """
    override = get_kora_home_override()
    if override:
        return Path(override)

    val = os.environ.get("KORA_HOME", "").strip()
    if val:
        return Path(val)

    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        _warn_hermes_env_var_bc_once()
        return Path(val)

    kora_home = Path.home() / ".kora"
    hermes_home = Path.home() / ".hermes"

    # Guard: if a non-default profile is sticky-active, warn once that
    # the fallback to the default profile is almost certainly wrong.
    # Fires BEFORE returning the resolved home, regardless of whether
    # ~/.kora or ~/.hermes exists — the wrongness is about KORA_HOME
    # being missing, not about which fallback dir we land in.
    global _profile_fallback_warned
    if not _profile_fallback_warned:
        try:
            # Inline the default-root resolution from get_default_kora_root()
            # to stay import-safe (this function is called from module scope
            # in 30+ files; we cannot afford to trigger logging setup here).
            active_path = kora_home / "active_profile"
            if not active_path.exists():
                # Check legacy location too — operator may not have migrated yet.
                active_path = hermes_home / "active_profile"
            active = active_path.read_text().strip() if active_path.exists() else ""
        except (UnicodeDecodeError, OSError):
            active = ""
        if active and active != "default":
            _profile_fallback_warned = True
            msg = (
                f"[KORA_HOME fallback] KORA_HOME is unset but active "
                f"profile is {active!r}. Falling back to ~/.kora, which "
                f"is the DEFAULT profile — not {active!r}. Any data this "
                f"process writes will land in the wrong profile. The "
                f"subprocess spawner should pass KORA_HOME explicitly "
                f"(see upstream issue #18594)."
            )
            try:
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    if kora_home.exists():
        return kora_home

    if hermes_home.exists():
        _warn_hermes_home_dir_bc_once()
        return hermes_home

    return kora_home


def get_default_kora_root() -> Path:
    """Return the root Kora directory for profile-level operations.

    In standard deployments this is ``~/.kora``.

    In Docker or custom deployments where ``KORA_HOME`` points outside
    ``~/.kora`` (e.g. ``/opt/data``), returns ``KORA_HOME`` directly
    — that IS the root.

    In profile mode where ``KORA_HOME`` is ``<root>/profiles/<name>``,
    returns ``<root>`` so that ``profile list`` can see all profiles.
    Works both for standard (``~/.kora/profiles/coder``) and Docker
    (``/opt/data/profiles/coder``) layouts.

    Honors ``HERMES_HOME`` as a backwards-compat fallback for the env
    var, and ``~/.hermes`` as a backwards-compat fallback for the
    on-disk default — both warn once.

    Import-safe — no dependencies beyond stdlib.
    """
    native_home = Path.home() / ".kora"
    env_home = os.environ.get("KORA_HOME", "")
    if not env_home:
        env_home = os.environ.get("HERMES_HOME", "")
        if env_home:
            _warn_hermes_env_var_bc_once()
    if not env_home:
        # No env override — return the on-disk default, accounting for the
        # legacy ~/.hermes layout if ~/.kora is not yet provisioned.
        if not native_home.exists() and (Path.home() / ".hermes").exists():
            _warn_hermes_home_dir_bc_once()
            return Path.home() / ".hermes"
        return native_home

    env_path = Path(env_home)
    try:
        env_path.resolve().relative_to(native_home.resolve())
        # KORA_HOME is under ~/.kora (normal or profile mode)
        return native_home
    except ValueError:
        pass

    # Legacy: KORA_HOME may point under ~/.hermes during BC operation.
    legacy_home = Path.home() / ".hermes"
    try:
        env_path.resolve().relative_to(legacy_home.resolve())
        return legacy_home
    except ValueError:
        pass

    # Docker / custom deployment.
    # Check if this is a profile path: <root>/profiles/<name>
    # If the immediate parent dir is named "profiles", the root is
    # the grandparent — this covers Docker profiles correctly.
    if env_path.parent.name == "profiles":
        return env_path.parent.parent

    # Not a profile path — KORA_HOME itself is the root
    return env_path


def _get_packaged_data_dir(name: str) -> Path | None:
    """Return an installed data-files directory if one exists.

    Used to discover bundled skills/optional-skills when Kora is installed
    from a wheel that emitted them via setuptools data_files.
    """
    candidates = []
    for scheme in ("data", "purelib", "platlib"):
        raw = sysconfig.get_path(scheme)
        if raw:
            candidates.append(Path(raw) / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def get_optional_skills_dir(default: Path | None = None) -> Path:
    """Return the optional-skills directory, honoring package-manager wrappers.

    Packaged installs may ship ``optional-skills`` outside the Python package
    tree and expose it via ``KORA_OPTIONAL_SKILLS`` (or the legacy
    ``HERMES_OPTIONAL_SKILLS`` for BC).
    """
    override = os.getenv("KORA_OPTIONAL_SKILLS", "").strip()
    if not override:
        override = os.getenv("HERMES_OPTIONAL_SKILLS", "").strip()
        if override:
            _warn_hermes_env_var_bc_once()
    if override:
        return Path(override)
    packaged = _get_packaged_data_dir("optional-skills")
    if packaged is not None:
        return packaged
    if default is not None:
        return default
    return get_kora_home() / "optional-skills"


def get_bundled_skills_dir(default: Path | None = None) -> Path:
    """Return the bundled skills directory for source and packaged installs.

    Resolution order:
        1. ``KORA_BUNDLED_SKILLS`` env var (Nix wrapper / explicit override).
        2. ``HERMES_BUNDLED_SKILLS`` env var (BC; warns once).
        3. Wheel-installed ``<sysconfig data>/skills`` (pip install path).
        4. Caller-supplied ``default`` (typically the source-checkout path).
        5. ``<KORA_HOME>/skills`` last-resort.
    """
    override = os.getenv("KORA_BUNDLED_SKILLS", "").strip()
    if not override:
        override = os.getenv("HERMES_BUNDLED_SKILLS", "").strip()
        if override:
            _warn_hermes_env_var_bc_once()
    if override:
        return Path(override)
    packaged = _get_packaged_data_dir("skills")
    if packaged is not None:
        return packaged
    if default is not None:
        return default
    return get_kora_home() / "skills"


def get_kora_dir(new_subpath: str, old_name: str) -> Path:
    """Resolve a Kora subdirectory with backward compatibility.

    New installs get the consolidated layout (e.g. ``cache/images``).
    Existing installs that already have the old path (e.g. ``image_cache``)
    keep using it — no migration required.

    Args:
        new_subpath: Preferred path relative to KORA_HOME (e.g. ``"cache/images"``).
        old_name: Legacy path relative to KORA_HOME (e.g. ``"image_cache"``).

    Returns:
        Absolute ``Path`` — old location if it exists on disk, otherwise the new one.
    """
    home = get_kora_home()
    old_path = home / old_name
    if old_path.exists():
        return old_path
    return home / new_subpath


def display_kora_home() -> str:
    """Return a user-friendly display string for the current KORA_HOME.

    Uses ``~/`` shorthand for readability::

        default:  ``~/.kora``
        profile:  ``~/.kora/profiles/coder``
        custom:   ``/opt/kora-custom``
        legacy:   ``~/.hermes`` (during HERMES_HOME BC fallback)

    Use this in **user-facing** print/log messages instead of hardcoding
    ``~/.kora``.  For code that needs a real ``Path``, use
    :func:`get_kora_home` instead.
    """
    home = get_kora_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


def get_subprocess_home() -> str | None:
    """Return a per-profile HOME directory for subprocesses, or None.

    When ``{KORA_HOME}/home/`` exists on disk, subprocesses should use it
    as ``HOME`` so system tools (git, ssh, gh, npm …) write their configs
    inside the Kora data directory instead of the OS-level ``/root`` or
    ``~/``.  This provides:

    * **Docker persistence** — tool configs land inside the persistent volume.
    * **Profile isolation** — each profile gets its own git identity, SSH
      keys, gh tokens, etc.

    The Python process's own ``os.environ["HOME"]`` and ``Path.home()`` are
    **never** modified — only subprocess environments should inject this value.
    Activation is directory-based: if the ``home/`` subdirectory doesn't
    exist, returns ``None`` and behavior is unchanged.

    Honors both ``KORA_HOME`` and the legacy ``HERMES_HOME`` env var.
    """
    kora_home_env = (
        get_kora_home_override()
        or os.getenv("KORA_HOME")
        or os.getenv("HERMES_HOME")
    )
    if not kora_home_env:
        return None
    profile_home = os.path.join(kora_home_env, "home")
    if os.path.isdir(profile_home):
        return profile_home
    return None


def propagate_kora_home_env(path: str) -> None:
    """Write the Kora home path to both env-var names for subprocess BC.

    Sets ``KORA_HOME`` (primary) and ``HERMES_HOME`` (legacy) so any
    subprocess that reads either gets a consistent value. Use this at
    every site that previously called ``os.environ["HERMES_HOME"] = path``.
    """
    os.environ["KORA_HOME"] = path
    os.environ["HERMES_HOME"] = path


VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def parse_reasoning_effort(effort: str) -> dict | None:
    """Parse a reasoning effort level into a config dict.

    Valid levels: "none", "minimal", "low", "medium", "high", "xhigh".
    Returns None when the input is empty or unrecognized (caller uses default).
    Returns {"enabled": False} for "none".
    Returns {"enabled": True, "effort": <level>} for valid effort levels.
    """
    if not effort or not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort == "none":
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


def is_termux() -> bool:
    """Return True when running inside a Termux (Android) environment.

    Checks ``TERMUX_VERSION`` (set by Termux) or the Termux-specific
    ``PREFIX`` path.  Import-safe — no heavy deps.
    """
    prefix = os.getenv("PREFIX", "")
    return bool(os.getenv("TERMUX_VERSION") or "com.termux/files/usr" in prefix)


_wsl_detected: bool | None = None


def is_wsl() -> bool:
    """Return True when running inside WSL (Windows Subsystem for Linux).

    Checks ``/proc/version`` for the ``microsoft`` marker that both WSL1
    and WSL2 inject.  Result is cached for the process lifetime.
    Import-safe — no heavy deps.
    """
    global _wsl_detected
    if _wsl_detected is not None:
        return _wsl_detected
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            _wsl_detected = "microsoft" in f.read().lower()
    except Exception:
        _wsl_detected = False
    return _wsl_detected


_container_detected: bool | None = None


def is_container() -> bool:
    """Return True when running inside a Docker/Podman container.

    Checks ``/.dockerenv`` (Docker), ``/run/.containerenv`` (Podman),
    and ``/proc/1/cgroup`` for container runtime markers.  Result is
    cached for the process lifetime.  Import-safe — no heavy deps.
    """
    global _container_detected
    if _container_detected is not None:
        return _container_detected
    if os.path.exists("/.dockerenv"):
        _container_detected = True
        return True
    if os.path.exists("/run/.containerenv"):
        _container_detected = True
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup = f.read()
            if "docker" in cgroup or "podman" in cgroup or "/lxc/" in cgroup:
                _container_detected = True
                return True
    except OSError:
        pass
    _container_detected = False
    return False


# ─── Well-Known Paths ─────────────────────────────────────────────────────────


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under KORA_HOME.

    Replaces the ``get_kora_home() / "config.yaml"`` pattern repeated
    in 7+ files (skill_utils.py, kora_logging.py, kora_time.py, etc.).
    """
    return get_kora_home() / "config.yaml"


def get_skills_dir() -> Path:
    """Return the path to the skills directory under KORA_HOME."""
    return get_kora_home() / "skills"



def get_env_path() -> Path:
    """Return the path to the ``.env`` file under KORA_HOME."""
    return get_kora_home() / ".env"


# ─── Network Preferences ─────────────────────────────────────────────────────


def apply_ipv4_preference(force: bool = False) -> None:
    """Monkey-patch ``socket.getaddrinfo`` to prefer IPv4 connections.

    On servers with broken or unreachable IPv6, Python tries AAAA records
    first and hangs for the full TCP timeout before falling back to IPv4.
    This affects httpx, requests, urllib, the OpenAI SDK — everything that
    uses ``socket.getaddrinfo``.

    When *force* is True, patches ``getaddrinfo`` so that calls with
    ``family=AF_UNSPEC`` (the default) resolve as ``AF_INET`` instead,
    skipping IPv6 entirely.  If no A record exists, falls back to the
    original unfiltered resolution so pure-IPv6 hosts still work.

    Safe to call multiple times — only patches once.
    Set ``network.force_ipv4: true`` in ``config.yaml`` to enable.
    """
    if not force:
        return

    import socket

    # Guard against double-patching
    if getattr(socket.getaddrinfo, "_kora_ipv4_patched", False):
        return

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0:  # AF_UNSPEC — caller didn't request a specific family
            try:
                return _original_getaddrinfo(
                    host, port, socket.AF_INET, type, proto, flags
                )
            except socket.gaierror:
                # No A record — fall back to full resolution (pure-IPv6 hosts)
                return _original_getaddrinfo(host, port, family, type, proto, flags)
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _ipv4_getaddrinfo._kora_ipv4_patched = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _ipv4_getaddrinfo  # type: ignore[assignment]


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
