# Three-day power card

Date: 2026-11-13

## Intent

The planner's owner looks at a wall display and wants to read, at a glance,
what the battery is doing and what the house's power cost. Today the wall's
Power page shows one cost figure per day that mixes two things: what the
house's use cost, and money spent filling the battery. Since the low-power
hold shipped, most days no longer empty the battery, so a charge night makes
a day look expensive although most of that money is still stored.

We want a card that shows three days side by side (yesterday, today,
tomorrow) with honest per-day figures, shows the battery level with held
hours visible, and ships with the integration so other users of the planner
get it without building their own dashboard.

The agreed look is mockup 2 ("Three day panels") in
`~/Sites/ha-wall/.superpowers/brainstorm/86225-1794554588/content/power-explore-4.html`,
with the filled battery level and hatched held hours from mockup C in
`power-day-split.html` of the same folder.

## Goals

- One card, part of this integration, available in the card picker after
  installing the integration. No other custom cards required.
- Per day: house use (kWh), house cost (€), saved (€), average price.
- Battery line on top: level now, energy stored, what the stored energy cost
  per kWh, current state (charging, holding, to house, exporting, idle), and
  the 30-day saving.
- One battery level graph across all three days, filled, held hours hatched.
- Under each day: price over the day, coloured by what the battery did, and
  house use and solar per hour.
- Works on the owner's 464×459 px wall displays and on ordinary dashboards.

## Non-goals

- Replacing the two YAML example cards in `lovelace/`. They stay.
- The four tiles above the card on the wall (House, Solar, Battery, Price
  now). They are ordinary Home Assistant tiles and stay in the wall repo.
- More than three days, zooming, tooltips, or tap actions.
- A visual card editor. YAML config with sensible defaults is enough.
- Changing how the planner decides anything. This is display and accounting.

## Anti-goals

- A day's cost that jumps because the battery was charged. Charging is not a
  cost of the day it happens on.
- "Saved" looking better because stored solar was counted as free.
- A card that needs the owner's theme or the wall's fixed size to look right.
- A build step or bundled dependencies in the repo.

## The numbers (decided)

The battery is treated like a pantry: money spent filling it becomes a cost
on the day the house uses that energy.

**House cost per day** = grid power the house used directly, at that
moment's import price, plus battery power the house used, at what that
energy cost to store. Stored cost follows the existing weighted-average
inventory in `margin.py`, with one difference decided by the owner:
**solar that charges the battery is free** in this figure. Conversion losses
need no line of their own, because cost follows the battery level the way
`margin.py` already does it: when the level falls in an interval, the cost
that fall releases is booked. The battery-to-house cost of an interval is
that released cost times the house's share of the battery's AC output. A
level fall with no output (standby drain) goes to house cost in full. So
charge losses raise the cost per stored kWh, and discharge losses and drain
are booked when they happen. Export revenue is not part of house cost.

**Saved per day** is `battery_margin_eur`: output valued at that minute's
price minus stored cost, for grid-charged energy only. What stored solar
earned over the **export price given up** shows apart as "sun"
(`battery_solar_gain_eur`); each kWh out is grid or solar in proportion to
what is stored. The owner agreed to this split
knowingly: house cost answers "what did my use cost", saved answers "what
did the battery earn", and house cost and saved no longer add up exactly.
That is accepted.

**Stored cost per kWh** shown on the battery line uses the solar-is-free
inventory (the same one as house cost), so it reads as "what I paid for
what's in there".

**Today and tomorrow use the same two definitions, rolled over the plan.**
Both inventories (solar free for house cost, solar at export price for
saved) are rolled forward over the published plan rows from their current
state, with the stored energy taken from the plan's `soc`. Today is actual
so far plus the planned rest; tomorrow is fully planned and labelled
"plan". The existing `savings_eur` is not used: it books a whole charge on
the day it is bought, which is the jump this design removes.

The actual grid bill per day is deliberately not shown.

### Details settled after the builder check (the author's calls, not the owner's)

- **Splitting each interval.** Use `house_load_w` as `margin.py` does. While
  discharging, the battery's house part is `min(output, max(load − pv, 0))`,
  the same rule the margin uses; the rest of the output is export. The
  grid-direct part is whatever of `max(load − pv, 0)` the battery did not
  cover. While charging or idle, grid-direct is `max(load − pv, 0)`. Power
  solar covers directly costs nothing.
- **Battery output is counted on the AC side.** The margin currently values
  `batt_w`, which is DC and overstates output by the inverter loss (about
  15% at low power). For house cost use AC output (`load − pv − grid`
  while discharging). Fixing the same thing in the margin is welcome but
  changes published savings; do it as its own commit.
- **No reconciliation** with the existing `daily_stats` cash figures is
  required. The new figures only need to be consistent with each other.
- **House use** is the total house load, including what solar covered. It
  matches the kWh the wall shows today.
- **Average price** is the plain time mean of the import price over the
  local day, as the wall shows today. Cost ÷ kWh will not equal it; that is
  fine and needs no note.
- **Today** is one number: actual so far plus planned rest, no split shown.
- **Planned figures** need no change to the DP. Roll both inventories
  forward over the published plan rows: planned grid charge adds at the
  slot price, planned solar charge adds at zero (house cost) or at the
  export price (saved), and each fall of the plan's `soc` releases cost by
  the same rule as the replay. Planned grid use by the house costs the slot
  price.
- **Tomorrow before prices are published** (until about 13:00): the
  planner's estimated-tail rows do not count. The tomorrow column shows
  dashes for cost and saved, and whatever use and solar forecast exists.
- **Names.** `days[].house_cost_eur` already exists in `daily_stats` and
  means meter import × price. The new figure must use a different name,
  for example `use_cost_eur`.
- **Seed.** The solar-free inventory is seeded like the margin's (stored
  energy × import price at the first row); it has washed out long before
  the reported days in a 32-day replay.
- **Battery level per hour** is the level at the end of the hour for both
  past and plan rows, so the line does not step at "now".
- **Days are local days** and may have 23 or 25 hours.
- **Per-day solar kWh** is published too (actual, or forecast for
  tomorrow); the tomorrow column shows it.
- **Hourly rows.** Average the quarter prices. The hour's battery state is
  the one that moved the most energy; an hour is "hold" when it was held
  for more than half of it and nothing moved more than 0.05 kWh. Past
  states are derived from samples: recorded `hold` (since 1b197b1), otherwise the sign of
  battery power with a 50 W dead band, export when the grid meter shows
  export while discharging, grid versus solar charge by the margin's solar
  split.
- **History.** `samples` keeps one row per minute for `retention_days`
  (90 on the owner's box), with every field needed. Replaying two days of
  raw samples every five minutes is fine; the margin already replays 32.
- **Where it lives.** One new sensor, everything in attributes, with the
  series attribute excluded from the recorder. `daily_stats` and
  `smartgrid_plan` keep their current shape.
- **Frontend.** Add `http` and `frontend` to the manifest dependencies.
  Register the static path and the extra JS module URL once in
  `async_setup` (not per config entry, entries get reloaded), cache-busted
  with a hash of the file, since boxes deployed by copying files keep the
  same manifest version. Add the card to `window.customCards`. Loading the
  small module on every page is acceptable. The card re-renders only when
  its own sensor's state object changes, never on every `hass` update.
- **30-day saving** shows `battery_margin_30d_eur` unchanged. No footnote
  in the card; the README explains the two cost bases in two sentences.

## What the planner must publish

The card reads planner sensors only; it does no accounting itself.

New per-day figures, for yesterday, today (actual so far plus planned rest)
and tomorrow (planned):

- house use kWh, house cost €, saved €, average import price.

New battery figures: energy stored now (kWh), stored cost per kWh, current
state, 30-day saving (exists as `battery_margin_30d_eur`).

An hourly series from yesterday 00:00 local to the end of the published
plan, one row per hour: start, price, battery level at the start of the
hour, what the battery did that hour (grid charge, solar charge, to house,
export, hold, idle), house use kWh, solar kWh, and whether the row is
actual or plan.

Builder's call, with these constraints:

- Where this lives. The existing `sensor.smartgrid_plan.horizon` is large and
  per-slot; the card wants a compact hourly series reaching back to
  yesterday midnight. My call: one new sensor carries everything the card
  needs; `horizon` and `daily_stats` stay unchanged so existing dashboards
  keep working.
- Past hours come from recorded samples (the planner already reads 48 h of
  past actuals). `samples.state` records `hold` since 1b197b1, so past held hours can be marked. Samples from before that
  commit have no hold state; those hours show as idle.
- The house-cost replay can share the pass that already produces the margin
  (`Controller._refresh_battery_margin`, 5-minute cache, 32-day replay). It
  needs a second inventory cost that prices solar charge at zero.
- With the hold option off, and with export disabled, everything still
  works; the card just never shows those states.

## The card

- Plain JavaScript custom element drawing SVG. No framework, no bundler, no
  external requests. Served by the integration and registered as a frontend
  module on setup, so it appears in the card picker as a custom card.
- Config: `type: custom:<name>` with no required options. Optional: title
  off/on.
- Layout as in mockup 2: battery line and level graph across the full
  width; below it three columns, each with a header (day name, house cost
  as the large number, kWh, saved, average price) over a price line coloured
  by battery action and small bars for use and solar. A "now" marker in
  today's column. Future hours drawn fainter than past ones.
- Held hours are hatched on the level graph. The price line uses one
  "charge" colour (grid and solar charge are not told apart there), a
  "to house" colour, an "export" colour, and neutral for idle and hold.
  Marks are explained by a legend of at most two lines at the bottom.
- Colours come from Home Assistant theme variables with fallbacks, so it
  follows light and dark themes. The wall's theme supplies its own palette
  (bg #000, tile #1a1917, ink #efe8da, amber #f2b134, warm #e8743b, cool
  #4fb3c8).
- Sizes to its container width. At the wall's 456 px card width the
  smallest text is 11 px. Below about 380 px wide the three columns stack or
  the card drops yesterday; builder's call which.
- Missing data never breaks the card: a day without figures shows dashes,
  a missing series leaves that panel empty, an unavailable sensor shows one
  plain line saying the planner has no data yet.
- On the level graph, meaning must not depend on colour alone (hatch for
  hold, labels for the extremes). The thin price line is exempt.

## The wall

In `~/Sites/ha-wall`, the Power view becomes: the first tile row (House,
Solar, Battery, Price now) and this card. The second tile row and the
generated SVG chart go away; their content is in the card. The uncommitted
wall changes from 2026-11-13 that add "holding" to the Battery tile stay;
the zero-line dashes in the old chart are dropped with the chart. Content
height limit on the wall stays 399 px.

## Left to the builder

- Sensor and attribute names, and the card's element name.
- How the frontend module is registered and cache-busted across releases.
- Exact geometry, type sizes above the 11 px floor, and the narrow-width
  behaviour.
- How to test the card: at least a static harness page with fixture data
  rendered in headless Chrome at 464×459 and at a desktop width, in light
  and dark.
- README section with a screenshot and the config options.

## Open points to verify while building

- Whether `batt_w` is DC-side while charging. If it is, the charger's loss
  would count as house use on charge nights (about €0.10 a night). Check on
  the owner's data before trusting the split while charging.
- Whether the Shelly wall display's browser runs the card unchanged; it
  already runs the current Home Assistant frontend.
- The low-power hold rule itself is being corrected separately (count the
  ~100 W discharge overhead, no drain while held). That work does not block
  this card.

## Order of work

Three parts, in this order:

1. Accounting and the new sensor (pure Python, tested by replay on the
   owner's recorded data).
2. The card and its frontend registration. It can start on fixture data as
   soon as the attribute schema of part 1 is fixed.
3. The wall swap in `~/Sites/ha-wall`.

Counting the margin's battery output on the AC side stays a separate
commit.
