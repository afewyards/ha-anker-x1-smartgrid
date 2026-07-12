"""Regenerate the GOLDEN fixture in tests/test_optimize_dt60_golden.py.

Recomputes optimize_grid() output for the canonical dt=60 scenario defined in
test_optimize_dt60_golden.py (imported, not duplicated) and prints a
paste-ready GOLDEN dict literal in the same format as the in-file fixture.

Run: python -m tests.regen_goldens
"""
from custom_components.anker_x1_smartgrid.optimize import optimize_grid

from tests.test_optimize_dt60_golden import _cfg, _scenario


def _format_golden(out: dict) -> str:
    lines = ["GOLDEN = {"]
    lines.append('    "schedule": [')
    for v in out["schedule"]:
        lines.append(f"        {v!r},")
    lines.append("    ],")
    lines.append(f'    "kwh": {out["kwh"]!r},')
    lines.append(f'    "eur": {out["eur"]!r},')
    lines.append("}")
    return "\n".join(lines)


def main() -> None:
    pv, load, price = _scenario()
    out = optimize_grid(
        pv, load, price, soc_start=50.0, cfg=_cfg(),
        window_start_h=0, window_len=24, dt_h=1.0, slots_per_day=24,
    )
    print(_format_golden(out))


if __name__ == "__main__":
    main()
