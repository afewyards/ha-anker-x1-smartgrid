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
function signed(v, cur = '') {
  if (num(v) === null) return DASH;
  const s = Math.abs(v).toFixed(2);
  return (v < 0 && s !== '0.00' ? '−' : '+') + cur + s;
}
function eur(v) {
  return num(v) === null ? DASH : (v < 0 ? '−€' : '€') + Math.abs(v).toFixed(2);
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
}
ha-card { padding: 6px 10px; color: var(--sg-ink); font-size: 11px; }
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
.cheap { min-height: 1.35em; overflow: hidden; text-overflow: ellipsis; }
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
.gh { fill: var(--sg-dim); } .gc { fill: var(--sg-charge); } .go { fill: var(--sg-export); } .cut { fill: var(--sg-bg); }
.uo, .so { fill: none; stroke-width: 1; stroke-linejoin: round; }
.uo { stroke: var(--sg-ink); stroke-opacity: .55; } .so { stroke: var(--sg-sun); }
.sw.charge { fill: var(--sg-charge); } .sw.house { fill: var(--sg-house); } .sw.export { fill: var(--sg-export); } .sw.idle { fill: var(--sg-dim); }
</style>`;

const LEVEL_H = 48;
const PANEL_H = 125;
// Grid bars: zero line, px per kWh, and the cut that keeps a night's charge from flattening the house hours.
const Z = 84;
const KWH_PX = 10;
const CUT = 2.5;
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
const LEGEND = `<div class="lg dm">
<span>price${swatch('sw charge')}charge${swatch('sw house')}to house${swatch('sw export')}export${swatch('sw idle', 1.5)}idle, hold<svg width="10" height="9"><rect width="10" height="9" fill="url(#sgh)"/></svg>held</span>
<span>grid kWh${swatch('gh', 9)}house${swatch('gc', 9)}charge${swatch('go', 9)}export</span>
<span>${stroke('uo')}use${stroke('so')}sun<svg width="6" height="9"><rect x="1" width="4" height="9" class="gc"/>${cuts(1, 1.5, 4)}</svg>cut at ${CUT} · faint: plan</span>
</div>`;

// What stored solar earned over exporting it; left out while it rounds to zero.
function sun(v, cur = '') {
  return num(v) === null || Math.abs(v) < 0.005 ? '' : `sun ${signed(v, cur)}`;
}

function saved(v, prefix = '') {
  if (num(v) === null) return `<span class="sv">${DASH}</span>`;
  return `<span class="sv ${v < 0 ? 'neg' : 'pos'}">${prefix}${signed(v)}</span>`;
}

class SmartgridPowerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).innerHTML = STYLE + '<ha-card></ha-card>';
    this.shadowRoot.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]');
      if (b) this._act(b.dataset.act);
    });
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
    this._cfg = { entity: 'sensor.smartgrid_power_card', switch: 'switch.smartgrid_enabled', ...config };
    this._state = this._hass ? this._hass.states[this._cfg.entity] : null;
    this._sw = this._hass ? this._hass.states[this._cfg.switch] : null;
    this._drawn = null;
    this._render();
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
    this._render();
  }

  connectedCallback() {
    // Rendering changes the card's height; doing it inside the observer callback trips the ResizeObserver loop error.
    if (!this._ro) this._ro = new ResizeObserver(() => requestAnimationFrame(() => this._render()));
    this._ro.observe(this);
  }

  disconnectedCallback() {
    if (this._ro) this._ro.disconnect();
  }

  getCardSize() {
    return 7;
  }

  static getStubConfig() {
    return {};
  }

  _render() {
    const width = this.clientWidth;
    if (!this._cfg || !this._hass || !width) return;
    const d = this._drawn;
    if (d && d.st === this._state && d.sw === this._sw && d.pend === this._pending && d.width === width) return;
    this._drawn = { st: this._state, sw: this._sw, pend: this._pending, width };
    const card = this.shadowRoot.querySelector('ha-card');
    const ae = this.shadowRoot.activeElement;
    const refocus = !!ae && ae.matches('[role=switch]');
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
    } catch (e) {
      console.error(TAG, e);
      card.innerHTML = `${this._battery(null, this._swState(), false)}<div class="empty">The planner has no data yet.</div>`;
    }
    const sw = refocus && card.querySelector('[role=switch]');
    if (sw) sw.focus();
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
    const title = cfg.title ? `<div class="title">${esc(typeof cfg.title === 'string' ? cfg.title : 'Power')}</div>` : '';
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

    return (
      `<div class="${off ? 'off' : ''}">` +
      title +
      this._battery(a.battery, sw, off) +
      `<div class="lv">${this._level(rows, geo, nowCol >= 0 ? X(nowCol, now, 0) : null)}${off ? '<span class="pill">SmartGrid disabled</span>' : ''}</div>` +
      this._headers(days, fr, geo) +
      this._panels(rows, geo, nowCol >= 0 ? X(nowCol, now, PAD) : null) +
      LEGEND +
      '</div>'
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
    return s + '</svg>';
  }

  _cheap(c, tz) {
    const hh = (iso) => parts(Date.parse(iso), tz).hour;
    if (!c || !Number.isFinite(Date.parse(c.start)) || !Number.isFinite(Date.parse(c.end)) || num(c.price) === null) return '<div class="cheap"></div>';
    return `<div class="cheap pos">cheap ${pad2(hh(c.start))}${DASH}${pad2(hh(c.end) || 24)} · ${eur(c.price)}</div>`;
  }

  _headers(days, fr, { dates, today, cw, tz }) {
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
<div class="l3">${use.join(' · ')}</div>
${this._cheap(d.cheap, tz)}
</div>`;
      }
      return `<div class="ph">
<div><span class="name">${date < today ? 'Yesterday' : 'Tomorrow'}</span></div>
<div class="cost"><span>${eur(d.use_cost_eur)}</span></div>
<div><span>${fx(d.use_kwh, 1)} kWh</span>${saved(d.saved_eur, date > today ? 'plan ' : '')}</div>
${date > today ? this._cheap(d.cheap, tz) : '<div class="cheap"></div>'}
</div>`;
    });
    return `<div class="days dm" style="grid-template-columns:${fr.map((f) => `${f * 100}%`).join(' ')}">${cols.join('')}</div>`;
  }

  _panels(rows, { dates, cx, cw, W, tz, X }, nowX) {
    let s = `<svg class="dm" width="${W}" height="${PANEL_H}">`;
    for (let i = 1; i < cx.length; i++) s += `<line x1="${r1(cx[i])}" y1="0" x2="${r1(cx[i])}" y2="${PANEL_H - 2}" class="rule"/>`;
    for (let i = 0; i < cx.length; i++) s += `<line x1="${r1(cx[i] + PAD)}" y1="${Z}" x2="${r1(cx[i] + cw[i] - PAD)}" y2="${Z}" class="rule"/>`;
    const priced = rows.filter((r) => num(r.price) !== null);
    const pmax = Math.max(...priced.map((r) => r.price));
    const pmin = Math.min(...priced.map((r) => r.price));
    const py = (p) => (pmax > pmin ? 14 + ((pmax - p) / (pmax - pmin)) * 27 : 28);
    const kh = (v) => Math.min(Math.max(0, v), CUT) * KWH_PX;

    // House use and sun as stepped outlines behind the bars, one path per run of hours.
    for (const [key, cls] of [['use_kwh', 'uo'], ['solar_kwh', 'so']]) {
      let d = '';
      let prev = null;
      const flush = () => {
        if (d) s += `<path d="${d} V${Z}" class="${cls}"${prev.plan ? ' opacity=".5"' : ''}/>`;
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
          d = `M${r1(r.p0)},${Z}`;
        }
        d += ` V${r1(Z - kh(v))} H${r1(r.p1)}`;
        prev = r;
      }
      flush();
    }

    const bars = [];
    for (const r of rows) {
      const house = num(r.grid_in_house_kwh);
      const charge = num(r.grid_in_charge_kwh);
      const out = num(r.grid_out_kwh);
      if (house === null && charge === null && out === null) continue;
      const w = Math.max(1, (r.p1 - r.p0) * 0.72);
      const bx = r.p0 + (r.p1 - r.p0 - w) / 2;
      const inn = Math.max(0, house || 0) + Math.max(0, charge || 0);
      const hh = kh(house || 0);
      const up = kh(inn);
      const dn = kh(out || 0);
      let b = '';
      if (hh > 0) b += `<rect x="${r1(bx)}" y="${r1(Z - hh)}" width="${r1(w)}" height="${r1(hh)}" class="gh"/>`;
      if (up > hh) b += `<rect x="${r1(bx)}" y="${r1(Z - up)}" width="${r1(w)}" height="${r1(up - hh)}" class="gc"/>`;
      if (inn > CUT) b += cuts(bx, Z - up + 2.5, w);
      if (dn > 0) b += `<rect x="${r1(bx)}" y="${Z}" width="${r1(w)}" height="${r1(dn)}" class="go"/>`;
      if ((out || 0) > CUT) b += cuts(bx, Z + dn - 6.9, w);
      if (b) s += r.plan ? `<g opacity=".5">${b}</g>` : b;
      if (up + dn > 0) bars.push({ x: bx, y: Z - up, w, h: up + dn });
    }

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
        const lab = (r, y) =>
          `<text x="${r1(clamp((r.p0 + r.p1) / 2, 13))}" y="${r1(y)}" font-size="11" text-anchor="middle" class="ink">${r.price.toFixed(2)}</text>`;
        s += lab(top, py(top.price) - 5);
        if (bot.price < top.price) s += lab(bot, py(bot.price) + 14);
      }
      s += `<text x="${r1(lo)}" y="122" font-size="11" class="ink">${new Date(localTime(date, 12, tz)).toLocaleDateString('en', { weekday: 'short', timeZone: tz || undefined })}</text>`;
      for (const hr of [6, 12, 18]) {
        const x = X(i, localTime(date, hr, tz), PAD);
        // Room for the weekday name; on narrow columns the 06 tick would run into it.
        if (x - 7 < lo + 26) continue;
        s += `<text x="${r1(x)}" y="122" font-size="11" text-anchor="middle" class="dim">${pad2(hr)}</text>`;
      }

      // Each charge and export block gets its total beside it, or nothing if that would overlap.
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
          } else {
            blk = { kwh: v, first: r, last: r };
            blocks.push(blk);
          }
        }
        for (const bl of blocks) {
          if (bl.kwh < 0.5) continue;
          const text = `${sign}${bl.kwh.toFixed(1)}`;
          const w = text.length * 6;
          const base = sign === '+' ? Z - CUT * KWH_PX + 8 : Z + 10;
          const pw = (r) => (r.p1 - r.p0) * 0.86;
          for (const x of [bl.last.p0 + pw(bl.last) + 2, bl.first.p1 - pw(bl.first) - 2 - w]) {
            const box = { x, y: base - 9, w, h: 10 };
            if (x < cx[i] + 2 || x + w > cx[i] + cw[i] - 2) continue;
            if (nowX !== null && nowX > x - 2 && nowX < x + w + 2) continue;
            if (placed.some((p) => hit(p, box)) || bars.some((b) => hit(b, box))) continue;
            placed.push(box);
            s += `<text x="${r1(x)}" y="${r1(base)}" font-size="11" class="ink">${text}</text>`;
            break;
          }
        }
      }
    }
    if (nowX !== null) s += `<line x1="${r1(nowX)}" y1="4" x2="${r1(nowX)}" y2="${Z + CUT * KWH_PX}" class="now"/>`;
    return s + '</svg>';
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
