"""KR-1 ST3 tests — module rename + ~/.hermes → ~/.kora migration.

Covers:

1. **Import smoke** — all renamed modules (`kora_constants`, `kora_bootstrap`,
   `kora_state`, `kora_logging`, `kora_time`, `kora_cli`) import cleanly.
2. **`get_kora_home()` resolution order** — KORA_HOME env > HERMES_HOME env
   (BC) > ~/.kora dir > ~/.hermes dir (BC) > ~/.kora default.
3. **`init_kora_home_env()`** — bidirectional env var sync at bootstrap.
4. **`migrate_hermes_home` script** — --check / --symlink / --copy / --force,
   idempotency, missing-legacy behavior.

These tests mock `Path.home()` (when not using a real tmp_path) and clear
KORA_HOME/HERMES_HOME from `os.environ` so they don't pick up the host
machine's actual state.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Import smoke — KR-1 ST3 module renames
# ---------------------------------------------------------------------------


def test_renamed_modules_import_cleanly():
    """The five top-level renamed modules + kora_cli package import without error."""
    import kora_bootstrap  # noqa: F401
    import kora_constants  # noqa: F401
    import kora_state  # noqa: F401
    import kora_logging  # noqa: F401
    import kora_time  # noqa: F401
    import kora_cli  # noqa: F401


def test_renamed_helper_names_are_exported_from_kora_constants():
    """The Kora-named helpers exist after the ST3 rename."""
    import kora_constants

    expected = [
        "get_kora_home",
        "get_kora_home_override",
        "set_kora_home_override",
        "reset_kora_home_override",
        "get_default_kora_root",
        "get_kora_dir",
        "display_kora_home",
        "propagate_kora_home_env",
        "get_optional_skills_dir",
        "get_bundled_skills_dir",
    ]
    missing = [name for name in expected if not hasattr(kora_constants, name)]
    assert not missing, f"kora_constants missing renamed helpers: {missing}"


def test_legacy_helper_names_are_removed():
    """The Hermes-named helpers no longer exist (ST3 deletes — no BC at this seam)."""
    import kora_constants

    legacy = [
        "get_hermes_home",
        "get_hermes_home_override",
        "set_hermes_home_override",
        "reset_hermes_home_override",
        "get_default_hermes_root",
        "get_hermes_dir",
        "display_hermes_home",
    ]
    leftover = [name for name in legacy if hasattr(kora_constants, name)]
    assert not leftover, (
        f"kora_constants still exports legacy helpers: {leftover}. "
        "ST3 sweep is incomplete."
    )


# ---------------------------------------------------------------------------
# get_kora_home() resolution order
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Clear KORA_HOME/HERMES_HOME and point Path.home() at a tmp dir."""
    monkeypatch.delenv("KORA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Reset module-level warn-once flags so each test sees a fresh state.
    import kora_constants
    kora_constants._hermes_env_var_bc_warned = False
    kora_constants._hermes_home_dir_bc_warned = False
    kora_constants._profile_fallback_warned = False
    yield tmp_path


def test_get_kora_home_prefers_KORA_HOME_env(isolated_env, monkeypatch):
    custom = isolated_env / "custom_kora"
    custom.mkdir()
    monkeypatch.setenv("KORA_HOME", str(custom))
    from kora_constants import get_kora_home
    assert get_kora_home() == custom


def test_get_kora_home_falls_back_to_HERMES_HOME_env(isolated_env, monkeypatch, capsys):
    legacy = isolated_env / "custom_hermes"
    legacy.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(legacy))
    from kora_constants import get_kora_home
    assert get_kora_home() == legacy
    # Warn-once message goes to stderr.
    err = capsys.readouterr().err
    assert "[KORA_HOME bc] Using legacy HERMES_HOME" in err


def test_get_kora_home_prefers_dotkora_dir_when_envs_unset(isolated_env):
    kora_dir = isolated_env / ".kora"
    kora_dir.mkdir()
    from kora_constants import get_kora_home
    assert get_kora_home() == kora_dir


def test_get_kora_home_falls_back_to_dothermes_dir_with_warning(isolated_env, capsys):
    hermes_dir = isolated_env / ".hermes"
    hermes_dir.mkdir()
    # ~/.kora does NOT exist
    from kora_constants import get_kora_home
    assert get_kora_home() == hermes_dir
    err = capsys.readouterr().err
    assert "[KORA_HOME bc] Using legacy ~/.hermes" in err


def test_get_kora_home_defaults_to_dotkora_when_nothing_exists(isolated_env):
    from kora_constants import get_kora_home
    home = get_kora_home()
    assert home == isolated_env / ".kora"
    # Note: get_kora_home() does NOT create the directory; caller does.


def test_get_kora_home_warn_once(isolated_env, monkeypatch, capsys):
    """The legacy-env-var warning fires only once per process."""
    legacy = isolated_env / "custom_hermes"
    legacy.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(legacy))
    from kora_constants import get_kora_home
    get_kora_home()
    err_1 = capsys.readouterr().err
    get_kora_home()
    err_2 = capsys.readouterr().err
    # First call warns; subsequent calls do not.
    assert "[KORA_HOME bc]" in err_1
    assert err_2.count("[KORA_HOME bc]") == 0


# ---------------------------------------------------------------------------
# init_kora_home_env (bootstrap)
# ---------------------------------------------------------------------------


def test_init_kora_home_env_mirrors_kora_to_hermes(monkeypatch):
    monkeypatch.delenv("KORA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("KORA_HOME", "/some/path")
    import kora_bootstrap
    kora_bootstrap._kora_home_env_init_applied = False
    kora_bootstrap._kora_home_env_warned = False
    mutated = kora_bootstrap.init_kora_home_env()
    assert mutated is True
    assert os.environ.get("HERMES_HOME") == "/some/path"


def test_init_kora_home_env_mirrors_hermes_to_kora(monkeypatch, capsys):
    monkeypatch.delenv("KORA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/legacy/path")
    import kora_bootstrap
    kora_bootstrap._kora_home_env_init_applied = False
    kora_bootstrap._kora_home_env_warned = False
    mutated = kora_bootstrap.init_kora_home_env()
    assert mutated is True
    assert os.environ.get("KORA_HOME") == "/legacy/path"
    err = capsys.readouterr().err
    assert "HERMES_HOME is set but KORA_HOME is not" in err


def test_init_kora_home_env_noop_when_both_set(monkeypatch):
    monkeypatch.setenv("KORA_HOME", "/k")
    monkeypatch.setenv("HERMES_HOME", "/h")
    import kora_bootstrap
    kora_bootstrap._kora_home_env_init_applied = False
    kora_bootstrap._kora_home_env_warned = False
    mutated = kora_bootstrap.init_kora_home_env()
    assert mutated is False
    assert os.environ.get("KORA_HOME") == "/k"
    assert os.environ.get("HERMES_HOME") == "/h"


def test_init_kora_home_env_noop_when_neither_set(monkeypatch):
    monkeypatch.delenv("KORA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    import kora_bootstrap
    kora_bootstrap._kora_home_env_init_applied = False
    kora_bootstrap._kora_home_env_warned = False
    mutated = kora_bootstrap.init_kora_home_env()
    assert mutated is False
    assert "KORA_HOME" not in os.environ
    assert "HERMES_HOME" not in os.environ


def test_init_kora_home_env_idempotent(monkeypatch):
    monkeypatch.delenv("KORA_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/legacy")
    import kora_bootstrap
    kora_bootstrap._kora_home_env_init_applied = False
    kora_bootstrap._kora_home_env_warned = False
    assert kora_bootstrap.init_kora_home_env() is True
    # Second call is a no-op (guard flag set).
    assert kora_bootstrap.init_kora_home_env() is False


# ---------------------------------------------------------------------------
# migrate_hermes_home script
# ---------------------------------------------------------------------------


def _make_legacy(tmp_path: Path) -> Path:
    """Create a fake ~/.hermes install at tmp_path/.hermes with two files."""
    legacy = tmp_path / ".hermes"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("model:\n  provider: kora\n", encoding="utf-8")
    (legacy / "SOUL.md").write_text("You are Hermes (legacy).\n", encoding="utf-8")
    return legacy


def test_migrate_check_reports_no_legacy(tmp_path, capsys):
    from kora_cli.migrate_hermes_home import main
    legacy = tmp_path / ".hermes"
    target = tmp_path / ".kora"
    rc = main(["--check", "--from", str(legacy), "--to", str(target)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "legacy-does-not-exist" in err


def test_migrate_check_reports_legacy_present(tmp_path, capsys):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    rc = main(["--check", "--from", str(legacy), "--to", str(target)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "event=found" in err
    assert "event=recommend mode=symlink" in err


def test_migrate_symlink_creates_link(tmp_path):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    rc = main(["--symlink", "--from", str(legacy), "--to", str(target)])
    assert rc == 0
    assert target.is_symlink()
    assert target.resolve() == legacy.resolve()
    # Files visible through the symlink.
    assert (target / "config.yaml").read_text(encoding="utf-8").startswith("model:")


def test_migrate_symlink_idempotent_when_already_correct(tmp_path):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    assert main(["--symlink", "--from", str(legacy), "--to", str(target)]) == 0
    # Second run is also a no-op success.
    assert main(["--symlink", "--from", str(legacy), "--to", str(target)]) == 0
    assert target.is_symlink()


def test_migrate_symlink_refuses_to_clobber_without_force(tmp_path):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    target.mkdir()
    (target / "existing_file.txt").write_text("don't clobber me", encoding="utf-8")
    rc = main(["--symlink", "--from", str(legacy), "--to", str(target)])
    assert rc != 0
    # File untouched.
    assert (target / "existing_file.txt").read_text(encoding="utf-8") == "don't clobber me"


def test_migrate_symlink_with_force_clobbers(tmp_path):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    target.mkdir()
    (target / "stale.txt").write_text("stale", encoding="utf-8")
    rc = main(["--symlink", "--force", "--from", str(legacy), "--to", str(target)])
    assert rc == 0
    assert target.is_symlink()
    assert (target / "config.yaml").exists()  # Through the symlink.


def test_migrate_copy_does_deep_copy(tmp_path):
    from kora_cli.migrate_hermes_home import main
    legacy = _make_legacy(tmp_path)
    target = tmp_path / ".kora"
    rc = main(["--copy", "--from", str(legacy), "--to", str(target)])
    assert rc == 0
    assert target.is_dir()
    assert not target.is_symlink()
    # Both legacy and target exist independently.
    assert legacy.is_dir()
    assert (target / "config.yaml").read_text(encoding="utf-8").startswith("model:")
    assert (target / "SOUL.md").read_text(encoding="utf-8").startswith("You are Hermes")
    # Writes to target don't affect legacy.
    (target / "new_file.txt").write_text("kora-only", encoding="utf-8")
    assert not (legacy / "new_file.txt").exists()


def test_migrate_missing_legacy_errors_in_symlink_mode(tmp_path, capsys):
    from kora_cli.migrate_hermes_home import main
    legacy = tmp_path / ".hermes"  # Does not exist.
    target = tmp_path / ".kora"
    rc = main(["--symlink", "--from", str(legacy), "--to", str(target)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "legacy-does-not-exist" in err
