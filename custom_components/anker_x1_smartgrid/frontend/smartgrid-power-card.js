const TAG = 'smartgrid-power-card';
const H = 3600e3;
const DASH = '–';
const PAD = 6;
const STATE_TEXT = {
  charge_grid: 'charging', charge_solar: 'charging', house: 'to house', export: 'exporting', hold: 'holding', idle: 'idle', disabled: 'disabled',
};
const LINE = { charge_grid: 'charge', charge_solar: 'charge', house: 'house', export: 'export' };

function num(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
function fx(v, d) {
  return num(v) === null ? DASH : v.toFixed(d);
}
function signed(v, cur = '', d = 2) {
  if (num(v) === null) return DASH;
  const s = Math.abs(v).toFixed(d);
  return (v < 0 && +s !== 0 ? '−' : '+') + cur + s;
}
function eur(v, d = 2) {
  return num(v) === null ? DASH : (v < 0 ? '−€' : '€') + Math.abs(v).toFixed(d);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
}
function r1(v) {
  return Math.round(v * 10) / 10;
}

const fmts = {};
function parts(t, tz) {
  let f = fmts[tz];
  if (!f) {
    const o = { hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric' };
    try {
      f = new Intl.DateTimeFormat('en-US', { ...o, timeZone: tz });
    } catch (e) {
      f = new Intl.DateTimeFormat('en-US', o);
    }
    fmts[tz] = f;
  }
  const p = {};
  for (const x of f.formatToParts(t)) p[x.type] = +x.value;
  return p;
}
function offset(t, tz) {
  const p = parts(t, tz);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - (t - (t % 1000));
}
// Epoch of a local wall-clock hour on a 'YYYY-MM-DD' date in the HA time zone (DST-safe).
function localTime(date, hour, tz) {
  const [y, m, d] = date.split('-').map(Number);
  const u = Date.UTC(y, m - 1, d, hour);
  let t = u;
  for (let i = 0; i < 2; i++) t = u - offset(t, tz);
  return t;
}
function pad2(n) {
  return String(n).padStart(2, '0');
}
function dateOf(t, tz) {
  const p = parts(t, tz);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
}
function addDays(date, k) {
  const [y, m, d] = date.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + k)).toISOString().slice(0, 10);
}
function weekday(t, tz) {
  return new Date(t).toLocaleDateString('en', { weekday: 'short', timeZone: tz || undefined });
}
function clock(t, tz) {
  const p = parts(t, tz);
  return `${pad2(p.hour)}:${pad2(p.minute)}`;
}
function when(a, b, tz) {
  let start = clock(a, tz);
  let end = clock(b, tz);
  if (start === end) {
    // The repeated hour of a DST fall-back reads the same at both ends without the zone.
    const zone = (t) => new Intl.DateTimeFormat('en', { timeZone: tz || undefined, timeZoneName: 'short' }).formatToParts(t).find((p) => p.type === 'timeZoneName')?.value;
    start += ` ${zone(a)}`;
    end += ` ${zone(b)}`;
  }
  return `${weekday(a, tz)} ${start}${DASH}${end === '00:00' ? '24:00' : end}`;
}


const STYLE = `<style>
:host {
  display: block;
  --sg-ink: var(--primary-text-color, #efe8da);
  --sg-dim: var(--secondary-text-color, #a59f92);
  --sg-rule: var(--divider-color, #3a3833);
  --sg-bg: var(--ha-card-background, var(--card-background-color, #1a1917));
  --sg-charge: var(--smartgrid-power-charge-color, #4fb3c8);
  --sg-house: var(--smartgrid-power-house-color, #b3aa98);
  --sg-export: var(--smartgrid-power-export-color, #e8743b);
  --sg-sun: var(--smartgrid-power-solar-color, #f2b134);
  --sg-pos: var(--success-color, #6dbf8b);
  --sg-neg: var(--warning-color, #e8743b);
  --sg-debug: var(--smartgrid-power-debug-color, #b48cf2);
}
ha-card { position: relative; padding: 6px 10px; color: var(--sg-ink); font-size: 11px; }
.title { font-size: 16px; font-weight: 500; padding: 4px 0 6px; }
.bat { display: flex; align-items: center; column-gap: 8px; font-size: 11.5px; line-height: 1.45; margin-bottom: 3px; white-space: nowrap; }
.bat b { color: var(--sg-charge); }
.bat .b1 { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.bat .m30 { flex: 0 0 auto; }
.bat button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; border-radius: 6px; min-width: 44px; height: 28px; margin: -6px -9px -6px 0; display: flex; align-items: center; justify-content: center; }
.bat button:focus-visible { outline: 2px solid var(--sg-charge); outline-offset: -2px; }
.trk { position: relative; width: 26px; height: 14px; border-radius: 7px; box-sizing: border-box; border: 1.5px solid var(--sg-dim); }
.trk i { position: absolute; top: 1.5px; left: 1.5px; width: 8px; height: 8px; border-radius: 50%; background: var(--sg-dim); }
[aria-checked=true] .trk { background: var(--sg-charge); border-color: var(--sg-charge); }
[aria-checked=true] .trk i { left: 13.5px; background: var(--sg-bg); }
.lv { position: relative; }
.pill { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); padding: 1px 9px; border-radius: 10px; background: var(--sg-bg); border: 1px solid var(--sg-rule); font-size: 11px; white-space: nowrap; }
.off .dm { opacity: .3; }
svg { display: block; }
.days { display: grid; margin: 2px 0 0; }
.ph { padding: 0 ${PAD}px; border-left: 1px solid var(--sg-rule); color: var(--sg-dim); line-height: 1.35; min-width: 0; }
.ph:first-child { border-left-color: transparent; }
.ph > div { display: flex; justify-content: space-between; align-items: baseline; column-gap: 4px; white-space: nowrap; }
.cheap, .bl { min-height: 1.35em; overflow: hidden; text-overflow: ellipsis; }
.ph .cost { color: var(--sg-ink); font-size: 18px; font-weight: 700; line-height: 1.2; }
.ph.on .cost { font-size: 22px; line-height: 1.1; }
.ph.on .cost .sv { font-size: 14px; }
.ph.on .name { color: var(--sg-ink); font-weight: 700; }
.ph .l3, .ph .info { display: block; overflow: hidden; text-overflow: ellipsis; }
.ph .info { min-width: 0; }
.pos { color: var(--sg-pos); } .neg { color: var(--sg-neg); }
.lg { display: flex; flex-wrap: wrap; column-gap: 10px; color: var(--sg-dim); margin-top: 2px; line-height: 1.5; }
.lg > span { white-space: nowrap; }
.lg svg { display: inline-block; vertical-align: -1px; margin: 0 2px 0 5px; }
.empty { font-size: 14px; color: var(--sg-dim); padding: 8px 0; }
.ink { fill: var(--sg-ink); } .dim { fill: var(--sg-dim); }
.rule { stroke: var(--sg-rule); }
.now { stroke: var(--sg-ink); stroke-dasharray: 2 3; opacity: .7; }
.lvl { fill: none; stroke: var(--sg-charge); stroke-width: 1.5; stroke-linejoin: round; }
.lvlf { fill: var(--sg-charge); fill-opacity: .15; }
.hs { fill: var(--sg-ink); opacity: .3; }
.lbg { fill: var(--sg-bg); opacity: .85; }
.p { stroke-width: 3; } .p.charge { stroke: var(--sg-charge); } .p.house { stroke: var(--sg-house); }
.p.export { stroke: var(--sg-export); } .p.idle { stroke: var(--sg-dim); stroke-width: 1.5; }
.conn { stroke: var(--sg-dim); stroke-opacity: .45; }
.gh { fill: var(--sg-dim); fill-opacity: .5; } .gb { fill: var(--sg-pos); } .gc { fill: var(--sg-charge); } .go { fill: var(--sg-export); } .cut { fill: var(--sg-bg); }
.pl { paint-order: stroke; stroke: var(--sg-bg); stroke-width: 3px; stroke-linejoin: round; }
.uo, .so { fill: none; stroke-width: 1; stroke-linejoin: round; }
.uo { stroke: var(--sg-ink); stroke-width: 1.8; } .so { stroke: var(--sg-sun); }
.gd { fill: var(--sg-ink); fill-opacity: .14; pointer-events: none; }
.sw.charge { fill: var(--sg-charge); } .sw.house { fill: var(--sg-house); } .sw.export { fill: var(--sg-export); } .sw.idle { fill: var(--sg-dim); }
.tip { position: absolute; z-index: 1; width: 256px; box-sizing: border-box; padding: 7px 9px; border-radius: 8px; border: 1px solid var(--sg-rule); background: var(--primary-background-color, var(--sg-bg)); box-shadow: 0 4px 14px rgba(0, 0, 0, .4); line-height: 1.45; pointer-events: none; }
.tip .hd { font-size: 13px; font-weight: 600; }
.tip .k { color: var(--sg-dim); }
.tip .per { margin-top: 5px; }
.tip .dec { display: grid; grid-template-columns: 9px auto 1fr auto; align-items: center; column-gap: 5px; color: var(--sg-dim); }
.tip .opt { display: contents; }
.tip .opt.win { color: var(--sg-ink); }
.tip .opt i { font-style: normal; color: var(--sg-pos); }
.tip .ol { white-space: nowrap; }
.tip .tr { height: 6px; border-radius: 3px; background: var(--sg-rule); overflow: hidden; }
.tip .tr b { display: block; height: 100%; background: var(--sg-dim); }
.tip .win .tr b { background: var(--sg-pos); }
.tip .opt .v { font-weight: 600; }
.tip .note { color: var(--sg-pos); margin-top: 3px; }
.tip .hu { border-top: 1px solid var(--sg-rule); margin-top: 6px; padding-top: 4px; }
.tip .split { display: flex; height: 7px; border-radius: 3px; overflow: hidden; background: var(--sg-rule); margin: 2px 0; }
.tip .split .g { background: var(--sg-dim); } .tip .split .b { background: var(--sg-pos); } .tip .split .s { background: var(--sg-sun); }
.dt { display: flex; align-items: center; gap: 8px; }
.chip { font-size: 10px; font-weight: 600; letter-spacing: .04em; line-height: 1.5; color: var(--sg-debug); border: 1px solid var(--sg-debug); border-radius: 9px; padding: 0 7px; }
.dbg { margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--sg-rule); }
.dl { fill: none; stroke: var(--sg-debug); stroke-width: 1.2; stroke-dasharray: 3 2; }
.dsum { display: flex; flex-wrap: wrap; column-gap: 14px; margin-top: 5px; color: var(--sg-dim); line-height: 1.35; }
.dsum b { display: block; color: var(--sg-ink); font-size: 14px; }
.dsel { cursor: pointer; }
.chart, .dsel { touch-action: pan-y pinch-zoom; }
.dsel:focus-visible { outline: 2px solid var(--sg-debug); outline-offset: 1px; }
.dsb { fill: var(--sg-debug); fill-opacity: .3; }
.din { margin-top: 6px; padding-top: 5px; border-top: 1px solid var(--sg-rule); line-height: 1.35; color: var(--sg-dim); }
.dcap { display: flex; flex-wrap: wrap; justify-content: space-between; column-gap: 8px; }
.dcap b { color: var(--sg-ink); font-weight: 600; }
.dtw { overflow-x: auto; margin-top: 2px; }
.dtw table { width: 100%; border-collapse: collapse; white-space: nowrap; font-variant-numeric: tabular-nums; }
.dtw th, .dtw td { text-align: right; padding: 2px 3px; border-bottom: 1px solid var(--sg-rule); }
.dtw th { font-weight: 400; }
.dtw td { color: var(--sg-ink); }
.dtw th:first-child, .dtw td:first-child { text-align: left; padding-left: 0; }
.dtw th:last-child, .dtw td:last-child { padding-right: 0; }
.dtw td.sp { color: var(--sg-debug); } .dtw td.neg { color: var(--sg-neg); }
.dwhy { margin-top: 5px; }
.dwhy b { color: var(--sg-ink); font-weight: 600; }
.dkv { display: flex; flex-wrap: wrap; gap: 4px 5px; margin-top: 6px; }
.dkv span { border: 1px solid var(--sg-rule); border-radius: 9px; padding: 0 7px; white-space: nowrap; }
.dkv b { color: var(--sg-ink); font-weight: 600; }
.dkv .old { border-color: var(--sg-neg); }
.dkv .old b, .dkv .old i { color: var(--sg-neg); }
.dkv i { font-style: normal; font-weight: 700; }
.dvh { display: flex; flex-wrap: wrap; align-items: center; column-gap: 4px; margin-bottom: 4px; }
.dvh .dvt { color: var(--sg-debug); font-weight: 700; margin-right: 4px; }
.dvg { display: flex; column-gap: 4px; }
.dvb { font: inherit; color: var(--sg-dim); background: none; border: 0; padding: 0; cursor: pointer; height: 28px; margin: -5px 0; display: flex; align-items: center; }
.dvb span { border: 1px solid var(--sg-rule); border-radius: 9px; padding: 0 8px; line-height: 1.5; }
.dvb[aria-pressed=true] span { border-color: var(--sg-debug); color: var(--sg-ink); }
.dvb:focus-visible { outline: none; }
.dvb:focus-visible span { outline: 2px solid var(--sg-debug); outline-offset: 1px; }
.dvw .so { stroke-width: 1.8; }
.dn { fill: var(--sg-neg); } .dp { fill: var(--sg-pos); }
.dbus { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 6px 12px; align-items: start; }
.dbu { border-left: 2px solid var(--sg-debug); padding-left: 7px; min-width: 0; }
.dbu .dq { color: var(--sg-debug); font-weight: 700; }
.dld { display: grid; grid-template-columns: auto 1fr auto; column-gap: 6px; align-items: center; font-variant-numeric: tabular-nums; white-space: nowrap; }
.dld .t { color: var(--sg-ink); font-weight: 600; }
.dld .v { text-align: right; color: var(--sg-ink); }
.dld .v.pos { color: var(--sg-pos); } .dld .v.neg { color: var(--sg-neg); }
.dtr { position: relative; height: 7px; }
.dtr b { position: absolute; top: 0; height: 100%; border-radius: 2px; background: var(--sg-house); }
.dtr b.tot { background: var(--sg-ink); } .dtr b.dp { background: var(--sg-pos); } .dtr b.dn { background: var(--sg-neg); }
.dans { margin-top: 2px; }
.dans b { font-weight: 600; }
.dans b.pos { color: var(--sg-pos); } .dans b.neg { color: var(--sg-neg); }
</style>`;

const LEVEL_H = 48;
// Lower chart: px per kWh, and the cut that keeps a heavy house hour from flattening the rest.
const KWH_PX = 10;
const CUT = 2.5;
// The price line spans PRICE_PX; charge and export bars on it are at most BAR_PX tall.
const PRICE_PX = 26;
const BAR_PX = 30;
const COLS = { 3: [0.26, 0.44, 0.3], 2: [0.58, 0.42] };
function ly(soc) {
  return 46 - Math.max(0, Math.min(100, soc)) * 0.42;
}
function swatch(cls, h = 3) {
  return `<svg width="10" height="9"><rect y="${(9 - h) / 2}" width="10" height="${h}" class="${cls}"/></svg>`;
}
function stroke(cls) {
  return `<svg width="10" height="9"><line x1="0" y1="4.5" x2="10" y2="4.5" class="${cls}"/></svg>`;
}
function cuts(x, y, w) {
  return `<rect x="${r1(x)}" y="${r1(y)}" width="${r1(w)}" height="1.2" class="cut"/><rect x="${r1(x)}" y="${r1(y + 3.2)}" width="${r1(w)}" height="1.2" class="cut"/>`;
}
function kh(v) {
  return Math.min(Math.max(0, v), CUT) * KWH_PX;
}
function barBox(r) {
  const w = Math.max(1, (r.p1 - r.p0) * 0.72);
  return [r.p0 + (r.p1 - r.p0 - w) / 2, w];
}
// A key's hourly kWh as stepped outlines, one path per run of hours.
function outline(rows, key, cls, h, z) {
  let o = '';
  let d = '';
  let prev = null;
  const flush = () => {
    if (d) o += `<path d="${d} V${z}" class="${cls}"${prev.plan ? ' opacity=".5"' : ''}/>`;
    d = '';
  };
  for (const r of rows) {
    const v = num(r[key]);
    if (v === null || v < 0.005) {
      flush();
      prev = null;
      continue;
    }
    if (!prev || prev.t + H !== r.t || prev.i !== r.i || prev.plan !== r.plan) {
      flush();
      d = `M${r1(r.p0)},${z}`;
    }
    d += ` V${r1(z - h(v))} H${r1(r.p1)}`;
    prev = r;
  }
  flush();
  return o;
}
function out(r) {
  return Math.max(0, num(r.grid_out_kwh) || 0);
}
// Bar heights on the price line, px per kWh.
function barPx(rows) {
  const top = Math.max(6, ...rows.map((r) => Math.max(num(r.grid_in_charge_kwh) || 0, out(r))));
  return BAR_PX / top;
}
// Charge rises from its hour's price level and export hangs from it; house use and sun sit on the lower baseline Z.
function gridBars(rows, lv, k, Z) {
  let s = outline(rows, 'solar_kwh', 'so', kh, Z);
  let pb = '';
  const bars = [];
  for (const r of rows) {
    const house = num(r.grid_in_house_kwh);
    const [bx, w] = barBox(r);
    const fed = Math.max(0, house || 0) + Math.max(0, num(r.batt_house_kwh) || 0);
    const hh = kh(house || 0);
    const hb = kh(fed);
    let b = '';
    if (hh > 0) b += `<rect x="${r1(bx)}" y="${r1(Z - hh)}" width="${r1(w)}" height="${r1(hh)}" class="gh"/>`;
    if (hb > hh) b += `<rect x="${r1(bx)}" y="${r1(Z - hb)}" width="${r1(w)}" height="${r1(hb - hh)}" class="gb"/>`;
    if (fed > CUT) b += cuts(bx, Z - hb + 2.5, w);
    if (b) s += r.plan ? `<g opacity=".5">${b}</g>` : b;
    const y0 = lv(r);
    const up = Math.max(0, num(r.grid_in_charge_kwh) || 0) * k;
    const dn = out(r) * k;
    let p = '';
    if (up > 0) p += `<rect x="${r1(bx)}" y="${r1(y0 - up)}" width="${r1(w)}" height="${r1(up)}" class="gc"/>`;
    if (dn > 0) p += `<rect x="${r1(bx)}" y="${r1(y0)}" width="${r1(w)}" height="${r1(dn)}" class="go"/>`;
    if (p) pb += r.plan ? `<g opacity=".5">${p}</g>` : p;
    if (up + dn > 0) bars.push({ x: bx, y: y0 - up, w, h: up + dn });
  }
  return [pb + s + outline(rows, 'use_kwh', 'uo', kh, Z), bars];
}
function ticks(date, i, { X, tz }, lo, y) {
  let s = `<text x="${r1(lo)}" y="${y}" font-size="11" class="ink">${weekday(localTime(date, 12, tz), tz)}</text>`;
  for (const hr of [6, 12, 18]) {
    const x = X(i, localTime(date, hr, tz), PAD);
    // Room for the weekday name; on narrow columns the 06 tick would run into it.
    if (x - 7 < lo + 26) continue;
    s += `<text x="${r1(x)}" y="${y}" font-size="11" text-anchor="middle" class="dim">${pad2(hr)}</text>`;
  }
  return s;
}
const LEGEND = `<div class="lg dm">
<span>price${swatch('sw charge')}charge${swatch('sw house')}to house${swatch('sw export')}export${swatch('sw idle', 1.5)}idle, hold<svg width="10" height="9"><rect width="10" height="9" fill="url(#sgh)"/></svg>held</span>
<span>bars on the price line: kWh charged from grid (up) and sold (down)</span>
<span>grid kWh${swatch('gh', 9)}house${swatch('gb', 9)}battery to house</span>
<span>${stroke('uo')}use${stroke('so')}sun<svg width="6" height="9"><rect x="1" width="4" height="9" class="gb"/>${cuts(1, 1.5, 4)}</svg>cut at ${CUT} · faint: plan</span>
</div>`;
const DEBUG_FAIL = '<div class="dbg"><div class="empty">Debug data unavailable</div></div>';
const VIEWS = [['cost', 'cost'], ['use', 'house use'], ['sun', 'sun']];
const DEBUG_LEGEND = {
  use: `<span>first plan of the day${stroke('dl')}forecast${stroke('uo')}measured house use</span>`,
  sun: `<span>first plan of the day${stroke('dl')}forecast${stroke('so')}measured sun</span>`,
  cost: `<span>grid cost against the first plan of the day${swatch('dn', 9)}dearer${swatch('dp', 9)}cheaper<svg width="10" height="9"><rect width="10" height="9" fill="url(#sgh)"/></svg>unknown</span>`,
};

const TITLE = { sell: 'Sell it now', charge: 'Charge it now', house: 'Use it for the house now' };
const PART = { wear: 'Wear', conversion: 'Conversion loss', fee: 'Export fee', margin: 'Charge margin' };
function stamp(a, t, tz) {
  return dateOf(a, tz) === dateOf(t, tz) ? clock(a, tz) : `${weekday(a, tz)} ${clock(a, tz)}`;
}
// What the kept kWh is kept for, as a phrase.
function target(w, t, tz) {
  const f = w.kept_for || {};
  const a = Date.parse(f.start);
  if (f.kind === 'next_day') return 'the next day';
  if (!Number.isFinite(a)) return 'later';
  const at = stamp(a, t, tz);
  return { export: `the sale at ${at}`, house: `house use at ${at}`, refill: `the refill at ${at}${w.kept_estimated ? ' (estimated)' : ''}` }[f.kind] || 'later';
}
// One plain line: what the hour does and the two values it weighed.
function verdict(w, t, tz) {
  if (!w || !w.kind || num(w.keep_eur) === null || num(w.use_eur) === null) return '';
  const tgt = target(w, t, tz);
  const keep = `${eur(w.keep_eur, 3)}/kWh`;
  const use = `${eur(w.use_eur, 3)}/kWh`;
  if (w.kind === 'hold') return `Holding: ${tgt} is worth ${keep}, using it now only ${use}.`;
  if (w.kind === 'charge') return `Charging: it costs ${use} and is worth ${keep} for ${tgt}.`;
  return `${w.kind === 'export' ? 'Selling now' : 'Using it now'}: ${use} beats ${keep} for ${tgt}.`;
}
// Per stored kWh: from the hour's price down (up, for a charge) to what using it now is worth, against what it is kept for.
function buildups(w, t, tz) {
  const bs = (w && Array.isArray(w.beats) ? w.beats : []).filter((b) => b && TITLE[b.q] && num(b.price) !== null && num(b.now_eur) !== null && Array.isArray(b.now_parts) && num(w.keep_eur) !== null);
  if (!bs.length) return '';
  const keep = w.keep_eur;
  const ups = bs.map((b) => {
    let x = b.price;
    const steps = b.now_parts.filter((p) => p && num(p.eur) !== null && Math.abs(p.eur) >= 0.0005).map((p) => [p, x, (x += p.eur)]);
    return [b, steps];
  });
  // One scale for both build-ups, so their bars compare.
  const vals = ups.flatMap(([b, st]) => [b.price, b.now_eur, keep, ...st.flatMap(([, x, y]) => [x, y])]);
  const lo = Math.min(0, ...vals);
  const span = Math.max(0, ...vals) - lo || 1;
  const bar = (x, y, cls) => {
    const wd = (Math.abs(y - x) / span) * 100;
    return `<span class="dtr">${wd >= 0.5 ? `<b${cls ? ` class="${cls}"` : ''} style="left:${r1(((Math.min(x, y) - lo) / span) * 100)}%;width:${r1(wd)}%"></b>` : ''}</span>`;
  };
  const one = ([b, steps]) => {
    const charge = b.q === 'charge';
    const row = (label, x, y, v, cls) => `<span${cls ? ` class="${cls}"` : ''}>${label}</span>${bar(x, y, cls && 'tot')}<span class="v${cls ? ` ${cls}` : ''}">${v}</span>`;
    const rows = steps.map(([p, x, y]) => row(charge && p.key === 'conversion' ? 'Charge loss' : PART[p.key] || esc(p.key), x, y, signed(p.eur, '€', 3)));
    // The two rows are compared as shown, so the answer never disagrees with them by a rounding step.
    const now = Math.round(b.now_eur * 1000) / 1000;
    const d = charge ? keep - now : now - keep;
    const winner = charge ? 'Charging' : w.kind === 'export' ? 'Selling now' : 'Using it now';
    const end = b.blocked
      ? `${winner} would win, but ${b.blocked === 'reserve' ? 'the reserve holds this energy back' : 'a planning rule holds it back'}.`
      : `${d >= 0 ? winner : 'Keeping'} wins by <b class="${d >= 0 ? 'pos' : 'neg'}">${eur(Math.abs(d), 3)} per kWh</b>.`;
    return (
      `<div class="dbu"><div class="dq">${TITLE[b.q]}</div><div class="dld">` +
      `<span>${b.q === 'sell' ? 'Export' : 'Import'} price</span>${bar(0, b.price)}<span class="v">${eur(b.price, 3)}</span>${rows.join('')}` +
      row(charge ? 'Costs once stored' : 'Worth now', 0, b.now_eur, eur(b.now_eur, 3), 't') +
      row(`Kept for ${target(w, t, tz)}`, 0, keep, eur(keep, 3), 't') +
      `</div><div class="dans">${end}</div></div>`
    );
  };
  return `<div class="dbus">${ups.map(one).join('')}</div>`;
}

// What stored solar earned over exporting it; left out while it rounds to zero.
function sun(v, cur = '') {
  return num(v) === null || Math.abs(v) < 0.005 ? '' : `sun ${signed(v, cur)}`;
}

function saved(v, prefix = '') {
  if (num(v) === null) return `<span class="sv">${DASH}</span>`;
  return `<span class="sv ${v < 0 ? 'neg' : 'pos'}">${prefix}${signed(v)}</span>`;
}

// Above zero the battery bought more than it sold, below zero it sold more; within a cent it reads dim.
function owed(label, v) {
  if (num(v) === null) return '';
  const zero = Math.abs(v) < 0.005;
  return `${label} <span${zero ? '' : ` class="${v < 0 ? 'pos' : 'neg'}"`}>${eur(zero ? 0 : v)}</span>`;
}

// The planner's reason for an hour: the winning and the losing option per stored kWh, and what the energy is kept for.
function decision(w, tz) {
  if (!w || !w.kind || num(w.keep_eur) === null || num(w.use_eur) === null) return null;
  const kind = w.kind;
  const f = w.kept_for || {};
  const kept = kind === 'hold' || kind === 'charge';
  const later = [kind === 'charge' ? 'Worth later' : !kept && f.kind === 'refill' ? 'Refill later' : 'Keep for later', w.keep_eur];
  const now = [{ export: 'Sell now', charge: 'Costs now' }[kind] || 'Use for the house now', w.use_eur];
  const a = Date.parse(f.start);
  const b = Date.parse(f.end);
  const what = { export: 'export', house: 'house use' }[f.kind];
  const when_ = Number.isFinite(a) && Number.isFinite(b) ? when(a, b, tz) : '';
  const target = f.kind === 'next_day' ? 'the next day' : what && Number.isFinite(a) && Number.isFinite(b) ? `${when_} ${what}` : '';
  let note = '';
  if (f.kind === 'refill' && when_) note = `${kept ? 'Using it now means buying it back' : 'Refilled'} ${when_}.`;
  else if (target) note = `${kept ? 'Kept for' : 'Better than keeping it for'} ${target}.`;
  return { win: kept ? later : now, lose: kept ? now : later, note };
}

function per(v) {
  return `${eur(v)}/kWh`;
}
function pct(v) {
  return `${fx(v, 0)}%`;
}
function ago(s) {
  return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
}
function until(v, tz, today) {
  const t = Date.parse(v);
  if (!Number.isFinite(t)) return esc(v);
  const d = dateOf(t - 1, tz);
  const c = clock(t, tz);
  return `${d === today ? 'today' : d === addDays(today, 1) ? 'tomorrow' : weekday(t - 1, tz)} ${c === '00:00' ? '24:00' : c}`;
}
// Label, value, and when the value is stale; inputs first. A value of null leaves its chip out.
const INTERNALS = new Map([
  ['prices_until', ['prices to', until, (v, tz, today) => !(Date.parse(v) >= localTime(addDays(today, 2), 0, tz))]],
  ['tomorrow_estimated', ['tomorrow', (v) => (v === true ? 'estimated' : null), (v) => v === true]],
  ['pv_forecast_age_s', ['sun forecast', (v) => `${num(v) === null ? DASH : ago(v)} old`, (v) => v > 5400]],
  ['battery_sample_age_s', ['battery reading', (v) => `${num(v) === null ? DASH : ago(v)} ago`, (v) => v > 300]],
  ['built_at', ['plan built', (v, tz) => (Number.isFinite(Date.parse(v)) ? clock(Date.parse(v), tz) : esc(v))]],
  ['forecast_source', ['forecast', (v) => esc(v)]],
  ['load_correction_ratio', ['load correction', (v) => `×${fx(v, 2)}`]],
  ['reserve_soc', ['reserve', pct]],
  ['target_soc', ['target', pct]],
  ['end_value_eur_per_kwh', ['kept now', per]],
  ['eta_charge', ['efficiency in', (v) => fx(v, 2)]],
  ['eta_discharge', ['efficiency out', (v) => fx(v, 2)]],
  ['cycle_cost_eur_per_kwh', ['wear', per]],
  ['charge_margin_eur_per_kwh', ['charge margin', per]],
  ['idle_drain_w', ['standby', (v) => `${fx(v, 0)} W`]],
]);
// Charge-positive watts.
function watts(v) {
  return num(v) === null ? DASH : `${Math.round(v) ? signed(v, '', 0) : '0'} W`;
}
// Slot rows summed per local hour; an hour only partly covered (joined mid-way, or a slot missing) is left out.
// Its narrowest slot sets how many it needs, so hourly and quarter-hour rows can sit side by side.
function hourly(list, tz, init, add) {
  const xs = (Array.isArray(list) ? list : [])
    .map((x) => [Date.parse(x && x.start), x])
    .filter(([t]) => Number.isFinite(t))
    .sort((a, b) => a[0] - b[0]);
  const out = new Map();
  xs.forEach(([t, x], j) => {
    const p = parts(t, tz);
    const k = t - (p.minute * 60 + p.second) * 1e3;
    let a = out.get(k);
    if (!a) out.set(k, (a = { n: 0, step: H, ...init() }));
    // A slot lasts until the next one starts, an hour at most; the last one as long as the one before it.
    const span = j + 1 < xs.length ? xs[j + 1][0] - t : j ? t - xs[j - 1][0] : H;
    a.n++;
    a.step = Math.min(a.step, span);
    add(a, x);
  });
  for (const [k, a] of out) if (a.n * a.step < H) out.delete(k);
  return out;
}

class SmartgridPowerCard extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open' });
    root.innerHTML = STYLE + '<ha-card></ha-card>';
    this._view = 'cost';
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]');
      const v = e.target.closest('[data-view]');
      if (b) this._act(b.dataset.act);
      else if (v) {
        this._view = v.dataset.view;
        this._render();
      } else if (e.target.closest('.dsel')) this._select(this._hit(this._locate(e), '.dsel'));
    });
    root.addEventListener('keydown', (e) => {
      const k = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (!k || !e.target.matches('.dsel')) return;
      e.preventDefault();
      const rows = this._geo.rows;
      this._select(rows[rows.indexOf(this._pick(rows)) + k]);
    });
    // Mouse and pen hover. A touch hovers too once it slides sideways on a chart, where the browser keeps only vertical
    // pans (and cancels the touch when it takes one); a touch that never slides is a tap.
    root.addEventListener('pointermove', (e) => {
      const t = this._touch;
      if (e.pointerType === 'touch') {
        if (!t || t.id !== e.pointerId || !t.chart) return;
        if (!t.slide) {
          const dx = Math.abs(e.clientX - t.x);
          if (dx < 8 || dx <= Math.abs(e.clientY - t.y)) return;
          t.slide = true;
          // Selecting an hour redraws the inside of the card, the touched element too, so the card takes the rest of the slide.
          try {
            root.querySelector('ha-card').setPointerCapture(e.pointerId);
          } catch (err) {
            console.debug(TAG, err);
          }
        }
      }
      this._hover(this._locate(e));
    });
    this.addEventListener('pointerleave', (e) => e.pointerType !== 'touch' && this._point(null));
    root.addEventListener('pointerdown', (e) => {
      const chart = !!e.target.closest('.chart, .dsel');
      this._touch = e.pointerType === 'touch' ? { id: e.pointerId, x: e.clientX, y: e.clientY, chart, slide: false } : null;
    });
    root.addEventListener('pointercancel', () => (this._touch = null));
    root.addEventListener('pointerup', (e) => {
      const t = this._touch;
      this._touch = null;
      if (e.pointerType !== 'touch' || !t || t.id !== e.pointerId || t.slide) return;
      const at = this._locate(e);
      const r = this._hit(at);
      this._point(r && r.t !== this._row ? at : null);
    });
    this._away = (e) => this._at && !e.composedPath().includes(this) && this._point(null);
  }

  _act(act) {
    // Optimistic: draw the flip now; the real state replaces it, or the timeout / a rejected call reverts it.
    this._pending = act === 'on' ? 'off' : 'on';
    clearTimeout(this._pendingTimer);
    this._pendingTimer = setTimeout(() => this._clearPending(), 5000);
    this._render();
    Promise.resolve(this._hass.callService('homeassistant', act === 'on' ? 'turn_off' : 'turn_on', { entity_id: this._cfg.switch })).catch(() => this._clearPending());
  }

  _clearPending() {
    clearTimeout(this._pendingTimer);
    if (this._pending == null) return;
    this._pending = null;
    this._render();
  }

  setConfig(config) {
    this._cfg = { entity: 'sensor.smartgrid_power_card', switch: 'switch.smartgrid_enabled', debug: false, ...config };
    this._state = this._hass ? this._hass.states[this._cfg.entity] : null;
    this._sw = this._hass ? this._hass.states[this._cfg.switch] : null;
    this._dbg = { for: null, data: null, err: null };
    if (this._cfg.debug) this._fetch();
    this._drawn = null;
    this._render();
  }

  // Once per card state (its build time); an answer for an older state is dropped.
  _fetch() {
    const key = this._state && this._state.state;
    const hass = this._hass;
    if (!key || !hass || key === this._dbg.for) return;
    this._dbg.for = key;
    const done = (data, err) => {
      if (this._dbg.for !== key) return;
      this._dbg = { for: key, data, err };
      this._render();
    };
    Promise.resolve()
      .then(() => hass.callWS({ type: 'anker_x1_smartgrid/power_card_debug' }))
      .then((data) => done(data, null), (e) => done(null, String((e && e.message) || e)));
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._cfg) return;
    const s = hass.states[this._cfg.entity];
    const sw = hass.states[this._cfg.switch];
    if (s === this._state && sw === this._sw) return;
    if (sw !== this._sw) {
      this._pending = null;
      clearTimeout(this._pendingTimer);
    }
    this._state = s;
    this._sw = sw;
    if (this._cfg.debug) this._fetch();
    this._render();
  }

  connectedCallback() {
    // Rendering changes the card's height; doing it inside the observer callback trips the ResizeObserver loop error.
    if (!this._ro) this._ro = new ResizeObserver(() => requestAnimationFrame(() => this._render()));
    this._ro.observe(this);
    window.addEventListener('pointerdown', this._away, true);
  }

  disconnectedCallback() {
    if (this._ro) this._ro.disconnect();
    window.removeEventListener('pointerdown', this._away, true);
    this._at = null;
    this._row = null;
  }

  _locate(e) {
    const card = this.shadowRoot.querySelector('ha-card');
    const b = card.getBoundingClientRect();
    return { x: e.clientX - b.left - card.clientLeft, y: e.clientY - b.top - card.clientTop };
  }

  // The hour row under a point of the card, by its time on the column the point is in.
  _hit(at, box = '.chart') {
    const card = this.shadowRoot.querySelector('ha-card');
    const chart = card.querySelector(box);
    const g = this._geo;
    if (!chart || !g) return null;
    const b = card.getBoundingClientRect();
    const c = chart.getBoundingClientRect();
    const x = at.x - (c.left - b.left - card.clientLeft);
    const y = at.y - (c.top - b.top - card.clientTop);
    if (y < 0 || y >= c.height) return null;
    const i = g.cx.findIndex((l, k) => x >= l && x < l + g.cw[k]);
    if (i < 0) return null;
    // The level graph spans the full column, the panels below and the debug panel sit inside PAD.
    const pad = y < LEVEL_H && box === '.chart' ? 0 : PAD;
    const t = g.starts[i] + ((x - g.cx[i] - pad) / (g.cw[i] - 2 * pad)) * (g.ends[i] - g.starts[i]);
    const u = Math.min(Math.max(t, g.starts[i]), g.ends[i] - 1);
    return g.rows.find((r) => r.i === i && u >= r.t && u < r.t + H) || null;
  }

  // In debug mode the hour pointed at on either chart is also the one inspected; off the charts the last one stays.
  _hover(at) {
    const r = this._cfg && this._cfg.debug && (this._hit(at) || this._hit(at, '.dsel'));
    if (r && r.t !== this._sel) this._select(r);
    this._point(at);
  }

  _point(at) {
    const card = this.shadowRoot.querySelector('ha-card');
    const tip = card.querySelector('.tip');
    const r = at && tip ? this._hit(at) : null;
    this._at = r ? at : null;
    this._row = r ? r.t : null;
    for (const gd of card.querySelectorAll('.gd')) {
      const [a, b] = !r ? [0, 0] : gd.dataset.k === 'x' ? [r.x0, r.x1] : [r.p0, r.p1];
      gd.setAttribute('x', r1(a));
      gd.setAttribute('width', r1(b - a));
    }
    if (!tip) return;
    tip.hidden = !r;
    if (!r) return;
    tip.innerHTML = this._tip(r);
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    const left = at.x + 12 + tw <= card.clientWidth ? at.x + 12 : at.x - 12 - tw;
    const top = at.y + 16 + th <= card.clientHeight ? at.y + 16 : at.y - 16 - th;
    tip.style.left = `${Math.max(0, Math.min(card.clientWidth - tw, left))}px`;
    tip.style.top = `${Math.max(0, Math.min(card.clientHeight - th, top))}px`;
  }

  _tip(r) {
    const { rows, tz } = this._geo;
    const st = STATE_TEXT[r.state];
    const facts = [!r.plan ? 'measured' : num(r.price) === null && num(r.grid_in_house_kwh) === null && num(r.grid_in_charge_kwh) === null ? 'estimated' : 'planned'];
    let s = st ? `<div class="hd">${st[0].toUpperCase()}${st.slice(1)}</div>` : '';
    s += `<div><b>${when(r.t, r.t + H, tz)}</b> <span class="k">· ${facts.join(' · ')}</span></div>`;
    const p = num(r.price);
    const gh = num(r.grid_in_house_kwh);
    const gc = num(r.grid_in_charge_kwh);
    const imp = gh === null && gc === null ? null : (gh ?? 0) + (gc ?? 0);
    const out = num(r.grid_out_kwh);
    const row = (label, text) => `<div><span class="k">${label}</span> ${text}</div>`;
    const cost = (kwh) => (p === null ? '' : ` · ${eur(kwh * p)}`);
    if (p !== null) s += row('Price', `${eur(p, 3)}/kWh`);
    if (imp !== null) s += row('Import', `${imp.toFixed(2)} kWh${cost(imp)}${gc > 0.05 ? ` (${gc.toFixed(2)} to battery)` : ''}`);
    if (out !== null) s += row('Export', `${out.toFixed(2)} kWh${cost(out)}`);
    const dc = decision(r.why, tz);
    if (dc) {
      const mx = Math.max(dc.win[1], dc.lose[1]) * 1.05;
      const opt = ([label, v], win) =>
        `<div class="opt${win ? ' win' : ''}"><i>${win ? '✓' : ''}</i><span class="ol">${label}</span><span class="tr"><b style="width:${mx > 0 ? r1(Math.max(0, Math.min(100, (v / mx) * 100))) : 0}%"></b></span><span class="v">${eur(v, 3)}</span></div>`;
      s += `<div class="k per">per stored kWh</div><div class="dec">${opt(dc.win, true)}${opt(dc.lose, false)}</div>`;
      if (dc.note) s += `<div class="note">${dc.note}</div>`;
    }
    const use = num(r.use_kwh);
    if (use === null) return s;
    const gi = num(r.grid_in_house_kwh);
    const bh = num(r.batt_house_kwh);
    const grid = gi === null ? null : Math.min(Math.max(gi, 0), use);
    const batt = grid === null || bh === null ? null : Math.min(Math.max(bh, 0), use - grid);
    const sun = batt === null ? null : Math.min(Math.max(use - grid - batt, 0), num(r.solar_kwh) ?? Infinity);
    const prev = rows.find((x) => x.t === r.t - H);
    const s0 = prev ? num(prev.soc) : null;
    const s1 = num(r.soc);
    const line = [
      grid !== null && `${grid.toFixed(2)} grid`,
      batt !== null && `${batt.toFixed(2)} battery`,
      sun !== null && `${sun.toFixed(2)} sun`,
      s1 !== null && `battery ${s0 === null ? '' : `${s0.toFixed(0)}→`}${s1.toFixed(0)}%`,
    ].filter(Boolean);
    const seg = [[grid, 'g'], [batt, 'b'], [sun, 's']].filter(([v]) => v > 0);
    const bar = grid !== null && use > 0 ? `<div class="split">${seg.map(([v, c]) => `<i class="${c}" style="width:${r1((v / use) * 100)}%"></i>`).join('')}</div>` : '';
    return `${s}<div class="hu"><span class="k">House ${use.toFixed(2)} kWh</span>${bar}${line.length ? `<span class="k">${line.join(' · ')}</span>` : ''}</div>`;
  }

  // In rows of 50 px: about 340 px, or about 940 px with the debug panel and two build-ups.
  getCardSize() {
    return this._cfg && this._cfg.debug ? 19 : 7;
  }

  static getStubConfig() {
    return {};
  }

  _render() {
    const width = this.clientWidth;
    if (!this._cfg || !this._hass || !width) return;
    const d = this._drawn;
    if (d && d.st === this._state && d.sw === this._sw && d.pend === this._pending && d.width === width && d.dbg === this._dbg && d.sel === this._sel && d.view === this._view) return;
    this._drawn = { st: this._state, sw: this._sw, pend: this._pending, width, dbg: this._dbg, sel: this._sel, view: this._view };
    const card = this.shadowRoot.querySelector('ha-card');
    const ae = this.shadowRoot.activeElement;
    const refocus = ae && (ae.dataset.view ? `[data-view="${ae.dataset.view}"]` : ['[role=switch]', '.dsel'].find((q) => ae.matches(q)));
    try {
      card.innerHTML = this._body(width);
      // The 30-day line gives up its sun figure, then itself, before the battery text is clipped.
      const b1 = card.querySelector('.b1');
      const clipped = () => b1 && b1.scrollWidth > b1.clientWidth;
      const sunp = card.querySelector('.sunp');
      if (sunp && clipped()) sunp.remove();
      const m30 = card.querySelector('.m30');
      if (m30 && clipped()) m30.remove();
      // Today's name line gives up the average price before it clips the sun figure.
      const info = card.querySelector('.ph.on .info');
      const avg = info && info.querySelector('.avg');
      if (avg && info.scrollWidth > info.clientWidth) avg.remove();
      // Today's battery line gives up the balance before it clips the battery amount.
      const bal = card.querySelector('.ph.on .bal');
      if (bal && bal.parentNode.scrollWidth > bal.parentNode.clientWidth) bal.remove();
    } catch (e) {
      console.error(TAG, e);
      card.innerHTML = `${this._battery(null, this._swState(), false)}<div class="empty">The planner has no data yet.</div>`;
    }
    const el = refocus && card.querySelector(refocus);
    if (el) el.focus();
    if (this._at) this._point(this._at);
  }

  _swState() {
    const s = this._sw;
    if (!s || (s.state !== 'on' && s.state !== 'off')) return null;
    return this._pending ? { ...s, state: this._pending } : s;
  }

  _body(width) {
    const cfg = this._cfg;
    const st = this._state;
    const a = st && st.state !== 'unavailable' && st.state !== 'unknown' ? st.attributes || {} : {};
    const days = Array.isArray(a.days) ? a.days : [];
    const hours = Array.isArray(a.hours) ? a.hours : [];
    const name = cfg.title ? esc(typeof cfg.title === 'string' ? cfg.title : 'Power') : '';
    const title = cfg.debug ? `<div class="title dt">${name}<span class="chip">DEBUG</span></div>` : cfg.title ? `<div class="title">${name}</div>` : '';
    const sw = this._swState();
    const off = sw ? sw.state === 'off' : !!a.battery && a.battery.state === 'disabled';
    if (!days.length && !hours.length) {
      return `${title}${this._battery(a.battery, sw, off)}<div class="empty">The planner has no data yet.</div>`;
    }

    const tz = this._hass.config && this._hass.config.time_zone;
    const now = Date.now();
    // The server lists yesterday, today, tomorrow; its today stays right across midnight, the browser clock may be ahead.
    const today =
      days.length === 3 && days.every((d) => d && typeof d.date === 'string') ? days[1].date : dateOf(now, tz);
    let dates = [-1, 0, 1].map((k) => addDays(today, k));
    // shortcut: under 400 px the three columns don't fit at 11 px, so yesterday is dropped.
    if (width < 400) dates = dates.slice(1);
    const n = dates.length;
    const W = width - 20;
    const fr = COLS[n];
    const cw = fr.map((f) => f * W);
    const cx = cw.map((_, i) => cw.slice(0, i).reduce((s, w) => s + w, 0));
    const starts = dates.map((d) => localTime(d, 0, tz));
    const ends = dates.map((d) => localTime(addDays(d, 1), 0, tz));
    const colOf = (t) => starts.findIndex((s, i) => t >= s && t < ends[i]);
    const X = (i, t, pad) => cx[i] + pad + ((t - starts[i]) / (ends[i] - starts[i])) * (cw[i] - 2 * pad);

    const rows = [];
    for (const h of hours) {
      const t = Date.parse(h && h.start);
      const i = Number.isFinite(t) ? colOf(t) : -1;
      if (i >= 0) rows.push({ ...h, t, i, x0: X(i, t, 0), x1: X(i, t + H, 0), p0: X(i, t, PAD), p1: X(i, t + H, PAD) });
    }
    const nowCol = colOf(now);
    const geo = { dates, today, cx, cw, W, tz, X };
    this._geo = { rows, cx, cw, starts, ends, tz };
    let dbg = '';
    if (cfg.debug) {
      try {
        dbg = this._debug(rows, geo, nowCol >= 0 ? X(nowCol, now, PAD) : null);
      } catch (e) {
        console.error(TAG, e);
        dbg = DEBUG_FAIL;
      }
    }

    return (
      `<div class="${off ? 'off' : ''}">` +
      title +
      this._battery(a.battery, sw, off) +
      `<div class="chart"><div class="lv">${this._level(rows, geo, nowCol >= 0 ? X(nowCol, now, 0) : null)}${off ? '<span class="pill">SmartGrid disabled</span>' : ''}</div>` +
      this._headers(days, fr, geo, a.battery) +
      this._panels(rows, geo, nowCol >= 0 ? X(nowCol, now, PAD) : null) +
      '</div>' +
      LEGEND +
      dbg +
      '<div class="tip" hidden></div></div>'
    );
  }

  _control(sw) {
    if (!sw) return '';
    const name = esc((sw.attributes && sw.attributes.friendly_name) || 'SmartGrid enabled');
    const on = sw.state === 'on';
    return `<button role="switch" aria-checked="${on}" aria-label="${name}" data-act="${on ? 'on' : 'off'}"><span class="trk"><i></i></span></button>`;
  }

  _battery(b, sw, off) {
    b = b || {};
    const soc = num(b.soc);
    const items = [
      soc !== null && `${soc.toFixed(0)}%`,
      num(b.stored_kwh) !== null && `${b.stored_kwh.toFixed(1)} kWh`,
      num(b.stored_cost_eur_per_kwh) !== null && `${eur(b.stored_cost_eur_per_kwh)}/kWh`,
      STATE_TEXT[off && (!b.state || b.state === 'idle') ? 'disabled' : b.state],
    ].filter(Boolean);
    const gain = sun(b.solar_gain_30d_eur, '€');
    const margin = num(b.margin_30d_eur) !== null ? `30 days ${signed(b.margin_30d_eur, '€')}` : '';
    const m = margin || gain ? `<span class="m30">${margin}${gain && margin ? `<span class="sunp"> · ${gain}</span>` : gain}</span>` : '';
    return `<div class="bat"><span class="b1"><b>Battery</b> ${items.join(' · ') || DASH}</span>${m}${this._control(sw)}</div>`;
  }

  _level(rows, { cx, W }, nowX) {
    let s = `<svg class="dm" width="${W}" height="${LEVEL_H}"><defs><pattern id="sgh" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="2" height="5" class="hs"/></pattern></defs>`;
    s += `<line x1="0" y1="4" x2="${W}" y2="4" class="rule" stroke-dasharray="2 3"/><line x1="0" y1="46" x2="${W}" y2="46" class="rule"/>`;
    const groups = [];
    let prev = null;
    let g = null;
    for (const r of rows) {
      if (num(r.soc) === null) {
        prev = null;
        continue;
      }
      const joined = prev && prev.t + H === r.t;
      const y0 = joined ? ly(prev.soc) : ly(r.soc);
      const y1 = ly(r.soc);
      if (!g || !joined || g.plan !== !!r.plan) {
        g = { plan: !!r.plan, pts: [[r.x0, y0]] };
        groups.push(g);
      }
      g.pts.push([r.x1, y1]);
      if (r.state === 'hold') {
        s += `<polygon points="${r1(r.x0)},46 ${r1(r.x0)},${r1(y0)} ${r1(r.x1)},${r1(y1)} ${r1(r.x1)},46" fill="url(#sgh)"${r.plan ? ' opacity=".6"' : ''}/>`;
      }
      prev = r;
    }
    for (const gr of groups) {
      const pts = gr.pts.map(([x, y]) => `${r1(x)},${r1(y)}`).join(' ');
      const op = gr.plan ? ' opacity=".6"' : '';
      s += `<polygon points="${r1(gr.pts[0][0])},46 ${pts} ${r1(gr.pts[gr.pts.length - 1][0])},46" class="lvlf"${op}/>`;
      s += `<polyline points="${pts}" class="lvl"${op}/>`;
    }
    for (let i = 1; i < cx.length; i++) s += `<line x1="${r1(cx[i])}" y1="0" x2="${r1(cx[i])}" y2="${LEVEL_H}" class="rule"/>`;
    if (nowX !== null) s += `<line x1="${r1(nowX)}" y1="0" x2="${r1(nowX)}" y2="46" class="now"/>`;
    // Extremes are labelled so the graph does not rely on colour alone.
    const placed = [];
    for (let i = 0; i < cx.length; i++) {
      const col = rows.filter((r) => r.i === i && num(r.soc) !== null);
      if (!col.length) continue;
      const hi = col.reduce((a, r) => (r.soc > a.soc ? r : a));
      const lo = col.reduce((a, r) => (r.soc < a.soc ? r : a));
      if (hi.soc - lo.soc < 10) continue;
      for (const [r, below] of [[hi, true], [lo, false]]) {
        const y = ly(r.soc);
        const base = below ? Math.min(y + 13, 44) : Math.max(y - 4, 13);
        const right = r.x1 + 31 > W;
        const bx = right ? r.x1 - 31 : r.x1 + 1;
        const box = { x: bx, y: base - 10, w: 30, h: 13 };
        if (placed.some((p) => p.x < box.x + box.w && box.x < p.x + p.w && p.y < box.y + box.h && box.y < p.y + p.h)) continue;
        placed.push(box);
        s += `<rect x="${r1(bx)}" y="${r1(base - 10)}" width="30" height="13" rx="2" class="lbg"/>`;
        s += `<text x="${r1(bx + 15)}" y="${r1(base)}" font-size="11" text-anchor="middle" class="ink">${r.soc.toFixed(0)}%</text>`;
      }
    }
    return s + `<rect class="gd" data-k="x" y="0" width="0" height="${LEVEL_H}"/></svg>`;
  }

  _cheap(c, tz) {
    const hh = (iso) => parts(Date.parse(iso), tz).hour;
    if (!c || !Number.isFinite(Date.parse(c.start)) || !Number.isFinite(Date.parse(c.end)) || num(c.price) === null) return '<div class="cheap"></div>';
    return `<div class="cheap pos">cheap ${pad2(hh(c.start))}${DASH}${pad2(hh(c.end) || 24)} · ${eur(c.price)}</div>`;
  }

  _headers(days, fr, { dates, today, cw, tz }, b) {
    const cols = dates.map((date, i) => {
      const d = days.find((x) => x && x.date === date) || {};
      if (date === today) {
        // The detail line has no room for sun at the wall's width; the name line does.
        const gain = sun(d.solar_gain_eur, '€');
        const avg = num(d.avg_price) === null ? '' : `<span class="avg">${gain && ' · '}avg ${eur(d.avg_price)}/kWh</span>`;
        const info = gain || avg ? `<span class="info">${gain}${avg}</span>` : '';
        // The full wording needs about 200 px; the wall's Today column is narrower.
        const use = [`${fx(d.use_kwh, 1)} kWh${cw[i] - 2 * PAD - 1 >= 200 ? ' used' : ''}`];
        if (num(d.grid_in_kwh) !== null) use.push(`${d.grid_in_kwh.toFixed(1)} from grid`);
        if (num(d.grid_out_kwh) !== null) use.push(`${d.grid_out_kwh.toFixed(1)} sold`);
        return `<div class="ph on">
<div><span class="name">Today</span>${info}</div>
<div class="cost"><span>${eur(d.use_cost_eur)}</span>${saved(d.saved_eur, 'saved ')}</div>
<div class="bl"><span>${owed('battery', d.batt_grid_eur)}</span><span class="bal">${owed('balance', b && b.balance_eur)}</span></div>
<div class="l3">${use.join(' · ')}</div>
${this._cheap(d.cheap, tz)}
</div>`;
      }
      return `<div class="ph">
<div><span class="name">${date < today ? 'Yesterday' : 'Tomorrow'}</span></div>
<div class="cost"><span>${eur(d.use_cost_eur)}</span></div>
<div class="bl"><span>${owed('battery', d.batt_grid_eur)}</span></div>
<div><span>${fx(d.use_kwh, 1)} kWh</span>${saved(d.saved_eur, date > today ? 'plan ' : '')}</div>
${date > today ? this._cheap(d.cheap, tz) : '<div class="cheap"></div>'}
</div>`;
    });
    return `<div class="days dm" style="grid-template-columns:${fr.map((f) => `${f * 100}%`).join(' ')}">${cols.join('')}</div>`;
  }

  _panels(rows, { dates, cx, cw, W, tz, X }, nowX) {
    const priced = rows.filter((r) => num(r.price) !== null);
    const pmax = Math.max(...priced.map((r) => r.price));
    const pmin = Math.min(...priced.map((r) => r.price));
    const k = barPx(rows);
    const off = (p) => (p !== null && pmax > pmin ? ((pmax - p) / (pmax - pmin)) * PRICE_PX : PRICE_PX / 2);
    // A priceless hour sits at the nearest priced hour's level, the chart middle if none.
    for (const r of rows) {
      const near = priced.reduce((a, q) => (!a || Math.abs(q.t - r.t) < Math.abs(a.t - r.t) ? q : a), null);
      r.lvl = near ? near.price : null;
    }
    // Room for a full-height bar at the top or bottom of the price line, so the card keeps its height.
    const pt = BAR_PX + 17;
    const Z = pt + PRICE_PX + BAR_PX + 17 + 4 + CUT * KWH_PX;
    const PANEL_H = Z + 16;
    const py = (p) => pt + off(p);
    const lv = (r) => py(r.lvl);
    let s = `<svg class="dm" width="${W}" height="${PANEL_H}">`;
    for (let i = 1; i < cx.length; i++) s += `<line x1="${r1(cx[i])}" y1="0" x2="${r1(cx[i])}" y2="${PANEL_H - 2}" class="rule"/>`;
    for (let i = 0; i < cx.length; i++) s += `<line x1="${r1(cx[i] + PAD)}" y1="${Z}" x2="${r1(cx[i] + cw[i] - PAD)}" y2="${Z}" class="rule"/>`;
    const [gb, bars] = gridBars(rows, lv, k, Z);
    s += gb;

    let prev = null;
    for (const r of rows) {
      if (num(r.price) === null) {
        prev = null;
        continue;
      }
      const y = py(r.price);
      if (prev && prev.t + H === r.t && prev.i === r.i) {
        s += `<line x1="${r1(r.p0)}" y1="${r1(py(prev.price))}" x2="${r1(r.p0)}" y2="${r1(y)}" class="conn"/>`;
      }
      s += `<line x1="${r1(r.p0)}" y1="${r1(y)}" x2="${r1(r.p1)}" y2="${r1(y)}" class="p ${LINE[r.state] || 'idle'}" opacity="${r.plan ? 0.55 : 1}"/>`;
      prev = r;
    }

    const placed = [];
    const hit = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
    for (let i = 0; i < cx.length; i++) {
      const date = dates[i];
      const lo = cx[i] + PAD;
      const hi = cx[i] + cw[i] - PAD;
      const clamp = (x, half) => Math.max(lo + half, Math.min(hi - half, x));
      const col = rows.filter((r) => r.i === i);
      const cp = priced.filter((r) => r.i === i);
      if (cp.length) {
        const top = cp.reduce((a, r) => (r.price > a.price ? r : a));
        const bot = cp.reduce((a, r) => (r.price < a.price ? r : a));
        const lab = (r, y) => {
          const x = clamp((r.p0 + r.p1) / 2, 13);
          const box = { x: x - 13, y: y - 9, w: 26, h: 11 };
          if (bars.some((q) => hit(q, box))) return '';
          placed.push(box);
          return `<text x="${r1(x)}" y="${r1(y)}" font-size="11" text-anchor="middle" class="ink pl">${r.price.toFixed(2)}</text>`;
        };
        s += lab(top, py(top.price) - 5);
        if (bot.price < top.price) s += lab(bot, py(bot.price) + 14);
      }
      s += ticks(date, i, { X, tz }, lo, Z + 13);

      // Each charge and export block gets its total at the free end of its bars, or nothing if that would overlap.
      for (const [key, sign] of [['grid_in_charge_kwh', '+'], ['grid_out_kwh', '−']]) {
        const blocks = [];
        let blk = null;
        for (const r of col) {
          const v = num(r[key]);
          if (v === null || v <= 0.05) {
            blk = null;
          } else if (blk && blk.last.t + H === r.t) {
            blk.kwh += v;
            blk.last = r;
            blk.rows.push(r);
          } else {
            blk = { kwh: v, first: r, last: r, rows: [r] };
            blocks.push(blk);
          }
        }
        for (const bl of blocks) {
          if (bl.kwh < 0.5) continue;
          const text = `${sign}${bl.kwh.toFixed(1)}`;
          const w = text.length * 6;
          const ends = bl.rows.map((r) => lv(r) + (sign === '+' ? -1 : 1) * (num(r[key]) || 0) * k);
          const base = sign === '+' ? Math.min(...ends) - 4 : Math.max(...ends) + 13;
          // Centred on the block, else flush to either end so the now line can stay clear.
          for (const x of [(bl.first.p0 + bl.last.p1) / 2 - w / 2, bl.first.p0, bl.last.p1 - w]) {
            const box = { x, y: base - 9, w, h: 10 };
            if (x < cx[i] + 2 || x + w > cx[i] + cw[i] - 2) continue;
            if (nowX !== null && nowX > x - 2 && nowX < x + w + 2) continue;
            if (placed.some((p) => hit(p, box)) || bars.some((b) => hit(b, box))) continue;
            placed.push(box);
            s += `<text x="${r1(x)}" y="${r1(base)}" font-size="11" class="ink pl">${text}</text>`;
            break;
          }
        }
      }
    }
    if (nowX !== null) s += `<line x1="${r1(nowX)}" y1="4" x2="${r1(nowX)}" y2="${Z}" class="now"/>`;
    return s + `<rect class="gd" data-k="p" y="0" width="0" height="${PANEL_H - 14}"/></svg>`;
  }

  // Against the day's first plan, the past hours' grid cost, house use or sun, today's sum, and one hour inspected.
  _debug(rows, geo, nowP) {
    const { err, data } = this._dbg;
    if (err) return DEBUG_FAIL;
    const { dates, today, cx, cw, W, tz } = geo;
    const ref = hourly(
      data && data.reference,
      tz,
      () => ({ load_kwh: 0, pv_kwh: 0, xs: [] }),
      (a, x) => {
        a.load_kwh += num(x.load_kwh) ?? 0;
        a.pv_kwh += num(x.pv_kwh) ?? 0;
        a.xs.push(x);
      },
    );
    const cov = rows.filter((r) => ref.has(r.t)).map((r) => ({ ...r, ...ref.get(r.t) }));
    const past = cov.filter((r) => !r.plan);
    const tp = past.filter((r) => dates[r.i] === today);
    const sel = this._pick(rows);
    const view = this._view;
    const top = 12;
    const base = 92;
    const scale = (y, text) => `<line x1="0" y1="${r1(y)}" x2="${W}" y2="${r1(y)}" class="rule" stroke-dasharray="2 3"/><text x="${W - 2}" y="${r1(y - 2)}" font-size="10" text-anchor="end" class="dim">${text}</text>`;
    let v = `<svg class="dm dvw" width="${W}" height="${base + 17}">`;
    for (let i = 1; i < cx.length; i++) v += `<line x1="${r1(cx[i])}" y1="0" x2="${r1(cx[i])}" y2="${base}" class="rule"/>`;
    let sum;
    if (view === 'cost') {
      // Per hour over, what the grid cost against what the first plan would have: dearer up, cheaper down.
      const cost = new Map(Object.entries((data && data.hour_costs) || {}).map(([k, c]) => [Date.parse(k), c]));
      const costOf = (r) => {
        const c = cost.get(r.t);
        return c && num(c.real_eur) !== null && num(c.plan_eur) !== null ? c.real_eur - c.plan_eur : null;
      };
      const mid = (top + base) / 2;
      const mx = Math.max(0.05, ...past.map((r) => Math.abs(costOf(r) ?? 0)));
      v += scale(top, signed(mx, '€')) + scale(base, signed(-mx, '€'));
      for (let i = 0; i < cx.length; i++) v += `<line x1="${r1(cx[i] + PAD)}" y1="${r1(mid)}" x2="${r1(cx[i] + cw[i] - PAD)}" y2="${r1(mid)}" class="rule"/>`;
      for (const r of past) {
        const d = costOf(r);
        const [bx, w] = barBox(r);
        const h = d === null ? 0 : (Math.abs(d) / mx) * (mid - top);
        if (d === null) v += `<rect x="${r1(bx)}" y="${r1(mid - 5)}" width="${r1(w)}" height="10" fill="url(#sgh)"/>`;
        else if (h >= 0.5) v += `<rect x="${r1(bx)}" y="${r1(d > 0 ? mid - h : mid)}" width="${r1(w)}" height="${r1(h)}" class="${d > 0 ? 'dn' : 'dp'}"/>`;
      }
      // The day's gap split by cause: each slot at its own import price, each saved-plan row at the mean price of the slots it spans.
      // Sun that would have been sold is valued at the import price, so its difference lands in the rest.
      const known = tp.filter((r) => costOf(r) !== null);
      const slots = hourly(data && data.slots, tz, () => ({ ss: [] }), (a, x) => a.ss.push(x));
      const miss = (got, planned) =>
        known.reduce((t, r) => {
          const { ss } = slots.get(r.t);
          const w = H / ss.length;
          const priceOf = (x) => {
            const t0 = Date.parse(x.start);
            const on = ss.filter((s) => Date.parse(s.start) < t0 + x.minutes * 60e3 && Date.parse(s.start) + w > t0);
            return on.reduce((a, s) => a + s.price, 0) / on.length;
          };
          const real = ss.reduce((a, s) => a + (num(s[got]) ?? 0) * s.price, 0);
          return t + real - r.xs.reduce((a, x) => a + (num(x[planned]) ?? 0) * priceOf(x), 0);
        }, 0);
      const total = known.length ? known.reduce((t, r) => t + costOf(r), 0) : null;
      const use = known.length ? miss('use_kwh', 'load_kwh') : null;
      const sun = known.length ? -miss('solar_kwh', 'pv_kwh') : null;
      const money = (x) => (x === null ? DASH : `<span${Math.abs(x) < 0.005 ? '' : ` class="${x > 0 ? 'neg' : 'pos'}"`}>${signed(x, '€')}</span>`);
      sum = [
        [money(total), total === null || Math.abs(total) < 0.005 ? 'against plan today' : `${total > 0 ? 'dearer' : 'cheaper'} than planned today`],
        [money(use), 'from house use'],
        [money(sun), 'from sun'],
        [money(total === null ? null : total - use - sun), 'from battery and timing'],
      ];
    } else {
      const [key, fkey, cls, label] = view === 'use' ? ['use_kwh', 'load_kwh', 'uo', 'use'] : ['solar_kwh', 'pv_kwh', 'so', 'sun'];
      const mx = Math.max(0.2, ...cov.map((r) => num(r[fkey]) ?? 0), ...past.map((r) => num(r[key]) ?? 0));
      const k = (base - top) / mx;
      v += scale(top, `${fx(mx, 1)} kWh`);
      for (let i = 0; i < cx.length; i++) v += `<line x1="${r1(cx[i] + PAD)}" y1="${base}" x2="${r1(cx[i] + cw[i] - PAD)}" y2="${base}" class="rule"/>`;
      v += outline(cov, fkey, 'dl', (x) => x * k, base) + outline(past, key, cls, (x) => x * k, base);
      const m = tp.filter((r) => num(r[key]) !== null);
      sum = [[m.length ? `${signed(m.reduce((t, r) => t + r[key] - r[fkey], 0), '', 1)} kWh` : DASH, `${label} against forecast today`]];
    }
    for (let i = 0; i < cx.length; i++) v += ticks(dates[i], i, geo, cx[i] + PAD, base + 13);
    if (nowP !== null) v += `<line x1="${r1(nowP)}" y1="0" x2="${r1(nowP)}" y2="${base}" class="now"/>`;
    if (sel) v += `<rect x="${r1(sel.p0)}" y="0" width="${r1(sel.p1 - sel.p0)}" height="${base}" class="dsb"/>`;
    const slider = `<div class="dsel" tabindex="0" role="slider" aria-label="Hour to inspect" aria-valuemin="0" aria-valuemax="${rows.length - 1}" aria-valuenow="${rows.indexOf(sel)}"${sel ? ` aria-valuetext="${when(sel.t, sel.t + H, tz)}"` : ''}>`;
    const head = `<div class="dvh"><span class="dvt">Plan against reality</span><span role="group" aria-label="Panel" class="dvg">${VIEWS.map(([k, label]) => `<button type="button" class="dvb" data-view="${k}" aria-pressed="${k === view}"><span>${label}</span></button>`).join('')}</span></div>`;
    return (
      `<div class="dbg">${head}${slider}${v}</svg></div><div class="lg dm">${DEBUG_LEGEND[view]}</div><div class="dsum dm">${sum.map(([x, l]) => `<div><b>${x}</b>${l}</div>`).join('')}</div>` +
      `${data && sel ? this._inspect(sel, data, tz, today) : ''}</div>`
    );
  }

  // The hour the inspector shows: the one picked, else the running one.
  _pick(rows) {
    const now = Date.now();
    return rows.find((r) => r.t === this._sel) || rows.find((r) => now >= r.t && now < r.t + H);
  }

  _select(r) {
    if (!r) return;
    this._sel = r.t;
    this._render();
  }

  // One hour's tariff slots against the day's first plan, the planner's reason, and the settings of its last build.
  _inspect(r, { slots, reference, internals, reasons }, tz, today) {
    const byStart = (a) => new Map((Array.isArray(a) ? a : []).map((x) => [Date.parse(x && x.start), x]));
    const ref = byStart(reference);
    const cell = (m, f, d, floor) => {
      if (f === null) return `<td>${fx(m, d)}</td>`;
      const off = m !== null && Math.abs(m - f) > Math.max(0.25 * Math.abs(f), floor);
      return `<td${off ? ' class="neg"' : ''}>${f.toFixed(d)} → ${fx(m, d)}</td>`;
    };
    const trs = [...byStart(slots)]
      .filter(([t]) => t >= r.t && t < r.t + H)
      .map(([t, x]) => {
        const f = (!x.plan && ref.get(t)) || {};
        const sp = num(x.setpoint_w);
        const bw = num(x.batt_w);
        const off = sp !== null && Math.round(sp) !== 0 && bw !== null && Math.abs(bw - sp) > 0.15 * Math.abs(sp);
        return (
          `<tr><td>${clock(t, tz)}</td><td>${eur(x.price, 3)}</td><td>${Object.prototype.hasOwnProperty.call(STATE_TEXT, x.state) ? STATE_TEXT[x.state] : x.state ? esc(x.state) : DASH}</td><td class="sp">${watts(sp)}</td><td${off ? ' class="neg"' : ''}>${watts(bw)}</td>` +
          `${cell(num(x.use_kwh), num(f.load_kwh), 2, 0.05)}${cell(num(x.solar_kwh), num(f.pv_kwh), 2, 0.05)}${cell(num(x.soc), num(f.soc), 0, 5)}</tr>`
        );
      });
    const head = ['time', 'price', 'action', 'setpoint', 'battery', 'use kWh', 'sun kWh', 'level %'].map((h) => `<th>${h}</th>`).join('');
    const table = trs.length ? `<div class="dtw"><table><tr>${head}</tr>${trs.join('')}</table></div>` : '<div class="dwhy">No data for this hour</div>';
    // The last reason each hour had before it ran, keyed by hour start; planned hours fall back on the card's own.
    const stored = new Map(Object.entries(reasons && typeof reasons === 'object' ? reasons : {}).map(([k, x]) => [Date.parse(k), x]));
    const w = stored.get(r.t) || (r.plan ? r.why : null);
    const ups = buildups(w, r.t, tz);
    const from = !r.plan && w && Date.parse(w.at);
    const dc = !ups && decision(w, tz);
    const why = ups
      ? `${Number.isFinite(from) ? `<div>Reason from <b>${stamp(from, r.t, tz)}</b></div>` : ''}<div class="dans">${verdict(w, r.t, tz)}</div>${ups}`
      : dc
        ? `<span class="pos">✓</span> <b>${dc.win[0]} ${eur(dc.win[1], 3)}</b> · ${dc.lose[0]} ${eur(dc.lose[1], 3)} per stored kWh.${dc.note && ` ${dc.note}`}`
        : r.plan
          ? 'No reason given for this hour.'
          : 'No reason stored for this hour.';
    const iv = internals && typeof internals === 'object' ? internals : {};
    const own = Object.keys(iv);
    const kv = [...[...INTERNALS.keys()].filter((k) => own.includes(k)), ...own.filter((k) => !INTERNALS.has(k))]
      .map((k) => {
        const [label, f, old] = INTERNALS.get(k) || [esc(k), (x) => esc(typeof x === 'string' ? x : JSON.stringify(x))];
        const text = f(iv[k], tz, today);
        if (text === null) return '';
        return old && old(iv[k], tz, today) ? `<span class="old"><i>!</i> ${label} <b>${text}</b></span>` : `<span>${label} <b>${text}</b></span>`;
      })
      .filter(Boolean);
    return (
      `<div class="din dm"><div class="dcap"><span><b>${when(r.t, r.t + H, tz)}</b> · ${r.plan ? 'planned' : 'measured'}</span><span>click an hour</span></div>` +
      `${table}<div class="dwhy">${why}</div>${kv.length ? `<div class="dkv">${kv.join('')}</div>` : ''}</div>`
    );
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, SmartgridPowerCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === TAG)) {
  window.customCards.push({
    type: TAG,
    name: 'SmartGrid power',
    description: 'Battery level, price and grid use for yesterday, today and tomorrow.',
    preview: true,
  });
}
