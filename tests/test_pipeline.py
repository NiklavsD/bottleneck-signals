import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from pipeline import monthly, fetch_census_all as census


NOW = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)


def test_failed_refresh_stops_before_later_commands(monkeypatch, tmp_path):
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=1, stderr="source failed")
    monkeypatch.setattr(monthly.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="ABORT"):
        monthly.refresh("test-run", tmp_path / "candidate.json")
    assert len(commands) == 1


def test_snapshot_must_belong_to_current_run(monkeypatch, tmp_path):
    path = tmp_path / "snapshot.json"
    for run, generated in [("old", NOW), ("new", NOW - dt.timedelta(days=1)),
                           ("new", NOW + dt.timedelta(seconds=1))]:
        path.write_text(json.dumps({"run_id": run, "generated_at": generated.isoformat()}))
        with pytest.raises(RuntimeError):
            monthly.read_snapshot("new", NOW, NOW, path)
    path.write_text(json.dumps({"run_id": "new", "generated_at": NOW.isoformat()}))
    assert monthly.read_snapshot("new", NOW, NOW, path)["run_id"] == "new"


def test_constituent_prices_cannot_be_indefinitely_carried_forward():
    px = pd.DataFrame({"fresh": [None, 101.], "stale": [100., None]},
                      index=pd.to_datetime(["2026-09-01", "2026-10-16"]))
    assert monthly.basket_prices(px, ["fresh"], NOW) == {"fresh": 101.}
    with pytest.raises(RuntimeError, match="stale"):
        monthly.basket_prices(px, ["stale"], NOW)


@pytest.mark.parametrize("price", [0., -1., float("inf"), float("-inf")])
def test_constituent_prices_must_be_finite_and_positive(price):
    px = pd.DataFrame({"MU": [price]}, index=pd.to_datetime(["2026-10-16"]))
    with pytest.raises(RuntimeError, match="nonpositive or nonfinite"):
        monthly.basket_prices(px, ["MU"], NOW)


def test_stale_or_future_active_decisions_are_rejected():
    for decision in ["2026-09-16", "2026-11-16", None]:
        with pytest.raises(RuntimeError):
            monthly.validate_calls([{"bottleneck": "memory", "state": "OUT", "decision_date": decision}], NOW)
    monthly.validate_calls([{"bottleneck": "memory", "state": "OUT", "decision_date": "2026-10-16"}], NOW)


@pytest.fixture
def fresh_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    folder = tmp_path / "data/processed"
    folder.mkdir(parents=True)
    pd.DataFrame({"month": ["2005-01-01", "2026-09-01"]}).to_csv(folder / "tw_revenue.csv", index=False)
    pd.DataFrame({"month": ["2013-01-01", "2026-09-01"]}).to_csv(folder / "korea_customs_exports.csv", index=False)
    pd.DataFrame({"month": ["2013-01-01", "2026-08-01"]}).to_csv(folder / "us_imports_all.csv", index=False)
    for filename in ["prices.csv", "universe_prices.csv.gz"]:
        pd.DataFrame({"MU": [100.]}, index=pd.to_datetime(["2026-10-16"])).to_csv(folder / filename)
    return folder


def test_sanity_checks_both_history_and_latest_coverage(fresh_inputs):
    monthly.sanity_check(NOW)
    pd.DataFrame({"month": ["2005-01-01", "2026-08-01"]}).to_csv(fresh_inputs / "tw_revenue.csv", index=False)
    with pytest.raises(RuntimeError, match="completed month"):
        monthly.sanity_check(NOW)


def test_sanity_rejects_all_nan_price_rows(fresh_inputs):
    pd.DataFrame({"MU": [None]}, index=pd.to_datetime(["2026-10-16"])).to_csv(fresh_inputs / "prices.csv")
    with pytest.raises(RuntimeError, match="price observations"):
        monthly.sanity_check(NOW)


def test_publish_retry_pushes_when_commit_already_exists(monkeypatch):
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(monthly.subprocess, "run", run)
    monthly.publish({"logged_at": NOW.isoformat(), "calls": [], "hash": "abc"})
    assert ["git", "push"] in commands
    assert not any(cmd[:2] == ["git", "commit"] for cmd in commands)


def test_publish_failure_does_not_promote_snapshot_and_retry_does_not_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backtest").mkdir()
    folder = tmp_path / "web/data"
    folder.mkdir(parents=True)
    public = folder / "snapshot.json"
    public.write_text("previous public snapshot")
    date = dt.datetime.now(dt.timezone.utc)
    month = pd.Timestamp(date.date()).replace(day=1)
    if date.day < 16:
        month -= pd.offsets.MonthBegin(1)
    call = {"bottleneck": "memory", "state": "OUT", "model": "v4", "C": 1., "M": -.1,
            "data_month": str((month - pd.offsets.MonthBegin(1)).date()),
            "decision_date": str((month + pd.Timedelta(days=15)).date()), "basket_prices": {"MU": 100.}}
    commands = []
    def build(cmd):
        commands.append(cmd)
        if "--output" in cmd:
            path = cmd[cmd.index("--output") + 1]
            run_id = cmd[cmd.index("--run-id") + 1]
            with open(path, "w") as handle:
                json.dump({"run_id": run_id, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "themes": []}, handle)
    monkeypatch.setattr(monthly, "checked_run", build)
    monkeypatch.setattr(monthly, "sanity_check", lambda now: None)
    monkeypatch.setattr(monthly, "validate_source_freshness", lambda calls, now: None)
    monkeypatch.setattr(monthly, "current_calls", lambda now: [call.copy()])
    monkeypatch.setattr(monthly, "v6_calls", lambda snap, now: [])
    monkeypatch.setattr(monthly, "publish", lambda entry: (_ for _ in ()).throw(RuntimeError("push failed")))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="push failed"):
            monthly.run_monthly(["--no-refresh", "--publish"])
        assert public.read_text() == "previous public snapshot"
        assert len((tmp_path / monthly.LOG).read_text().splitlines()) == 1
        assert not list(folder.glob("snapshot.monthly-*.json"))
        assert monthly.pending_publication()["period"] == month.strftime("%Y-%m")
    assert not any("--digest" in cmd for cmd in commands)
    call["state"] = "IN"
    with pytest.raises(RuntimeError, match="different recorded calls"):
        monthly.run_monthly(["--no-refresh", "--publish"])
    assert len((tmp_path / monthly.LOG).read_text().splitlines()) == 1


def test_pending_marker_survives_push_until_snapshot_promotion(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    folder = tmp_path / "data"
    folder.mkdir()
    marker = folder / "monthly_pending.json"
    marker.write_text(json.dumps({"hash": "abc", "period": "2026-10"}))
    def failed(cmd, **kwargs):
        if cmd == ["git", "push"]:
            raise subprocess.CalledProcessError(1, cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(monthly.subprocess, "run", failed)
    entry = {"logged_at": NOW.isoformat(), "calls": [], "hash": "abc"}
    with pytest.raises(subprocess.CalledProcessError):
        monthly.publish(entry)
    assert marker.exists()
    monkeypatch.setattr(monthly.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monthly.publish(entry)
    assert marker.exists()  # Git publication alone is not successful snapshot promotion.
    monthly.complete_publication(entry)
    assert not marker.exists()


def test_normal_build_cannot_expose_unpublished_record(monkeypatch, tmp_path):
    from engine import build
    monkeypatch.setattr(build, "ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/monthly_pending.json").write_text('{"hash":"abc","period":"2026-10"}')
    folder = tmp_path / "web/data"
    folder.mkdir(parents=True)
    public = folder / "snapshot.json"
    public.write_text("last published snapshot")
    monkeypatch.setattr(build, "load_data", lambda: pytest.fail("Unpublished build read inputs"))
    with pytest.raises(RuntimeError, match="publication is pending"):
        build.main([])
    assert public.read_text() == "last published snapshot"


def test_pending_build_flag_cannot_replace_public_snapshot(monkeypatch, tmp_path):
    from engine import build
    monkeypatch.setattr(build, "ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build, "load_data", lambda: pytest.fail("Public pending build read inputs"))
    with pytest.raises(RuntimeError, match="only for staged output"):
        build.main(["--include-pending"])


def test_normal_build_and_refresh_use_same_lock(monkeypatch, tmp_path):
    from engine import build
    monkeypatch.setattr(build, "ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build, "load_data", lambda: pytest.fail("Concurrent builder read inputs"))
    with monthly.monthly_lock(tmp_path):
        with pytest.raises(RuntimeError, match="publication lock"):
            build.main([])


def test_failed_refresh_sets_durable_phase_before_mutating_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    def refresh(run_id, output):
        assert monthly.pending_publication()["phase"] == "refresh"
        raise RuntimeError("fetch failed")
    monkeypatch.setattr(monthly, "refresh", refresh)
    with pytest.raises(RuntimeError, match="fetch failed"):
        monthly.run_monthly(["--publish"])
    assert monthly.pending_publication()["phase"] == "refresh"
    with pytest.raises(RuntimeError, match="must be rerun completely"):
        monthly.run_monthly(["--no-refresh", "--publish"])


def test_active_physical_source_guard_runs_before_log_append(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "web/data"
    folder.mkdir(parents=True)
    public = folder / "snapshot.json"
    public.write_text("previous published snapshot")
    now = dt.datetime.now(dt.timezone.utc)
    month = pd.Timestamp(now.date()).replace(day=1)
    if now.day < 16:
        month -= pd.offsets.MonthBegin(1)
    call = {"model": "v4", "state": "IN", "bottleneck": "memory", "korea_source": "korea_customs",
            "decision_date": str((month + pd.Timedelta(days=15)).date())}
    def build(cmd):
        run_id = cmd[cmd.index("--run-id") + 1]
        output = cmd[cmd.index("--output") + 1]
        with open(output, "w") as handle:
            json.dump({"run_id": run_id, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "themes": []}, handle)
    monkeypatch.setattr(monthly, "checked_run", build)
    monkeypatch.setattr(monthly, "sanity_check", lambda now: None)
    monkeypatch.setattr(monthly, "current_calls", lambda now: [call])
    monkeypatch.setattr(monthly, "v6_calls", lambda snap, now: [])
    monkeypatch.setattr(monthly, "append", lambda *args, **kwargs: pytest.fail("Incomplete source was logged"))
    with pytest.raises(RuntimeError, match="required physical source"):
        monthly.run_monthly(["--no-refresh", "--publish"])
    assert public.read_text() == "previous published snapshot"
    assert monthly.pending_publication()["phase"] == "refresh"


def test_unlogged_interrupted_refresh_can_restart_in_later_period(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monthly.write_publication_state({"phase": "refresh", "period": "2000-01", "initial_head": "0" * 64})
    def refresh(run_id, output):
        assert monthly.pending_publication()["period"] != "2000-01"
        raise RuntimeError("new refresh reached")
    monkeypatch.setattr(monthly, "refresh", refresh)
    with pytest.raises(RuntimeError, match="new refresh reached"):
        monthly.run_monthly(["--publish"])


def test_orphan_marker_requires_full_refresh_and_checks_existing_log(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "backtest").mkdir()
    marker = tmp_path / "data/monthly_pending.json"
    marker.write_text('{"hash":"never-appended","period":"2026-10"}')
    assert monthly.recover_orphan_marker()["phase"] == "refresh"
    assert marker.exists()
    marker.write_text('{"hash":"never-appended","period":"2026-10"}')
    (tmp_path / monthly.LOG).write_text('{"hash":"tampered","prev":"wrong","calls":[]}\n')
    with pytest.raises(RuntimeError, match="chain is broken"):
        monthly.recover_orphan_marker()
    assert marker.exists()


def test_append_fsync_failure_preserves_guard_and_recovers_existing_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backtest").mkdir()
    monkeypatch.setattr(monthly.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("sync failed")))
    with pytest.raises(OSError, match="sync failed"):
        monthly.append([], period="2026-10")
    assert monthly.pending_publication()["phase"] == "refresh"
    state = monthly.recover_orphan_marker()
    assert state["phase"] == "entry"
    assert len((tmp_path / monthly.LOG).read_text().splitlines()) == 1


def test_monthly_lock_rejects_concurrent_transaction(monkeypatch, tmp_path):
    monkeypatch.setattr(monthly, "ROOT", tmp_path)
    with monthly.monthly_lock():
        with pytest.raises(RuntimeError, match="lock"):
            with monthly.monthly_lock():
                pytest.fail("Second publication transaction acquired lock")


def test_census_historical_timeout_preserves_dataset(monkeypatch, tmp_path):
    output = tmp_path / "old.csv"
    old = "month,hs,desc,usd\n2026-07-01,854232,Memory,100\n"
    output.write_text(old)
    monkeypatch.setattr(census.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(census.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(census.FetchError):
        census.refresh("test-key", today=NOW.date(), months=["2026-07"], output=output, raw_dir=tmp_path / "raw")
    assert output.read_text() == old


@pytest.mark.parametrize("ym,known,allowed", [("2026-08", (), False), ("2026-09", (), True),
                                               ("2026-09", ("2026-09",), False)])
def test_census_204_only_allowed_for_new_not_yet_due_month(monkeypatch, tmp_path, ym, known, allowed):
    monkeypatch.setattr(census.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(census.requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=204))
    if allowed:
        assert census.get(ym, "key", today=NOW.date(), known_months=known, raw_dir=tmp_path) == []
    else:
        with pytest.raises(census.FetchError):
            census.get(ym, "key", today=NOW.date(), known_months=known, raw_dir=tmp_path)


def test_census_success_atomically_replaces_processed_data(monkeypatch, tmp_path):
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return [["I_COMMODITY", "I_COMMODITY_SDESC", "GEN_VAL_MO"], ["854232", "Memory", "100"]]
    monkeypatch.setattr(census.requests, "get", lambda *args, **kwargs: Response())
    output = tmp_path / "new.csv"
    frame = census.refresh("key", today=NOW.date(), months=["2026-08"], output=output, raw_dir=tmp_path / "raw")
    assert frame.iloc[0].usd == 100
    assert output.is_file() and not (tmp_path / "new.csv.tmp").exists()


def test_census_missing_key_has_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setattr(census, "ROOT", tmp_path)
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    with pytest.raises(census.FetchError, match="CENSUS_API_KEY is required"):
        census.main()
