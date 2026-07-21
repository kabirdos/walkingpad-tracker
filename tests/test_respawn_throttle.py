import json
import os

import pytest

import walkingpad.cli as cli


def _ledger(tmp_path):
    # The ledger lives beside the db; pass a db path in tmp_path and derive it
    # the same way the daemon does.
    db = os.path.join(tmp_path, "walkingpad.db")
    return db, cli._respawn_ledger_path(db)


def test_respawn_allowed_fast_path():
    # The first few respawns are allowed immediately so a genuinely stuck
    # CoreBluetooth cache gets cleared quickly.
    now = 1000.0
    assert cli._respawn_allowed([], now)
    assert cli._respawn_allowed([now - 5], now)
    assert cli._respawn_allowed([now - 10, now - 5], now)


def test_respawn_throttled_after_fast_limit():
    # Once we've hit the fast limit with no successful connect, respawns are
    # rate-limited: denied until SLOW_RESPAWN_INTERVAL_S has elapsed.
    now = 100000.0
    recent = [now - 120, now - 60, now - 30]  # 3 respawns, all recent
    assert len(recent) == cli.FAST_RESPAWN_LIMIT
    assert not cli._respawn_allowed(recent, now)

    # ...but allowed again once the slow interval has passed since the last one.
    stale = [now - 3 * cli.SLOW_RESPAWN_INTERVAL_S,
             now - 2 * cli.SLOW_RESPAWN_INTERVAL_S,
             now - cli.SLOW_RESPAWN_INTERVAL_S - 1]
    assert cli._respawn_allowed(stale, now)


def test_load_respawns_drops_stale_and_survives_corruption(tmp_path):
    _, path = _ledger(str(tmp_path))
    now = 500000.0

    # Missing ledger reads as empty.
    assert cli._load_respawns(path, now) == []

    # Stale entries (older than RESPAWN_STALE_S) are pruned; fresh ones kept.
    fresh = now - 10
    with open(path, "w") as f:
        json.dump([now - cli.RESPAWN_STALE_S - 1, fresh], f)
    assert cli._load_respawns(path, now) == [fresh]

    # Corrupt JSON must not blow up the watchdog — reads as empty.
    with open(path, "w") as f:
        f.write("{not json")
    assert cli._load_respawns(path, now) == []


def test_save_and_clear_respawns_roundtrip(tmp_path):
    _, path = _ledger(str(tmp_path))
    now = 42.0
    assert cli._save_respawns(path, [now]) is True
    assert cli._load_respawns(path, now) == [now]
    assert cli._clear_respawns(path) is True
    assert not os.path.exists(path)
    # Clearing an already-absent ledger reports success (nothing left to clear).
    assert cli._clear_respawns(path) is True


def test_save_respawns_reports_failure_on_unwritable_dir(tmp_path):
    # Ledger dir doesn't exist -> the write fails and must be reported, not
    # swallowed, so the caller can refuse to respawn untracked.
    path = os.path.join(str(tmp_path), "missing-dir", "respawn_history.json")
    assert cli._save_respawns(path, [1.0]) is False


class _Replaced(Exception):
    """Stands in for a successful execv, which replaces the process image and
    therefore never returns to its caller."""


def test_maybe_reexec_runs_and_records_when_allowed(tmp_path, monkeypatch):
    db, path = _ledger(str(tmp_path))
    now = 777.0
    calls = []

    def fake_reexec(streak):
        calls.append(streak)
        raise _Replaced

    monkeypatch.setattr(cli, "_reexec_self", fake_reexec)

    with pytest.raises(_Replaced):
        cli._maybe_reexec(db, 6, now=now)

    assert calls == [6], "should re-exec when the throttle allows it"
    # The record must survive into the replacement image — it's the only memory
    # the new process has of how many times we've already tried.
    assert cli._load_respawns(path, now) == [now], "must record the re-exec"


def test_maybe_reexec_suppressed_when_throttled(tmp_path, monkeypatch):
    db, path = _ledger(str(tmp_path))
    now = 900000.0
    # Pre-seed the fast limit's worth of recent re-execs so another is denied.
    recent = [now - 90, now - 60, now - 30]
    cli._save_respawns(path, recent)

    calls = []
    monkeypatch.setattr(cli, "_reexec_self", lambda streak: calls.append(streak))

    cli._maybe_reexec(db, 6, now=now)

    assert calls == [], "must not re-exec while throttled — keep scanning"
    assert cli._load_respawns(path, now) == recent, "ledger unchanged when suppressed"


def test_maybe_reexec_refuses_when_ledger_unwritable(tmp_path, monkeypatch):
    # If we can't persist the re-exec, we can't throttle the next one, so we
    # must skip it — untracked re-execs churn the process every ~60s while the
    # pad is off. db lives in a non-existent dir so the write fails.
    db = os.path.join(str(tmp_path), "missing-dir", "walkingpad.db")
    now = 1234.0
    calls = []
    monkeypatch.setattr(cli, "_reexec_self", lambda streak: calls.append(streak))

    cli._maybe_reexec(db, 6, now=now)

    assert calls == [], "must not re-exec when the ledger can't be persisted"


def test_maybe_reexec_refunds_the_ledger_when_exec_fails(tmp_path, monkeypatch):
    # A re-exec that never happened must not consume throttle budget: three
    # failed execs would otherwise strand a genuinely wedged scanner in the
    # 30-minute slow path having accomplished nothing.
    db, path = _ledger(str(tmp_path))
    now = 555.0
    # _reexec_self returning is exactly what a failed exec looks like.
    monkeypatch.setattr(cli, "_reexec_self", lambda streak: None)

    cli._maybe_reexec(db, 6, now=now)

    assert cli._load_respawns(path, now) == [], "failed exec must not be recorded"


def test_reexec_self_replaces_image_with_module_command(monkeypatch):
    # The daemon must re-exec itself rather than exit: launchd has permanently
    # pended respawns for this job, so an exit is death. Re-exec must go through
    # `-m walkingpad.cli` so both launch styles normalize to one command line,
    # and must preserve the subcommand/flags the daemon was started with.
    monkeypatch.setattr(cli.sys, "executable", "/venv/bin/python3")
    monkeypatch.setattr(cli.sys, "argv",
                        ["/pkg/walkingpad/cli.py", "serve", "--vault", "/v"])
    seen = {}

    def fake_execv(path, argv):
        seen["path"], seen["argv"] = path, argv

    monkeypatch.setattr(cli.os, "execv", fake_execv)

    cli._reexec_self(6)

    assert seen["path"] == "/venv/bin/python3"
    assert seen["argv"] == ["/venv/bin/python3", "-m", "walkingpad.cli",
                            "serve", "--vault", "/v"]


def test_reexec_self_survives_a_failed_exec(monkeypatch):
    # A failed exec leaves us as the old (possibly wedged) process. Keep running
    # and scanning — a live daemon serving stale scans beats no daemon at all,
    # which is exactly the hole the old exit-for-respawn fell into.
    monkeypatch.setattr(cli.sys, "executable", "/venv/bin/python3")
    monkeypatch.setattr(cli.sys, "argv", ["/pkg/walkingpad/cli.py", "serve"])

    def boom(path, argv):
        raise OSError("exec format error")

    monkeypatch.setattr(cli.os, "execv", boom)

    cli._reexec_self(6)  # must return instead of raising or exiting


def test_reexec_self_skipped_without_interpreter_path(monkeypatch):
    # Without sys.executable there's nothing to exec; don't crash the daemon.
    monkeypatch.setattr(cli.sys, "executable", "")
    called = []
    monkeypatch.setattr(cli.os, "execv",
                        lambda p, a: called.append((p, a)))

    cli._reexec_self(6)

    assert called == [], "must not attempt exec without an interpreter path"
