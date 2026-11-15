from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.anker_x1_smartgrid.why import hour_reasons

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # 13:00 local
H = timedelta(hours=1)


def _row(k, price, soc, *, load=0.5, pv=0.0, house=0.0, export=0.0, grid=0.0, solar=0.0, mode="idle", est=False):
    return {
        "start": (NOW + k * H).isoformat(),
        "price": price,
        "soc": soc,
        "load_kwh": load,
        "pv_kwh": pv,
        "grid_charge_kwh": grid,
        "solar_charge_kwh": solar,
        "self_discharge_kwh": house,
        "grid_export_kwh": export,
        "mode": "estimated" if est else mode,
        "estimated": est,
    }


def _at(k):
    return (NOW + k * H).astimezone(AMS).isoformat()


def _reasons(horizon, terminal=0.2, **kw):
    return hour_reasons(
        horizon,
        NOW,
        AMS,
        **{
            "soc": None,
            "use_value": lambda p, _w: p * 0.9 - 0.03,
            "export_value": lambda _s, p: p - 0.05,
            "charge_cost": lambda p: p / 0.9,
            "terminal_value": terminal,
        }
        | kw,
    )


def test_a_hold_is_kept_for_the_later_export():
    why = _reasons([_row(0, 0.30, 80, mode="hold"), _row(1, 0.46, 60, export=2.0, mode="export")])
    assert why[_at(0)] == {
        "kind": "hold",
        "keep_eur": 0.41,
        "use_eur": 0.24,
        "kept_for": {"start": _at(1), "end": _at(2), "kind": "export"},
    }


def test_adjacent_export_hours_form_one_kept_for_run():
    why = _reasons(
        [
            _row(0, 0.30, 80, mode="hold"),
            _row(1, 0.46, 60, export=2.0, mode="export"),
            _row(2, 0.46, 40, export=2.0, mode="export"),
            _row(3, 0.30, 40, mode="hold"),
        ]
    )
    assert why[_at(0)]["kept_for"] == {"start": _at(1), "end": _at(3), "kind": "export"}
    assert why[_at(1)]["kept_for"]["kind"] == "next_day"


def test_without_a_later_release_the_energy_is_kept_for_the_next_day():
    horizon = [_row(0, 0.30, 80, mode="hold"), _row(1, 0.28, 80, mode="hold")]
    why = _reasons(horizon, terminal=0.35)
    assert why[_at(0)] == {
        "kind": "hold",
        "keep_eur": 0.35,
        "use_eur": 0.24,
        "kept_for": {"start": _at(2), "end": None, "kind": "next_day"},
    }
    assert _reasons(horizon, terminal=None) == {}


def test_a_house_hour_is_weighed_against_the_next_day_only():
    horizon = [_row(0, 0.30, 70, house=0.5), _row(1, 0.46, 50, export=2.0, mode="export")]
    assert _reasons(horizon, terminal=0.2)[_at(0)] == {
        "kind": "house",
        "keep_eur": 0.2,
        "use_eur": 0.24,
        "kept_for": {"start": _at(2), "end": None, "kind": "next_day"},
    }
    assert _at(0) not in _reasons(horizon, terminal=0.35)


def test_a_house_hour_before_a_cheaper_grid_charge_is_weighed_against_the_refill():
    horizon = [
        _row(0, 0.30, 70, house=0.5),
        _row(1, 0.20, 80, grid=2.0, mode="grid"),
        _row(2, 0.18, 90, grid=2.0, mode="grid"),
        _row(3, 0.30, 90, mode="hold"),
    ]
    assert _reasons(horizon, terminal=0.35)[_at(0)] == {
        "kind": "house",
        "keep_eur": 0.2,
        "use_eur": 0.24,
        "kept_for": {"start": _at(1), "end": _at(3), "kind": "refill"},
    }


def test_a_grid_charge_dearer_than_the_next_day_is_no_refill():
    why = _reasons([_row(0, 0.40, 70, house=0.5), _row(1, 0.30, 80, grid=2.0, mode="grid")], terminal=0.3)
    assert (why[_at(0)]["keep_eur"], why[_at(0)]["kept_for"]["kind"]) == (0.3, "next_day")


def test_an_export_before_a_richer_export_is_weighed_against_the_next_day():
    why = _reasons([_row(0, 0.40, 70, export=2.0, mode="export"), _row(1, 0.46, 50, export=2.0, mode="export")])
    assert why[_at(0)] == {
        "kind": "export",
        "keep_eur": 0.2,
        "use_eur": 0.35,
        "kept_for": {"start": _at(2), "end": None, "kind": "next_day"},
    }


def test_a_charge_hour_compares_its_cost_with_the_later_release():
    why = _reasons([_row(0, 0.24, 90, grid=2.0, mode="grid"), _row(1, 0.46, 70, export=2.0, mode="export")])
    assert why[_at(0)]["use_eur"] == round(0.24 / 0.9, 3)
    assert (why[_at(0)]["kind"], why[_at(0)]["keep_eur"]) == ("charge", 0.41)
    assert _at(0) not in _reasons([_row(0, 0.24, 90, grid=2.0, mode="grid")], terminal=0.2)


def test_an_hour_charging_only_from_the_sun_has_no_reason():
    horizon = [_row(0, 0.30, 80, solar=1.0, mode="solar"), _row(1, 0.46, 60, export=2.0, mode="export")]
    assert _at(0) not in _reasons(horizon)


def test_past_and_estimated_slots_have_no_reason():
    horizon = [
        _row(-1, 0.30, 80, mode="hold"),
        _row(0, 0.30, 80, mode="hold"),
        _row(1, 0.30, 80, est=True),
    ]
    assert list(_reasons(horizon, terminal=0.35)) == [_at(0)]
    assert _reasons([_row(0, 0.30, 80, mode="actual")], terminal=0.35) == {}


def test_an_hour_that_exports_and_feeds_the_house_is_an_export():
    why = _reasons([_row(0, 0.30, 80, mode="hold"), _row(1, 0.46, 60, house=0.5, export=1.0, mode="export")])
    assert why[_at(0)]["kept_for"]["kind"] == "export"
    assert why[_at(0)]["keep_eur"] == 0.41
    assert (why[_at(1)]["kind"], why[_at(1)]["use_eur"]) == ("export", 0.41)


def test_an_empty_pack_has_nothing_to_keep():
    # The level does not fall, so the plan's house use at the floor came from the grid.
    night = [_row(0, 0.30, 5, mode="hold"), _row(1, 0.50, 5, house=0.5), _row(2, 0.40, 5, mode="hold")]
    assert _reasons(night, terminal=0.35, soc=5.0) == {}
    assert _reasons([_row(0, 0.30, 4, house=0.2), _row(1, 0.30, 4, mode="hold")], terminal=0.35, soc=5.0) == {}


def test_a_grid_charge_up_to_the_floor_keeps_its_reason():
    why = _reasons([_row(0, 0.20, 5, grid=0.5, mode="grid"), _row(1, 0.30, 5, mode="hold")], terminal=0.35, soc=3.0)
    assert list(why) == [_at(0)]
    assert (why[_at(0)]["kind"], why[_at(0)]["kept_for"]["kind"]) == ("charge", "next_day")


def test_a_hold_is_kept_for_the_release_before_the_next_charge():
    horizon = [
        _row(0, 0.30, 80, mode="hold"),
        _row(1, 0.40, 60, export=2.0, mode="export"),
        _row(2, 0.15, 90, grid=3.0, mode="grid"),
        _row(3, 0.60, 50, export=4.0, mode="export"),
    ]
    why = _reasons(horizon, terminal=0.5)
    assert why[_at(0)]["keep_eur"] == 0.35
    assert why[_at(0)]["kept_for"] == {"start": _at(1), "end": _at(2), "kind": "export"}


def test_a_hold_before_a_refill_is_weighed_against_buying_it_back():
    horizon = [
        _row(0, 0.30, 80, mode="hold"),
        _row(1, 0.30, 85, grid=1.0, mode="grid"),
        _row(2, 0.28, 90, grid=1.0, mode="grid"),
        _row(3, 0.60, 50, export=4.0, mode="export"),
    ]
    assert _reasons(horizon, terminal=0.5)[_at(0)] == {
        "kind": "hold",
        "keep_eur": round(0.28 / 0.9, 3),
        "use_eur": 0.24,
        "kept_for": {"start": _at(1), "end": _at(3), "kind": "refill"},
    }
    horizon[1]["price"] = horizon[2]["price"] = 0.15
    assert _at(0) not in _reasons(horizon, terminal=0.5)


def test_a_charge_hour_looks_past_its_own_run():
    horizon = [
        _row(0, 0.20, 70, grid=1.0, mode="grid"),
        _row(1, 0.25, 80, grid=1.0, mode="grid"),
        _row(2, 0.60, 50, export=4.0, mode="export"),
    ]
    assert _reasons(horizon)[_at(0)]["kept_for"] == {"start": _at(2), "end": _at(3), "kind": "export"}


def test_the_next_day_beats_a_poorer_later_release():
    why = _reasons([_row(0, 0.30, 80, mode="hold"), _row(1, 0.35, 70, house=0.5)], terminal=0.4)
    assert why[_at(0)]["keep_eur"] == 0.4
    assert why[_at(0)]["kept_for"] == {"start": _at(2), "end": None, "kind": "next_day"}


def test_a_sunny_idle_hour_has_no_reason():
    horizon = [_row(0, 0.30, 80, load=0.3, pv=1.0), _row(1, 0.46, 60, export=2.0, mode="export")]
    assert _at(0) not in _reasons(horizon)


def test_an_unknown_value_gives_that_hour_no_reason():
    horizon = [_row(0, 0.30, 80, mode="hold"), _row(1, 0.46, 60, export=2.0, mode="export")]
    why = _reasons(horizon, terminal=0.35, export_value=lambda _s, _p: None)
    assert list(why) == [_at(0)] and why[_at(0)]["kept_for"]["kind"] == "next_day"
    assert list(_reasons(horizon, terminal=0.35, use_value=lambda _p, _w: None)) == [_at(1)]
