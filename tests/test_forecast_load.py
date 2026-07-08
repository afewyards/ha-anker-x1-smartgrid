from datetime import datetime, timezone
from custom_components.anker_x1_smartgrid import forecast

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)  # Monday


def _s(dt, w):
    return (dt.isoformat(), w)


def test_profile_averages_by_hour_and_daytype():
    samples = [
        _s(datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc), 400.0),  # Sat
        _s(datetime(2026, 6, 21, 8, 0, tzinfo=timezone.utc), 600.0),  # Sun
        _s(datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc), 1000.0),  # Fri (weekday)
    ]
    prof = forecast.rolling_load_profile(samples, lookback_days=14, now=NOW)
    assert prof[(True, 8)] == 500.0   # weekend avg of 400,600
    assert prof[(False, 8)] == 1000.0


def test_profile_respects_lookback():
    old = _s(datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc), 999.0)
    recent = _s(datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc), 400.0)
    prof = forecast.rolling_load_profile([old, recent], lookback_days=14, now=NOW)
    assert (True, 8) in prof
    assert prof[(True, 8)] == 400.0


def test_predict_uses_fallback_when_missing():
    prof = {(False, 8): 800.0}
    when = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)  # Monday hour 9 missing
    assert forecast.predict_load_w(prof, when, fallback_w=350.0) == 350.0
    when8 = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
    assert forecast.predict_load_w(prof, when8, fallback_w=350.0) == 800.0


def test_load_predictor_bare_fallback_with_quantile():
    """LoadPredictor with neither profile nor model returns fallback_w even with quantile kwarg."""
    lp = forecast.LoadPredictor()
    when = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    assert lp.predict(when, temp=10.0, fallback_w=300.0, quantile=0.8) == 300.0


def test_load_predictor_profile_p80_exceeds_p50_for_spread_distribution():
    """LoadPredictor with profile: P80 > P50 for a spread distribution."""
    # Use a custom NOW with a 30-day lookback so all 10 weekday samples are within window.
    # June 2026 weekdays at hour 8:
    #   Mon 1, Tue 2, Wed 3, Thu 4, Fri 5, Mon 8, Tue 9, Wed 10, Thu 11, Fri 12
    # Values: 100,200,...,1000 → mean=550; P80 at n=10: pos=0.8*9=7.2 → 820.0 > 550
    now_custom = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    weekday_dates = [1, 2, 3, 4, 5, 8, 9, 10, 11, 12]
    load_values = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
    samples = [
        (datetime(2026, 6, d, 8, 0, tzinfo=timezone.utc).isoformat(), w)
        for d, w in zip(weekday_dates, load_values)
    ]
    lp = forecast.LoadPredictor.from_profile_samples(samples, lookback_days=30, now=now_custom)
    when = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)  # Monday → (False, 8)

    p50 = lp.predict(when, temp=None, fallback_w=0.0, quantile=0.5)
    p80 = lp.predict(when, temp=None, fallback_w=0.0, quantile=0.8)

    assert p80 > p50, f"P80={p80} must exceed P50={p50} for spread distribution"


def test_load_predictor_profile_p80_equals_p50_for_constant_distribution():
    """LoadPredictor with profile: P80 == P50 for a constant distribution."""
    samples = []
    for i in range(8):
        dt = datetime(2026, 6, 9 + i, 8, 0, tzinfo=timezone.utc)
        samples.append((dt.isoformat(), 500.0))
    lp = forecast.LoadPredictor.from_profile_samples(samples, lookback_days=14, now=NOW)
    when = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)

    p50 = lp.predict(when, temp=None, fallback_w=0.0, quantile=0.5)
    p80 = lp.predict(when, temp=None, fallback_w=0.0, quantile=0.8)

    assert p50 == 500.0
    assert p80 == 500.0, f"Constant distribution: P80 must equal P50=500, got {p80}"


def test_load_predictor_profile_p50_regression():
    """Regression: profile-based P50 cross-weekend fallback behavior.

    With Q2 fix: an unseen weekday class uses the opposite-weekend same-hour mean
    (500.0) instead of the flat fallback.  A truly unseen hour (no data in either
    class) still returns the fallback.
    """
    samples = [
        (datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc).isoformat(), 400.0),  # Sat
        (datetime(2026, 6, 21, 8, 0, tzinfo=timezone.utc).isoformat(), 600.0),  # Sun
    ]
    lp = forecast.LoadPredictor.from_profile_samples(samples, lookback_days=14, now=NOW)
    when = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)  # Monday, hour 8 — weekend data exists
    when_sat = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)  # Saturday
    when_mon_h9 = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)  # Monday, hour 9 — no data at all

    # Q2: cross-weekend fallback — Monday h8 returns the weekend h8 mean, not fallback
    assert lp.predict(when, temp=None, fallback_w=999.0, quantile=0.5) == 500.0
    # Direct hit unchanged
    p50_sat = lp.predict(when_sat, temp=None, fallback_w=0.0, quantile=0.5)
    assert p50_sat == 500.0  # mean of 400+600
    # Truly unseen hour (no data in either class) still returns fallback
    assert lp.predict(when_mon_h9, temp=None, fallback_w=999.0, quantile=0.5) == 999.0
