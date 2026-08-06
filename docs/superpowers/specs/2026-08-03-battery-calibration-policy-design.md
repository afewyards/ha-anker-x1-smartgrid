# Periodic full-charge calibration policy — design

Date: 2026-08-03

## Context

The pack reports 20 kWh nominal (4 modules × 5 kWh) and the planner converts
SoC% ↔ kWh linearly against it. Measurement on the lab instance shows the
bottom of the reported SoC scale is very nearly empty, so the DP books
overnight energy that does not exist and the pack collapses to the firmware
floor earlier than planned (it reached 2% on the morning of 2026-08-03).

Evidence, from HA history (AC energy at `sensor.anker_x1_battery_power`,
integrated between SoC steps):

Full discharge runs:

| peak → trough | modules | SoC | Δ% | AC out | Wh/1% | nominal | ratio |
|---|---|---|---|---|---|---|---|
| 07-24 16:44 → 07-25 09:47 | 2 | 99→5 | 94 | 8.05 kWh | 86 | 100 | 0.86 |
| 07-26 16:34 → 07-27 06:26 | 2 | 99→4 | 95 | 8.17 kWh | 86 | 100 | 0.86 |
| 07-30 16:38 → 07-31 10:08 | 4 | 98→21 | 77 | 14.78 kWh | 192 | 200 | 0.96 |
| 08-02 17:12 → 08-03 08:06 | 4 | 99→2 | 97 | 15.03 kWh | 155 | 200 | 0.77 |

The 07-30 run is decisive: across 98→21% the pack delivers 192 Wh per 1%,
96% of the 200 Wh/1% implied by 4 × 5 kWh. All four modules deliver rated
energy over the top three-quarters of the scale — the added pair works. The
08-02 run then went 19 points deeper and gained only 0.25 kWh. So roughly
**3.6 kWh is stranded below ~21%**, not missing from the pack as a whole.

Wh delivered per 1% SoC by band, 4-module era (nominal ~190 Wh/1%):

```
90-40%   172-194      20-29%   152      10-19%   76      0-9%   10
```

The 2-module era shows the same defect, smaller: ~1.4 kWh stranded (14% of
nominal) against 3.6 kWh (19%) with four modules. Stranded energy more than
doubled in absolute terms when the modules were added, consistent with the
weakest module reaching its low-voltage cutoff first and stopping the stack.

The pack has had no opportunity to top-balance. On 2026-08-02 it tapered from
12 kW to ~350 W, reached 99% at 17:15, then held **0 W for 2.5 h** while
1.0–1.4 kW of PV was exported instead. Peak pack voltage was 54.3 V, against
54.7 V and 55.0 V on the two days it did report 100%.

## Goal

Ensure the pack periodically reaches and dwells at the top of its range, so
the module BMSs get taper current to balance, and so we can test whether that
recovers the stranded capacity.

## Non-goals

Explicitly out of scope for this wave, decided with the user:

- **No planning-floor change.** The DP keeps clamping at the firmware 5%
  floor and will keep booking the ~3.6 kWh that isn't there until a later
  wave acts on it.
- **No stranded-capacity instrumentation.** Verification is a manual
  re-run of the band analysis (see Verification), not a shipped feature.
- **No manual trigger.** No button or service call; the policy is automatic
  only.

## Approach

A pure `calibration.py` module decides whether a calibration cycle is running
in the current slot. `controller.py::_tick_impl` consults it and overrides
the plan when it is. The DP core is untouched.

The DP-integrated alternative — threading a per-hour SoC floor array through
`optimize_grid` alongside `reserve_by_hour`/`hedge_drain_kwh` and raising the
floor clamp at `optimize.py:855-857` — was rejected. It would let the DP shop
for the cheapest pre-charge on its own, but the DP core is under a byte-parity
gate with the oracle (`regret.hindsight_optimal_grid`,
`tests/test_optimize_parity.py`) so every change must be mirrored, and the
floor clamp models a breach as forced import with an unbounded fill rate,
which is wrong for a floor that binds for hours. The expensive decision is
*which day*, and that is policy, not DP. Keeping a deliberately non-economic
behaviour quarantined from the economic planner is also the point:
`economic-only-charging-merged` removed every other force-charge path.

Note `reserve_by_hour` is **not** a usable hook — it constrains voluntary
export only (`optimize.py:891`), not load-serving discharge.

## Module interface

`calibration.py` is pure: no HA imports, no I/O, no clock reads. It keeps no
state of its own, though — the hold latch (Success detection) needs to know
whether the *previous* tick was already holding, so the caller threads that
in as a keyword argument.

```
calibration_action(
    now, soc_pct, slots, soc_samples, price_history, cfg,
    *, already_holding=False,
) -> CalibAction | None
```

`CalibAction` carries the phase (`charging` | `holding`) and the active
window. `None` means calibration is not running this slot.

## Success detection

A completed cycle is a contiguous run in `samples` of `state == FORCING`
samples that **starts** at `soc >= calibration_top_soc` and **continues**
while `soc >= continue_soc(cfg)`, lasting at least `calibration_dwell_h`.
Read back from history; never stored.

The asymmetry is the point. Entry at the target is what makes the hour an
hour at the *top*; entry at a discounted bar buys an hour of ordinary bulk
charging instead. Measured 2026-07-30, this pack was still drawing −3.9 kW at
98% and −5.1 kW at 99% — both bulk — with the taper only appearing at −540 W
right at the cap. The continuation allowance exists for the opposite reason:
at a true 100% the inverter cuts charge dead and the pack self-discharges
into house load (+270 W on the same run), so without it a genuine dwell would
break on drift alone while FORCING is still commanded.

Both halves are load-bearing. An earlier revision of this spec asserted that
"whether charging was *permitted* during the run is not reconstructible from
`samples` and is not checked — the outcome is what matters". That was wrong
on both counts: `samples.state` records it exactly, and the outcome is not
what matters, because a high SoC reached passively is not the same physical
event as a commanded hold. Shipping the SoC-only rule made the policy
self-suppressing, verified on 42 days of lab history (2026-08-06):

- 19 runs cleared the SoC bar for ≥1 h. **All 19 were `state=passive` with
  `setpoint_w=0.0`** — sunny-afternoon plateaus where the controller
  commanded nothing. Sampled across two of them, `batt_w` averaged +237 W and
  −202 W: the pack was idling and even net-discharging, delivering no taper
  current and balancing nothing.
- Gaps between those phantom successes ran 0.6–7.9 days, so `days_since`
  almost never reached the 5-day interval. The override never fired once.
- Meanwhile the pack kept stranding ~3.6 kWh at the bottom — on 2026-08-06 it
  fell 20%→5% in 79 minutes — which is the symptom the policy exists to fix.

With the `state` half in place the run-membership rule is uniform: a
non-FORCING sample breaks the run exactly as a below-bar one does, since in
both cases the current stopped and the halves either side are separate
dwells. A NULL `state` (rows predating the column being populated) is simply
not FORCING, which is the fail-closed answer.

Remaining consequences, all intended:

- No new table, no `Store`, no schema migration; restart-safe by
  construction. Sample retention is 90 days against a 5-day interval.
- A naturally-full pack no longer counts as a success, so summer no longer
  suppresses the policy. Once due, the hold-through branch (Execution/Safety
  bounds) still forces FORCING and suppresses export the moment the pack is
  next observed at the hold bar regardless of cause — a solar-driven full
  charge landing after the interval elapsed converts into a forced
  ≤`calibration_dwell_h` hold, which is the cheap way to satisfy the cycle.
- The dwell needs no timer: during the hold the run is not yet `dwell_h`
  long, so the state stays `holding`; when it crosses, the same query reports
  success and calibration goes idle.

## Trigger and window selection

Due when `days_since_last_success >= calibration_interval_days`. When no
qualifying run exists at all, due only if the SoC history *spans* at least
`calibration_interval_days` — that distinguishes "genuinely never calibrated"
from "fresh install with no data yet", which stays idle.

Once due, each tick selects the cheapest contiguous run of
`charge_h + dwell_h` in the visible price curve, where

```
charge_h = (calibration_top_soc - soc_pct)/100 * capacity_kwh
           / (max_charge_w/1000 * eta_charge)
```

clamped at zero. The window is accepted when the unweighted mean of its slot
prices is at or below the `CALIBRATION_PRICE_PERCENTILE`-th percentile of all
individual slot prices in `PriceHistoryStore.history` (day → slot → price,
`max_days` = the live `price_history_days`). Past
`interval_days + CALIBRATION_GRACE_DAYS`, the cheapest visible window is
accepted unconditionally.

Prices are visible roughly one day ahead (tomorrow publishes ~13:00), so there
is no multi-day window to shop in; this is an accept-first-cheap-enough rule
against a historical bar, not a search.

**Stability without stored commitment.** The decision is a deterministic
function of (now, prices, SoC, history), and published prices do not change
within a day, so recomputation each tick yields the same window. Two pure
rules cover the edges:

- A window that has already started is never abandoned, so the 13:00
  publication of tomorrow's prices cannot yank a cycle mid-charge.
- At most one window per UTC start-date. `select_window` groups candidates by
  `cand[1].date()` on the UTC-normalised `PriceSlot.start` (parsers.py), and
  this module never threads a timezone in — so the boundary is 00:00 UTC
  (02:00 local in CEST), not local midnight. The real bound is up to 2
  attempts per local day, not 1.

## Execution

When `calibration_action` returns non-`None`, `controller.py::_tick_impl`
overrides the plan in place: `new_plan.state` becomes `FORCING` at max charge
rate (`state_since` carried over from an already-FORCING plan, or stamped
`now` on a fresh entry). Export logic, plan publishing and the DP itself are
unchanged.

Cancellation lives in the same block, via `self._calibration_engaged` (true
iff `self.plan` was FORCING because of calibration as of the end of the
previous tick). Once `calibration_action` stops returning non-`None`, a
calibration-held `FORCING` plan reverts to `PASSIVE` only when ALL four hold:
the previous tick was calibration-engaged, `new_plan` is the SAME `PlanState`
object the scheduler returned this tick (identity — the scheduler is
coasting, not making a fresh decision), that plan is still `FORCING`, and the
DP does not currently want this hour (`_dp_out["now_selected"]` is falsy). If
any one fails — most importantly, if the DP itself still wants the hour — the
plan is left alone.

Both phases map to the same actuation. When the pack is full the BMS accepts
~0 and house load falls to the grid — that **is** the hold, so no separate
hold mechanism exists. The phase label is for reporting only.

The actuator's live-BMS-limit clamp (`ce224ac`) is load-bearing here.
Calibration is precisely the case that pins a setpoint against a shrinking
`sensor.anker_x1_rechargeable_power` for hours, which is what half-engaged the
inverter on 2026-07-14.

## Safety bounds

These bounds cover the `select_window`/"charging" path only. The
hold-through branch (Execution) is separate and unbounded by any of them —
see below.

- Active only within `[window_start, window_start + charge_h + dwell_h]`. One
  failed attempt costs at most one window's charge.
- Up to 2 windows per local day (1 per UTC start-date — see Trigger and
  window selection).
- `calibration_top_soc` defaults to 100 (the firmware cap) but is the *charge
  target* only; the reachability question is about `hold_soc_bar(cfg)` =
  target − `CALIBRATION_HOLD_TOLERANCE` = 98, which the pack cleared on 23 of
  43 observed days. If the pack never reaches the bar, the attempt lapses
  cleanly at window end and retries at the next cheap-enough day. If the pack
  reaches it but wobbles at the bar, the hold latch can keep renewing the
  hold without the dwell ever completing — not bounded by window end; see
  Known limitations.
- Calibration wins over export for its slots. Cheap windows land overnight or
  midday, so overlap with the evening peak is unlikely in practice.
- Ships off.

**Hold-through has none of the above guards.** Once a cycle is due
(`days_since >= calibration_interval_days`) and the pack is observed at or
above `calibration_top_soc` — by any cause, not just a policy-selected
window, including ordinary solar overproduction — `calibration_action`
returns `holding` unconditionally: no window check, no price bar, no
time-of-day guard, and no once-per-day limit. The controller then forces
`FORCING`, which makes `executor.run_forcing_and_export` take the FORCING
branch and skip the export executor entirely (mutual exclusion — see
Execution). So on a summer day where solar crosses 97% at 16:40, the system
force-charges (a no-op once the BMS is already tapering) and suppresses ALL
export until the run has held `calibration_dwell_h` — straight across the
evening peak, whatever that costs that day. Accepted, user-confirmed
behaviour for this wave; see Success detection for the self-satisfying case's
real cost.

**Is the 100% target actually reachable?** An earlier revision discounted the
entry bar by 2 points on the grounds that "13 of 43 observed days topped out
at exactly 99%, against only 6 reaching 100". That statistic is real but was
misread: **every one of those 13 days was `state=passive`**, and on 11 of
them the power flowing in at the plateau was 353–861 W — solar trickle
running out, not the BMS refusing. The remaining two (07-18, 07-26) show
short 6 kW bursts that reach 99, cut to **0 W**, let the pack drift to 97,
then burst again — charge stopping, not a ceiling.

The one *sustained* charge on record (2026-07-30, ~12 kW continuous, driven
by the Anker app rather than this controller) went 99 → 100 cleanly and then
**held 100 for 1.6 hours**. So under sustained commanded charge the cap is
both reachable and holdable for longer than `calibration_dwell_h`, and the
entry bar belongs at the target.

The residual hazard is unchanged in shape but narrower: if the pack cannot
reach 100 under sustained max-rate charge, `last_success_end` never returns,
`days_since` never resets, and past `interval_days + CALIBRATION_GRACE_DAYS`
the policy buys the cheapest window every day indefinitely. No day in 42
demonstrates that, so it is guarded operationally rather than structurally —
the controller logs a warning after 3 consecutive lapsed attempts so nightly
forcing is visible rather than silent. Raise `calibration_interval_days` in
winter, and watch `calibration_days_since` climbing without bound.

**Fail-closed on missing data.** Empty or short SoC history (fresh install)
means idle, not "never calibrated, charge now". An empty price history
disables the percentile path; only the deadline path can fire. A recorder read
failure returns `None` and logs once. Nothing forces a charge on absent data.

Cost framing: a full charge on a cheap night is not money burned — the DP
exports or self-consumes it later. Net cost is the kWh above what the DP would
have bought anyway, less what comes back.

## Known limitations

**RESOLVED: the latch softening now applies to success detection.** This was
listed here as candidate fix #1 and has been taken. `calibration_action`,
`_open_run_start` and `last_success_end` all use the same pair of bars —
entry at `calibration_top_soc`, continuation at `continue_soc(cfg)` — so a
pack oscillating across the top can no longer sustain `holding` while the
underlying success run keeps resetting. The remaining candidate (latching on
elapsed wall-clock since the run's real start rather than on SoC) stays
untaken; nothing yet demands it.

**Whether a commanded hold at the top actually passes current is unverified.**
The policy has never completed a real forced dwell, so there is no
observation of `FORCING` at ≥98% SoC. The one full charge on record
(2026-07-30, driven by the Anker app rather than this controller) shows the
inverter cutting charge dead at 100% and the pack drifting back down at
+270 W. If a commanded hold behaves the same way, the dwell is a rest at
open circuit and delivers no balancing regardless of these knobs — that would
be a firmware/actuation problem, not a policy one. Check `batt_w` during the
first live `holding` phase before trusting the cycle.

## Configuration

In `config_flow._TUNABLES`. Options outside `_TUNABLES` are wiped by the next
UI options save.

| Option | Default |
|---|---|
| `calibration_enabled` | `True` |
| `calibration_interval_days` | `5` |
| `calibration_top_soc` | `100` (charge target — see below) |
| `calibration_dwell_h` | `1` |

Consts in `const.py`, not user-tunable: `CALIBRATION_PRICE_PERCENTILE = 30`,
`CALIBRATION_GRACE_DAYS = 7`, `CALIBRATION_HOLD_TOLERANCE = 1.0`.

`calibration_top_soc` is BOTH the charge target and the dwell-entry bar.
`CALIBRATION_HOLD_TOLERANCE` is a continuation allowance only — it never
discounts entry. Setting the target back to 98 therefore reinstates the
pre-fix behaviour of holding in bulk charge rather than in the taper.

The 5-day interval no longer self-satisfies in summer, because passive
plateaus stopped counting: expect a genuine forced cycle every 5 days
year-round. Raise it in the UI if that is too often. If `calibration_interval_days` is ever
raised above `retention_days` (90 by default), the never-calibrated fallback
(history must *span* `calibration_interval_days` to count as overdue) can
never be satisfied from a cold start — the controller logs this once.

## Observability

Plan-sensor attributes: `calibration_state`, `calibration_window_start`,
`calibration_window_end`, `calibration_last_success`, `calibration_days_since`.

`calibration_days_since` saturates on the never-calibrated fallback: it
reports the read window's span, which is capped at
`calibration_interval_days × 3` (~15 days at defaults) regardless of how much
longer the pack has actually gone uncalibrated. Deliberate parity with the
policy's own read window, not a diagnostic of true elapsed time.

## Files

| File | Change |
|---|---|
| `calibration.py` | new, pure |
| `controller.py` | override + cancel logic in `_tick_impl` |
| `recorder.py` | `read_soc_samples`, mirroring `read_load_samples` |
| `config_flow.py` | four options in `_TUNABLES` |
| `const.py` | defaults and the two consts |
| `models.py` | `Config` fields |
| `sensor.py` | plan attributes |
| `tests/` | as below |

## Testing

Pure unit tests on `calibration.py`:

- due / not-due at the interval boundary
- success detection from synthetic SoC series, including a run just under
  `dwell_h` (must not count)
- percentile gate, accept and reject
- deadline force past `interval_days + grace`
- at most one window per UTC start-date (up to 2 per local day)
- a started window is not abandoned when the horizon extends
- fail-closed: empty SoC history yields `None`; empty price history blocks
  the percentile path but still permits the deadline path

At the controller boundary (`_tick_impl`, `tests/test_calibration_controller.py`):

- isolation: `calibration_enabled=False` ⇒ decision unchanged, per the parity
  convention used throughout this codebase
- active path: calibration active ⇒ `FORCING` at max rate regardless of what
  the DP wanted
- cancel path: reverting a calibration-held `FORCING` plan to `PASSIVE`
  requires all four of previous-tick engagement, plan-object identity,
  still-`FORCING`, and the DP not wanting the hour
- hold-through wobble: the latch's softened re-entry bar does not churn
  engage/release on a one-point SoC dip

No live-fire test.

## Verification

Manual, after the first completed cycle: re-run the Wh-per-1%-by-band analysis
over HA history and compare the 10–19% band against the current baseline of
42–76 Wh/1% (nominal ~190). Recovery toward nominal confirms that balancing
reclaims the stranded capacity; no change refutes it, and the planning-floor
wave becomes the answer instead.

## Open risks

- The mechanism is unproven. Balancing may not recover the stranded capacity,
  in which case the policy costs a periodic charge for nothing. This wave is
  in part the experiment that decides it.
- 07-30 reported 100% and the following deep run on 08-02 still showed the
  collapse. That is one observation against a single dwell being sufficient,
  and it may take several cycles to show an effect — or the cause may not be
  balance at all.
- Until the planning-floor wave lands, the DP continues to book ~3.6 kWh that
  does not exist, so overnight overruns continue regardless of this policy.
