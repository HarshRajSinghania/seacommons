from __future__ import annotations


def test_humanitarian_reconcile_job_calls_persisted_reconciler(monkeypatch):
    from core import scheduler

    calls = []

    def fake_reconcile_stale_incidents():
        calls.append(True)
        return 2

    monkeypatch.setattr(
        "core.intel.humanitarian_incident.reconcile_stale_incidents",
        fake_reconcile_stale_incidents,
    )
    scheduler._job_reconcile_humanitarian_incidents()
    assert calls == [True]


def test_incident_watch_job_claims_bounded_batch_and_executes_each(monkeypatch):
    from core import scheduler

    claimed = [
        {"watch_id": "watch:a", "incident_id": "a"},
        {"watch_id": "watch:b", "incident_id": "b"},
    ]
    calls = []

    monkeypatch.setattr(
        "core.intel.incident_watch.claim_due_watches",
        lambda **kwargs: claimed,
    )
    monkeypatch.setattr(
        "core.intel.incident_watch.run_claimed_watch",
        lambda watch_id, **kwargs: calls.append(watch_id) or {"executed": True},
    )

    scheduler._job_incident_watch()
    assert calls == ["watch:a", "watch:b"]


def test_darkship_cue_refresh_job_is_bounded(monkeypatch):
    from core import scheduler
    from core.mda.watch import mda_watch

    calls = []
    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return {
            "scanned": 0,
            "refreshed": 0,
            "with_unmatched_sar": 0,
            "hypotheses_evaluated": 0,
        }

    monkeypatch.setattr(mda_watch, "refresh_darkship_cues", fake_refresh)

    scheduler._job_darkship_cue_refresh()
    assert calls == [
        {
            "limit": 4,
            "min_age_hours": 0.25,
            "max_age_days": 1,
            "recheck_hours": 1.0,
            "include_s1": True,
        },
        {
            "limit": 2,
            "min_age_hours": 72.0,
            "max_age_days": 10,
            "recheck_hours": 24.0,
            "include_s1": False,
        },
    ]


def test_scheduler_registers_incident_watch_job():
    from core import scheduler

    scheduler.stop()
    try:
        scheduler.start()
        ids = {job["id"] for job in scheduler.status()["jobs"]}
        assert "incident_watch" in ids
        assert "darkship_cue_refresh" in ids
    finally:
        scheduler.stop()
