# Anker X1 Forecast add-on — on-box activation runbook (de-dormant gate)

Gate for promoting the add-on from dormant (`boot: manual`, not started) to active.
Prereqs: integration deployed with periodic `wal_checkpoint(TRUNCATE)` (Task 9 of
2026-07-08) live and writing every 60s; add-on built with B1–B4 landed.

## Procedure
1. **Verify the ACTUAL installed slug/DNS on-box FIRST** — `ha addons list`. Do NOT
   trust the repo `config.yaml` slug: the 2026-07-03 deploy registered the add-on from
   `/addons/x1_forecast` as `local_x1_forecast`, not the repo's `anker_x1_forecast`.
   The internal hostname is the slug with underscores→dashes (e.g. `local-x1-forecast`),
   port 8099. Use the verified slug/DNS in every URL below.
2. Deploy the add-on to HAOS, leave it DORMANT (installed, not started).
3. Confirm the container runs NON-ROOT (uid 1000) and can still (a) READ
   `/config/anker_x1_smartgrid.db` on the `config:ro` mount and (b) persist model
   artifacts to its writable `/data` — a permission failure here silently no-ops training.
4. Start the add-on. Watch the Supervisor log for `train_once complete` (may be
   `ready=False` if <_MIN_TRAIN_ROWS rows yet — that is fine).
5. `GET http://<verified-slug-dns>:8099/health` and confirm `db_readable: true` and
   `n_rows > 0` under the live 60s writer.
6. Re-check `/health` across **≥2 wal_checkpoint cycles** (checkpoint is hourly, so
   span ≥2 clock-hours): `db_readable` stays `true`, `n_rows` grows.
7. Confirm `train_once` completes (`/health` `last_trained` advances at retrain_hour).
8. Grep the add-on log for `SQLITE_BUSY` / `OperationalError` / permission errors — expect NONE.

## Pass criteria (ALL)
- Installed slug/DNS confirmed via `ha addons list` and used in all URLs.
- Non-root (uid 1000) reads `/config` (config:ro) and persists to `/data`.
- `db_readable: true` on every check across ≥2 checkpoint cycles.
- `n_rows` strictly increases between cycles.
- `train_once` completes at least once (ready or honest not-ready, never crashed).
- Zero `SQLITE_BUSY` / `OperationalError` / permission errors in the add-on log.

## Rollback
Stop the add-on (returns to dormant). The integration is unaffected (it only WRITES;
the add-on only READS immutable). No data migration to undo.

## Only after PASS
Flip `boot: manual` → `boot: auto` (or start-on-boot) to de-dormant. This is a
separate, user-approved change — NOT part of chore/deferred-cleanup-2026-07-09.
