"""SQLite append-only feature log for training/analysis."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone, UTC

from .const import TICK_SECONDS
from .dataquality import house_load_w as _house_load_w
from .rollup import aggregate_hour

_COLUMNS = [
    # p1_w: grid import/export power, sourced from the Anker meter_total_power
    # sensor (was: sum of the p1_l1/l2/l3 phase sensors). p1_l1/l2/l3 are now
    # legacy per-phase columns, written NULL (kept for schema/history compat).
    "ts",
    "hour",
    "weekday",
    "soc",
    "pv_w",
    "batt_w",
    "p1_w",
    "p1_l1",
    "p1_l2",
    "p1_l3",
    "import_price",
    "export_price",
    "temp",
    "irradiance",
    "state",
    "setpoint_w",
    # Weather-forecast columns added in v3 migration (nullable, aligned to clock-hour).
    "temp_forecast",
    "cloud_cover",
    "humidity",
    "wind_speed",
    # load_w: computed house load each tick = pv + meter (p1_w) + batt − inverter
    # loss (v6 migration, nullable). NULL = pre-v6 row; rollup/training use ONLY
    # non-null rows.
    "load_w",
    # v7 export-arbitrage signal columns (nullable; NULL for pre-v7 rows).
    # export_setpoint_w: the net-export setpoint issued to the inverter (W).
    # export_kwh: estimated energy exported to grid in this tick interval (kWh).
    # reserve_kwh: P50 house-load reserve protecting overnight/recovery (kWh).
    # surplus_kwh: battery kWh available above reserve for export (kWh).
    "export_setpoint_w",
    "export_kwh",
    "reserve_kwh",
    "surplus_kwh",
    # persons_home: count of configured person.* entities in state 'home'
    # each tick (v8 migration, nullable). NULL = pre-v8 row; ML feature is NaN-native.
    "persons_home",
    # v9 per-tick energy delta columns (nullable; NULL = pre-v9 row,
    # first-append-after-start, sensor None, or delta-compute failure).
    # W × clamped-dt integrations computed in append(); see
    # docs/superpowers/specs/2026-07-06-per-tick-energy-accounting-design.md.
    # Signs: p1_w import +, batt_w discharge + (dataquality.py).
    # house_load_kwh uses RAW load_w ONLY — no house_load_w() derive fallback
    # (derived p1+batt+pv overcounts; NULL on a load_w blip is honest).
    "grid_import_kwh",
    "grid_export_kwh",
    "house_load_kwh",
    "pv_kwh",
    "batt_charge_kwh",
    "batt_discharge_kwh",
]

_SCHEMA_SAMPLES = (
    "CREATE TABLE IF NOT EXISTS samples ("
    "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
    "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
    "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
    "state TEXT, setpoint_w REAL)"
)

_SCHEMA_DECISIONS = (
    "CREATE TABLE IF NOT EXISTS decisions ("
    "ts TEXT, active INTEGER, start_soc REAL, "
    "deadline TEXT, committed_hours TEXT, horizon_mode TEXT, "
    "pv_today_forecast_kwh REAL, pv_tomorrow_forecast_kwh REAL, "
    "predicted_load_json TEXT, price_window_json TEXT, "
    "setpoint_w REAL, state TEXT)"
)

_DECISIONS_COLUMNS = [
    "ts",
    "active",
    "start_soc",
    "deadline",
    "committed_hours",
    "horizon_mode",
    "pv_today_forecast_kwh",
    "pv_tomorrow_forecast_kwh",
    "predicted_load_json",
    "price_window_json",
    "setpoint_w",
    "state",
]

_SCHEMA_SAMPLES_HOURLY = (
    # Stable column order — Phase 2 featureset reads by name but order is
    # fixed here to keep positional access consistent across schema versions.
    # 10 features × 5 stats (mean/max/min/std/count) + 1 PK + 6 kWh sums
    # (v10) = 57 columns.
    # DO NOT reorder; see rollup.py::_ROLLUP_FEATURES for the feature list.
    "CREATE TABLE IF NOT EXISTS samples_hourly ("
    "hour_ts TEXT PRIMARY KEY, "
    # house_load: load_w (v6+, computed at sample time = pv + meter + batt − inverter
    # loss) primary; derive p1+batt+pv fallback — ML target source.
    # During the ~14-day transition window after deploying v6, hourly aggregates blend derived (old)
    # and recorded (new) values; the mix shifts to ground-truth readings as old rows age out.
    "house_load_mean REAL, house_load_max REAL, house_load_min REAL, "
    "house_load_std REAL, house_load_count INTEGER, "
    # pv_w: AC PV output (NULL at night → excluded from stats when NULL)
    "pv_w_mean REAL, pv_w_max REAL, pv_w_min REAL, "
    "pv_w_std REAL, pv_w_count INTEGER, "
    # soc: battery state of charge
    "soc_mean REAL, soc_max REAL, soc_min REAL, "
    "soc_std REAL, soc_count INTEGER, "
    # irradiance: live irradiance sensor (not forecast)
    "irradiance_mean REAL, irradiance_max REAL, irradiance_min REAL, "
    "irradiance_std REAL, irradiance_count INTEGER, "
    # temp: live ambient temperature
    "temp_mean REAL, temp_max REAL, temp_min REAL, "
    "temp_std REAL, temp_count INTEGER, "
    # temp_forecast: forecast temperature aligned to the clock-hour
    "temp_forecast_mean REAL, temp_forecast_max REAL, temp_forecast_min REAL, "
    "temp_forecast_std REAL, temp_forecast_count INTEGER, "
    # cloud_cover: forecast cloud cover
    "cloud_cover_mean REAL, cloud_cover_max REAL, cloud_cover_min REAL, "
    "cloud_cover_std REAL, cloud_cover_count INTEGER, "
    # humidity: forecast humidity
    "humidity_mean REAL, humidity_max REAL, humidity_min REAL, "
    "humidity_std REAL, humidity_count INTEGER, "
    # wind_speed: forecast wind speed
    "wind_speed_mean REAL, wind_speed_max REAL, wind_speed_min REAL, "
    "wind_speed_std REAL, wind_speed_count INTEGER, "
    # persons_home: hourly-mean count of person.* entities in state 'home' (v8)
    "persons_home_mean REAL, persons_home_max REAL, persons_home_min REAL, "
    "persons_home_std REAL, persons_home_count INTEGER, "
    # kWh sums (v10): SUM of per-tick *_kwh deltas over the hour (tier 1,
    # honest rectangle-rule integration); falls back to mean_watts×1h/1000
    # (tier 2) when every tick in the hour is NULL for that column — see
    # rollup.py::aggregate_hour.
    "grid_import_kwh_sum REAL, grid_export_kwh_sum REAL, "
    "house_load_kwh_sum REAL, pv_kwh_sum REAL, "
    "batt_charge_kwh_sum REAL, batt_discharge_kwh_sum REAL"
    ")"
)

# Ordered column list for samples_hourly INSERT — must mirror the schema above.
# Stable: Phase 2 featureset may reference these names positionally.
_HOURLY_COLUMNS: list[str] = [
    "hour_ts",
    "house_load_mean",
    "house_load_max",
    "house_load_min",
    "house_load_std",
    "house_load_count",
    "pv_w_mean",
    "pv_w_max",
    "pv_w_min",
    "pv_w_std",
    "pv_w_count",
    "soc_mean",
    "soc_max",
    "soc_min",
    "soc_std",
    "soc_count",
    "irradiance_mean",
    "irradiance_max",
    "irradiance_min",
    "irradiance_std",
    "irradiance_count",
    "temp_mean",
    "temp_max",
    "temp_min",
    "temp_std",
    "temp_count",
    "temp_forecast_mean",
    "temp_forecast_max",
    "temp_forecast_min",
    "temp_forecast_std",
    "temp_forecast_count",
    "cloud_cover_mean",
    "cloud_cover_max",
    "cloud_cover_min",
    "cloud_cover_std",
    "cloud_cover_count",
    "humidity_mean",
    "humidity_max",
    "humidity_min",
    "humidity_std",
    "humidity_count",
    "wind_speed_mean",
    "wind_speed_max",
    "wind_speed_min",
    "wind_speed_std",
    "wind_speed_count",
    "persons_home_mean",
    "persons_home_max",
    "persons_home_min",
    "persons_home_std",
    "persons_home_count",
    "grid_import_kwh_sum",
    "grid_export_kwh_sum",
    "house_load_kwh_sum",
    "pv_kwh_sum",
    "batt_charge_kwh_sum",
    "batt_discharge_kwh_sum",
]

_SCHEMA_DAILY_REGRET = (
    "CREATE TABLE IF NOT EXISTS daily_regret ("
    "day TEXT PRIMARY KEY, regret_eur REAL, over_buy_kwh REAL, over_buy_eur REAL, "
    "under_buy_kwh REAL, cost_regret_eur REAL, optimal_kwh REAL, optimal_eur REAL, "
    "realized_kwh REAL, realized_eur REAL, infeasible INTEGER, computed_ts TEXT)"
)

_DAILY_REGRET_COLUMNS = [
    "day",
    "regret_eur",
    "over_buy_kwh",
    "over_buy_eur",
    "under_buy_kwh",
    "cost_regret_eur",
    "optimal_kwh",
    "optimal_eur",
    "realized_kwh",
    "realized_eur",
    "infeasible",
    "computed_ts",
    # dp_regret_eur added in v5 migration (ALTER TABLE); NULL for pre-v5 rows.
    "dp_regret_eur",
]

# Increment this when adding new migration steps.
_VERSION_TARGET = 10

# v9 energy deltas: clamp dt so a restart/stall undercounts (honest, visible
# as a gap) instead of crediting one giant tick. 2 × TICK_SECONDS.
_ENERGY_MAX_DT_S = 2.0 * TICK_SECONDS

_ENERGY_NULLS = {
    "grid_import_kwh": None,
    "grid_export_kwh": None,
    "house_load_kwh": None,
    "pv_kwh": None,
    "batt_charge_kwh": None,
    "batt_discharge_kwh": None,
}


def _normalize_utc_iso(ts_raw):
    """Canonicalize a ts to a UTC ISO string ('+00:00') so lexicographic ts
    compares (rollup watermark bounded read, purge cutoff) stay sound.

    Naive input is assumed UTC. Unparseable input is returned unchanged —
    the telemetry INSERT must never fail on a bad ts.
    """
    if ts_raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_raw))
    except (ValueError, TypeError):
        return ts_raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class DataRecorder:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # H2 puts writes on executor threads sharing this one connection, so
        # WAL (better read/write concurrency) + a busy_timeout + a process-side
        # lock are REQUIRED, not optional.  WAL is a silent no-op on :memory:.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        # v9 energy deltas: ts of the previous append(). In-memory only — the
        # first append after process start records NULL deltas (honest gap).
        # Read/updated ONLY under self._lock (append runs on executor threads).
        self._last_sample_ts: datetime | None = None
        self._migrate()

    # ------------------------------------------------------------------
    # Migration ladder
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Apply all pending schema migrations in version order."""
        # Always ensure the original samples table exists (idempotent).
        self._conn.execute(_SCHEMA_SAMPLES)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON samples(ts)")

        version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]

        if version < 1:
            # v0 → v1: add decisions table + index.
            # Each step sets its OWN literal version number — do NOT use
            # _VERSION_TARGET here so future steps (v2, v3, …) can't be skipped.
            self._conn.execute(_SCHEMA_DECISIONS)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)")
            self._conn.execute("PRAGMA user_version = 1")

        if version < 2:
            # v1 → v2: add daily_regret outcomes table (PRIMARY KEY on day → upsert support).
            self._conn.execute(_SCHEMA_DAILY_REGRET)
            self._conn.execute("PRAGMA user_version = 2")

        if version < 3:
            # v2 → v3: add nullable weather-forecast columns to samples.
            # Column names (referenced verbatim by P1-T2):
            #   temp_forecast, cloud_cover, humidity, wind_speed
            # Guard each ALTER against pre-existing columns so a crash between
            # ALTERs (before user_version was bumped) is recoverable — SQLite
            # ALTER ADD COLUMN has no IF NOT EXISTS equivalent.
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
            for name, col_def in (
                ("temp_forecast", "temp_forecast REAL"),
                ("cloud_cover", "cloud_cover REAL"),
                ("humidity", "humidity REAL"),
                ("wind_speed", "wind_speed REAL"),
            ):
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE samples ADD COLUMN {col_def}")
            self._conn.execute("PRAGMA user_version = 3")

        if version < 4:
            # v3 → v4: create samples_hourly roll-up table.
            # CREATE TABLE IF NOT EXISTS keeps this crash-idempotent: if the
            # process dies after the CREATE but before user_version = 4 is
            # written, re-running this step is a safe no-op.
            self._conn.execute(_SCHEMA_SAMPLES_HOURLY)
            self._conn.execute("PRAGMA user_version = 4")

        if version < 5:
            # v4 → v5: add dp_regret_eur column to daily_regret for shadow
            # DP logging (T0.5c).  Guard with table-existence check first (a
            # test fixture that creates a v3+ DB without running v2 migration
            # will not have daily_regret; skipping the ALTER is correct there
            # since the column is only needed when the table exists).  Then
            # guard with PRAGMA table_info so a crash between the ALTER and the
            # version bump is safely retried (crash-idempotent step).
            _tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "daily_regret" in _tables:
                existing = {row[1] for row in self._conn.execute("PRAGMA table_info(daily_regret)")}
                if "dp_regret_eur" not in existing:
                    self._conn.execute("ALTER TABLE daily_regret ADD COLUMN dp_regret_eur REAL")
            self._conn.execute("PRAGMA user_version = 5")

        if version < 6:
            # v5 → v6: add nullable load_w column to samples.
            # Records the computed house load each tick (pv + meter + batt −
            # inverter loss) — ground-truth house load, free of the overcounting
            # derive_house_load_w has when it must reconstruct load from
            # p1+batt+pv alone (no loss term).
            # NULL for pre-v6 rows; rollup/training use ONLY non-null rows to avoid
            # contaminating the ML target with the overcounted derived values.
            # Guard with PRAGMA table_info so a crash between the ALTER and the
            # user_version bump is safely retried (crash-idempotent step).
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
            if "load_w" not in existing:
                self._conn.execute("ALTER TABLE samples ADD COLUMN load_w REAL")
            self._conn.execute("PRAGMA user_version = 6")

        if version < 7:
            # v6 → v7: add export-arbitrage signal columns to samples.
            # These columns capture the per-tick export decision state so
            # post-hoc analysis can reconstruct what drove each export event.
            # All nullable; NULL for pre-v7 rows (no export occurred / pre-feature).
            # Guard each ALTER with PRAGMA table_info so a crash between ALTERs
            # (before user_version=7 is written) is safely retried — SQLite
            # ALTER ADD COLUMN has no IF NOT EXISTS equivalent.
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
            for name, col_def in (
                ("export_setpoint_w", "export_setpoint_w REAL"),
                ("export_kwh", "export_kwh REAL"),
                ("reserve_kwh", "reserve_kwh REAL"),
                ("surplus_kwh", "surplus_kwh REAL"),
            ):
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE samples ADD COLUMN {col_def}")
            self._conn.execute("PRAGMA user_version = 7")

        if version < 8:
            # v7 → v8: add nullable persons_home to samples AND its 5 rollup-stat
            # columns to samples_hourly (home-presence signal for the load model).
            # NULL for pre-v8 rows. Guard every ALTER with PRAGMA table_info so a
            # crash between ALTERs (before the user_version bump) is safely retried —
            # SQLite ALTER ADD COLUMN has no IF NOT EXISTS equivalent. samples_hourly
            # is guaranteed to exist here (created at the v3→v4 step for any DB that
            # reaches this ladder position).
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
            if "persons_home" not in existing:
                self._conn.execute("ALTER TABLE samples ADD COLUMN persons_home REAL")

            existing_hourly = {row[1] for row in self._conn.execute("PRAGMA table_info(samples_hourly)")}
            for name, col_def in (
                ("persons_home_mean", "persons_home_mean REAL"),
                ("persons_home_max", "persons_home_max REAL"),
                ("persons_home_min", "persons_home_min REAL"),
                ("persons_home_std", "persons_home_std REAL"),
                ("persons_home_count", "persons_home_count INTEGER"),
            ):
                if name not in existing_hourly:
                    self._conn.execute(f"ALTER TABLE samples_hourly ADD COLUMN {col_def}")
            self._conn.execute("PRAGMA user_version = 8")

        if version < 9:
            # v8 → v9: add per-tick energy delta columns to samples.
            # Computed in append() from the row's power readings (W × clamped
            # dt, rectangle rule); all nullable — NULL for pre-v9 rows. Guard
            # each ALTER with PRAGMA table_info so a crash between ALTERs
            # (before user_version=9 is written) is safely retried — SQLite
            # ALTER ADD COLUMN has no IF NOT EXISTS equivalent.
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(samples)")}
            for name, col_def in (
                ("grid_import_kwh", "grid_import_kwh REAL"),
                ("grid_export_kwh", "grid_export_kwh REAL"),
                ("house_load_kwh", "house_load_kwh REAL"),
                ("pv_kwh", "pv_kwh REAL"),
                ("batt_charge_kwh", "batt_charge_kwh REAL"),
                ("batt_discharge_kwh", "batt_discharge_kwh REAL"),
            ):
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE samples ADD COLUMN {col_def}")
            self._conn.execute("PRAGMA user_version = 9")

        if version < 10:
            # v9 → v10: add per-hour kWh sum columns to samples_hourly.
            # aggregate_hour() SUMs the v9 per-tick *_kwh deltas for the hour
            # (tier 1, honest rectangle-rule integration) and falls back to
            # mean_watts×1h/1000 (tier 2) when the hour predates v9. All
            # nullable. Guard each ALTER with PRAGMA table_info so a crash
            # between ALTERs (before user_version=10 is written) is safely
            # retried — SQLite ALTER ADD COLUMN has no IF NOT EXISTS
            # equivalent. samples_hourly is guaranteed to exist here (created
            # at the v3→v4 step for any DB that reaches this ladder position).
            existing_hourly = {row[1] for row in self._conn.execute("PRAGMA table_info(samples_hourly)")}
            for name, col_def in (
                ("grid_import_kwh_sum", "grid_import_kwh_sum REAL"),
                ("grid_export_kwh_sum", "grid_export_kwh_sum REAL"),
                ("house_load_kwh_sum", "house_load_kwh_sum REAL"),
                ("pv_kwh_sum", "pv_kwh_sum REAL"),
                ("batt_charge_kwh_sum", "batt_charge_kwh_sum REAL"),
                ("batt_discharge_kwh_sum", "batt_discharge_kwh_sum REAL"),
            ):
                if name not in existing_hourly:
                    self._conn.execute(f"ALTER TABLE samples_hourly ADD COLUMN {col_def}")
            # Backfill existing hourly rows with the tier-2 approximation for
            # house_load/pv (direct from the already-stored _mean columns).
            # Grid import/export and battery charge/discharge need per-tick
            # sign splits that aren't available in samples_hourly — those
            # stay NULL for pre-v10 rows (re-derivable only from raw samples,
            # out of scope for this migration). WHERE guard makes the UPDATE
            # crash-idempotent (a re-run only touches still-NULL rows). Guard
            # on the source columns' existence too — a samples_hourly table
            # that predates the v3→v4 full-schema CREATE (e.g. a hand-built
            # test fixture) may not have house_load_mean/pv_w_mean; skip the
            # backfill rather than crash, leaving the new columns NULL.
            if "house_load_mean" in existing_hourly and "pv_w_mean" in existing_hourly:
                self._conn.execute(
                    "UPDATE samples_hourly SET "
                    "house_load_kwh_sum = house_load_mean / 1000.0, "
                    "pv_kwh_sum = pv_w_mean / 1000.0 "
                    "WHERE house_load_kwh_sum IS NULL"
                )
            self._conn.execute("PRAGMA user_version = 10")

        self._conn.commit()

    # ------------------------------------------------------------------
    # samples table
    # ------------------------------------------------------------------

    def append(self, row: dict) -> None:
        placeholders = ",".join("?" for _ in _COLUMNS)
        cols = ",".join(_COLUMNS)
        with self._lock:
            row = {**row, "ts": _normalize_utc_iso(row.get("ts"))}
            row = {**row, **self._energy_deltas(row)}
            values = [row.get(col) for col in _COLUMNS]
            self._conn.execute(f"INSERT INTO samples ({cols}) VALUES ({placeholders})", values)
            self._conn.commit()

    def _energy_deltas(self, row: dict) -> dict:
        """Per-tick kWh deltas from the row's power readings (v9).

        Rectangle rule (current reading × clamped dt). Signs: p1_w import +,
        batt_w discharge +. dt correctness relies on ts being aware-UTC
        (producer: dt_util.utcnow().isoformat() in controller.tick).
        Caller MUST hold self._lock (reads/updates _last_sample_ts).
        Failure-isolated: any unforeseen error returns all-NULL — energy
        accounting must never abort the telemetry INSERT.
        """
        try:
            ts = datetime.fromisoformat(row["ts"])
            last = self._last_sample_ts
            if last is None or ts > last:
                self._last_sample_ts = ts
            if last is None:
                return dict(_ENERGY_NULLS)
            dt_h = min(max((ts - last).total_seconds(), 0.0), _ENERGY_MAX_DT_S) / 3600.0

            def _kwh(w: float | None) -> float | None:
                return None if w is None else w / 1000.0 * dt_h

            p1 = row.get("p1_w")
            batt = row.get("batt_w")
            return {
                "grid_import_kwh": _kwh(None if p1 is None else max(0.0, p1)),
                "grid_export_kwh": _kwh(None if p1 is None else max(0.0, -p1)),
                # RAW load_w only — no house_load_w() derive fallback
                # (derived p1+batt+pv overcounts; NULL on blip is honest).
                "house_load_kwh": _kwh(row.get("load_w")),
                "pv_kwh": _kwh(row.get("pv_w")),
                "batt_charge_kwh": _kwh(None if batt is None else max(0.0, -batt)),
                "batt_discharge_kwh": _kwh(None if batt is None else max(0.0, batt)),
            }
        except Exception:
            return dict(_ENERGY_NULLS)

    def purge_older_than(self, now_iso: str, retention_days: int) -> int:
        cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ? OR ts IS NULL", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # decisions table
    # ------------------------------------------------------------------

    def append_decision(
        self,
        *,
        ts: str,
        active: bool,
        start_soc: float,
        deadline: str | None,
        committed_hours: list[str],
        horizon_mode: str,
        pv_today_forecast_kwh: float | None,
        pv_tomorrow_forecast_kwh: float | None,
        predicted_load_json: str | None,
        price_window_json: str | None,
        setpoint_w: float,
        state: str,
    ) -> None:
        """Append one decision row.  committed_hours is serialised to JSON."""
        values = [
            _normalize_utc_iso(ts),
            int(active),
            start_soc,
            deadline,
            json.dumps(committed_hours),
            horizon_mode,
            pv_today_forecast_kwh,
            pv_tomorrow_forecast_kwh,
            predicted_load_json,
            price_window_json,
            setpoint_w,
            state,
        ]
        cols = ",".join(_DECISIONS_COLUMNS)
        placeholders = ",".join("?" for _ in _DECISIONS_COLUMNS)
        with self._lock:
            self._conn.execute(f"INSERT INTO decisions ({cols}) VALUES ({placeholders})", values)
            self._conn.commit()

    def purge_decisions_older_than(self, cutoff_iso: str) -> int:
        """Delete decision rows with ts < cutoff_iso.  Returns deleted count."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM decisions WHERE ts < ?", (cutoff_iso,))
            self._conn.commit()
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def read_load_samples(self, since_iso: str | None = None) -> list[tuple[str, float]]:
        """Return (ts, house_load_w) rows ordered by ts ascending.

        House-load value uses the same precedence as
        :func:`~dataquality.house_load_w`: ``load_w`` column when non-null
        (computed at sample time as pv + meter + batt − inverter loss, v6+),
        else derived from ``p1_w + batt_w + pv_w`` (pre-v6 rows).  A row is
        skipped only when BOTH ``load_w`` and ``p1_w`` are NULL (no load
        value at all).

        During the ~14-day transition window after deploying v6, most rows will
        still be pre-v6 (load_w=NULL) and will be served via the derive fallback.
        As new recorded rows accumulate the mix shifts toward the ground-truth
        sensor values automatically.

        Optionally filtered to ts >= since_iso.
        """
        with self._lock:
            if since_iso is not None:
                cur = self._conn.execute(
                    "SELECT ts, p1_w, batt_w, pv_w, load_w FROM samples "
                    "WHERE (load_w IS NOT NULL OR p1_w IS NOT NULL) AND ts >= ? "
                    "ORDER BY ts ASC",
                    (since_iso,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT ts, p1_w, batt_w, pv_w, load_w FROM samples "
                    "WHERE load_w IS NOT NULL OR p1_w IS NOT NULL "
                    "ORDER BY ts ASC"
                )
            raw = cur.fetchall()
        result: list[tuple[str, float]] = []
        for ts, p1_w, batt_w, pv_w, load_w in raw:
            row = {"p1_w": p1_w, "batt_w": batt_w, "pv_w": pv_w, "load_w": load_w}
            val = _house_load_w(row)
            if val is not None:
                result.append((ts, val))
        return result

    def read_persons_home_samples(self, since_iso: str | None = None) -> list[tuple[str, float]]:
        """Return (ts, persons_home) for rows with a non-null persons_home.

        Mirrors read_load_samples: optional ts>=since window, NULL-filtered in
        SQL, ascending. Feeds the serve-time hour-of-week presence climatology.
        """
        with self._lock:
            if since_iso is not None:
                cur = self._conn.execute(
                    "SELECT ts, persons_home FROM samples WHERE persons_home IS NOT NULL AND ts >= ? ORDER BY ts ASC",
                    (since_iso,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT ts, persons_home FROM samples WHERE persons_home IS NOT NULL ORDER BY ts ASC"
                )
            raw = cur.fetchall()
        return [(str(ts), float(v)) for ts, v in raw]

    def read_efficiency_samples(self, since_iso: str | None = None) -> list[dict]:
        """Return per-tick residual-power samples for the efficiency-fitting
        pipeline (live again since 2026-07; see efficiency.py's module
        docstring).

        NOTE — AC side is not independent, and that is acceptable: ``load_w``
        is a COMPUTED house load (``pv + p1_w(meter) + batt_w − inverter_loss``,
        since the meter/house-load refactor), so ``residual_w = load_w - p1_w
        - pv_w`` algebraically collapses to ``batt_w - inverter_loss``. The
        pipeline's independent ground truth is the DC side (ΔSoC × capacity,
        the BMS coulomb counter) — the fitted per-bin η calibrates the
        computed-AC ↔ measured-ΔSoC mapping, which is exactly what the
        planner consumes as "load". This function still requires ``load_w``,
        ``p1_w``, ``soc``, and ``batt_w`` to all be non-NULL (rows missing
        any of these — pre-v6, or a dropped reading — are skipped).

        ``residual_w`` is computed as ``load_w - p1_w - pv_w`` (``pv_w`` NULL
        treated as 0, matching the derive/rollup convention elsewhere in this
        module).

        Optionally filtered to ts >= since_iso.  Ordered by ts ascending.
        """
        where = "load_w IS NOT NULL AND p1_w IS NOT NULL AND soc IS NOT NULL AND batt_w IS NOT NULL"
        with self._lock:
            if since_iso is not None:
                cur = self._conn.execute(
                    "SELECT ts, soc, batt_w, p1_w, pv_w, load_w FROM samples "
                    f"WHERE {where} AND ts >= ? ORDER BY ts ASC",
                    (since_iso,),
                )
            else:
                cur = self._conn.execute(
                    f"SELECT ts, soc, batt_w, p1_w, pv_w, load_w FROM samples WHERE {where} ORDER BY ts ASC"
                )
            raw = cur.fetchall()
        out: list[dict] = []
        for ts, soc, batt_w, p1_w, pv_w, load_w in raw:
            out.append(
                {
                    "ts": ts,
                    "soc": float(soc),
                    "batt_w": float(batt_w),
                    "residual_w": float(load_w) - float(p1_w) - float(pv_w or 0.0),
                }
            )
        return out

    def read_feature_rows(self, since_iso: str | None = None) -> list[dict]:
        """Return rows as column-keyed dicts ordered by ts ascending."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row  # per-cursor: never mutates the connection
            if since_iso is None:
                cur.execute("SELECT * FROM samples ORDER BY ts ASC")
            else:
                cur.execute("SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (since_iso,))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # samples_hourly table
    # ------------------------------------------------------------------

    def rollup_hours(self, now_iso: str) -> int:
        """Aggregate raw samples into samples_hourly for all completed clock-hours.

        A *completed* hour is any UTC clock-hour whose start is strictly before
        the current hour boundary of ``now_iso``.  The in-progress current hour
        is never rolled up.

        Idempotent: uses INSERT OR REPLACE (upsert) on ``hour_ts`` — re-running
        is safe and produces no duplicate rows.

        Args:
            now_iso: ISO timestamp representing "now" (UTC).

        Returns:
            Number of clock-hours newly aggregated (0 on a no-op re-run).
        """
        now = datetime.fromisoformat(now_iso)
        # Start of the in-progress (current) clock-hour.
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        current_hour_iso = current_hour.isoformat()

        with self._lock:
            # D1 (write-side companion to the watermark clamp): a completed hour can
            # never be >= now. Any such row is from a pre-NTP-boot rollup on a future
            # wall clock; drop it so MAX(hour_ts) falls back to a real past hour and
            # the bounded watermark read resumes (no perpetual full-scan). append()
            # has no clock reference, so a future RAW ts cannot be rejected there —
            # this is the correct home.
            self._conn.execute("DELETE FROM samples_hourly WHERE hour_ts >= ?", (current_hour_iso,))

            # Hours already present in samples_hourly — used to skip work on re-run.
            existing_hours = {row[0] for row in self._conn.execute("SELECT hour_ts FROM samples_hourly")}

            # Bounded read: only scan raw rows newer than the latest already-rolled hour
            # (samples_hourly watermark), instead of re-reading all completed-hour history
            # each call. The existing existing_hours skip still guards the boundary, and the
            # INSERT OR REPLACE upsert keeps re-runs idempotent.
            wm = self._conn.execute("SELECT MAX(hour_ts) FROM samples_hourly").fetchone()
            watermark_hour = wm[0] if wm else None

            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row  # per-cursor: never mutates the connection
            if watermark_hour:
                watermark_hour_end = (datetime.fromisoformat(str(watermark_hour)) + timedelta(hours=1)).isoformat()
            else:
                watermark_hour_end = None
            # Use the bounded read ONLY when the watermark is strictly before the
            # current hour; otherwise full-scan completed hours to recover (belt-and-
            # suspenders with the future-row DELETE above). existing_hours + upsert
            # keep it idempotent.
            if watermark_hour_end is not None and watermark_hour_end < current_hour_iso:
                cur.execute(
                    "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                    (watermark_hour_end, current_hour_iso),
                )
            else:
                cur.execute(
                    "SELECT * FROM samples WHERE ts < ? ORDER BY ts ASC",
                    (current_hour_iso,),
                )
            raw_rows = [dict(r) for r in cur.fetchall()]

            # Group rows by their UTC clock-hour bucket.
            hour_groups: dict[str, list[dict]] = defaultdict(list)
            for row in raw_rows:
                ts_raw = row.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_raw))
                except (ValueError, TypeError):
                    continue
                hour_key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
                hour_groups[hour_key].append(row)

            # Aggregate and upsert each hour not yet in samples_hourly.
            cols = ",".join(_HOURLY_COLUMNS)
            placeholders = ",".join("?" for _ in _HOURLY_COLUMNS)
            rolled = 0
            for hour_key in sorted(hour_groups):
                if hour_key in existing_hours:
                    # Already-rolled hours are intentionally immutable: a late or
                    # back-filled raw row for a closed hour will not trigger
                    # re-aggregation.  INSERT OR REPLACE could handle it, but the
                    # skip keeps the incremental path cheap and avoids surprise
                    # overwrites.  In practice rollup runs once/clock-hour after
                    # the hour closes, so late arrivals are near-impossible.
                    continue
                agg = aggregate_hour(hour_groups[hour_key])
                values = [agg.get(col) for col in _HOURLY_COLUMNS]
                self._conn.execute(
                    f"INSERT OR REPLACE INTO samples_hourly ({cols}) VALUES ({placeholders})",
                    values,
                )
                rolled += 1

            if rolled:
                self._conn.commit()
            return rolled

    def purge_hourly_older_than(self, cutoff_iso: str) -> int:
        """Delete samples_hourly rows with hour_ts < cutoff_iso (strict, exclusive).

        Boundary semantics match purge_older_than / purge_decisions_older_than:
          - Rows with ``hour_ts < cutoff_iso`` are deleted.
          - The cutoff row itself is **kept** (exclusive boundary).

        Args:
            cutoff_iso: ISO timestamp; rows strictly older than this are deleted.

        Returns:
            Number of rows deleted.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples_hourly WHERE hour_ts < ?", (cutoff_iso,))
            self._conn.commit()
            return cur.rowcount

    def read_hourly_rows(self, since_iso: str | None = None) -> list[dict]:
        """Return samples_hourly rows as column-keyed dicts ordered by hour_ts ASC.

        Optionally filtered to ``hour_ts >= since_iso``.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row  # per-cursor: never mutates the connection
            if since_iso is None:
                cur.execute("SELECT * FROM samples_hourly ORDER BY hour_ts ASC")
            else:
                cur.execute(
                    "SELECT * FROM samples_hourly WHERE hour_ts >= ? ORDER BY hour_ts ASC",
                    (since_iso,),
                )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # daily_regret table
    # ------------------------------------------------------------------

    def upsert_daily_regret(
        self,
        *,
        day: str,
        regret_eur: float | None,
        over_buy_kwh: float | None,
        over_buy_eur: float | None,
        under_buy_kwh: float | None,
        cost_regret_eur: float | None,
        optimal_kwh: float | None,
        optimal_eur: float | None,
        realized_kwh: float | None,
        realized_eur: float | None,
        infeasible: int,
        computed_ts: str,
        dp_regret_eur: float | None = None,
    ) -> None:
        """Insert or replace one row in daily_regret (keyed by day YYYY-MM-DD).

        Metric columns (regret_eur, over/under_buy_*, etc.) must be None when
        infeasible=1 — a genuinely infeasible day is not a decision error and must
        not be scored as max-bad.

        ``dp_regret_eur`` (added in v5): realized cost of the DP schedule minus
        the hindsight-optimal cost for the same day.  None when the DP shadow
        computation failed or the day was infeasible.
        """
        cols = ",".join(_DAILY_REGRET_COLUMNS)
        placeholders = ",".join("?" for _ in _DAILY_REGRET_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO daily_regret ({cols}) VALUES ({placeholders})",
                (
                    day,
                    regret_eur,
                    over_buy_kwh,
                    over_buy_eur,
                    under_buy_kwh,
                    cost_regret_eur,
                    optimal_kwh,
                    optimal_eur,
                    realized_kwh,
                    realized_eur,
                    infeasible,
                    computed_ts,
                    dp_regret_eur,
                ),
            )
            self._conn.commit()

    def read_latest_daily_regret(self) -> dict | None:
        """Return the most recent daily_regret row (by day DESC), or None."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row  # per-cursor: never mutates the connection
            row = cur.execute("SELECT * FROM daily_regret ORDER BY day DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def read_daily_regret_range(self, since_day: str, until_day: str | None = None) -> list[dict]:
        """Return daily_regret rows in [since_day, until_day) ordered by day ASC."""
        with self._lock:
            cur = self._conn.cursor()
            cur.row_factory = sqlite3.Row  # per-cursor: never mutates the connection
            if until_day is None:
                cur.execute(
                    "SELECT * FROM daily_regret WHERE day >= ? ORDER BY day ASC",
                    (since_day,),
                )
            else:
                cur.execute(
                    "SELECT * FROM daily_regret WHERE day >= ? AND day < ? ORDER BY day ASC",
                    (since_day, until_day),
                )
            return [dict(r) for r in cur.fetchall()]

    def wal_checkpoint(self) -> None:
        """Truncate the WAL into the main DB file so a read-only immutable reader
        (the addon, mounted config:ro) can see recent rows. Failure-isolated: a
        busy checkpoint (SQLITE_BUSY) must never abort the tick — isolation is the
        contract here (matches _energy_deltas)."""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def close(self) -> None:
        self._conn.close()
