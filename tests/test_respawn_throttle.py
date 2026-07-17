import json
import os

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


def test_maybe_force_respawn_exits_and_records_when_allowed(tmp_path, monkeypatch):
    db, path = _ledger(str(tmp_path))
    now = 777.0
    calls = []
    monkeypatch.setattr(cli, "_force_respawn", lambda streak: calls.append(streak))

    cli._maybe_force_respawn(db, 6, now=now)

    assert calls == [6], "should exit for a respawn when the throttle allows it"
    assert cli._load_respawns(path, now) == [now], "must record the respawn"


def test_maybe_force_respawn_suppressed_when_throttled(tmp_path, monkeypatch):
    db, path = _ledger(str(tmp_path))
    now = 900000.0
    # Pre-seed the fast limit's worth of recent respawns so another is denied.
    recent = [now - 90, now - 60, now - 30]
    cli._save_respawns(path, recent)

    calls = []
    monkeypatch.setattr(cli, "_force_respawn", lambda streak: calls.append(streak))

    cli._maybe_force_respawn(db, 6, now=now)

    assert calls == [], "must not respawn while throttled — keep scanning"
    assert cli._load_respawns(path, now) == recent, "ledger unchanged when suppressed"


def test_maybe_force_respawn_refuses_when_ledger_unwritable(tmp_path, monkeypatch):
    # If we can't persist the respawn, we can't throttle the next one, so we
    # must NOT exit — an untracked exit re-enables the ~60s crash-loop that
    # launchd eventually pends. db lives in a non-existent dir so the write fails.
    db = os.path.join(str(tmp_path), "missing-dir", "walkingpad.db")
    now = 1234.0
    calls = []
    monkeypatch.setattr(cli, "_force_respawn", lambda streak: calls.append(streak))

    cli._maybe_force_respawn(db, 6, now=now)

    assert calls == [], "must not respawn when the ledger can't be persisted"
