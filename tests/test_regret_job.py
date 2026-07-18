"""Task 6 — overnight terminal credit wiring in ``regret_job`` (oracle + shadow DP).

The nightly regret job runs the DP core **twice** — the perfect-foresight oracle
(``regret.hindsight_optimal_grid``) and a shadow ``optimize.optimize_grid`` whose
schedule is scored into ``dp_regret_eur`` (fed to the 7-day HGBR gate).  With the
overnight-terminal flag ON, BOTH calls MUST receive **identical**
``(water_value_hi, overnight_need_kwh)`` or the shadow schedule diverges from the
oracle and ``dp_regret_eur`` acquires phantom bias.

Gap prices are the **realized D+1 prices** bucketed from the job's own 38 h read
window (the persistence estimator is not reconstructible at job time).  When D+1
gap prices are absent (old backfill data) the day degrades to the byte-identical
legacy single-segment terminal (``water_value_hi=None``) — never guessed.
"""

from custom_components.anker_x1_smartgrid import regret_job
from custom_components.anker_x1_smartgrid.models import Config
from tests.helpers import StubRecorder

DAY = "2026-06-19"
NEXT = "2026-06-20"
COMPUTED_TS = "2026-06-20T12:00:00+00:00"


def _cfg(**overrides) -> Config:
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        terminal_overnight_credit=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _seed_day(
    rec,
    day,
    *,
    soc_start,
    load_w,
    import_price,
    export_price=None,
    batt_w=0.0,
    pv_w=0.0,
    hours=None,
):
    if hours is None:
        hours = range(24)
    for h in hours:
        row = {
            "ts": f"{day}T{h:02d}:00:00+00:00",
            "soc": soc_start,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "p1_w": load_w,
            "import_price": import_price,
            "state": "passive",
            "setpoint_w": 0.0,
        }
        if export_price is not None:
            row["export_price"] = export_price
        rec.rows.append(row)


def _spy_terminal_params(monkeypatch):
    """Wrap both internal DP calls; return a dict capturing the
    ``(water_value_hi, overnight_need_kwh)`` each received."""
    captured: dict[str, tuple] = {}
    _orig_hog = regret_job.regret_mod.hindsight_optimal_grid
    _orig_og = regret_job.optimize_mod.optimize_grid

    def _hog(*a, **kw):
        captured["oracle"] = (kw.get("water_value_hi"), kw.get("overnight_need_kwh"))
        return _orig_hog(*a, **kw)

    def _og(*a, **kw):
        captured["shadow"] = (kw.get("water_value_hi"), kw.get("overnight_need_kwh"))
        return _orig_og(*a, **kw)

    monkeypatch.setattr(regret_job.regret_mod, "hindsight_optimal_grid", _hog)
    monkeypatch.setattr(regret_job.optimize_mod, "optimize_grid", _og)
    return captured


def test_internal_dp_symmetry(monkeypatch):
    """Flag ON + D+1 prices present → both internal DP calls get IDENTICAL
    ``(v_hi, need)`` and ``dp_regret_eur ≈ 0`` on a synthetic hold night."""
    monkeypatch.setattr(regret_job.dt_util, "as_local", lambda dt: dt)
    captured = _spy_terminal_params(monkeypatch)
    rec = StubRecorder()
    cfg = _cfg()
    # Day-D: uniform-price hold night, modest load, no charging → both DP cores agree.
    _seed_day(rec, DAY, soc_start=60.0, load_w=500.0, import_price=0.20)
    # D+1 overnight prices present in the 38 h window (local hours 0-13) → the
    # flag path builds real params rather than degrading to legacy.
    _seed_day(rec, NEXT, soc_start=60.0, load_w=500.0, import_price=0.30, hours=range(14))

    regret_job.run_daily_regret(rec, cfg, DAY, COMPUTED_TS, slot_minutes=60)

    # PRIMARY invariant: both internal DP calls received IDENTICAL overnight params.
    assert "oracle" in captured and "shadow" in captured
    assert captured["oracle"] == captured["shadow"]
    # Flag ON + D+1 prices present → non-None v_hi (legacy branch NOT taken).
    assert captured["oracle"][0] is not None
    # No phantom bias: identical params → shadow schedule mirrors oracle → dp_regret ≈ 0.
    row = rec.daily_regret_rows[DAY]
    assert row["dp_regret_eur"] is not None
    assert abs(row["dp_regret_eur"]) < 1e-6


def test_backfill_without_next_day_prices_degrades_to_legacy(monkeypatch):
    """Old backfill day with no D+1 rows in the window → both DP calls get
    ``water_value_hi=None`` (legacy), no raise, no biased regret."""
    monkeypatch.setattr(regret_job.dt_util, "as_local", lambda dt: dt)
    captured = _spy_terminal_params(monkeypatch)
    rec = StubRecorder()
    cfg = _cfg()  # flag ON
    # Only day-D samples — no D+1 rows at all (backfill exceeded retention).
    _seed_day(rec, DAY, soc_start=60.0, load_w=500.0, import_price=0.20)

    updates = regret_job.run_daily_regret(rec, cfg, DAY, COMPUTED_TS, slot_minutes=60)

    # Degrades to legacy: both DP calls get water_value_hi=None (never guessed).
    assert captured["oracle"] == (None, 0.0)
    assert captured["shadow"] == (None, 0.0)
    # Still fully scored, no raise, no biased regret.
    row = rec.daily_regret_rows[DAY]
    assert row["regret_eur"] is not None
    assert row["dp_regret_eur"] is not None
    assert abs(row["dp_regret_eur"]) < 1e-6
    assert "last_regret" in updates


def test_flag_off_passes_none_to_both_cores(monkeypatch):
    """Flag OFF → legacy single-segment terminal for BOTH cores even when D+1
    prices are present in the window."""
    monkeypatch.setattr(regret_job.dt_util, "as_local", lambda dt: dt)
    captured = _spy_terminal_params(monkeypatch)
    rec = StubRecorder()
    cfg = _cfg(terminal_overnight_credit=False)
    _seed_day(rec, DAY, soc_start=60.0, load_w=500.0, import_price=0.20)
    _seed_day(rec, NEXT, soc_start=60.0, load_w=500.0, import_price=0.30, hours=range(14))

    regret_job.run_daily_regret(rec, cfg, DAY, COMPUTED_TS, slot_minutes=60)

    assert captured["oracle"] == (None, 0.0)
    assert captured["shadow"] == (None, 0.0)
