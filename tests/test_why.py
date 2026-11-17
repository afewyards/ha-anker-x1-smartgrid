from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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


def _why(horizon, terminal=0.2, **kw):
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
            "export_price": lambda _s, p: p,
            "export_fee": 0.02,
            "eta_charge": 0.9,
            "eta_discharge": 1.0,
            "cycle_cost": 0.03,
            "charge_margin": 0.0,
        }
        | kw,
    )


def _reasons(horizon, terminal=0.2, **kw):
    return {k: {f: v for f, v in r.items() if f != "beats"} for k, r in _why(horizon, terminal, **kw).items()}


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


def test_a_pack_parked_at_the_backup_reserve_has_nothing_to_keep():
    night = [_row(0, 0.30, 30, mode="hold"), _row(1, 0.50, 30, house=0.5), _row(2, 0.40, 30, mode="hold")]
    assert _reasons(night, terminal=0.35, soc=30.0)
    assert _reasons(night, terminal=0.35, soc=30.0, backup_reserve_soc=30.0) == {}


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


ETA_D, ETA_C, FEE, WEAR, MARGIN, DRAIN_W = 0.92, 0.95, 0.02, 0.03, 0.04, 100.0


def _house_value(p, w):
    return p * (w + DRAIN_W) / (w / ETA_D + DRAIN_W) - WEAR


PRICED = {
    "use_value": _house_value,
    "export_value": lambda _s, p: (p + 0.01 - FEE) * ETA_D - WEAR,
    "charge_cost": lambda p: p / ETA_C + MARGIN,
    "export_price": lambda _s, p: p + 0.01,
    "export_fee": FEE,
    "eta_charge": ETA_C,
    "eta_discharge": ETA_D,
    "cycle_cost": WEAR,
    "charge_margin": MARGIN,
}
VALUE = {
    "sell": lambda x, _w: (x - FEE) * ETA_D - WEAR,
    "charge": lambda p, _w: p / ETA_C + MARGIN,
    "house": _house_value,
}
NOW_PARTS = {"sell": ["fee", "conversion", "wear"], "charge": ["conversion", "margin"], "house": ["conversion", "wear"]}
PARTS = {
    "sell": ["kept", "wear", "conversion", "fee"],
    "charge": ["kept", "margin", "conversion"],
    "house": ["kept", "wear", "conversion"],
}


def _day():
    return [
        _row(0, 0.20, 60, grid=2.0, mode="grid"),
        _row(1, 0.30, 60, mode="hold"),
        _row(2, 0.34, 55, house=0.5),
        _row(3, 0.60, 40, export=2.0, mode="export"),
    ]


def test_each_build_up_is_priced_where_using_it_equals_keeping_it():
    why = _why(_day(), terminal=0.1, **PRICED)
    assert {b["q"] for r in why.values() for b in r["beats"]} == set(VALUE)
    for row in why.values():
        for b in row["beats"]:
            keep = b["parts"][0]["eur"]
            assert VALUE[b["q"]](b["beat_eur"], 500.0) == pytest.approx(keep, abs=1e-6)
            assert sum(p["eur"] for p in b["parts"]) == pytest.approx(b["beat_eur"], abs=1e-9)
            assert [p["key"] for p in b["parts"]] == PARTS[b["q"]]
            assert keep == pytest.approx(row["keep_eur"], abs=5e-4)


def test_each_kind_gets_its_build_ups_against_its_own_price():
    why = _why(_day(), terminal=0.1, **PRICED)
    assert [why[_at(k)]["kind"] for k in range(4)] == ["charge", "hold", "house", "export"]
    assert [[b["q"] for b in why[_at(k)]["beats"]] for k in range(4)] == [
        ["charge"],
        ["house", "sell"],
        ["house", "sell"],
        ["sell"],
    ]
    assert [[b["price"] for b in why[_at(k)]["beats"]] for k in range(4)] == [
        [0.20],
        [0.30, 0.31],
        [0.34, 0.35],
        [0.61],
    ]
    unpriced = _why(_day(), terminal=0.1, **PRICED | {"export_price": lambda s, p: p if s == NOW + 3 * H else None})
    assert [[b["q"] for b in unpriced[_at(k)]["beats"]] for k in (1, 2)] == [["house"], ["house"]]


def test_a_hold_whose_export_price_clears_the_selling_bar_says_what_held_it():
    def sell(export_now, reserve):
        horizon = [_row(0, 0.30, 50, mode="hold") | {"reserve_soc": reserve}, _row(1, 0.46, 30, export=2.0)]
        why = _why(horizon, export_price=lambda s, p: export_now if s == NOW else p)
        return why[_at(0)]["beats"][1]

    assert sell(0.90, 50.0)["beat_eur"] == pytest.approx(0.46)
    assert sell(0.90, 50.0)["blocked"] == "reserve"
    assert sell(0.90, 20.0)["blocked"] == "rule"
    assert "blocked" not in sell(0.30, 50.0)


def test_a_price_that_clears_the_unrounded_bar_by_a_hair_beats_it():
    terminal = 0.1996
    bar = (terminal + 0.03) / 1.0 + 0.02
    why = _why([_row(0, bar + 0.0003, 60, export=2.0, mode="export")], terminal=terminal)
    (sell,) = why[_at(0)]["beats"]
    assert sell["price"] >= sell["beat_eur"]


def test_a_hold_or_house_hour_keeps_for_the_value_the_hold_rule_weighs():
    kept = {NOW: 0.50, NOW + H: 0.10}
    why = _reasons(
        [_row(0, 0.30, 80, mode="hold"), _row(1, 0.46, 60, house=0.5, mode="battery")],
        kept_value=lambda s: kept.get(s),
    )
    assert why[_at(0)]["keep_eur"] == 0.5
    assert why[_at(1)]["keep_eur"] == 0.1


def test_a_held_hour_names_the_slot_its_kept_value_comes_from():
    horizon = [_row(0, 0.30, 80, mode="hold"), _row(1, 0.30, 80, mode="hold"), _row(2, 0.46, 60, export=2.0)]
    why = _why(
        horizon,
        kept_value=lambda s: 0.40,
        kept_source=lambda s: ("export", NOW + 2 * H),
    )
    assert why[_at(0)]["kept_for"] == {"start": _at(2), "end": _at(3), "kind": "export"}
    assert why[_at(0)]["kept_price"] == 0.46
    assert why[_at(0)]["kept_estimated"] is False


def test_a_kept_value_bought_back_in_an_estimated_slot_says_so():
    horizon = [_row(0, 0.30, 80, mode="hold"), _row(1, 0.25, 80, est=True)]
    why = _why(horizon, kept_value=lambda s: 0.30, kept_source=lambda s: ("refill", NOW + H))
    assert why[_at(0)]["kept_for"] == {"start": _at(1), "end": _at(2), "kind": "refill"}
    assert (why[_at(0)]["kept_price"], why[_at(0)]["kept_estimated"]) == (0.25, True)


def test_a_kept_value_past_the_horizon_is_the_next_day():
    why = _why(
        [_row(0, 0.30, 80, mode="hold")],
        kept_value=lambda s: 0.30,
        kept_source=lambda s: ("next_day", None),
    )
    assert why[_at(0)]["kept_for"]["kind"] == "next_day"


def test_an_export_hour_is_weighed_against_its_own_kept_value():
    horizon = [_row(0, 0.46, 60, export=2.0, mode="export")]
    assert _at(0) not in _why(horizon, terminal=0.5)
    assert _why(horizon, terminal=0.5, kept_value=lambda s: 0.20)[_at(0)]["beats"]


def test_rising_export_hours_each_keep_a_reason_with_a_build_up():
    horizon = [_row(0, 0.40, 80, export=2.0, mode="export"), _row(1, 0.50, 60, export=2.0, mode="export")]
    why = _why(horizon, terminal=0.2, kept_value=lambda s: 0.45 if s == NOW else 0.20)
    assert why[_at(0)]["beats"]
    assert why[_at(1)]["beats"]


def test_an_hours_kept_source_is_its_last_quarter_s():
    horizon = [_row(0, 0.30, 80, mode="hold")]
    horizon = [dict(horizon[0], start=(NOW + k * H / 4).isoformat(), load_kwh=0.125) for k in range(4)]
    src = {NOW + k * H / 4: ("house", NOW + (2 * H if k == 3 else H / 4)) for k in range(4)}
    why = _why(horizon, kept_value=lambda s: 0.40, kept_source=src.get)
    assert why[_at(0)]["kept_for"]["start"] == _at(2)


def test_each_build_up_also_walks_from_the_price_to_what_using_it_now_is_worth():
    why = _why(_day(), terminal=0.1, **PRICED)
    for row in why.values():
        for b in row["beats"]:
            assert b["price"] + sum(p["eur"] for p in b["now_parts"]) == pytest.approx(b["now_eur"], abs=1e-9)
            assert b["now_eur"] == pytest.approx(VALUE[b["q"]](b["price"], 500.0), abs=1e-4)
            assert [p["key"] for p in b["now_parts"]] == NOW_PARTS[b["q"]]
            wins = b["now_eur"] >= row["keep_eur"] - 5e-4
            if b["q"] == "charge":
                assert not wins
            elif b["q"] == "house":
                assert wins == (row["kind"] == "house")
            elif not b.get("blocked"):
                assert wins == (row["kind"] == "export")
