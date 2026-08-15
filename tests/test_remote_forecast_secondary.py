"""Tier-0 remote forecast: local-model delegation past the ML map's edge.

The add-on's forecast map only reaches as far as the weather entity's hourly
forecast — 24 entries on the live box (``weather.knmi_home``) — while the DP
optimizes the FULL price horizon (~36 h).  Before this change every hour past
the map's edge returned the flat ``const.DEFAULT_FALLBACK_LOAD_W`` (400 W),
including all of tomorrow evening: the hours that size the overnight charge.
Verified live 2026-08-15: 46 of 143 forward real-tariff slots pinned at
exactly 400.0 W.

That was a regression against the tiers remote replaced — bucketed/profile are
hour-of-day models that shape ANY future hour.  ``RemoteForecastPredictor``
now takes an optional *secondary* predictor and delegates map misses to it:
ML where the ML has coverage, the local hour-of-day model beyond it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid import const, forecast, plan
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot
from custom_components.anker_x1_smartgrid.remote_forecast import RemoteForecastPredictor

_HOUR_A = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)
_MISS = datetime(2026, 6, 23, 19, 0, tzinfo=UTC)  # tomorrow evening: past a 24 h map


class _SpySecondary:
    """Records every delegated call and returns a shaped (non-400) value."""

    def __init__(self, p50_w: float = 1234.0, p80_w: float = 1700.0) -> None:
        self.p50_w = p50_w
        self.p80_w = p80_w
        self.calls: list[tuple] = []

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls.append((when, temp, fallback_w, quantile))
        return self.p80_w if quantile > 0.5 else self.p50_w


class _RaisingSecondary:
    def __init__(self) -> None:
        self.calls = 0

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls += 1
        raise RuntimeError("secondary exploded")


# ---------------------------------------------------------------------------
# (a) Unit: RemoteForecastPredictor delegation contract
# ---------------------------------------------------------------------------


def test_map_hit_returns_ml_value_and_never_consults_secondary():
    sec = _SpySecondary()
    pred = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)}, secondary=sec)

    assert pred.predict(_HOUR_A, 18.0, 400.0, quantile=0.5) == 400.0
    assert pred.predict(_HOUR_A, 18.0, 400.0, quantile=0.8) == 550.0
    assert sec.calls == [], "map hit must not fall through to the secondary"


def test_map_miss_with_secondary_returns_shaped_value_not_flat_400():
    sec = _SpySecondary(p50_w=1234.0)
    pred = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)}, secondary=sec)

    got = pred.predict(_MISS, 18.0, const.DEFAULT_FALLBACK_LOAD_W, quantile=0.5)

    assert got == 1234.0
    assert got != const.DEFAULT_FALLBACK_LOAD_W, "the whole point: the tail must not be flat 400 W"


def test_map_miss_without_secondary_returns_fallback_exactly():
    """Existing contract (test-locked): no secondary → fallback_w, unchanged."""
    pred = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)})
    assert pred.predict(_MISS, 20.0, 350.0, quantile=0.8) == 350.0

    pred_explicit_none = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)}, secondary=None)
    assert pred_explicit_none.predict(_MISS, 20.0, 350.0, quantile=0.8) == 350.0


def test_secondary_that_raises_degrades_to_fallback():
    sec = _RaisingSecondary()
    pred = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)}, secondary=sec)

    assert pred.predict(_MISS, 20.0, 350.0, quantile=0.5) == 350.0
    assert sec.calls == 1, "the secondary must actually have been tried"


def test_secondary_returning_non_finite_degrades_to_fallback():
    """A NaN/inf leaking into build_intervals would poison the whole DP."""

    class _NaNSecondary:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            return float("nan")

    pred = RemoteForecastPredictor({_HOUR_A: (400.0, 550.0)}, secondary=_NaNSecondary())
    assert pred.predict(_MISS, 20.0, 350.0, quantile=0.5) == 350.0


def test_map_miss_forwards_when_temp_fallback_and_quantile_verbatim():
    sec = _SpySecondary()
    pred = RemoteForecastPredictor({}, secondary=sec)

    pred.predict(_MISS, 7.5, 400.0, quantile=0.8)

    assert sec.calls == [(_MISS, 7.5, 400.0, 0.8)]


def test_quantile_routing_preserved_through_delegation():
    """>0.5 must reach the secondary as an upper-quantile request, not a p50 one."""
    sec = _SpySecondary(p50_w=900.0, p80_w=1500.0)
    pred = RemoteForecastPredictor({}, secondary=sec)

    assert pred.predict(_MISS, None, 400.0, quantile=0.8) == 1500.0
    assert pred.predict(_MISS, None, 400.0, quantile=0.5) == 900.0
    assert pred.predict(_MISS, None, 400.0, quantile=0.2) == 900.0
    assert [c[3] for c in sec.calls] == [0.8, 0.5, 0.2]


def test_delegation_uses_the_caller_hour_not_the_floored_key():
    """The secondary is an hour-of-day model — it must see the real ``when``."""
    sec = _SpySecondary()
    pred = RemoteForecastPredictor({}, secondary=sec)
    when = datetime(2026, 6, 23, 19, 37, 15, tzinfo=UTC)

    pred.predict(when, None, 400.0)

    assert sec.calls[0][0] == when


# ---------------------------------------------------------------------------
# (b) Controller wiring: the local tier becomes the remote tier's secondary
# ---------------------------------------------------------------------------


def _shaped_hourly_rows(days: int = 20) -> list[dict]:
    """Hourly rollups with a strong hour-of-day shape (night trough, evening peak).

    20 days x 24 h = 480 rows: clears DEFAULT_MIN_TRAIN_HOURS (48) but only
    ~13 lag-complete dates, so HGBR's is_ready() coverage gate still fails
    naturally and the bucketed tier wins — same construction as
    tests/test_controller_phase2.py::_cold_warm_hourly_rows.
    """
    rows: list[dict] = []
    base = datetime(2026, 6, 1, tzinfo=UTC)
    for d in range(days):
        for h in range(24):
            ts = base + timedelta(days=d, hours=h)
            kwh = 0.2 if h < 6 else (1.9 if 17 <= h < 22 else 0.8)
            rows.append({"hour_ts": ts.isoformat(), "house_load_kwh_sum": kwh, "temp_mean": 12.0})
    return rows


class _Rec:
    def __init__(self, hourly_rows: list[dict]) -> None:
        self._hourly = hourly_rows

    def read_hourly_rows(self, since_iso=None):
        return self._hourly

    def read_feature_rows(self, since_iso=None):
        return []

    def read_load_samples(self, since_iso=None):
        return []


class _BoomRec(_Rec):
    def read_hourly_rows(self, since_iso=None):
        raise RuntimeError("recorder unavailable")


def _make_ctl(rec, *, addon_enabled: bool = True):
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = {
        "use_learned_model": True,
        "train_days": 14,
        "backtest_test_days": 3,
        "addon_enabled": addon_enabled,
    }
    ctl = Controller.__new__(Controller)
    ctl._hass = None
    ctl._data = data
    ctl._recorder = rec
    ctl.cfg = Config.from_dict(data)
    ctl.profile = {}
    ctl.predictor = forecast.LoadPredictor.from_profile({})
    ctl._profile_predictor = forecast.LoadPredictor.from_profile({})
    ctl.backtest_result = None
    ctl.active_model_name = "profile"
    ctl.coverage_lag_complete_days = None
    ctl._remote_forecast_map = None
    return ctl


def test_retrain_sync_remote_wraps_the_local_tier_as_secondary():
    ctl = _make_ctl(_Rec(_shaped_hourly_rows()))
    ctl._remote_forecast_map = {_HOUR_A: (400.0, 550.0)}

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    assert ctl.active_model_name == "remote"
    assert isinstance(ctl.predictor, RemoteForecastPredictor)
    assert ctl.predictor._secondary is not None, "the local tier must be wired in as the tail model"


def test_retrain_sync_remote_tail_hour_uses_the_local_model_not_flat_400():
    ctl = _make_ctl(_Rec(_shaped_hourly_rows()))
    ctl._remote_forecast_map = {_HOUR_A: (400.0, 550.0)}

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    # Inside the map → the ML value.
    assert ctl.predictor.predict(_HOUR_A, 12.0, const.DEFAULT_FALLBACK_LOAD_W) == 400.0

    # Past the map → the local hour-of-day shape, and it must actually be shaped.
    night = ctl.predictor.predict(
        datetime(2026, 6, 23, 3, 0, tzinfo=UTC), 12.0, const.DEFAULT_FALLBACK_LOAD_W
    )
    evening = ctl.predictor.predict(
        datetime(2026, 6, 23, 19, 0, tzinfo=UTC), 12.0, const.DEFAULT_FALLBACK_LOAD_W
    )
    assert night != const.DEFAULT_FALLBACK_LOAD_W
    assert evening != const.DEFAULT_FALLBACK_LOAD_W
    assert evening > night * 2, f"tail must keep the night/evening shape, got {night=} {evening=}"


def test_retrain_sync_local_fit_failure_still_yields_remote():
    """A broken recorder must not cost us the remote tier (it did not before)."""
    ctl = _make_ctl(_BoomRec([]))
    ctl._remote_forecast_map = {_HOUR_A: (400.0, 550.0)}

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    assert ctl.active_model_name == "remote"
    assert isinstance(ctl.predictor, RemoteForecastPredictor)
    # Degrades to the flat fallback rather than raising out of _retrain_sync.
    assert ctl.predictor.predict(_MISS, 12.0, const.DEFAULT_FALLBACK_LOAD_W) == const.DEFAULT_FALLBACK_LOAD_W


def test_retrain_sync_remote_leaves_backtest_result_untouched():
    """Parity: the ml-status sensor's backtest metrics on the remote tier are
    exactly what they were before the local chain started running underneath."""
    ctl = _make_ctl(_Rec(_shaped_hourly_rows()))
    ctl._remote_forecast_map = {_HOUR_A: (400.0, 550.0)}
    sentinel = {"model_mae": 42.0}
    ctl.backtest_result = sentinel

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    assert ctl.backtest_result is sentinel


def test_retrain_sync_without_remote_map_is_unchanged():
    """No map → the local chain owns predictor/name/backtest exactly as before."""
    ctl = _make_ctl(_Rec(_shaped_hourly_rows()))

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"
    assert not isinstance(ctl.predictor, RemoteForecastPredictor)
    assert ctl.backtest_result is not None


def test_retrain_sync_addon_disabled_ignores_map_and_keeps_local_predictor():
    ctl = _make_ctl(_Rec(_shaped_hourly_rows()), addon_enabled=False)
    ctl._remote_forecast_map = {_HOUR_A: (400.0, 550.0)}

    ctl._retrain_sync("2026-05-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"
    assert not isinstance(ctl.predictor, RemoteForecastPredictor)


# ---------------------------------------------------------------------------
# (c) Integration: the built plan intervals past the map edge are shaped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("map_hours", [24])
def test_display_intervals_past_the_map_edge_are_shaped_not_pinned_at_400(map_hours):
    """End state: a 24 h ML map + a 36 h price horizon must still yield a
    shaped tail.  This is the live failure mode — 46/143 forward slots at
    exactly 400.0 W, tomorrow evening included."""
    # Midday start, 36 h horizon: the tail lands squarely on tomorrow evening —
    # exactly the hours that size the overnight charge.
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    horizon_h = 36

    # Local tier: a real hour-of-day model fitted on the shaped rollups.
    from custom_components.anker_x1_smartgrid.dataquality import clean_hourly_rows
    from custom_components.anker_x1_smartgrid.loadmodel import BucketedLoadModel

    local = forecast.LoadPredictor.from_model(BucketedLoadModel.fit(clean_hourly_rows(_shaped_hourly_rows())))

    forecast_map = {now + timedelta(hours=h): (500.0 + 10.0 * h, 700.0 + 10.0 * h) for h in range(map_hours)}
    predictor = RemoteForecastPredictor(forecast_map, secondary=local)

    slots = [PriceSlot(start=now + timedelta(hours=h), price=0.20) for h in range(horizon_h)]
    pv_curve = [(now + timedelta(hours=h), 0.0) for h in range(horizon_h)]

    ivs = plan.build_display_intervals(slots, now, pv_curve, predictor, 12.0, const.DEFAULT_FALLBACK_LOAD_W)

    assert len(ivs) == horizon_h
    tail = [iv for iv in ivs if iv.start >= now + timedelta(hours=map_hours)]
    assert len(tail) == horizon_h - map_hours

    pinned = [iv for iv in tail if iv.load_w == const.DEFAULT_FALLBACK_LOAD_W]
    assert not pinned, f"{len(pinned)} tail intervals still pinned at the flat fallback"
    assert len({round(iv.load_w, 3) for iv in tail}) > 1, "tail must be shaped, not a constant"

    # Tomorrow evening (the hours that size the overnight charge) must carry the
    # local model's evening peak, above the surrounding daytime hours.
    evening = [iv.load_w for iv in tail if 17 <= iv.start.hour < 22]
    daytime = [iv.load_w for iv in tail if 12 <= iv.start.hour < 17]
    assert evening and daytime
    assert min(evening) > max(daytime)


def test_display_intervals_inside_the_map_still_come_from_the_ml():
    """Parity guard: delegation must not disturb the ML-covered hours."""
    now = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    forecast_map = {now + timedelta(hours=h): (500.0 + 10.0 * h, 700.0 + 10.0 * h) for h in range(24)}
    local = forecast.LoadPredictor.from_profile({(True, h): 9999.0 for h in range(24)})
    predictor = RemoteForecastPredictor(forecast_map, secondary=local)

    slots = [PriceSlot(start=now + timedelta(hours=h), price=0.20) for h in range(24)]
    pv_curve = [(now + timedelta(hours=h), 0.0) for h in range(24)]

    ivs = plan.build_display_intervals(slots, now, pv_curve, predictor, 12.0, const.DEFAULT_FALLBACK_LOAD_W)

    assert [round(iv.load_w, 3) for iv in ivs] == [500.0 + 10.0 * h for h in range(24)]
