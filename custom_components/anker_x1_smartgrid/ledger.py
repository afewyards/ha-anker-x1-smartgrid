"""Cash-basis battery ledger: daily + lifetime euro accumulators (Task C4).

Extracted verbatim from controller.py. Controller keeps thin wrapper methods
(``_rollover_daily_ledgers`` / ``_accumulate_cash_ledger``) that delegate to
this module's ``CashLedger`` — its fields are exposed on Controller via
properties of the same name so ``_PERSIST_GROUPS`` (table-driven persist/
restore, Task A9) keeps working unchanged: the store payload/format is
byte-identical to before this extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import const, coordinator, optimize as optimize_mod, resolution
from .models import Config, PlantInputs, PriceSlot


@dataclass
class CashLedger:
    """Realized battery €-flows: daily accumulators + a lifetime total.

    ``today_export_pnl_eur`` is E3's realized-arbitrage PnL (economic, with
    cycle-cost / opportunity deductions) — deliberately distinct from the
    cash-basis ``today_charge_cost_eur`` / ``today_export_revenue_eur`` /
    ``total_net_eur`` (spec 2026-07-10-battery-cash-ledger). The two share
    one day-stamp (``day``) and reset together in :meth:`rollover`.
    """

    today_export_pnl_eur: float = 0.0
    today_charge_cost_eur: float = 0.0
    today_export_revenue_eur: float = 0.0
    total_net_eur: float = 0.0
    # Local-date string of the day the daily fields cover (YYYY-MM-DD).
    # None on first tick so the day-rollover logic fires immediately to initialise.
    day: str | None = None

    def rollover(self, now: datetime) -> None:
        """Reset ALL per-day € ledgers on local-day rollover (single day key).

        E3 (today_export_pnl_eur) and the cash ledger share ``day``: every
        daily field MUST be reset here, in one pass, before the key is
        advanced — a second key-comparison block elsewhere would never fire.
        ``day`` is None on first-ever tick (or pre-key stores) — treated as
        rollover; a mid-day restart restores the key from the persisted blob
        and same-day accumulation continues.
        """
        _today = dt_util.as_local(now).date().isoformat()
        if _today != self.day:
            self.today_export_pnl_eur = 0.0
            self.today_charge_cost_eur = 0.0
            self.today_export_revenue_eur = 0.0
            self.day = _today

    def accumulate(
        self,
        hass: HomeAssistant,
        data: dict,
        cfg: Config,
        now: datetime,
        inputs: PlantInputs,
        slots: list[PriceSlot],
        slot_minutes: int,
        raw_export_price: float | None,
    ) -> None:
        """Accumulate realized battery cash flows for this tick (cash basis).

        Two independent legs; each is skipped when its own price is missing,
        and a missing battery reading skips both:

        - cost leg   — grid import feeding the battery × current import-slot
          price.  Price comes from the DP's ``slots`` list via
          resolution.price_at (static-tariff safe) — NEVER from a direct
          CONF_ENT_PRICE read, which is empty under static tariff mode and
          would silently zero this leg.
        - credit leg — battery-sourced grid export × effective (post-fee)
          feed-in price, as in the C3 path.

        Attribution mirrors the C3 battery-sourced-export rule (PV covers the
        house first; PV-spill export out of scope).  See
        optimize.cash_flows_eur for the math.
        """
        batt_w = coordinator.read_float(hass, data.get(const.CONF_ENT_BATTERY_POWER, ""))
        if batt_w is None:
            return
        import_price = resolution.price_at(slots, now, slot_minutes)
        export_price_eff = (
            optimize_mod.effective_export_price(raw_export_price, cfg) if raw_export_price is not None else None
        )
        cost, credit = optimize_mod.cash_flows_eur(
            inputs.meter_w,
            batt_w,
            import_price,
            export_price_eff,
            const.TICK_SECONDS / 3600.0,
        )
        self.today_charge_cost_eur += cost
        self.today_export_revenue_eur += credit
        self.total_net_eur += credit - cost
