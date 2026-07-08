"""Plan B pricing_store: B1 realized-price ring buffer + extract."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import pricing_store
from custom_components.anker_x1_smartgrid.models import PriceSlot


class _FakeStore:
    """Duck-typed HA Store: in-memory async load/save (no .storage I/O)."""
    def __init__(self, initial=None):
        self.saved = initial
    async def async_load(self):
        return self.saved
    async def async_save(self, data):
        self.saved = data


def test_prune_history_keeps_most_recent_days():
    hist = {f"2026-06-{d:02d}": {"0": 0.2} for d in range(1, 13)}  # 12 days
    pruned = pricing_store.prune_history(hist, max_days=8)
    assert len(pruned) == 8
    assert min(pruned) == "2026-06-05"   # oldest 4 dropped
    assert max(pruned) == "2026-06-12"


def test_extract_realized_day_buckets_by_local_hour():
    # Patch as_local to identity so the test is timezone-independent.
    # (pytest_homeassistant_custom_component may set DEFAULT_TIME_ZONE to the
    # system local TZ via its autouse hass fixture; we must NOT mutate
    # DEFAULT_TIME_ZONE here — that interferes with verify_cleanup teardown.)
    from unittest.mock import patch
    day = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(day + timedelta(hours=i), 0.10 + 0.01 * i) for i in range(26)]
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        got = pricing_store.extract_realized_day(slots, day.date())
    assert len(got) == 24
    assert got["0"] == pytest.approx(0.10)
    assert got["23"] == pytest.approx(0.10 + 0.23)
    assert "24" not in got  # hour 0 of the NEXT day is a different date, excluded


@pytest.mark.asyncio
async def test_store_snapshot_is_date_keyed_idempotent_and_pruned():
    store = pricing_store.PriceHistoryStore(_FakeStore(), max_days=2)
    await store.async_load()
    await store.async_snapshot("2026-06-24", {"0": 0.20})
    await store.async_snapshot("2026-06-25", {"0": 0.21})
    await store.async_snapshot("2026-06-26", {"0": 0.22})
    # Ring-pruned to 2 most-recent dates.
    assert set(store.history) == {"2026-06-25", "2026-06-26"}
    # Re-snapshotting with equal-or-fewer keys keeps the existing entry (completeness guard).
    await store.async_snapshot("2026-06-26", {"0": 0.99})
    assert set(store.history) == {"2026-06-25", "2026-06-26"}
    assert store.history["2026-06-26"] == {"0": 0.22}   # original preserved
    # A MORE-COMPLETE snapshot (extra keys) does update the entry.
    await store.async_snapshot("2026-06-26", {"0": 0.99, "1": 0.98})
    assert store.history["2026-06-26"] == {"0": 0.99, "1": 0.98}


@pytest.mark.asyncio
async def test_store_round_trips_through_underlying_store():
    backing = _FakeStore()
    s1 = pricing_store.PriceHistoryStore(backing, max_days=8)
    await s1.async_snapshot("2026-06-26", {"0": 0.30})
    # A fresh wrapper over the same backing store reloads the saved history.
    s2 = pricing_store.PriceHistoryStore(backing, max_days=8)
    await s2.async_load()
    assert s2.history == {"2026-06-26": {"0": 0.30}}


@pytest.mark.asyncio
async def test_snapshot_completeness_guard_preserves_full_day():
    """N1: a partial re-snapshot (e.g. mid-day restart) must not clobber a full stored day."""
    store = pricing_store.PriceHistoryStore(_FakeStore(), max_days=8)
    await store.async_load()
    full_day = {str(h): 0.20 + 0.001 * h for h in range(24)}   # 24 keys
    await store.async_snapshot("2026-06-25", full_day)
    # Simulate a partial re-snapshot (Zonneplan back-horizon only 12 h after restart).
    partial = {str(h): 0.25 for h in range(12)}                  # only 12 keys
    await store.async_snapshot("2026-06-25", partial)
    # Full day must be preserved — partial must NOT win.
    assert len(store.history["2026-06-25"]) == 24
    assert store.history["2026-06-25"]["0"] == pytest.approx(0.20)
    assert store.history["2026-06-25"]["23"] == pytest.approx(0.20 + 0.023)


# ── B2: blended tomorrow estimate ─────────────────────────────────────────────

from datetime import date as _date  # noqa: E402


def _full_day(base: float) -> dict[str, float]:
    return {str(h): base + 0.001 * h for h in range(24)}


def test_blend_today_and_same_weekday():
    tomorrow = _date(2026, 6, 27)               # Saturday
    history = {
        "2026-06-26": _full_day(0.30),          # today
        "2026-06-20": _full_day(0.10),          # same weekday last week (Sat - 7d)
    }
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None and len(est) == 24
    # 0.5*0.30 + 0.5*0.10 = 0.20 at hour 0.
    assert est[0] == pytest.approx(0.20)


def test_blend_same_weekday_missing_falls_back_to_today_only():
    tomorrow = _date(2026, 6, 27)
    history = {"2026-06-26": _full_day(0.30)}   # no same-weekday day
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None
    assert est[0] == pytest.approx(0.30)        # w forced to 1.0


def test_blend_today_incomplete_uses_most_recent_full_day():
    tomorrow = _date(2026, 6, 27)
    history = {
        "2026-06-26": {"0": 0.30, "1": 0.31},   # today: only 2 hours -> incomplete
        "2026-06-24": _full_day(0.40),          # most recent FULL day -> the 'today' term
    }
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None
    assert est[0] == pytest.approx(0.40)        # full-day fallback, today-only (no same-weekday)


def test_blend_returns_none_without_any_full_day():
    est = pricing_store.blend_price_prior({"2026-06-26": {"0": 0.30}}, _date(2026, 6, 27), weight_today=0.5)
    assert est is None


# ── B3: estimated slots bounded to the pre-solar window ───────────────────────

def test_build_estimated_slots_bounds_to_presolar_window():
    est = [0.10 + 0.01 * h for h in range(24)]   # local-hour-indexed
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)   # tonight ends 00:00
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)     # winter-late pickup 08:00
    # Patch as_local to identity (UTC) so slot hours index by UTC hour.
    from unittest.mock import patch
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        slots = pricing_store.build_estimated_slots(est, real_end, pickup)
    # Exactly the 8 pre-solar hours [00:00, 08:00) carry the estimate (by local hour).
    assert [s.start for s in slots] == [real_end + timedelta(hours=i) for i in range(8)]
    assert slots[0].price == pytest.approx(est[0])
    assert slots[7].price == pytest.approx(est[7])


def test_build_estimated_slots_empty_post_publication():
    est = [0.20] * 24
    # Real prices already extend PAST tomorrow's pickup -> nothing to estimate.
    real_end = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
    assert pricing_store.build_estimated_slots(est, real_end, pickup) == []


def test_build_estimated_slots_empty_without_pickup_or_estimate():
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
    assert pricing_store.build_estimated_slots(None, real_end, pickup) == []
    assert pricing_store.build_estimated_slots([0.2] * 24, real_end, None) == []


def test_estimated_slots_are_disjoint_from_real_slots():
    """Containment: estimated PriceSlots never coincide with the real horizon."""
    est = [0.20] * 24
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
    real_slots = [PriceSlot(datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc) + timedelta(hours=i), 0.30)
                  for i in range(6)]  # 18:00..23:00 tonight
    from unittest.mock import patch
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        est_slots = pricing_store.build_estimated_slots(est, real_end, pickup)
    real_starts = {s.start for s in real_slots}
    assert all(s.start >= real_end for s in est_slots)       # strictly after tonight
    assert all(s.start not in real_starts for s in est_slots)


# ── B4: upside-only held-extra ────────────────────────────────────────────────

from custom_components.anker_x1_smartgrid.models import Config  # noqa: E402


def _b4_cfg(**kw) -> Config:
    d = dict(
        capacity_kwh=10.0, soc_target=97.0, export_fee_eur_per_kwh=0.0,
        export_peak_band_frac=0.5,        # wide band so all evening hours are eligible
        max_export_w=3000.0, grid_export_limit_w=3000.0,
        anticipation_confidence_haircut=0.15, anticipation_margin_eur_per_kwh=0.02,
    )
    d.update(kw)
    return Config(**d)  # type: ignore[arg-type]


NOW_H = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
REAL_END = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
PICKUP = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)


def _evening_slots() -> list[PriceSlot]:
    # 18:00..23:00 tonight; modest export prices.
    prices = [0.30, 0.32, 0.34, 0.36, 0.28, 0.26]
    return [PriceSlot(NOW_H + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _base_reserve() -> dict[datetime, float]:
    # Pre-pickup hours carry a small survival reserve; one post-pickup hour to prove it is left alone.
    rsv = {NOW_H + timedelta(hours=i): 1.0 for i in range(14)}  # 18:00..07:00
    rsv[PICKUP] = 0.5                                            # 08:00 (>= pickup)
    return rsv


def test_held_extra_positive_when_estimate_beats_tonight():
    cfg = _b4_cfg()
    # Pricey estimated morning peak inside the pre-solar window (hour 7 = 07:00).
    est_slots = [PriceSlot(REAL_END + timedelta(hours=i), 0.20) for i in range(7)]
    est_slots.append(PriceSlot(REAL_END + timedelta(hours=7), 0.80))   # peak
    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=est_slots, real_slots=_evening_slots(),
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )
    assert held > 0.0


def test_no_hold_when_estimate_below_tonight():
    cfg = _b4_cfg()
    est_slots = [PriceSlot(REAL_END + timedelta(hours=i), 0.10) for i in range(8)]  # cheap morning
    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=est_slots, real_slots=_evening_slots(),
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )
    assert held == 0.0


def test_hold_zero_when_pack_headroom_exhausted():
    # soc_target barely above the base reserve -> ~no headroom -> nothing held (upside-only).
    cfg = _b4_cfg(soc_target=10.0)   # 10% of 10 kWh = 1.0 kWh == base reserve -> headroom 0
    est_slots = [PriceSlot(REAL_END + timedelta(hours=i), 0.80) for i in range(8)]  # very pricey
    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=est_slots, real_slots=_evening_slots(),
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )
    assert held == 0.0


def test_held_extra_never_negative_and_empty_estimate_is_zero():
    cfg = _b4_cfg()
    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=[], real_slots=_evening_slots(),
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )
    assert held == 0.0


def test_anticipation_includes_late_local_peak_via_window():
    """Windowed band includes the late local-peak hour the old global-max band excluded.

    Scenario: strong early peak at 18:00 (0.80) inflates the OLD global
    band_floor to 0.704.  The late tonight slot at 23:00 (0.52) is a
    down-slope from the early peak but is the highest price in the
    19:00–23:00 window.

    Old global band: 0.52 < 0.704 → 23:00 excluded → tonight = [18:00 only]
    → held = 1 × per_hour_dc.

    New windowed (lookback=4): peak_ref[5] = max(eprices[1:]) = 0.52;
    floor = 0.52 × 0.88 = 0.458; 0.52 ≥ 0.458 → 23:00 IS eligible →
    tonight = [23:00, 18:00] → held = 2 × per_hour_dc.

    est_morning (0.85 × 1.20 = 1.02) beats both hours, so both contribute.
    """
    cfg = _b4_cfg(export_peak_band_frac=0.12, export_peak_lookback_h=4)
    # Strong early peak (18:00=0.80) then low mid-evening, late local-peak (23:00=0.52).
    prices = [0.80, 0.20, 0.20, 0.20, 0.20, 0.52]
    real_slots = [PriceSlot(NOW_H + timedelta(hours=i), p) for i, p in enumerate(prices)]
    # Very high estimate: est_morning = 0.85 × 1.20 = 1.02; beats 0.52+0.02 and 0.80+0.02.
    est_slots = [PriceSlot(REAL_END + timedelta(hours=i), 1.20) for i in range(8)]

    from custom_components.anker_x1_smartgrid import optimize as _opt
    eta_d = _opt.eta_discharge(cfg)
    per_hour_dc = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0 / eta_d

    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=est_slots, real_slots=real_slots,
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )
    # New windowed code includes the late 23:00 slot (local peak in its context) so
    # both tonight hours contribute: held = 2 × per_hour_dc.
    # Old global-band code excludes 23:00 → held = 1 × per_hour_dc (test FAILS with old).
    assert held == pytest.approx(2 * per_hour_dc)


# ---------------------------------------------------------------------------
# eta_curve threading (Task 15) — optional curve param, None branch byte-identical
# ---------------------------------------------------------------------------

def _held_extra_kwargs(cfg: Config) -> dict:
    est_slots = [PriceSlot(REAL_END + timedelta(hours=i), 0.20) for i in range(7)]
    est_slots.append(PriceSlot(REAL_END + timedelta(hours=7), 0.80))   # peak
    return dict(
        estimated_slots=est_slots, real_slots=_evening_slots(),
        now_h=NOW_H, real_horizon_end=REAL_END, tomorrow_solar_pickup=PICKUP,
        base_reserve_by_hour=_base_reserve(), cfg=cfg,
    )


def test_held_extra_eta_curve_none_is_byte_identical():
    cfg = _b4_cfg()
    kwargs = _held_extra_kwargs(cfg)
    assert (
        pricing_store.compute_anticipation_held_extra(**kwargs)
        == pricing_store.compute_anticipation_held_extra(**kwargs, eta_curve=None)
    )


def test_held_extra_eta_curve_static_matches_scalar_path():
    """A static-fallback EfficiencyCurve reproduces the same scalar eta_discharge
    as the default cfg-derived path, so held-extra is unchanged."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
    cfg = _b4_cfg()
    kwargs = _held_extra_kwargs(cfg)
    curve = EfficiencyCurve.static(cfg)
    assert (
        pricing_store.compute_anticipation_held_extra(**kwargs)
        == pricing_store.compute_anticipation_held_extra(**kwargs, eta_curve=curve)
    )


def test_held_extra_eta_curve_lower_export_eta_raises_held():
    """A curve with a lower discharge eta at the export-cap bin means more DC
    kWh is needed per AC kWh sold, so more is withheld for tomorrow's peak
    (proves the curve is actually threaded through, not dead-code)."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
    # Large headroom + a single real/estimated hour so neither the pack-headroom
    # cap nor the morning-sellable cap binds and the eta scaling shows through.
    cfg = _b4_cfg(capacity_kwh=100.0)
    now_h = datetime(2026, 6, 26, 20, 0, tzinfo=timezone.utc)
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
    real_slots = [PriceSlot(now_h, 0.20)]
    est_slots = [PriceSlot(real_end, 0.80)]
    base_reserve = {now_h: 0.5}
    kwargs = dict(
        estimated_slots=est_slots, real_slots=real_slots,
        now_h=now_h, real_horizon_end=real_end, tomorrow_solar_pickup=pickup,
        base_reserve_by_hour=base_reserve, cfg=cfg,
    )
    base = EfficiencyCurve.static(cfg)
    disch = list(base._discharge)
    bin_i = 4   # [2500, 4000) W — covers min(max_export_w, grid_export_limit_w)=3000W
    disch[bin_i] = BinStat(
        disch[bin_i].lo_w, disch[bin_i].hi_w, "discharge",
        base._fd * 0.5, base._fd * 0.5, 99, 9.0, True, "",
    )
    curve = EfficiencyCurve(list(base._charge), disch, base._fc, base._fd)

    held_default = pricing_store.compute_anticipation_held_extra(**kwargs)
    held_curve = pricing_store.compute_anticipation_held_extra(**kwargs, eta_curve=curve)
    assert held_default > 0.0
    assert held_curve == pytest.approx(2 * held_default)
