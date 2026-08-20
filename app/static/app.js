/* LiteGate console.
   Plain browser JS: the page must run from a static mount with no build step
   and no network access beyond the gateway itself. */

const THEME_STORE = 'litegate_theme';
const $ = (id) => document.getElementById(id);
// The console authenticates with a session cookie, set by signing in. It never
// holds an API key: a key is a credential for a program, and asking a person to
// paste one into a browser is how production keys end up in shell history.
const state = { me: null, cache: {} };

/* ---------------------------------------------------------------- theme */
(function initTheme() {
  const saved = localStorage.getItem(THEME_STORE);
  if (saved) document.documentElement.setAttribute('data-theme', saved);
})();
$('theme').onclick = () => {
  const root = document.documentElement;
  const current = root.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = current === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  localStorage.setItem(THEME_STORE, next);
};

/* ---------------------------------------------------------------- utils */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
const num = (v) => (v || 0).toLocaleString();

// The API hands back naive UTC timestamps. Reading one as local time shifts it
// by the offset, which turns "tested a moment ago" into "tested in 7 hours".
const stamp = (value) => new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`);

function banner(target, kind, message) {
  $(target).innerHTML = message ? `<div class="banner ${kind}">${esc(message)}</div>` : '';
}
const showError = (m) => banner('error', 'err', m);

function flash(el, kind, message) {
  const node = typeof el === 'string' ? $(el) : el;
  node.className = 'sub';
  node.style.color = kind === 'err' ? 'var(--bad)' : kind === 'ok' ? 'var(--ok)' : 'var(--fg2)';
  node.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  if (!response.ok) {
    const err = body.error || body;
    const detail = err.details?.problems
      ? ' — ' + err.details.problems.map((p) => `${p.field}: ${p.message}`).join('; ')
      : '';
    throw new Error(`${err.code || response.status}: ${err.message || 'request failed'}${detail}`);
  }
  return body;
}
const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b || {}) });
const del = (p) => api(p, { method: 'DELETE' });
const patch = (p, b) => api(p, { method: 'PATCH', body: JSON.stringify(b || {}) });

function modal(title, html, copyText) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  const copy = $('modal-copy');
  copy.hidden = !copyText;
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(copyText); copy.textContent = 'Copied'; }
    catch { copy.textContent = 'Copy failed — select manually'; }
    setTimeout(() => { copy.textContent = 'Copy'; }, 2000);
  };
  $('modal').showModal();
}
$('modal-close').onclick = () => $('modal').close();

/* ----------------------------------------------------------------- tabs */
function showTab(name) {
  for (const btn of document.querySelectorAll('#tabs button')) {
    btn.setAttribute('aria-selected', String(btn.dataset.tab === name));
  }
  for (const sec of document.querySelectorAll('main > section')) {
    sec.hidden = sec.id !== `tab-${name}`;
  }
  const loaders = {
    account: loadAccount, models: loadModels, assistant: loadAssistant,
    access: loadAccess, quota: loadQuota, tools: loadTools,
  };
  if (loaders[name]) loaders[name]().catch((e) => showError(e.message));
}
for (const btn of document.querySelectorAll('#tabs button')) {
  btn.onclick = () => showTab(btn.dataset.tab);
}

// บทบาทของคนที่ล็อกอินอยู่ — ตารางบางอันตัดสินใจวาดปุ่มจากตรงนี้ ไม่ใช่จากการซ่อนแท็บ
let myRole = '';

function applyRole(role) {
  myRole = role;
  const admin = role === 'admin';
  const staff = admin || role === 'manager';
  for (const btn of document.querySelectorAll('#tabs button')) {
    if (btn.hasAttribute('data-admin')) btn.hidden = !admin;
    if (btn.hasAttribute('data-staff')) btn.hidden = !staff;
  }
  $('health-wrap').hidden = !admin;
  $('usage-wrap').hidden = !staff;
}

/* ------------------------------------------------------------ dashboard */
// Inline SVG chart primitives — no CDN, so they draw the same on an air-gapped
// install. Colours are passed in from the --c1..c4 family so a chart belongs to
// the same page as the gradients, not to a palette of its own.
function compact(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2).replace(/\.?0+$/, '') + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return String(n);
}

function sparkSvg(vals, color) {
  const clean = vals.filter((v) => Number.isFinite(v));
  if (clean.length < 2) return '';
  const w = 120, h = 30, p = 3, mn = Math.min(...clean), mx = Math.max(...clean), r = (mx - mn) || 1;
  const pt = clean.map((v, i) => [p + i * (w - 2 * p) / (clean.length - 1), h - p - (v - mn) / r * (h - 2 * p)]);
  const line = 'M' + pt.map((q) => q.map((n) => n.toFixed(1)).join(' ')).join(' L ');
  const area = `M ${pt[0][0]} ${h} L ` + pt.map((q) => q.map((n) => n.toFixed(1)).join(' ')).join(' L ')
    + ` L ${pt[pt.length - 1][0]} ${h} Z`;
  const id = 'sp' + Math.random().toString(36).slice(2, 7);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs><path d="${area}" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${pt[pt.length - 1][0].toFixed(1)}" cy="${pt[pt.length - 1][1].toFixed(1)}" r="2.4" fill="${color}"/></svg>`;
}

function ringSvg(pct, color, size = 64) {
  const r = (size - 8) / 2, c = size / 2, C = 2 * Math.PI * r, on = C * Math.min(100, pct) / 100;
  return `<svg class="ring" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true">
    <circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="var(--sunken)" stroke-width="6"/>
    <circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="${color}" stroke-width="6" stroke-linecap="round"
      stroke-dasharray="${on.toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 ${c} ${c})"/>
    <text x="${c}" y="${c + 4}" text-anchor="middle" font-size="14" font-weight="700" fill="var(--fg)">${pct}%</text></svg>`;
}

function areaSvg(vals) {
  const clean = vals.map((v) => Number(v) || 0);
  if (clean.length < 2) return '<div class="empty">Not enough data yet</div>';
  const w = 620, h = 210, pl = 6, ptop = 16, pb = 22, mx = Math.max(...clean, 1);
  const pt = clean.map((v, i) => [pl + i * (w - 2 * pl) / (clean.length - 1), h - pb - (v / mx) * (h - ptop - pb)]);
  let grid = '';
  for (let g = 0; g <= 3; g++) {
    const y = (ptop + (h - ptop - pb) * g / 3).toFixed(1);
    grid += `<line x1="0" x2="${w}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-width="1"/>`;
  }
  const line = 'M' + pt.map((q) => q.map((n) => n.toFixed(1)).join(' ')).join(' L ');
  const area = `M ${pt[0][0]} ${h - pb} L ` + pt.map((q) => q.map((n) => n.toFixed(1)).join(' ')).join(' L ')
    + ` L ${pt[pt.length - 1][0]} ${h - pb} Z`;
  const last = pt[pt.length - 1];
  return `<svg class="areachart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="usage over time">
    <defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--c2)" stop-opacity=".28"/><stop offset="1" stop-color="var(--c2)" stop-opacity="0"/></linearGradient>
      <linearGradient id="al" x1="0" y1="0" x2="${w}" y2="0"><stop offset="0" stop-color="var(--c1)"/><stop offset="1" stop-color="var(--c3)"/></linearGradient></defs>
    ${grid}<path d="${area}" fill="url(#ag)"/>
    <path d="${line}" fill="none" stroke="url(#al)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.6" fill="var(--c3)" stroke="var(--card)" stroke-width="2"/></svg>`;
}

const GAUGE_COLORS = ['var(--c2)', 'var(--c3)', 'var(--c1)', 'var(--c4)'];

function renderQuota(me) {
  renderQuotaInto('quota', me);
}

// Quota as radial gauges: a member reads "how much is left" at a glance, which a
// table of raw numbers never gave them. Colour turns to warn/bad past 80/100%.
function renderQuotaInto(target, me) {
  const { used, limits } = me.quota;
  const rows = [
    ['Requests', used.requests, limits.max_requests],
    ['Input tokens', used.input_tokens, limits.max_input_tokens],
    ['Output tokens', used.output_tokens, limits.max_output_tokens],
    ['Images', used.images, limits.max_images],
  ].filter(([, u, lim]) => lim || u);
  const cell = ([label, u, lim], i) => {
    const pct = lim ? Math.min(100, Math.round((u / lim) * 100)) : null;
    const color = pct >= 100 ? 'var(--bad)' : pct >= 80 ? 'var(--warn)' : GAUGE_COLORS[i % GAUGE_COLORS.length];
    return `<div class="gaugecell">
      ${pct != null ? ringSvg(pct, color) : '<div class="ring-none">∞</div>'}
      <div class="g-meta"><b>${label}</b><div class="g-q">${num(u)}${lim ? ` / ${num(lim)}` : ' · unlimited'}</div></div></div>`;
  };
  $(target).innerHTML = `<div class="gaugewrap">${rows.map(cell).join('')}</div>
    <div class="g-foot">Window ${esc(me.quota.window)} · resets ${new Date(me.quota.window_end).toLocaleString()}</div>`;
}

// Inline because a gateway may run air-gapped: no icon font, no CDN, no build.
// Stroked rather than filled so one set reads correctly in both themes.
const ICONS = {
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/>'
     + '<circle cx="12" cy="12" r="3"/>',
  image: '<circle cx="8.5" cy="9.5" r="1.5"/><path d="M3 15l4.5-4.5L15 18"/>'
       + '<rect x="3" y="3" width="18" height="18" rx="2"/>',
  code: '<path d="M15.5 18L21 12l-5.5-6M8.5 6L3 12l5.5 6"/>',
  agent: '<rect x="5" y="8" width="14" height="12" rx="2"/><path d="M12 8V4M9 14h.01M15 14h.01"/>'
       + '<circle cx="12" cy="3" r="1.5"/>',
  reasoning: '<path d="M9 20h6M10 23h4"/>'
           + '<path d="M12 2a6.5 6.5 0 0 0-4 11.6V17h8v-3.4A6.5 6.5 0 0 0 12 2z"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',

  // The mark: many clients on the left, many machines on the right, one door
  // between them. That is the whole product in one glyph.
  gateway: '<path d="M4 7h4M4 12h4M4 17h4M16 7h4M16 12h4M16 17h4"/>'
         + '<rect x="9" y="4" width="6" height="16" rx="2"/><path d="M12 9v6"/>',

  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/>'
           + '<rect x="14" y="3" width="7" height="5" rx="1.5"/>'
           + '<rect x="14" y="12" width="7" height="9" rx="1.5"/>'
           + '<rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  account: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  models: '<path d="M12 3l8 4.5-8 4.5-8-4.5z"/><path d="M4 12l8 4.5 8-4.5"/>'
        + '<path d="M4 16.5L12 21l8-4.5"/>',
  assistant: '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6z"/>'
           + '<path d="M18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z"/>',
  keys: '<circle cx="8" cy="12" r="4"/><path d="M12 12h9M18 12v3.5M15.5 12v2.5"/>',
  quota: '<path d="M12 20a8 8 0 1 1 8-8"/><path d="M12 12l4.5-3"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
  theme: '<circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2'
       + 'M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
  signout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/>'
         + '<path d="M10 8l-4 4 4 4M6 12h9"/>',

  // Section headings. Same set as the nav, so a heading and the tab that leads
  // to it are drawn the same way.
  health: '<path d="M3 12h4l2.5-6 4 12L16 12h5"/>',
  usage: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 19a6.5 6.5 0 0 1 13 0"/>'
        + '<path d="M16 5.5a3.2 3.2 0 0 1 0 5M18 19a6.6 6.6 0 0 0-1.6-4.3"/>',
  workspace: '<path d="M3 9.5L12 4l9 5.5V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>'
           + '<path d="M9 21v-6h6v6"/>',
  backends: '<rect x="3" y="4" width="18" height="6" rx="1.5"/>'
          + '<rect x="3" y="14" width="18" height="6" rx="1.5"/>'
          + '<path d="M7 7h.01M7 17h.01"/>',
  capabilities: '<path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"/>',
  suitability: '<path d="M20 6L9 17l-5-5"/>',
  deploy: '<circle cx="12" cy="12" r="3"/>'
        + '<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1'
        + 'M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1"/>',
  trending: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  bundle: '<path d="M3 8l9-4 9 4-9 4z"/><path d="M3 8v8l9 4 9-4V8"/><path d="M12 12v8"/>',
  gauge: '<path d="M4.5 18a8 8 0 1 1 15 0"/><path d="M12 14l4-3.5"/><circle cx="12" cy="14" r="1.2"/>',
  pulse: '<path d="M3 12h3.5L9 5l4 14 2.5-7H21"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  text: '<path d="M4 6.5V5h16v1.5M12 5v14M9 19h6"/>',
  tools: '<path d="M14.7 6.3a4 4 0 0 0 5.2 5.2l-7.4 7.4a2.5 2.5 0 0 1-3.5-3.5z"/>'
       + '<path d="M14.7 6.3 12 3.6M6.5 14.5 3.8 11.8"/>',
  audio: '<path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="M15.5 9.5a3.5 3.5 0 0 1 0 5"/>'
       + '<path d="M18 7a7 7 0 0 1 0 10"/>',
  plug: '<path d="M9 3v6M15 3v6"/><path d="M6 9h12v3a6 6 0 0 1-12 0z"/><path d="M12 18v3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  swap: '<path d="M4 7h11M12 4l3 3-3 3M20 17H9M12 14l-3 3 3 3"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9.5 12l2 2 3.5-3.5"/>',
};

// ป้ายความสามารถมาจากเซิร์ฟเวอร์เป็นคำเดียว ๆ (Text/Code/Tools/…) — ไอคอนช่วยให้
// กวาดตาทั้งตารางแล้วเห็นว่าโมเดลไหนทำอะไรได้ โดยไม่ต้องอ่านทีละคำ
// กางไว้แล้วรีเฟรชทีเดียวยุบหมด แปลว่าต้องไปไล่กดใหม่ทุกครั้งที่แก้อะไรสักอย่าง
// แล้วกด Reload registry · เก็บไว้ในเบราว์เซอร์ ไม่ใช่ที่เซิร์ฟเวอร์ เพราะเป็น
// มุมมองของคนที่นั่งอยู่ ไม่ใช่การตั้งค่าของระบบ
const OPEN_KEY = 'litegate:models:open';

function openAliases() {
  try { return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || '[]')); }
  catch { return new Set(); }
}

function rememberOpen(set) {
  try { localStorage.setItem(OPEN_KEY, JSON.stringify([...set])); } catch { /* โหมดส่วนตัว */ }
}

const BADGE_ICONS = {
  Text: 'text', Image: 'image', Audio: 'audio', Code: 'code',
  Tools: 'tools', Reasoning: 'reasoning', Agent: 'agent',
  OpenAI: 'plug', Anthropic: 'plug', images: 'image',
};

const badgeChip = (label) =>
  `<span class="badge chip">${BADGE_ICONS[label] ? icon(BADGE_ICONS[label], 12) : ''}${esc(label)}</span>`;

const icon = (name, size = 20) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
        stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true">${ICONS[name] || ICONS.chat}</svg>`;

// Markup carries `data-icon="name"` and the drawing is filled in here, so the
// set stays in one place instead of being pasted into the HTML as well.
function paintIcons(root = document) {
  for (const slot of root.querySelectorAll('[data-icon]')) {
    slot.innerHTML = icon(slot.dataset.icon, Number(slot.dataset.iconSize) || 18);
  }
}

// What a model is *for*, in one glyph. Read from what it can do rather than
// from its name, so a model added later gets the right icon without an edit.
function modelGlyph(model) {
  const badges = new Set(model.badges || []);
  if (model.supports_images || badges.has('Image')) return 'image';
  if (badges.has('Agent')) return 'agent';
  if (badges.has('Reasoning')) return 'reasoning';
  if (badges.has('Code')) return 'code';
  return 'chat';
}

// One row per model, closed by default. `details` rather than a click handler:
// the browser gives keyboard access, the open state and the semantics for free,
// and it still works if the script fails.
function modelRow(model) {
  const facts = [
    ['Context', model.context_tokens ? `${num(model.context_tokens)} tokens` : model.context],
    ['Max output', model.max_output_tokens ? `${num(model.max_output_tokens)} tokens` : '—'],
    ['Protocols', (model.protocols || []).join(' · ') || '—'],
    ['Images', model.supports_images ? 'ได้' : 'ไม่ได้'],
    ['Tools', model.supports_tools ? 'ได้' : 'ไม่ได้'],
    ['Streaming', model.supports_streaming ? 'ได้' : 'ไม่ได้'],
  ];
  return `<details class="model-row">
    <summary>
      <span class="model-glyph">${icon(modelGlyph(model))}</span>
      <span class="model-name">
        <strong>${esc(model.name)}</strong>
        <code>${esc(model.id)}</code>
      </span>
      <span class="model-badges">
        ${(model.badges || []).map((b) => `<span class="badge">${esc(b)}</span>`).join('')}
      </span>
      <span class="model-meta">
        ${esc(model.context)}
        ${model.claude_code_ready ? '<span class="pill ok">Claude Code Ready</span>' : ''}
      </span>
      <span class="model-chevron">${icon('chevron', 18)}</span>
    </summary>
    <div class="model-detail">
      ${model.description ? `<p class="model-desc">${esc(model.description)}</p>` : ''}
      <dl class="model-facts">
        ${facts.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}
      </dl>
      <div class="model-use">
        <span class="hint">เรียกใช้โดยตั้ง</span>
        <code class="mono">model = "${esc(model.id)}"</code>
        <span class="hint">ที่</span>
        <code class="mono">${esc(location.origin)}/v1</code>
        <button class="ghost small" data-copy-model="${esc(model.id)}"
          >${icon('copy', 14)} คัดลอกชื่อ</button>
      </div>
    </div>
  </details>`;
}

function modelList(models, empty) {
  return models.length
    ? `<div class="model-list">${models.map(modelRow).join('')}</div>`
    : `<div class="empty">${empty}</div>`;
}

// A catalogue that has quietly shrunk reads as models having disappeared, and
// the first guess is that the gateway is broken. Naming the rule that narrowed
// it turns "where did they go" into something the reader can act on.
function accessNote(access) {
  if (!access?.restricted) return '';
  return `<p class="hint" style="margin:0 0 12px">
    เห็นเท่านี้เพราะถูกจำกัดโดย <strong>${esc(access.reason)}</strong> ·
    ต้องใช้ตัวอื่นให้ติดต่อผู้ดูแลของคุณ</p>`;
}

// Delegated once, on the document: both the dashboard catalogue and the
// account page re-render their lists, and a handler per button would have to
// be re-attached every time one of them did.
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-copy-model]');
  if (!button) return;
  event.preventDefault();
  navigator.clipboard.writeText(button.dataset.copyModel);
  const was = button.innerHTML;
  button.textContent = 'คัดลอกแล้ว';
  setTimeout(() => { button.innerHTML = was; }, 1200);
});

// Close any open row overflow menu (⋯) when clicking elsewhere. Registered once;
// the toggle button stops propagation so its own click does not immediately close it.
document.addEventListener('click', (event) => {
  for (const menu of document.querySelectorAll('.rowmenu.open')) {
    if (!menu.contains(event.target)) menu.classList.remove('open');
  }
});

// หมวดในแค็ตตาล็อกยุบได้ และจำไว้ว่าผู้ใช้เปิดอันไหนค้างไว้
//
// โมเดลตัวเดียวโผล่ได้หลายหมวด (claude-sonnet อยู่ทั้ง General, Vision, Reasoning)
// พอ registry โตขึ้น หน้านี้จึงยาวหลายจอโดยที่เนื้อหาซ้ำกันเป็นส่วนใหญ่ · ตั้งต้นกาง
// หมวดแรกหมวดเดียว ที่เหลือยุบ — คนที่มาหา alias ไปวางใน client เห็นครบตั้งแต่จอแรก
const CATALOG_FOLD_KEY = 'litegate:catalog-open';

function openSections() {
  try { return new Set(JSON.parse(localStorage.getItem(CATALOG_FOLD_KEY) || 'null') ?? []); }
  catch { return new Set(); }
}

function rememberSections(set) {
  try { localStorage.setItem(CATALOG_FOLD_KEY, JSON.stringify([...set])); }
  catch { /* โหมดส่วนตัว */ }
}

function renderCatalog(data) {
  const remembered = localStorage.getItem(CATALOG_FOLD_KEY);
  const open = openSections();
  const sections = data.sections.map((s, index) => {
    // ยังไม่เคยเลือกเอง = กางหมวดแรก · เลือกแล้วเคารพสิ่งที่เลือกไว้ รวมถึง "ยุบหมด"
    const isOpen = remembered === null ? index === 0 : open.has(s.title);
    return `
    <details class="model-section-box" data-section="${esc(s.title)}"${isOpen ? ' open' : ''}>
      <summary class="model-section">${esc(s.title)}
        <span class="hint">${s.models.length} โมเดล</span></summary>
      ${modelList(s.models, 'ไม่มีโมเดลในหมวดนี้')}
    </details>`;
  }).join('');
  $('catalog').innerHTML = accessNote(data.access)
    + (sections || '<div class="empty">ยังไม่มีโมเดลที่เปิดให้คุณใช้ · ติดต่อผู้ดูแลของคุณ</div>');

  for (const box of $('catalog').querySelectorAll('[data-section]')) {
    box.addEventListener('toggle', () => {
      const chosen = openSections();
      box.open ? chosen.add(box.dataset.section) : chosen.delete(box.dataset.section);
      rememberSections(chosen);
    });
  }
}


// ── "ถ้าไม่ได้รันเอง จะจ่ายเท่าไร" ─────────────────────────────────────────────
//
// โรงเรียนที่ลงทุนซื้อเครื่องมารันเองต้องตอบผู้บริหารให้ได้ว่าคุ้มไหม · เรานับ token
// ครบอยู่แล้ว ขาดแค่ตารางราคา — ตัวเลขนี้จึงได้มาโดยไม่ต้องเก็บอะไรเพิ่ม
let savingsBaselines = null;

async function loadSavings(baseline) {
  const body = $('savings-body');
  if (!body) return;
  if (!savingsBaselines) {
    const meta = await api('/admin/usage/savings/baselines');
    savingsBaselines = meta.data || [];
    const select = $('savings-baseline');
    select.innerHTML = savingsBaselines
      .map((b) => `<option value="${esc(b.id)}">${esc(b.label)}</option>`).join('');
    select.value = baseline || meta.default;
    select.onchange = () => loadSavings(select.value);
  }
  const chosen = baseline || $('savings-baseline').value;
  const d = await api(`/admin/usage/savings?days=14&baseline=${encodeURIComponent(chosen)}`);
  renderSavings(d);
}

function renderSavings(d) {
  // ทราฟฟิกน้อย ๆ ปัดเป็น $0.00 จะอ่านเหมือนรายงานพัง — บอกว่า "น้อยกว่าหนึ่งเซนต์" ตรง ๆ
  const money = (n) => {
    const value = Number(n || 0);
    if (value > 0 && value < 0.01) return '<$0.01';
    return '$' + value.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  const rows = (d.by_model || []).slice(0, 8).map((r) => `
    <tr><td>${esc(r.model)}</td>
        <td class="num">${(r.requests || 0).toLocaleString()}</td>
        <td class="num">${((r.input_tokens || 0) + (r.output_tokens || 0)).toLocaleString()}</td>
        <td class="num">${money(r.would_have_cost_usd)}</td></tr>`).join('');
  $('savings-body').innerHTML = `
    <div class="savings-head">
      <div class="savings-big">${money(d.would_have_cost_usd)}</div>
      <div class="hint">ใน ${d.window_days} วัน · ${(d.requests || 0).toLocaleString()} คำขอ ·
        เทียบกับ ${esc((d.baseline || {}).label || '')}</div>
    </div>
    ${rows ? `<table class="tbl"><thead><tr><th>โมเดล</th><th class="num">คำขอ</th>
        <th class="num">token</th><th class="num">ถ้าจ่าย</th></tr></thead>
      <tbody>${rows}</tbody></table>` : '<div class="empty">ยังไม่มีทราฟฟิกในช่วงนี้</div>'}
    <p class="hint" style="margin:10px 0 0">${esc(d.caveat || '')} ·
      ราคา ณ ${esc(d.prices_updated || '')}</p>`;
}



// ── model="auto" — อันดับตอนนี้ ────────────────────────────────────────────────
//
// ฟีเจอร์ที่ไม่โผล่ในหน้าเว็บเท่ากับไม่มีอยู่จริงสำหรับคนที่ใช้ผ่านคอนโซลล้วน ๆ ·
// และ "เกตเวย์เลือกให้" จะเชื่อถือได้ก็ต่อเมื่ออธิบายได้ว่าเลือกจากอะไร
async function loadAutoPreview() {
  const body = $('auto-body');
  if (!body) return;
  const d = await api('/admin/auto/preview?prompt_tokens=1000');
  const rows = (d.ranked || []).map((r) => `
    <tr${r.rank === 1 ? ' class="auto-win"' : ''}>
      <td>${r.rank}</td><td>${esc(r.alias)}</td>
      <td class="num">${r.output_tps != null ? r.output_tps.toFixed(1) : '—'}</td>
      <td class="num">${r.ttft_ms != null ? r.ttft_ms + ' ms' : '—'}</td>
      <td class="num">${(r.context_tokens || 0).toLocaleString()}</td>
      <td class="num">${r.samples || 0}</td></tr>`).join('');
  body.innerHTML = rows ? `
    <div class="hint" style="margin-bottom:8px">ตอนนี้จะเลือก
      <strong>${esc(d.chosen || '—')}</strong> — ${esc(d.reason || '')}</div>
    <table class="tbl"><thead><tr><th>#</th><th>โมเดล</th>
      <th class="num">tok/s</th><th class="num">TTFT</th>
      <th class="num">context</th><th class="num">ตัวอย่าง</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <p class="hint" style="margin:8px 0 0">ตัวเลขมาจากทราฟฟิกจริงที่ผ่านเกตเวย์ ·
      ต้องเห็นอย่างน้อย ${d.min_samples} คำขอก่อนถึงจะนับ ตัวที่ยังไม่ถึงจะอยู่ท้ายแถว
      แต่ยังถูกเลือกได้ถ้าไม่มีตัวอื่น</p>`
    : `<div class="empty">${esc(d.reason || 'ยังไม่มีโมเดลที่รับคำขอตัวอย่างนี้ได้')}</div>`;
}


// ── พับหมวดในหน้า ────────────────────────────────────────────────────────────
//
// แท็บ Access & keys มีสี่ส่วนยาว ๆ ต่อกัน (People, API keys, Access groups,
// Workspaces) · พอผู้ใช้จริงมีคนเป็นสิบและ key เป็นสิบใบ หน้าเดียวยาวหลายจอ แล้วส่วน
// ที่อยากดูจริงอยู่ล่างสุดเสมอ
//
// ทำเป็นกลไกกลางแทนที่จะแก้ทีละหมวด: หมวดไหนอยากพับได้ก็ใส่ data-fold-section="<คีย์>"
// ที่ div ครอบ แล้วที่เหลือทำงานเอง — หมวดใหม่ในอนาคตจะได้ไม่ต้องเขียนโค้ดซ้ำ
const SECTION_FOLD_KEY = 'litegate:sections-folded';

function foldedSections() {
  try { return new Set(JSON.parse(localStorage.getItem(SECTION_FOLD_KEY) || '[]')); }
  catch { return new Set(); }
}

function rememberFolded(set) {
  try { localStorage.setItem(SECTION_FOLD_KEY, JSON.stringify([...set])); }
  catch { /* โหมดส่วนตัว */ }
}

// ย่อ/กางทุกหมวดในแท็บที่เปิดอยู่ทีเดียว
//
// พับทีละหมวดยังช้าเมื่อแท็บหนึ่งมีสี่ห้าหมวด · ปุ่มนี้ทำงานเฉพาะแท็บที่เห็นอยู่
// ไม่ไปยุ่งกับแท็บอื่น เพราะสถานะที่ผู้ใช้ตั้งใจไว้ในแท็บอื่นไม่ควรถูกลบด้วยการกดปุ่มนี้
function foldAllInTab(folded) {
  const tab = document.querySelector('section[id^="tab-"]:not([hidden])');
  if (!tab) return;
  const chosen = foldedSections();
  for (const section of tab.querySelectorAll('[data-fold-section]')) {
    folded ? chosen.add(section.dataset.foldSection)
           : chosen.delete(section.dataset.foldSection);
  }
  rememberFolded(chosen);
  applyFoldState(tab);
}

function applyFoldState(root = document) {
  const chosen = foldedSections();
  for (const section of root.querySelectorAll('[data-fold-section]')) {
    const body = section.querySelector(':scope > .fold-body');
    const button = section.querySelector(':scope > * .fold-toggle, :scope > .fold-toggle');
    if (!body || !button) continue;
    const isFolded = chosen.has(section.dataset.foldSection);
    body.hidden = isFolded;
    button.textContent = isFolded ? '+' : '−';
    button.setAttribute('aria-expanded', String(!isFolded));
  }
}

function setupFoldSections(root = document) {
  const folded = foldedSections();
  for (const section of root.querySelectorAll('[data-fold-section]')) {
    if (section.dataset.foldReady) continue;
    section.dataset.foldReady = '1';
    const key = section.dataset.foldSection;

    // หัวข้อคือลูกคนแรก · ที่เหลือคือเนื้อ — ห่อไว้ก้อนเดียวจะได้ซ่อน/แสดงทีเดียว
    const header = section.firstElementChild;
    if (!header) continue;
    const body = document.createElement('div');
    body.className = 'fold-body';
    while (header.nextSibling) body.appendChild(header.nextSibling);
    section.appendChild(body);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'fold-toggle';
    button.title = 'ย่อ/กางหมวดนี้';
    // ปุ่มไปอยู่ใน h2 เพื่อให้เรียงในบรรทัดเดียวกับชื่อหมวด ไม่ว่าหัวข้อจะเป็น h2 เดี่ยว
    // หรืออยู่ใน .bar ที่มีปุ่มอื่นอยู่ด้วย
    const title = header.tagName === 'H2' ? header : header.querySelector('h2') || header;
    title.insertBefore(button, title.firstChild);

    const apply = () => {
      const isFolded = folded.has(key);
      body.hidden = isFolded;
      button.textContent = isFolded ? '+' : '−';
      button.setAttribute('aria-expanded', String(!isFolded));
    };
    button.onclick = () => {
      folded.has(key) ? folded.delete(key) : folded.add(key);
      rememberFolded(folded);
      apply();
    };
    apply();
  }
}

function renderHealth(report) {
  const rows = Object.values(report);
  $('health').innerHTML = `
    <tr><th>Model</th><th>Endpoint</th><th>Type</th><th>Status</th>
        <th class="num">In flight</th><th class="num">Requests</th><th>Last error</th></tr>
    ${rows.map((r) => `<tr>
      <td><code>${esc(r.model)}</code></td><td>${esc(r.endpoint)}</td><td>${esc(r.server_type)}</td>
      <td><span class="pill ${r.healthy ? 'ok' : 'err'}">${r.healthy ? 'healthy' : 'down'}</span></td>
      <td class="num">${r.in_flight}/${r.max_concurrency}</td>
      <td class="num">${num(r.total_requests)}</td>
      <td class="empty">${esc((r.last_error || '').slice(0, 70))}</td></tr>`).join('')}`;
}

// Usage for staff: four summary cards (each with a real sparkline from the daily
// series), the daily-requests area chart, and a per-model share of requests.
// Every number is a real aggregate — nothing is drawn that the data can't back.
function renderUsage(summary, daily) {
  const series = (daily && daily.series) || [];
  const totalReq = summary.by_model.reduce((a, r) => a + r.requests, 0);
  const totalTok = summary.by_model.reduce(
    (a, r) => a + r.text_input_tokens + r.visual_input_tokens + r.output_tokens, 0);
  const wLat = totalReq
    ? Math.round(summary.by_model.reduce((a, r) => a + r.avg_latency_ms * r.requests, 0) / totalReq)
    : 0;
  const card = (label, iconName, val, foot, spark) => `
    <div class="statcard"><div class="lbl"><span class="ic">${icon(iconName, 15)}</span>${label}</div>
      <div class="val">${val}</div><div class="foot">${foot}</div>${spark || ''}</div>`;
  $('usage-stats').innerHTML =
    card('Requests', 'usage', num(totalReq), `${summary.window_days} days`,
      sparkSvg(series.map((d) => d.requests), 'var(--c2)'))
    + card('Tokens', 'text', compact(totalTok), 'in + out',
      sparkSvg(series.map((d) => d.input_tokens + d.output_tokens), 'var(--c3)'))
    + card('Models used', 'models', String(summary.by_model.length), 'in this window', '')
    + card('Avg latency', 'clock', wLat ? `${num(wLat)}<small>ms</small>` : '—',
      'weighted by requests', '');

  $('usage-area').innerHTML = areaSvg(series.map((d) => d.requests));

  const top = [...summary.by_model].sort((a, b) => b.requests - a.requests);
  const max = top.reduce((m, r) => Math.max(m, r.requests), 0) || 1;
  const grad = [
    'linear-gradient(90deg,var(--c1),var(--c2))',
    'linear-gradient(90deg,var(--c2),var(--c3))',
    'linear-gradient(90deg,var(--c3),var(--c4))',
  ];
  const bars = top.length
    ? `<div class="barlist">${top.map((r, i) => `<div class="bar-row">
        <div class="t"><b>${esc(r.model)}</b><span>${num(r.requests)} requests · ${
          totalReq ? Math.round(r.requests / totalReq * 100) : 0}%</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${
          Math.max(2, Math.round(r.requests / max * 100))}%;background:${grad[i % grad.length]}"></div></div>
      </div>`).join('')}</div>`
    : '<div class="empty">ยังไม่มีการใช้งานin this window</div>';
  $('usage-bars').innerHTML = bars + (summary.errors.length
    ? `<div class="g-foot">Errors: ${
      summary.errors.map((e) => esc(e.code) + ' ×' + e.count).join(', ')}</div>`
    : '');
}

/* --------------------------------------------------------- client tools */
// The gateway mirrors these third-party tools and offers the OS-correct build,
// pre-verified. See app/tools/ (mirror engine) and /admin/tools (the API).
const TOOL_LOOK = {
  'cc-switch': { icon: 'swap', grad: 'linear-gradient(135deg,#3b82f6,#8b5cf6)' },
  rtk: { icon: 'bundle', grad: 'linear-gradient(135deg,#06b6d4,#3b82f6)' },
};
const OS_LABEL = { windows: 'Windows', macos: 'macOS', linux: 'Linux' };

function detectOS() {
  const s = `${navigator.platform} ${navigator.userAgent}`;
  if (/Win/i.test(s)) return 'windows';
  if (/Mac/i.test(s)) return 'macos';
  if (/Linux|X11|Android/i.test(s)) return 'linux';
  return '';
}

async function loadTools() {
  const { tools } = await api('/admin/tools');
  if (!tools.length) {
    $('tools').innerHTML = '<div class="empty">No tools in the registry</div>';
    return;
  }
  const myOS = detectOS();
  const card = (t) => {
    const look = TOOL_LOOK[t.slug] || { icon: 'bundle', grad: 'var(--g-mix)' };
    const dl = t.assets.filter((a) => a.download);
    const proven = t.assets.filter((a) => a.verified === true).length;
    const platforms = [...new Set(dl.map((a) => a.platform))];
    const pick = dl.find((a) => a.platform === myOS) || dl[0];
    const verifyPill = `<span class="pill ok">${icon('shield', 12)} Verified · ${
      t.verify.method === 'minisign' ? 'minisign' : 'SHA-256'}</span>`;
    const ver = t.published
      ? `<span class="pill mute">v${esc(t.published)}</span>`
      : '<span class="pill warn">not published</span>';
    return `<div class="card tool">
      <div class="tool-top">
        <div class="tool-logo" style="background:${look.grad}">${icon(look.icon, 22)}</div>
        <div style="flex:1;min-width:0">
          <div class="tool-name">${esc(t.name)} ${ver}</div>
          <div class="tool-desc">${esc(t.summary || '')}</div>
        </div>
      </div>
      <div class="meta-row">${t.published ? verifyPill : ''}<span class="pill mute">${esc(t.license.spdx)}</span></div>
      ${platforms.length ? `<div class="meta-row">${platforms.map((p) =>
        `<span class="os">${esc(OS_LABEL[p] || p)}</span>`).join('')}</div>` : ''}
      <div class="verline">${t.published
        ? `${icon('shield', 13)} ${proven}/${dl.length} assets verified · published`
        : `Not mirrored yet — run on the server: <code>python -m app.tools sync ${esc(t.slug)}</code>`}</div>
      <div class="tool-foot">
        ${pick ? `<a class="tbtn primary" href="${esc(pick.download)}" download>${
          icon('download', 15)} Download for ${esc(OS_LABEL[pick.platform] || pick.platform)}</a>` : ''}
        ${t.published ? `<button class="tbtn ghost" data-connect="${esc(t.slug)}">${
          icon('plug', 15)} Connect</button>` : ''}
        <a class="tbtn ghost" href="${esc(t.homepage || `https://github.com/${t.repo}`)}"
           target="_blank" rel="noopener">Details</a>
      </div>
    </div>`;
  };
  $('tools').innerHTML = `<div class="tools-grid">${tools.map(card).join('')}</div>`;

  // "Connect" is only drawn on published tools, so a click always has a mirrored
  // build to point at. The tool row is looked up here where the list is in hand,
  // rather than re-fetched, so the button carries only the slug.
  for (const btn of $('tools').querySelectorAll('[data-connect]')) {
    const tool = tools.find((x) => x.slug === btn.dataset.connect);
    btn.onclick = () => connectTool(tool).catch((e) => showError(e.message));
  }
}

// The first alias a person may actually call — a real value makes the snippet
// runnable as handed over, instead of a placeholder they have to go find.
function firstAlias(catalog) {
  for (const section of catalog?.sections || []) {
    if (section.models?.length) return section.models[0].id;
  }
  return '';
}

// One click mints a personal key and shows the exact settings that wire a
// downloaded client (Claude Code / cc-switch) at THIS gateway. The key comes
// back once from the same endpoint the account page uses, so it is shown here
// and nowhere else — not logged, not put in a URL.
async function connectTool(t) {
  const today = new Date().toISOString().slice(0, 10);
  const created = await post('/v1/me/api-keys', { name: `cc-switch ${today}` });
  const key = created.api_key;
  const baseUrl = `${location.origin}/v1`;

  // The catalogue is the list of models this person is allowed to call. Cached
  // by the account page; fetched here on demand for anyone who lands on Tools
  // first. If it cannot be read, fall back to a placeholder they can edit.
  if (!state.cache.catalog) {
    try { state.cache.catalog = await api('/v1/catalog'); } catch { /* placeholder below */ }
  }
  const model = firstAlias(state.cache.catalog) || 'your-model-alias';

  const settings = JSON.stringify(
    { env: { ANTHROPIC_BASE_URL: baseUrl, ANTHROPIC_AUTH_TOKEN: key, ANTHROPIC_MODEL: model } },
    null, 2,
  );
  const href = `data:application/json,${encodeURIComponent(settings)}`;

  const row = (label, value) => `<div class="connect-row">
    <div class="connect-key">${label}</div>
    <div class="connect-val">
      <code class="mono">${esc(value)}</code>
      <button class="ghost small" data-copy-model="${esc(value)}">${icon('copy', 14)} Copy</button>
    </div>
  </div>`;

  modal(`Connect — ${esc(t.name)}`, `
    <p class="hint" style="margin:0 0 14px">
      ตั้งค่าให้ ${esc(t.name)} ชี้มาที่เกตเวย์นี้ · คีย์นี้เพิ่งถูกสร้างและจะแสดงเพียงครั้งเดียว
      ผูกกับบัญชีของคุณคนเดียว เพิกถอนได้ที่ My account → API keys</p>
    ${row('ANTHROPIC_BASE_URL', baseUrl)}
    ${row('ANTHROPIC_AUTH_TOKEN', key)}
    ${row('ANTHROPIC_MODEL', model)}
    <div class="tool-foot" style="margin-top:16px">
      <a class="tbtn primary" download="settings.json" href="${href}">${
        icon('download', 15)} Download settings.json</a>
      <span class="hint" style="margin:0">วางไว้ที่ <code>~/.claude/settings.json</code></span>
    </div>`);
}

/* --------------------------------------------------------------- models */
const STATUS_PILL = { pass: 'ok', fail: 'err', degraded: 'warn', not_tested: 'mute' };

async function loadModels() {
  // Cheap, and the answer decides whether findings can offer a button at all.
  // Failure is not an error: no deploy tool is the normal case.
  try { state.lmds = await api('/admin/integrations/lmds'); }
  catch { state.lmds = { configured: false }; }

  const [registry, status] = await Promise.all([
    api('/admin/models'), api('/admin/registry/status'),
  ]);
  state.cache.models = registry.data;
  state.cache.writable = status.writable;

  if (!status.writable) {
    banner('registry-note', 'warn',
      `The registry at ${status.config_dir} is read-only, so "Save to registry" is unavailable. ` +
      'Use Preview YAML and commit the file to git.');
  } else if (registry.errors?.length) {
    banner('registry-note', 'err', 'Registry errors: ' + registry.errors.join(' | '));
  } else {
    banner('registry-note', '', '');
  }

  const compat = await Promise.all(
    registry.data.map((m) => api(`/admin/models/${encodeURIComponent(m.alias)}/compatibility`)
      .catch(() => ({ status: 'NOT TESTED', results: [] }))),
  );

  // แถวบนคือสิ่งที่ client เรียก · แถวย่อยคือเครื่องจริงที่ตอบให้ สองอย่างนี้เคยอัดอยู่ใน
  // เซลล์เดียวกัน พออ่านแล้วแยกไม่ออกว่าอะไรเป็นของ alias อะไรเป็นของเครื่อง
  // One backend that answers for this alias — a row inside the card's expanded
  // tray. Same data-attrs as before, so the tune/enable handlers below still bind.
  const endpointRow = (m, e) => {
    const dead = !e.health?.healthy;
    const speaks = [
      e.protocols?.openai ? 'OpenAI' : '',
      e.protocols?.anthropic ? 'Anthropic' : '',
      e.modalities?.image ? 'images' : '',
    ].filter(Boolean);
    return `<div class="brow">
      <span class="ep-mark">↳</span> <code>${esc(e.name)}</code>
      <span class="hint">${esc(e.server_type)}</span>
      <code class="mono ep-url">${esc(e.base_url)}</code>
      <div class="badges tight">${speaks.map(badgeChip).join('')}</div>
      ${e.enabled === false
        ? '<span class="pill mute">ปิดไว้</span>'
        : `<span class="pill ${dead ? 'err' : 'ok'}">${dead ? 'down' : 'up'}</span>`}
      <span class="grow"></span>
      <label class="tune">priority
        <input type="number" min="0" max="1000" value="${esc(e.priority)}"
          data-tune="priority" data-model="${esc(m.alias)}" data-ep="${esc(e.name)}"></label>
      <label class="tune">พร้อมกัน
        <input type="number" min="1" max="4096" value="${esc(e.max_concurrency)}"
          data-tune="max_concurrency" data-model="${esc(m.alias)}" data-ep="${esc(e.name)}"></label>
      ${e.health?.in_flight ? `<span class="hint">กำลังวิ่ง ${esc(e.health.in_flight)}</span>` : ''}
      <button class="ghost small" data-ep-model="${esc(m.alias)}" data-ep="${esc(e.name)}"
        data-ep-to="${e.enabled === false ? '1' : '0'}"
        >${e.enabled === false ? 'เปิดเครื่องนี้' : 'ปิดเครื่องนี้'}</button>
    </div>`;
  };

  $('model-table').innerHTML = registry.data.map((m, i) => {
    // นับเฉพาะเครื่องที่เปิดอยู่ — เครื่องที่ถูกปิดไว้ไม่ได้รับงาน การนับรวมเข้าไป
    // ทำให้ตัวเลขบอกกำลังที่ไม่มีจริง ส่วนที่ปิดไว้บอกแยกเพราะเป็นคนละเรื่องกับ down
    const serving = m.endpoints.filter((e) => e.enabled !== false);
    const healthy = serving.filter((e) => e.health?.healthy).length;
    const total = serving.length;
    const off = m.endpoints.length - total;
    const cls = healthy === total ? 'ok' : healthy ? 'warn' : 'err';
    const c = compat[i];
    const ccls = c.status === 'READY' ? 'ok' : c.status === 'DEGRADED' ? 'err' : 'mute';
    // ป้ายความสามารถคือสิ่งที่ *ประกาศไว้* · ผลทดสอบคือสิ่งที่ *วัดได้จริง* สองอย่างนี้
    // ไม่ตรงกันได้ และเคสที่เจ็บคือประกาศว่าทำได้แต่วัดแล้วไม่ผ่าน จึงต้องบอกให้เห็น
    const results = c.results || [];
    const times = results.map((r) => r.tested_at).filter(Boolean).map((t) => stamp(t));
    const last = times.length ? new Date(Math.max(...times)) : null;
    const failed = results.filter((r) => r.status === 'fail').map((r) => r.feature);
    const open = openAliases().has(m.alias);
    return `<div class="mcard${ccls === 'err' ? ' deg' : ''}${open ? ' open' : ''}${
        m.enabled === false ? ' mdim' : ''}">
      <div class="mtop">
        <div class="mid">
          <button class="expand" data-expand="${esc(m.alias)}" aria-expanded="${open}"
            title="ดูเครื่องที่รองรับ alias นี้">${icon(open ? 'minus' : 'plus', 13)}</button>
          <div class="minfo">
            <div class="malias"><code>${esc(m.alias)}</code>${m.enabled === false
              ? ' <span class="pill mute">off</span>' : ''}
              <span class="pill ${cls}">${healthy}/${total} up${off ? ` · ปิดไว้ ${off}` : ''}</span></div>
            <div class="hint">${esc(m.display_name)}</div>
            <div class="mmeta">${total} เครื่อง</div>
          </div>
        </div>
        <div class="mright">
          <span class="pill ${ccls}">${esc(c.status)}</span>
          <div class="acts">
            <button class="ghost small" data-verify="${esc(m.alias)}">Verify</button>
            <button class="ghost small" data-edit="${esc(m.alias)}">Edit</button>
            <span class="rowmenu"><button class="ghost small menu-t" data-menu aria-label="More actions"
              >${icon('chevron', 14)}</button>
              <div class="menu-pop">
                <button data-test="${esc(m.alias)}">Run tests</button>
                <button data-enable="${esc(m.alias)}" data-to="${m.enabled === false ? '1' : '0'}"
                  >${m.enabled === false ? 'Enable' : 'Disable'}</button>
                <button class="danger" data-del="${esc(m.alias)}">Delete</button>
              </div></span>
          </div>
        </div>
        <div class="caps"><div class="badges tight">${m.badges.map(badgeChip).join('')}</div></div>
        <div class="upstream">upstream <code>${esc(m.upstream_model)}</code></div>
        <div class="measured${failed.length ? ' fail' : ''}">
          ${last
            ? `วัดล่าสุด ${esc(last.toLocaleString())}`
            : 'ยังไม่เคยวัด — ป้ายทางซ้ายเป็นสิ่งที่ประกาศไว้เท่านั้น'}${
            failed.length ? ` · ไม่ผ่าน: ${failed.map(esc).join(', ')}` : ''}
          <span class="hint" id="run-${esc(m.alias)}"></span>
        </div>
      </div>
      <div class="mback">${m.endpoints.map((e) => endpointRow(m, e)).join('')}</div>
    </div>`;
  }).join('');

  // ยุบไว้ก่อนเป็นค่าตั้งต้น · 7 โมเดล x 2 เครื่อง = 21 แถวรวด ซึ่งอ่านไม่ออกว่า
  // แถวไหนเป็นของใคร · กางทีละตัวเมื่อจะดูจริง
  const setOpen = (alias, opening) => {
    const btn = $('model-table').querySelector(`[data-expand="${alias}"]`);
    if (!btn) return;
    btn.setAttribute('aria-expanded', String(opening));
    btn.innerHTML = icon(opening ? 'minus' : 'plus', 13);
    btn.closest('.mcard')?.classList.toggle('open', opening);
  };

  for (const btn of $('model-table').querySelectorAll('[data-expand]')) {
    btn.onclick = () => {
      const alias = btn.dataset.expand;
      const opening = btn.getAttribute('aria-expanded') !== 'true';
      setOpen(alias, opening);
      const open = openAliases();
      opening ? open.add(alias) : open.delete(alias);
      rememberOpen(open);
    };
  }

  const all = $('models-expand-all');
  if (all) {
    const aliases = [...$('model-table').querySelectorAll('[data-expand]')]
      .map((b) => b.dataset.expand);
    const paint = () => {
      const anyClosed = aliases.some((a) => !openAliases().has(a));
      all.textContent = anyClosed ? 'กางทั้งหมด' : 'ยุบทั้งหมด';
    };
    all.onclick = () => {
      const anyClosed = aliases.some((a) => !openAliases().has(a));
      for (const a of aliases) setOpen(a, anyClosed);
      rememberOpen(anyClosed ? new Set(aliases) : new Set());
      paint();
    };
    paint();
  }

  for (const btn of $('model-table').querySelectorAll('[data-menu]')) {
    btn.onclick = (ev) => { ev.stopPropagation(); btn.closest('.rowmenu').classList.toggle('open'); };
  }
  for (const btn of $('model-table').querySelectorAll('[data-verify]')) {
    btn.onclick = () => verifyModel(btn.dataset.verify, btn);
  }
  for (const btn of $('model-table').querySelectorAll('[data-test]')) {
    btn.onclick = () => runTests(btn.dataset.test, btn);
  }
  for (const btn of $('model-table').querySelectorAll('[data-edit]')) {
    btn.onclick = () => openEditor(state.cache.models.find((m) => m.alias === btn.dataset.edit));
  }
  // ปิดโดยไม่ลบ — ไฟล์และค่าที่ปรับมายังอยู่ครบ ต่างจากปุ่ม Delete ข้าง ๆ
  const setEnabled = async (alias, body) => {
    try { await patch(`/admin/models/${encodeURIComponent(alias)}/enabled`, body); await loadModels(); }
    catch (e) { showError(e.message); }
  };
  for (const btn of $('model-table').querySelectorAll('[data-enable]')) {
    btn.onclick = () => {
      const on = btn.dataset.to === '1';
      if (!on && !confirm(`ปิด "${btn.dataset.enable}" ชั่วคราว?\n\n` +
                          'สมาชิกจะไม่เห็น alias นี้ในรายการ แต่ไฟล์ตั้งค่ายังอยู่ เปิดกลับได้ทุกเมื่อ')) return;
      setEnabled(btn.dataset.enable, { enabled: on });
    };
  }
  for (const btn of $('model-table').querySelectorAll('[data-ep-model]')) {
    btn.onclick = () => setEnabled(btn.dataset.epModel, {
      enabled: btn.dataset.epTo === '1', endpoint: btn.dataset.ep,
    });
  }
  // Saved on blur, not on every keystroke: each save rewrites the registry file
  // and reloads it, and typing "100" would do that three times.
  for (const input of $('model-table').querySelectorAll('[data-tune]')) {
    const original = input.value;
    input.onchange = async () => {
      const { model, ep, tune } = input.dataset;
      try {
        await patch(
          `/admin/models/${encodeURIComponent(model)}/endpoints/${encodeURIComponent(ep)}`,
          { [tune]: Number(input.value) },
        );
        await loadModels();
      } catch (e) {
        input.value = original;
        showError(e.message);
      }
    };
  }
  for (const btn of $('model-table').querySelectorAll('[data-del]')) {
    btn.onclick = async () => {
      if (!confirm(`Delete the registry file for "${btn.dataset.del}"?\n\n` +
                   'Members calling this alias will get MODEL_NOT_FOUND.')) return;
      try { await del(`/admin/models/${encodeURIComponent(btn.dataset.del)}`); await loadModels(); }
      catch (e) { showError(e.message); }
    };
  }
}

async function runTests(alias, btn) {
  const out = $(`run-${alias}`);
  btn.disabled = true;
  out.textContent = 'starting…';
  try {
    const { run_id } = await post(`/admin/models/${encodeURIComponent(alias)}/test`);
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const run = await api(`/admin/test-runs/${run_id}`);
      const last = run.results[run.results.length - 1];
      out.innerHTML = `${run.completed}/${run.total}` +
        (last ? ` · ${esc(last.test_id)} <span class="pill ${STATUS_PILL[last.status]}">${esc(last.status)}</span>` : '');
      if (run.status !== 'running') {
        if (run.status === 'error') { out.innerHTML = `<span class="pill err">${esc(run.error)}</span>`; break; }
        const rows = run.results.map((r) => `<tr><td><code>${esc(r.test_id)}</code></td>
          <td>${esc(r.feature)}</td>
          <td><span class="pill ${STATUS_PILL[r.status]}">${esc(r.status)}</span></td>
          <td class="num">${r.latency_ms}</td><td class="hint">${esc(r.notes)}</td></tr>`).join('');
        modal(`Test results — ${alias}`,
          `<div class="scroll"><table><tr><th>Test</th><th>Feature</th><th>Status</th>
           <th class="num">ms</th><th>Notes</th></tr>${rows}</table></div>`);
        await loadModels();
        break;
      }
    }
  } catch (e) {
    out.innerHTML = `<span class="pill err">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

/* -- endpoint rows ---------------------------------------------------- */
function addEndpointRow(data = {}) {
  const node = $('endpoint-template').content.firstElementChild.cloneNode(true);
  const q = (cls) => node.querySelector('.' + cls);

  q('ep-name').value = data.name || '';
  q('ep-server').value = data.server_type || 'vllm';
  q('ep-url').value = data.base_url || '';
  q('ep-upstream').value = data.upstream_model || '';
  q('ep-keyenv').value = data.api_key_env || '';
  q('ep-priority').value = data.priority ?? 100;
  q('ep-conc').value = data.max_concurrency ?? 8;
  q('ep-openai').checked = data.protocols ? !!data.protocols.openai : true;
  q('ep-anthropic').checked = !!data.protocols?.anthropic;
  q('ep-image').checked = !!data.modalities?.image;
  q('ep-enabled').checked = data.enabled !== false;

  const retitle = () => {
    q('ep-title').textContent = q('ep-name').value.trim() || q('ep-url').value.trim() || 'Backend';
  };
  q('ep-name').addEventListener('input', retitle);
  q('ep-url').addEventListener('input', retitle);
  retitle();

  q('ep-remove').onclick = () => {
    if ($('endpoints').children.length <= 1) {
      alert('A model needs at least one backend.');
      return;
    }
    node.remove();
  };

  q('ep-detect').onclick = () => detectEndpoint(node);

  $('endpoints').appendChild(node);
  return node;
}

async function detectEndpoint(node) {
  const q = (cls) => node.querySelector('.' + cls);
  const out = q('ep-detect-out');
  const base_url = q('ep-url').value.trim();
  if (!base_url) { banner_in(out, 'err', 'Enter a base URL first'); return; }

  q('ep-detect').disabled = true;
  out.innerHTML = '<p class="hint">probing…</p>';
  try {
    const { suggestion } = await post('/admin/models/detect', {
      base_url,
      upstream_model: q('ep-upstream').value.trim() || $('m-upstream').value.trim(),
      api_key_env: q('ep-keyenv').value.trim(),
    });
    if (!suggestion.reachable) {
      banner_in(out, 'err', 'Backend not reachable. ' + suggestion.notes.join(' '));
      return;
    }

    // Endpoint-level facts go on this row; model-level ones fill the form only
    // when it is still empty, so detecting a second backend cannot silently
    // rewrite what the admin already decided.
    q('ep-anthropic').checked = !!suggestion.protocols?.anthropic;
    q('ep-openai').checked = !!suggestion.protocols?.openai;
    q('ep-image').checked = !!suggestion.capabilities.vision;

    if (!$('m-upstream').value.trim() && suggestion.upstream_model) {
      $('m-upstream').value = suggestion.upstream_model;
    } else if (suggestion.upstream_model
               && suggestion.upstream_model !== $('m-upstream').value.trim()
               && !q('ep-upstream').value.trim()) {
      // This backend calls it something else - that is what the override is for.
      q('ep-upstream').value = suggestion.upstream_model;
    }
    if (suggestion.context_tokens && !Number($('m-ctx').value)) {
      $('m-ctx').value = suggestion.context_tokens;
    }
    for (const [cap, on] of Object.entries(suggestion.capabilities)) {
      if ($(`c-${cap}`)) $(`c-${cap}`).checked = on;
    }
    if (suggestion.protocols?.anthropic) $('x-anthropic').checked = true;

    const yes = (v) => (v ? '<span class="pill ok">yes</span>' : '<span class="pill err">no</span>');
    out.innerHTML = `<div class="banner ok" style="margin:10px 0 0">
      Detected — confirm the capability boxes before saving.
      <div style="margin-top:8px">
        chat ${yes(suggestion.capabilities.chat)} ·
        streaming ${yes(suggestion.capabilities.streaming)} ·
        tools ${yes(suggestion.capabilities.tools)} ·
        vision ${yes(suggestion.capabilities.vision)}
        ${suggestion.context_tokens ? ` · context ${num(suggestion.context_tokens)}` : ''}
      </div>
      ${suggestion.served_models.length
        ? `<div class="hint">serves: ${suggestion.served_models.map(esc).join(', ')}</div>` : ''}
      ${suggestion.notes.length
        ? `<div class="hint">${suggestion.notes.map(esc).join('<br>')}</div>` : ''}
    </div>`;
  } catch (e) {
    banner_in(out, 'err', e.message);
  } finally {
    q('ep-detect').disabled = false;
  }
}

function banner_in(node, kind, message) {
  node.innerHTML = message
    ? `<div class="banner ${kind}" style="margin:10px 0 0">${esc(message)}</div>` : '';
}

function readEndpoints() {
  return [...$('endpoints').children].map((node, index) => {
    const q = (cls) => node.querySelector('.' + cls);
    const url = q('ep-url').value.trim();
    const fallback = url.replace(/^https?:\/\//, '').replace(/[^\w.-]/g, '-').slice(0, 40);
    return {
      name: q('ep-name').value.trim() || fallback || `backend-${index + 1}`,
      server_type: q('ep-server').value,
      base_url: url,
      upstream_model: q('ep-upstream').value.trim(),
      api_key_env: q('ep-keyenv').value.trim(),
      priority: Number(q('ep-priority').value) || 100,
      weight: 1,
      max_concurrency: Number(q('ep-conc').value) || 8,
      health_path: '/health',
      protocols: { openai: q('ep-openai').checked, anthropic: q('ep-anthropic').checked },
      modalities: {
        text: true, image: q('ep-image').checked, audio: false, video: false,
      },
      enabled: q('ep-enabled').checked,
    };
  });
}

function editorValues() {
  const checked = (id) => $(id).checked;
  const purposes = ['general', 'coding', 'vision', 'reasoning', 'agent', 'fast']
    .filter((p) => checked(`p-${p}`));
  const vision = checked('c-vision');

  const definition = {
    apiVersion: 'litegate.dev/v1',
    kind: 'Model',
    metadata: {
      alias: $('m-alias').value.trim(),
      display_name: $('m-name').value.trim() || $('m-alias').value.trim(),
      description: $('m-desc').value.trim(),
      visibility: $('m-visibility').value,
      tags: state.cache.editingTags || [],
    },
    spec: {
      upstream_model: $('m-upstream').value.trim(),
      purpose: purposes.length ? purposes : ['general'],
      limits: {
        context_tokens: Number($('m-ctx').value) || 8192,
        max_output_tokens: Number($('m-out').value) || 2048,
      },
      modalities: { input: vision ? ['text', 'image'] : ['text'], output: ['text'] },
      capabilities: {
        chat: checked('c-chat'), vision, tools: checked('c-tools'),
        streaming: checked('c-streaming'), coding: checked('c-coding'),
        reasoning: checked('c-reasoning'), agentic: checked('c-agentic'),
        audio: false, embedding: false,
      },
      protocols: { openai: checked('x-openai'), anthropic: checked('x-anthropic') },
      endpoints: readEndpoints(),
      enabled: true,
    },
  };
  if ($('x-claudecode').checked) {
    definition.spec.agent_clients = { claude_code: { enabled: true, tested: false } };
  }
  return definition;
}

function openEditor(model) {
  $('editor').hidden = false;
  $('editor-title').textContent = model ? `Edit ${model.alias}` : 'Add AI model';
  flash('save-status', '', '');
  $('endpoints').innerHTML = '';
  state.cache.editingTags = model?.tags || [];

  // Null-safe: the API returns every capability flag, including ones the form
  // deliberately has no box for (audio, embedding). Without the guard the first
  // missing id throws and everything after it - including the backend rows -
  // silently never renders.
  const set = (id, v) => { const el = $(id); if (el) el.value = v; };
  const check = (id, v) => { const el = $(id); if (el) el.checked = !!v; };

  if (!model) {
    ['m-alias', 'm-name', 'm-desc', 'm-upstream'].forEach((i) => set(i, ''));
    set('m-ctx', 131072); set('m-out', 8192); set('m-visibility', 'member');
    ['c-vision', 'c-tools', 'c-coding', 'c-reasoning', 'c-agentic', 'x-anthropic', 'x-claudecode']
      .forEach((i) => check(i, false));
    ['c-chat', 'c-streaming', 'x-openai', 'p-general'].forEach((i) => check(i, true));
    ['p-coding', 'p-vision', 'p-reasoning', 'p-agent', 'p-fast'].forEach((i) => check(i, false));
    $('m-alias').disabled = false;
    addEndpointRow();
  } else {
    set('m-alias', model.alias); $('m-alias').disabled = true;
    set('m-name', model.display_name);
    set('m-desc', model.description || '');
    set('m-visibility', model.visibility);
    set('m-upstream', model.upstream_model);
    set('m-ctx', model.limits.context_tokens); set('m-out', model.limits.max_output_tokens);
    for (const [k, v] of Object.entries(model.capabilities)) check(`c-${k}`, v);
    check('x-openai', model.protocols.openai); check('x-anthropic', model.protocols.anthropic);
    check('x-claudecode', !!model.agent_clients?.claude_code?.enabled);
    ['general', 'coding', 'vision', 'reasoning', 'agent', 'fast']
      .forEach((p) => check(`p-${p}`, model.purpose.includes(p)));
    model.endpoints.forEach((e) => addEndpointRow(e));
  }

  $('save-model').disabled = state.cache.writable === false;
  $('editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('new-model').onclick = () => openEditor(null);
$('add-endpoint').onclick = () => addEndpointRow();
$('editor-close').onclick = () => { $('editor').hidden = true; };
$('reload-registry').onclick = async () => {
  try { await post('/admin/registry/reload'); await loadModels(); }
  catch (e) { showError(e.message); }
};

$('preview-yaml').onclick = async () => {
  try {
    const { yaml, filename } = await post('/admin/models/preview', editorValues());
    modal(`config/models/${filename}`,
      `<p class="hint">Commit this to git so the registry keeps its review trail.</p>
       <pre>${esc(yaml)}</pre>`, yaml);
    flash('save-status', 'ok', 'valid');
  } catch (e) {
    flash('save-status', 'err', e.message);
  }
};

$('save-model').onclick = async () => {
  flash('save-status', '', 'saving…');
  try {
    const result = await post('/admin/models', editorValues());
    flash('save-status', 'ok',
      `saved ${result.alias} — all workers pick it up within ${result.propagation_seconds}s`);
    $('editor').hidden = true;
    await loadModels();
  } catch (e) {
    flash('save-status', 'err', e.message);
  }
};


/* -- verify: what the backend actually does, and how to fix it ---------- */
const VERDICT_PILL = { consistent: 'ok', drift: 'warn', blocked: 'err' };

async function verifyModel(alias, btn) {
  const out = $(`run-${alias}`);
  btn.disabled = true;
  out.textContent = 'probing…';
  try {
    const r = await api(`/admin/models/${encodeURIComponent(alias)}/advice`);
    out.innerHTML = `<span class="pill ${VERDICT_PILL[r.summary.verdict] || 'mute'}">${esc(r.summary.verdict)}</span>`;

    const rows = r.backends.map((b) => `
      <h3 style="margin:16px 0 6px">${esc(b.endpoint)}
        <span class="pill ${b.reachable ? 'ok' : 'err'}">${b.reachable ? 'reachable' : 'unreachable'}</span></h3>
      <div class="hint" style="margin-bottom:8px">${esc(b.base_url)} ·
        ${esc(b.server_type)} · context ${b.context_tokens ? num(b.context_tokens) : 'unknown'}</div>
      ${b.drift.length ? `<div class="banner warn">Registry disagrees with the backend:
        ${b.drift.map((d) => `<div class="mono">${esc(d.capability)}: declared ${d.declared} · measured ${d.measured}</div>`).join('')}
      </div>` : ''}
      ${b.advice.length ? b.advice.map((a) => `
        <div class="banner ${a.severity === 'blocker' ? 'err' : a.severity === 'warning' ? 'warn' : 'ok'}">
          <strong>${esc(a.issue)}</strong>
          <div style="margin-top:5px">${esc(a.detail)}</div>
          <div style="margin-top:5px">${esc(a.fix)}</div>
          ${a.command ? `<pre style="margin-top:8px">${esc(a.command)}</pre>` : ''}
          ${applyButton(alias, b.endpoint, a)}
        </div>`).join('')
        : '<div class="banner ok">Nothing to fix on this backend.</div>'}`).join('');

    modal(`Verify — ${alias}`, rows);
  } catch (e) {
    out.innerHTML = `<span class="pill err">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

$('verify-all').onclick = async () => {
  // รายชื่อโมเดลเคยมาจากแคชที่เติมตอนเปิดแท็บ Models เท่านั้น — คนที่ login แล้วมา
  // หน้านี้เลยเห็น "open the Models tab first" แทนช่องติ๊ก แล้วดูเหมือนไม่มีฟีเจอร์
  if (!state.cache.models) {
    try { state.cache.models = (await api('/admin/models')).data; } catch { state.cache.models = []; }
  }
  const aliases = (state.cache.models || []).map((m) => m.alias);
  banner('registry-note', 'ok', `Probing ${aliases.length} model(s)…`);
  const findings = [];
  for (const alias of aliases) {
    try {
      const r = await api(`/admin/models/${encodeURIComponent(alias)}/advice`);
      for (const b of r.backends) {
        for (const a of b.advice) findings.push({ alias, endpoint: b.endpoint, ...a });
        for (const d of b.drift) {
          findings.push({
            alias, endpoint: b.endpoint, severity: 'warning', issue: 'capability_drift',
            detail: `${d.capability}: registry says ${d.declared}, backend says ${d.measured}`,
            fix: 'Correct the registry, or fix the backend so it matches.', command: '',
          });
        }
      }
    } catch (e) {
      findings.push({ alias, endpoint: '-', severity: 'blocker', issue: 'probe_failed',
                      detail: e.message, fix: '', command: '' });
    }
  }
  banner('registry-note', '', '');
  modal('Fleet verification', findings.length ? `
    <div class="scroll"><table>
      <tr><th>Model</th><th>Backend</th><th>Severity</th><th>Issue</th><th>What to do</th></tr>
      ${findings.map((f) => `<tr>
        <td><code>${esc(f.alias)}</code></td><td>${esc(f.endpoint)}</td>
        <td><span class="pill ${f.severity === 'blocker' ? 'err' : f.severity === 'warning' ? 'warn' : 'mute'}">${esc(f.severity)}</span></td>
        <td>${esc(f.issue)}<div class="hint">${esc(f.detail)}</div></td>
        <td>${esc(f.fix)}${f.command ? `<pre style="margin-top:6px">${esc(f.command)}</pre>` : ''}
          ${applyButton(f.alias, f.endpoint, f)}</td>
      </tr>`).join('')}
    </table></div>`
    : '<div class="banner ok">Every backend matches what the registry declares.</div>');
};

// ---------------------------------------------------------------------------
// Applying a finding through the deploy tool
// ---------------------------------------------------------------------------
// Verification used to stop at a command you had to go and paste. When a deploy
// tool is connected *and* the endpoint records which machine and bundle it came
// from, the same finding gets a button. Both conditions matter: a gateway can
// have one managed backend and three that nobody manages.
function applyButton(alias, endpoint, finding) {
  if (!finding.appliable || !state.lmds?.configured) return '';
  const guess = finding.parser_confident ? '' :
    `<span class="hint"> — ${esc(finding.parser)} is a guess for this model family;
      a wrong parser fails quietly, so check the result.</span>`;
  return `<div style="margin-top:8px">
    <button class="ghost small" data-fix="${esc(alias)}" data-fix-endpoint="${esc(endpoint)}"
      data-fix-issue="${esc(finding.issue)}" data-fix-parser="${esc(finding.parser)}">
      Apply via LMDS</button>
    <code style="margin-left:6px">${esc(finding.parser)}</code>${guess}
  </div>`;
}

document.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-fix]');
  if (!btn) return;
  const { fix: alias, fixEndpoint, fixIssue, fixParser } = btn.dataset;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'restarting…';
  try {
    const done = await post(`/admin/models/${encodeURIComponent(alias)}/apply-fix`, {
      issue: fixIssue, endpoint: fixEndpoint, parser: fixParser,
    });
    // Deliberately not "fixed": the model server is restarting, and whether the
    // finding is gone is a question only a fresh probe can answer.
    btn.replaceWith(Object.assign(document.createElement('span'), {
      className: 'hint',
      textContent: `Sent to ${done.node}/${done.slug}. ${done.next}`,
    }));
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    showError(e.message);
  }
});

/* --------------------------------------------------------- access & keys */
// ---------------------------------------------------------------------------
// Assistant: which model answers in the chat panel, and how well it would
// ---------------------------------------------------------------------------
function fitTag(fit) {
  if (!fit.usable) return '<span class="pill err">Cannot be used</span>';
  if (fit.reasons.some((r) => r.kind === 'warning')) return '<span class="pill warn">Usable</span>';
  return '<span class="pill ok">Good fit</span>';
}

async function loadAssistant() {
  await loadLmds();
  const data = await api('/admin/assistant');

  // The dropdown lists only what can actually be chosen. The cards below list
  // everything, so a model missing from the dropdown still explains itself.
  const options = data.candidates.filter((c) => c.usable);
  const auto = data.automatic_choice
    ? `Automatic — currently ${data.automatic_choice}`
    : 'Automatic — no model qualifies yet';
  $('as-model').innerHTML = [
    `<option value="">${esc(auto)}</option>`,
    ...options.map((c) =>
      `<option value="${esc(c.alias)}"${c.alias === data.pinned ? ' selected' : ''}>` +
      `${esc(c.display_name)} (${esc(c.alias)})</option>`),
  ].join('');

  const source = {
    console: 'pinned here in the console',
    environment: 'set at deploy time by GW_ASSISTANT_MODEL',
    automatic: 'chosen automatically from the ranking below',
  }[data.source] || data.source;
  $('as-current').innerHTML = data.effective
    ? `Answering with <code>${esc(data.effective)}</code> — ${esc(source)}.`
    : 'No model can serve the assistant yet, so the chat panel is hidden.';

  $('as-candidates').innerHTML = data.candidates.map((fit) => `
    <div class="fit ${fit.usable ? '' : 'blocked'} ${fit.alias === data.effective ? 'picked' : ''}">
      <div class="fit-head">
        <b>${esc(fit.display_name)}</b>
        <span class="alias">${esc(fit.alias)}</span>
        ${fitTag(fit)}
      </div>
      <ul>${fit.reasons.map((r) => `<li class="${r.kind}">${esc(r.detail)}</li>`).join('')}</ul>
    </div>`).join('');
}

async function loadLmds() {
  state.lmds = await api('/admin/integrations/lmds');
  $('lm-url').value = state.lmds.base_url || '';
  $('lm-state').textContent = state.lmds.configured
    ? `Connected${state.lmds.has_token ? ' with a token' : ' without a token'}. ` +
      `Findings that can be applied: ${state.lmds.appliable_issues.join(', ')}.`
    : 'Not connected — findings show the command to run yourself.';
}

$('lm-save').onclick = async () => {
  try {
    // An empty token field means "keep the stored one", not "clear it": editing
    // the URL should not silently disconnect the tool.
    const token = $('lm-token').value;
    await api('/admin/integrations/lmds', {
      method: 'PUT',
      body: JSON.stringify({ base_url: $('lm-url').value.trim(), ...(token ? { token } : {}) }),
    });
    $('lm-token').value = '';
    await loadLmds();
  } catch (e) { showError(e.message); }
};

$('lm-test').onclick = async () => {
  // "Configured" only means somebody typed a URL. This asks the tool who it is,
  // so nobody discovers they pointed at the staging fleet by restarting a
  // production model.
  $('lm-state').textContent = 'asking…';
  try {
    const r = await post('/admin/integrations/lmds/test');
    $('lm-state').innerHTML = r.ok
      ? `<span class="pill ok">connected</span> ${esc(r.hostname || '?')}
         <code>${esc(r.ip || '')}</code> · LMDS ${esc(r.version || '?')} ·
         ${r.nodes} machine(s)${r.node_names.length
           ? `: ${r.node_names.map(esc).join(', ')}` : ''}`
      : `<span class="pill err">not connected</span> ${esc(r.reason)}`;
  } catch (e) { $('lm-state').innerHTML = `<span class="pill err">${esc(e.message)}</span>`; }
};

$('as-save').onclick = async () => {
  try {
    await api('/admin/assistant', {
      method: 'PUT',
      body: JSON.stringify({ alias: $('as-model').value }),
    });
    await loadAssistant();
    // The panel picks its model when it boots, so a change here does not reach
    // an already-open chat until it asks again.
    await initAssistant();
  } catch (e) { showError(e.message); }
};

// A bundle is edited in place: the models are checkboxes on its own row, and
// saving reaches every class holding it — which is the whole point and also the
// thing to be careful about, so the row says how many that is.
function renderAccessGroups(groups, aliases) {
  $('access-group-table').innerHTML = `
    <tr><th>Name</th><th>Models</th><th>Used by</th><th></th></tr>
    ${groups.map((g) => `<tr${g.enabled ? '' : ' class="ws-suspended"'}>
      <td><code>${esc(g.name)}</code>${g.enabled ? '' : ' <span class="pill mute">ปิดอยู่</span>'}
          ${g.description ? `<div class="hint">${esc(g.description)}</div>` : ''}</td>
      <td class="checks">${aliases.map((a) => `
        <label><input type="checkbox" data-group="${esc(g.id)}" value="${esc(a)}"${
          (g.models || []).includes(a) ? ' checked' : ''}> ${esc(a)}</label>`).join('')
        || '<span class="hint">ยังไม่มีโมเดลใน registry</span>'}</td>
      <td class="hint">${g.used_by} วิชา</td>
      <td style="white-space:nowrap">
        <button class="ghost small" data-savegroup="${esc(g.id)}">Save</button>
        <button class="ghost small" data-togglegroup="${esc(g.id)}"
          data-to="${g.enabled ? '0' : '1'}">${g.enabled ? 'ปิด' : 'เปิด'}</button>
        <button class="danger small" data-delgroup="${esc(g.id)}"
          data-name="${esc(g.name)}" data-used="${g.used_by}">Delete</button>
      </td></tr>`).join('')
      || '<tr><td class="empty">ยังไม่มีมัดโมเดล · กด + New group เพื่อสร้างชุดแรก</td></tr>'}`;

  const save = async (id, body) => {
    try { await patch(`/admin/access-groups/${id}`, body); await loadAccess(); }
    catch (e) { showError(e.message); }
  };
  for (const btn of $('access-group-table').querySelectorAll('[data-savegroup]')) {
    btn.onclick = () => save(btn.dataset.savegroup, {
      models: [...document.querySelectorAll(
        `input[data-group="${btn.dataset.savegroup}"]:checked`)].map((i) => i.value),
    });
  }
  for (const btn of $('access-group-table').querySelectorAll('[data-togglegroup]')) {
    btn.onclick = () => save(btn.dataset.togglegroup, { enabled: btn.dataset.to === '1' });
  }
  for (const btn of $('access-group-table').querySelectorAll('[data-delgroup]')) {
    btn.onclick = async () => {
      const { name, used } = btn.dataset;
      if (!confirm(`ลบมัด "${name}"?\n\n`
        + (Number(used) ? `ยังมี ${used} วิชาถืออยู่ — ต้องเอาออกจากวิชาเหล่านั้นก่อน\n\n` : '')
        + 'ถ้าแค่อยากหยุดให้สิทธิ์ชั่วคราว ใช้ปุ่ม "ปิด" แทน ซึ่งย้อนกลับได้')) return;
      try { await del(`/admin/access-groups/${btn.dataset.delgroup}`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }
}

// The limit belongs to the person, so the bar does too. Four limits can apply
// at once; the one nearest its ceiling is the one that will stop them, so that
// is the one drawn — a bar showing the roomiest number would read as "fine"
// right up until a request is refused.
// Activity, not allowance. A key is one of several a person may hold and the
// limit belongs to the person, so this answers the question a key list cannot
// otherwise answer: which of these is still in use.
// A coloured dot + word reads state faster than a plain pill, and matches the
// health dots used elsewhere in the console.
function statusDot(active, labelOn = 'active', labelOff = 'disabled') {
  return `<span class="stat-dot ${active ? 'on' : 'off'}"><span class="d"></span>${
    active ? labelOn : labelOff}</span>`;
}

function activityCell(a) {
  if (!a || !a.requests) return '<span class="hint">—</span>';
  return `<span title="7 days">${num(a.requests)} ครั้ง</span>
    <div class="hint">${num(a.tokens)} โทเคน</div>`;
}

function quotaCell(q) {
  if (!q) return '<span class="hint">—</span>';
  const named = [
    ['requests', 'max_requests', 'ครั้ง'],
    ['input_tokens', 'max_input_tokens', 'โทเคนเข้า'],
    ['output_tokens', 'max_output_tokens', 'โทเคนออก'],
    ['images', 'max_images', 'ภาพ'],
  ].filter(([, cap]) => q.limits[cap]);

  if (!named.length) {
    return `<span class="hint">ไม่จำกัด · ${num(q.used.requests)} ครั้งใน${
      q.window === 'day' ? 'วันนี้' : `รอบ${esc(q.window)}นี้`}</span>`;
  }
  const [useKey, capKey, unit] = named
    .map((n) => [...n, q.used[n[0]] / q.limits[n[1]]])
    .sort((a, b) => b[3] - a[3])[0];
  const pct = Math.min(100, Math.round(100 * q.used[useKey] / q.limits[capKey]));
  const level = pct >= 90 ? 'err' : pct >= 70 ? 'warn' : 'ok';
  return `<div class="meter" title="รอบ${esc(q.window)} · เกณฑ์จาก ${esc(q.source)}">
    <div class="meter-bar"><span class="${level}" style="width:${pct}%"></span></div>
    <div class="meter-text">${num(q.used[useKey])} / ${num(q.limits[capKey])} ${unit}
      <span class="hint">${pct}%</span></div>
  </div>`;
}

async function loadAccess() {
  // โควตาอ่านต่อคน · กิจกรรมอ่านต่อ key · ล้มแล้วไม่ทำให้ทั้งหน้าพัง เพราะสองอันนี้
  // เป็นข้อมูลประกอบ ไม่ใช่สิ่งที่หน้านี้มีไว้ทำ
  const [users, workspaces, keys, groups, quota, activity] = await Promise.all([
    api('/admin/users'), api('/admin/workspaces'), api('/admin/api-keys'),
    api('/admin/access-groups'),
    api('/admin/usage/quota').catch(() => ({ data: [] })),
    api('/admin/usage/by-key?days=7').catch(() => ({ data: [] })),
  ]);
  const quotaOf = Object.fromEntries((quota.data || []).map((q) => [q.user_id, q]));
  const seenOf = Object.fromEntries((activity.data || []).map((a) => [a.api_key_id, a]));
  state.cache.users = users.data;
  state.cache.workspaces = workspaces.data;
  state.cache.accessGroups = groups.data;

  const userOpts = users.data
    .map((u) => `<option value="${esc(u.id)}">${esc(u.external_id)} — ${esc(u.display_name || u.role)}</option>`)
    .join('');
  $('k-user').innerHTML = userOpts;
  $('q-user').innerHTML = userOpts;
  const workspaceOpts = workspaces.data
    .map((c) => `<option value="${esc(c.id)}">${esc(c.code)} — ${esc(c.name)}</option>`).join('');
  $('k-workspace').innerHTML = '<option value="">— none —</option>' + workspaceOpts;

  // จำกัดโมเดลต่อ key — ว่าง = พฤติกรรมเดิม · เลือกแล้ว = แคบลงเท่านั้น
  if (!state.cache.models) {
    try { state.cache.models = (await api('/admin/models')).data; } catch { state.cache.models = []; }
  }
  $('k-models').innerHTML = (state.cache.models || []).map((m) =>
    `<label><input type="checkbox" class="k-model" value="${esc(m.alias)}"> ${esc(m.alias)}</label>`
  ).join('') || '<span class="hint">ยังไม่มีโมเดลใน registry</span>';
  $('q-workspace').innerHTML = workspaceOpts;

  // ตาราง People — ก่อนหน้านี้ผู้ใช้โผล่แค่ใน dropdown ตอนออก key · role ตั้งได้
  // ตอนสร้างเท่านั้น แล้วไม่มีทางแก้จากหน้าเว็บอีกเลย
  const roles = ['member', 'manager', 'admin'];
  const adminCount = users.data.filter((u) => u.role === 'admin').length;
  $('user-table').innerHTML = `
    <tr><th>ID</th><th>Name</th><th>Role</th><th>Workspaces</th>
        <th>โควตาที่ใช้ไป</th><th>Status</th></tr>
    ${users.data.map((u) => {
      // admin คนสุดท้ายเปลี่ยน role ไม่ได้ — ไม่มี admin แปลว่าไม่มีใครออก key
      // ตั้ง quota หรือแก้ registry ได้อีก และไม่มีทางกลับผ่านหน้าเว็บ
      const locked = u.role === 'admin' && adminCount <= 1;
      // workspace ที่คนนี้อยู่ + ตัวเลือกให้เพิ่มเข้าที่ยังไม่ได้อยู่
      // `join` มีมาตั้งแต่ต้นแต่ไม่มีหน้าไหนเรียกใช้เลย — สร้าง workspace ได้
      // แต่ใส่คนเข้าไปไม่ได้
      const mine = u.workspaces || [];
      const spare = workspaces.data.filter((c) => !mine.includes(c.code));
      return `<tr>
        <td><code>${esc(u.external_id)}</code></td>
        <td>${esc(u.display_name || '—')}<div class="hint">${esc(u.email || '')}</div></td>
        <td>${locked
          ? `<span class="pill ok" title="ผู้ดูแลคนเดียวที่เหลืออยู่ — ตั้งให้คนอื่นเป็น admin ก่อนถึงจะเปลี่ยนได้">admin (คนเดียว)</span>`
          : `<select class="small" data-role="${esc(u.id)}">${roles.map((r) =>
              `<option value="${r}"${u.role === r ? ' selected' : ''}>${r}</option>`).join('')}</select>`}</td>
        <td>
          ${mine.map((code) => {
            const ws = workspaces.data.find((c) => c.code === code);
            // วิชาที่ถูกระงับไม่ให้สิทธิ์อะไรเลย · ป้ายที่ดูเหมือนใช้ได้คือป้ายที่โกหก
            const held = ws && ws.status === 'suspended';
            return `<span class="pill mute">${esc(code)}${held
              ? ' <span class="hint">(ระงับ)</span>' : ''}<button class="link small"
              data-leave="${esc(ws ? ws.id : '')}" data-who="${esc(u.id)}"
              title="เอา ${esc(u.external_id)} ออกจาก ${esc(code)}">×</button></span>`;
          }).join(' ') || '<span class="hint">—</span>'}
          ${spare.length ? `<select class="small" data-join="${esc(u.id)}">
            <option value="">+ add…</option>
            ${spare.map((c) => `<option value="${esc(c.id)}">${esc(c.code)}</option>`).join('')}
          </select>` : ''}
        </td>
        <td>${quotaCell(quotaOf[u.id])}${myRole !== 'admin' || !quotaOf[u.id] ? ''
          : `<button class="link small" data-reset-quota="${esc(u.id)}"
               data-who="${esc(u.external_id)}"
               title="คืนโควตารอบนี้ให้ ${esc(u.external_id)} · ประวัติการใช้งานยังอยู่ครบ"
               >คืนโควตา</button>`}</td>
        <td>${statusDot(u.status === 'active', 'active', esc(u.status || 'disabled'))}</td>
      </tr>`;
    }).join('') || '<tr><td class="empty">No people yet.</td></tr>'}`;

  // คืนโควตารอบนี้ · เคสจริงคือคนเผลอรันลูปแล้วโควตาทั้งเทอมหมดในบ่ายเดียว แล้วทางออก
  // มีแค่ขยายเพดานถาวร (ทั้งที่เพดานไม่ผิด) หรือบอกให้รอรอบใหม่ (ทั้งที่ติดอยู่ตอนนี้)
  for (const btn of $('user-table').querySelectorAll('[data-reset-quota]')) {
    btn.onclick = async () => {
      const q = quotaOf[btn.dataset.resetQuota];
      if (!confirm(`คืนโควตารอบ${q?.window || ''}ให้ ${btn.dataset.who}?\n\n`
        + 'ตัวนับกลับไปเป็นศูนย์ · ประวัติการใช้งานและรายงานยังอยู่ครบ '
        + 'และการคืนครั้งนี้จะถูกบันทึกว่าใครเป็นคนทำ')) return;
      try {
        const out = await post(`/admin/users/${btn.dataset.resetQuota}/quota/reset`, {});
        await loadAccess();
        banner('error', 'ok',
          `คืนโควตาให้ ${btn.dataset.who} แล้ว · ล้างไป ${num(out.cleared.requests)} ครั้ง`);
      } catch (e) { showError(e.message); }
    };
  }

  for (const sel of $('user-table').querySelectorAll('[data-join]')) {
    sel.onchange = async () => {
      if (!sel.value) return;
      try { await post(`/admin/workspaces/${sel.value}/join`, { user_id: sel.dataset.join }); await loadAccess(); }
      catch (e) { sel.value = ''; showError(e.message); }
    };
  }
  for (const btn of $('user-table').querySelectorAll('[data-leave]')) {
    btn.onclick = async () => {
      // key ที่ผูกกับ workspace นี้ไม่ถูกเพิกถอน — มันเลิกใช้ quota ของ workspace เอง
      if (!confirm('เอาออกจาก workspace นี้?\n\nkey ที่ออกให้ภายใต้ workspace นี้ไม่ถูกเพิกถอน '
        + 'แต่จะไม่ใช้โควตาของ workspace อีก')) return;
      try { await del(`/admin/workspaces/${btn.dataset.leave}/members/${btn.dataset.who}`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }
  for (const sel of $('user-table').querySelectorAll('[data-role]')) {
    const before = sel.value;
    sel.onchange = async () => {
      const who = users.data.find((u) => u.id === sel.dataset.role);
      if (!confirm(`เปลี่ยน ${who.external_id} จาก ${before} เป็น ${sel.value}?\n\n`
        + 'admin แก้ทุกอย่างได้ · manager ออก key และดูการใช้งานได้ · member ใช้โมเดลอย่างเดียว')) {
        sel.value = before; return;
      }
      try { await patch(`/admin/users/${sel.dataset.role}`, { role: sel.value }); await loadAccess(); }
      catch (e) { sel.value = before; showError(e.message); }
    };
  }

  const byId = Object.fromEntries(users.data.map((u) => [u.id, u]));
  // Cards, not a table: each key carries three or four actions, and a per-card
  // ⋯ menu keeps them from sprawling — which a table could not do, its
  // overflow:hidden clips the popup. Every data-attr is kept so the handlers
  // below bind unchanged.
  $('key-table').innerHTML = keys.data.map((k) => {
    const u = byId[k.user_id];
    const workspace = workspaces.data.find((c) => c.id === k.workspace_id);
    const scope = [
      (k.models || []).length ? `เฉพาะ ${(k.models || []).map(esc).join(', ')}` : '',
      (k.access_groups || []).length ? `มัด ${(k.access_groups || []).length} ชุด` : '',
    ].filter(Boolean).join(' · ');
    const expired = k.expires_at && stamp(k.expires_at) < new Date();
    const actions = k.revoked
      ? `<button class="small" data-purge="${esc(k.id)}"
           title="ลบแถวนี้ถาวร — ประวัติการใช้งานยังอยู่">Delete</button>`
      : `<button class="ghost small" data-extend="${esc(k.id)}" data-name="${esc(k.name || k.key_prefix)}"
           title="เลื่อนวันหมดอายุ · ตัว key เดิมใช้ต่อได้เลย">ต่ออายุ</button>
         <span class="rowmenu"><button class="ghost small menu-t" data-menu aria-label="More actions"
           >${icon('chevron', 14)}</button>
           <div class="menu-pop">
             <button data-scope="${esc(k.id)}" data-name="${esc(k.name || k.key_prefix)}">Models…</button>
             ${myRole !== 'admin' ? '' : k.revealable
               ? `<button data-reveal="${esc(k.id)}" data-label="${esc(k.name || k.key_prefix)}">ดู key</button>`
               : '<div class="menu-note">ดู key ไม่ได้ — เก็บแค่ hash</div>'}
             <button class="danger" data-revoke="${esc(k.id)}">Revoke</button>
           </div></span>`;
    return `<div class="kcard${k.revoked ? ' kdim' : ''}">
      <div class="kc-top">
        <div class="kc-id">
          <div class="kc-label"><code>${esc(k.key_prefix)}…</code> ${esc(k.name || '—')}${
            k.kind === 'service' ? ' <span class="pill mute">service</span>' : ''}</div>
          <div class="hint">${esc(u ? u.external_id : k.user_id)}${
            workspace ? ' · ' + esc(workspace.code) : ''}${scope ? ' · ' + scope : ''}</div>
        </div>
        <div class="kc-right">${statusDot(!k.revoked, 'active', 'revoked')}
          <div class="rowacts">${actions}</div></div>
      </div>
      <div class="kc-meta">
        <span>ใช้งาน 7 วัน · ${activityCell(seenOf[k.id])}</span>
        <span>Expires · ${k.expires_at
          ? `${stamp(k.expires_at).toLocaleDateString()}${expired ? ' <b style="color:var(--bad)">หมดอายุแล้ว</b>' : ''}`
          : 'never'}</span>
        <span>Last used · ${k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'never'}</span>
      </div>
    </div>`;
  }).join('') || '<div class="empty">No keys issued yet.</div>';

  for (const btn of $('key-table').querySelectorAll('[data-menu]')) {
    btn.onclick = (ev) => { ev.stopPropagation(); btn.closest('.rowmenu').classList.toggle('open'); };
  }

  // เปิดดู key ที่ออกไปแล้ว — มีเฉพาะเมื่อผู้ดูแลเปิดใช้ (GW_KEY_REVEAL_SECRET)
  // และเฉพาะ admin · ทุกครั้งถูกบันทึก และประวัตินั้นแสดงให้เห็นตรงนี้เลย เพราะการ
  // บันทึกที่ต้องไปขุดจากไฟล์ log ไม่ได้ทำให้ใครระวังตัวขึ้น
  for (const btn of $('key-table').querySelectorAll('[data-reveal]')) {
    btn.onclick = async () => {
      const id = btn.dataset.reveal;
      try {
        const out = await post(`/admin/api-keys/${id}/reveal`, {});
        let history = [];
        try { history = (await api(`/admin/api-keys/${id}/reveals`)).data || []; } catch { /* ไม่สำคัญพอจะล้มทั้งกล่อง */ }
        const earlier = history.slice(1);
        $('key-out').innerHTML = `<div class="secret">
          <strong>${esc(btn.dataset.label)}</strong>
          <code class="mono">${esc(out.api_key)}</code>
          <button class="ghost small" id="copy-key">Copy</button>
          <div class="hint">การเปิดดูครั้งนี้ถูกบันทึกไว้แล้ว${earlier.length
            ? ` · ก่อนหน้านี้เปิดดูไปแล้ว ${earlier.length} ครั้ง ล่าสุด `
              + esc(new Date(earlier[0].at).toLocaleString())
            : ' · เป็นการเปิดดูครั้งแรกของใบนี้'}</div></div>`;
        $('copy-key').onclick = () => navigator.clipboard.writeText(out.api_key);
        $('key-out').scrollIntoView({ block: 'nearest' });
      } catch (e) { showError(e.message); }
    };
  }

  // หมดอายุแล้วไม่ได้แปลว่าใช้ไม่ได้ตลอดไป · งานที่ยังไม่จบควรต่อได้โดยไม่ต้องออกใบใหม่
  // แล้วให้ทุกคนไปแก้ config กันใหม่
  for (const btn of $('key-table').querySelectorAll('[data-extend]')) {
    btn.onclick = async () => {
      const answer = prompt(`ต่ออายุ "${btn.dataset.name}" อีกกี่วัน?\n\n`
        + 'นับจากวันนี้ · เว้นว่างแล้วกด OK = ไม่มีวันหมดอายุ', '7');
      if (answer === null) return;
      const days = answer.trim() === '' ? null : Number(answer);
      if (days !== null && (!Number.isFinite(days) || days < 1)) {
        showError('ใส่จำนวนวันเป็นตัวเลขตั้งแต่ 1 ขึ้นไป'); return;
      }
      try {
        const out = await patch(`/admin/api-keys/${btn.dataset.extend}`, { days });
        await loadAccess();
        banner('error', 'ok', out.expires_at
          ? `ต่ออายุถึง ${stamp(out.expires_at).toLocaleDateString()}`
          : 'เอาวันหมดอายุออกแล้ว');
      } catch (e) { showError(e.message); }
    };
  }
  // เพิ่ม/ลด model ของ key ที่แจกไปแล้ว · ก่อนหน้านี้ตั้งได้ตอนออกครั้งเดียว การเพิ่ม
  // model หนึ่งตัวจึงแปลว่าต้องเพิกถอนใบที่ใช้งานอยู่แล้วตามไปแก้ทุกที่ที่แปะไว้ —
  // คนเลยออก key แบบกว้างไว้ก่อน ซึ่งตรงข้ามกับเหตุผลที่มี scope
  //
  // เป็นช่องติ๊กไม่ใช่ช่องพิมพ์ เพราะพิมพ์ชื่อผิดหนึ่งตัวอักษร = key ที่เรียกไม่ได้
  // และไม่มีอะไรบอกจนกว่าจะไปลองใช้จริง
  for (const btn of $('key-table').querySelectorAll('[data-scope]')) {
    btn.onclick = async () => {
      const id = btn.dataset.scope;
      const key = keys.data.find((k) => k.id === id);
      const current = new Set(key?.models || []);
      const all = state.cache.models || [];
      if (!all.length) { showError('ยังไม่มีโมเดลใน registry'); return; }

      modal(`model ที่ "${btn.dataset.name}" เรียกได้`, `
        <div class="checks" id="scope-checks">${all.map((m) => `
          <label><input type="checkbox" class="scope-model" value="${esc(m.alias)}"
            ${current.has(m.alias) ? 'checked' : ''}> ${esc(m.alias)}</label>`).join('')}</div>
        <p class="hint" id="scope-note"></p>
        <button class="primary" id="scope-save">บันทึก</button>`);

      // ไม่ติ๊กเลย = ไม่จำกัด ซึ่งกว้างกว่าเดิม ไม่ใช่แคบกว่า · ต้องเห็นก่อนกดบันทึก
      const note = () => {
        const n = $('scope-checks').querySelectorAll('.scope-model:checked').length;
        $('scope-note').textContent = n
          ? `เรียกได้ ${n} ตัวนี้เท่านั้น`
          : 'ไม่ติ๊กเลย = เรียกได้ทุก model ที่คนถือ key มีสิทธิ์ (ไม่จำกัด)';
      };
      $('scope-checks').onchange = note;
      note();

      $('scope-save').onclick = async () => {
        const models = [...$('scope-checks').querySelectorAll('.scope-model:checked')]
          .map((c) => c.value);
        try {
          await patch(`/admin/api-keys/${id}`, { models });
          $('modal').close();
          await loadAccess();
          banner('error', 'ok', models.length
            ? `key ใบนี้เรียกได้: ${models.join(', ')}`
            : 'เอาข้อจำกัด model ออกแล้ว');
        } catch (e) { $('scope-note').textContent = e.message; }
      };
    };
  }

  for (const btn of $('key-table').querySelectorAll('[data-revoke]')) {
    btn.onclick = async () => {
      if (!confirm('Revoke this key? Any client using it stops working immediately.')) return;
      try { await del(`/admin/api-keys/${btn.dataset.revoke}`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }

  // ลบได้เฉพาะคีย์ที่เพิกถอนแล้ว — คีย์ที่ยังใช้งานอยู่ต้องกด Revoke ก่อน
  // เพื่อไม่ให้ใครถูกตัดการใช้งานโดยไม่มีร่องรอยว่าเป็นคีย์ใบไหน
  for (const btn of $('key-table').querySelectorAll('[data-purge]')) {
    btn.onclick = async () => {
      if (!confirm('ลบคีย์ใบนี้ถาวร?\n\nคีย์ถูกเพิกถอนไปแล้วจึงใช้งานไม่ได้อยู่แล้ว '
        + 'การลบเป็นการเอาแถวออกจากรายการ ประวัติการใช้งานยังอยู่ครบ')) return;
      try { await del(`/admin/api-keys/${btn.dataset.purge}/purge`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }

  const revokedCount = keys.data.filter((k) => k.revoked).length;
  const sweep = $('purge-revoked');
  if (sweep) {
    sweep.hidden = revokedCount === 0;
    sweep.textContent = `Clear ${revokedCount} revoked`;
    sweep.onclick = async () => {
      if (!confirm(`ลบคีย์ที่เพิกถอนแล้วทั้ง ${revokedCount} ใบถาวร?\n\n`
        + 'ทุกใบใช้งานไม่ได้อยู่แล้ว ประวัติการใช้งานยังอยู่ครบ')) return;
      try { await post('/admin/api-keys/purge-revoked', {}); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }

  const aliases = (state.cache.models || []).map((m) => m.alias);
  renderAccessGroups(groups.data, aliases);

  const groupName = Object.fromEntries(groups.data.map((g) => [g.id, g]));
  $('workspace-table').innerHTML = `
    <tr><th>Code</th><th>Name</th><th>Members</th><th>Allowed models</th><th></th></tr>
    ${workspaces.data.map((c) => {
      const held = c.status === 'suspended';
      return `<tr${held ? ' class="ws-suspended"' : ''}>
      <td><code>${esc(c.code)}</code>${held
        ? ' <span class="pill mute">ระงับอยู่</span>' : ''}</td>
      <td>${esc(c.name)}${c.term ? `<div class="hint">${esc(c.term)}</div>` : ''}</td>
      <td class="ws-members">
        <details>
          <summary>${(c.members || []).length} คน</summary>
          <div class="ws-roster">
            ${(c.members || []).map((m) => `<span class="pill mute">${esc(m.external_id)}
              <button class="link small" data-leave="${esc(c.id)}" data-who="${esc(m.id)}"
                title="เอา ${esc(m.external_id)} ออก">×</button></span>`).join(' ')
              || '<span class="hint">ยังไม่มีใครอยู่ในวิชานี้</span>'}
          </div>
          <label class="ws-add">เพิ่มหลายคนพร้อมกัน
            <select multiple size="4" data-bulk="${esc(c.id)}">${
              (state.cache.users || [])
                .filter((u) => !(c.members || []).some((m) => m.id === u.id))
                .map((u) => `<option value="${esc(u.id)}">${esc(u.external_id)}${
                  u.display_name ? ` — ${esc(u.display_name)}` : ''}</option>`).join('')
              || '<option disabled>ทุกคนอยู่ในวิชานี้แล้ว</option>'}</select></label>
          <button class="ghost small" data-bulkadd="${esc(c.id)}"
            data-code="${esc(c.code)}">เพิ่มที่เลือกไว้</button>
        </details>
      </td>
      <td class="checks">${aliases.map((a) => `
        <label><input type="checkbox" data-workspace="${esc(c.id)}" value="${esc(a)}"${
          (c.models || []).includes(a) ? ' checked' : ''}> ${esc(a)}</label>
      `).join('') || '<span class="hint">ยังไม่มีโมเดลใน registry</span>'}
        <div class="ws-defaults">
          <span class="ws-defaults-title">key ของสมาชิกใหม่เริ่มต้นที่</span>
          <label>จำกัดไว้ที่
            <select multiple size="3" data-wsdefault="${esc(c.id)}">${
              (c.models || []).map((a) => `
              <option value="${esc(a)}"${(c.default_member_models || []).includes(a)
                ? ' selected' : ''}>${esc(a)}</option>`).join('')
              || '<option disabled>ยังไม่ได้ติ๊กโมเดลไว้ข้างบน</option>'}</select></label>
          <label>อายุ (วัน)
            <input type="number" min="0" data-wsdays="${esc(c.id)}"
              value="${c.default_key_days || 0}"></label>
          <span class="hint">
            เลือกได้เฉพาะที่ติ๊กไว้ข้างบน · ไม่เลือก = key ใช้ได้ทุกตัวที่วิชานี้เปิด ·
            อายุ 0 = ไม่หมดอายุ
          </span>
        </div>
        ${groups.data.length ? `<div class="ws-bundles">${groups.data.map((g) => `
          <label><input type="checkbox" data-wsgroup="${esc(c.id)}" value="${esc(g.id)}"${
            (c.access_groups || []).includes(g.id) ? ' checked' : ''}>
            <span data-icon="bundle" data-icon-size="13" aria-hidden="true"></span>
            ${esc(g.name)}
            <span class="hint">${(groupName[g.id]?.models || []).join(', ') || '—'}</span>
          </label>`).join('')}</div>` : ''}</td>
      <td style="white-space:nowrap">
        <button class="ghost small" data-saveworkspace="${esc(c.id)}">Save</button>
        <button class="ghost small" data-holdworkspace="${esc(c.id)}"
          data-to="${held ? 'active' : 'suspended'}" data-code="${esc(c.code)}"
          >${held ? 'เปิดใช้' : 'ระงับ'}</button>
        <button class="danger small" data-delworkspace="${esc(c.id)}"
          data-code="${esc(c.code)}">Delete</button>
      </td>
    </tr>`;
    }).join('') || '<tr><td class="empty">No workspaces yet.</td></tr>'}`;

  // ระงับ ≠ เพิกถอน · คำในปุ่มและในคำถามยืนยันต้องบอกให้ชัดว่าอันนี้ย้อนกลับได้
  paintIcons($('tab-access'));
  for (const btn of $('workspace-table').querySelectorAll('[data-holdworkspace]')) {
    btn.onclick = async () => {
      const { holdworkspace: id, to, code } = btn.dataset;
      if (to === 'suspended' && !confirm(
        `ระงับ ${code} ชั่วคราว?\n\n`
        + 'สมาชิกจะเรียกโมเดลของวิชานี้ไม่ได้จนกว่าจะเปิดใช้อีกครั้ง · '
        + 'key ของทุกคนยังอยู่ครบ ไม่ถูกเพิกถอน')) return;
      try {
        await patch(`/admin/workspaces/${id}/status`, { status: to });
        await loadAccess();
      } catch (e) { showError(e.message); }
    };
  }
  for (const btn of $('workspace-table').querySelectorAll('[data-leave]')) {
    btn.onclick = async () => {
      try {
        await del(`/admin/workspaces/${btn.dataset.leave}/members/${btn.dataset.who}`);
        await loadAccess();
      } catch (e) { showError(e.message); }
    };
  }
  for (const btn of $('workspace-table').querySelectorAll('[data-bulkadd]')) {
    btn.onclick = async () => {
      const id = btn.dataset.bulkadd;
      const picked = [...(document.querySelector(`select[data-bulk="${id}"]`)
        ?.selectedOptions || [])].map((o) => o.value);
      if (!picked.length) { showError('ยังไม่ได้เลือกใครเลย'); return; }
      try {
        const result = await post(`/admin/workspaces/${id}/members`, { user_ids: picked });
        await loadAccess();
        banner('error', result.warning ? 'warn' : 'ok',
          `${btn.dataset.code}: เพิ่ม ${result.added} คน`
          + (result.already_in ? ` · อยู่แล้ว ${result.already_in} คน` : '')
          + (result.warning ? ` · ${result.warning}` : ''));
      } catch (e) { showError(e.message); }
    };
  }
  for (const btn of $('workspace-table').querySelectorAll('[data-delworkspace]')) {
    btn.onclick = async () => {
      if (!confirm(`ลบ ${btn.dataset.code} ถาวร?\n\n`
        + 'ถ้าแค่อยากหยุดใช้ชั่วคราว กด "ระงับ" แทน ซึ่งย้อนกลับได้\n'
        + 'การลบจะถูกปฏิเสธถ้ายังมีสมาชิกหรือ key ที่ผูกกับวิชานี้อยู่')) return;
      try { await del(`/admin/workspaces/${btn.dataset.delworkspace}`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }
  for (const btn of $('workspace-table').querySelectorAll('[data-saveworkspace]')) {
    btn.onclick = async () => {
      const id = btn.dataset.saveworkspace;
      const models = [...document.querySelectorAll(`input[data-workspace="${id}"]:checked`)]
        .map((i) => i.value);
      const access_groups = [...document.querySelectorAll(`input[data-wsgroup="${id}"]:checked`)]
        .map((i) => i.value);
      const picker = document.querySelector(`select[data-wsdefault="${id}"]`);
      const default_member_models = [...(picker?.selectedOptions || [])].map((o) => o.value);
      const default_key_days = Number(
        document.querySelector(`input[data-wsdays="${id}"]`)?.value) || 0;
      // ไม่ติ๊กอะไรเลย = workspace นี้เรียกโมเดลไม่ได้สักตัว (ไม่มีแถว = ไม่อนุญาต)
      // ซึ่งต่างจาก "ยังไม่ได้ตั้ง" อย่างสิ้นเชิง — key ที่ผูกกับมันจะใช้ไม่ได้ทันที
      if (!models.length && !access_groups.length && !confirm('ไม่ได้เลือกโมเดลหรือมัดเลย\n\n'
        + 'key ที่ผูกกับ workspace นี้จะเรียกโมเดลไม่ได้สักตัว ยืนยันไหม?')) return;
      try {
        await post(`/admin/workspaces/${id}/models`, {
          models, access_groups, default_member_models, default_key_days,
        });
        await loadAccess();
        banner('error', 'ok',
          `บันทึกแล้ว · โมเดล: ${models.join(', ') || 'ไม่มี'}`
          + (access_groups.length ? ` · มัด: ${access_groups.length}` : ''));
      } catch (e) { showError(e.message); }
    };
  }
}

$('issue-key').onclick = async () => {
  try {
    const result = await post('/admin/api-keys', {
      user_id: $('k-user').value,
      workspace_id: $('k-workspace').value || null,
      name: $('k-name').value.trim(),
      expires_in_days: Number($('k-days').value) || null,
      models: [...document.querySelectorAll('.k-model:checked')].map((i) => i.value),
    });
    $('key-out').innerHTML = `<div class="secret">
      <strong>${result.revealable
        ? 'เก็บ key นี้ไว้ — ผู้ดูแลเปิดดูซ้ำได้ทีหลัง และทุกครั้งถูกบันทึก'
        : 'Store this key now — it cannot be retrieved again.'}</strong>
      <code class="mono">${esc(result.api_key)}</code>
      <button class="ghost small" id="copy-key">Copy</button></div>`;
    $('copy-key').onclick = () => navigator.clipboard.writeText(result.api_key);
    await loadAccess();
  } catch (e) { showError(e.message); }
};

$('create-user').onclick = async () => {
  try {
    await post('/admin/users', {
      external_id: $('u-id').value.trim(),
      display_name: $('u-name').value.trim(),
      role: $('u-role').value,
    });
    $('u-id').value = ''; $('u-name').value = '';
    await loadAccess();
  } catch (e) { showError(e.message); }
};

$('create-workspace').onclick = async () => {
  try {
    await post('/admin/workspaces', {
      code: $('c-code').value.trim(),
      name: $('c-name').value.trim() || $('c-code').value.trim(),
      term: $('c-term').value.trim(),
    });
    $('c-code').value = ''; $('c-name').value = '';
    await loadAccess();
  } catch (e) { showError(e.message); }
};

$('new-access-group').onclick = async () => {
  const name = prompt('ชื่อมัด (เช่น coding-set, vision-set)');
  if (name === null || !name.trim()) return;
  try {
    // สร้างเปล่าไว้ก่อน แล้วติ๊กโมเดลในแถวของมัน — ช่องติ๊กในตารางอ่านง่ายกว่า
    // การให้พิมพ์รายชื่อโมเดลใส่กล่อง prompt
    await post('/admin/access-groups', { name: name.trim(), models: [] });
    await loadAccess();
    banner('error', 'ok', `สร้างมัด "${name.trim()}" แล้ว · ติ๊กโมเดลในแถวแล้วกด Save`);
  } catch (e) { showError(e.message); }
};

/* ---------------------------------------------------------------- quota */
$('q-scope').onchange = () => {
  $('q-workspace-wrap').hidden = $('q-scope').value !== 'workspace';
  $('q-user-wrap').hidden = $('q-scope').value !== 'user';
};

async function loadQuota() {
  if (!state.cache.users) await loadAccess();
  const [policies, top] = await Promise.all([
    api('/admin/quota-policies'), api('/admin/usage/top-users?days=7'),
  ]);

  // มัดอยู่ในลิสต์เดียวกับโมเดล เพราะเป็นคำตอบของคำถามเดียวกันว่า "ใช้กับอะไร"
  const bundles = state.cache.accessGroups || [];
  $('q-model').innerHTML = '<option value="">— all models —</option>'
    + (state.cache.models || [])
      .map((m) => `<option value="${esc(m.alias)}">${esc(m.alias)}</option>`).join('')
    + (bundles.length
      ? `<optgroup label="มัดโมเดล">${bundles.map((g) =>
          `<option value="group:${esc(g.id)}">${esc(g.name)} · ${
            (g.models || []).length} โมเดล</option>`).join('')}</optgroup>`
      : '');
  const groupNames = Object.fromEntries(bundles.map((g) => [g.id, g.name]));

  const users = Object.fromEntries((state.cache.users || []).map((u) => [u.id, u.external_id]));
  const workspaces = Object.fromEntries((state.cache.workspaces || []).map((c) => [c.id, c.code]));
  const lim = (v) => (v ? num(v) : '∞');

  $('quota-table').innerHTML = `
    <tr><th>Scope</th><th>Applies to</th><th>Model</th><th>Window</th>
        <th class="num">Requests</th><th class="num">Input</th>
        <th class="num">Output</th><th class="num">Images</th>
        <th class="num">Per minute</th><th></th></tr>
    ${policies.data.map((p) => `<tr>
      <td><span class="pill mute">${esc(p.scope)}</span>${p.name
        ? `<div class="hint">${esc(p.name)}</div>` : ''}</td>
      <td>${esc(users[p.user_id] || workspaces[p.workspace_id] || 'everyone')}</td>
      <td>${p.access_group_id
        ? `<code>${esc(groupNames[p.access_group_id] || 'bundle')}</code>
           <div class="hint">มัด</div>`
        : `<code>${esc(p.model_alias || 'all')}</code>`}</td>
      <td>${esc(p.window)}${p.expires_at
        ? `<div class="hint">ถึง ${esc(stamp(p.expires_at).toLocaleDateString())}</div>` : ''}</td>
      <td class="num">${lim(p.max_requests)}</td><td class="num">${lim(p.max_input_tokens)}</td>
      <td class="num">${lim(p.max_output_tokens)}</td><td class="num">${lim(p.max_images)}</td>
      <td class="num">${p.max_requests_per_minute || p.max_tokens_per_minute
        ? `${lim(p.max_requests_per_minute)} req<div class="hint">${
            lim(p.max_tokens_per_minute)} tok</div>`
        : '<span class="hint">ไม่จำกัด</span>'}</td>
      <td style="white-space:nowrap">
        <button class="ghost small" data-edit-policy="${esc(p.id)}"
          data-name="${esc(p.name || p.scope)}">Edit</button>${p.expires_at
        ? `<button class="ghost small" data-extend-policy="${esc(p.id)}"
             data-name="${esc(p.name || p.scope)}">ต่ออายุ</button>` : ''}
        <button class="danger small" data-del-policy="${esc(p.id)}">Delete</button></td>
    </tr>`).join('') || '<tr><td class="empty">No policies — the gateway.yaml defaults apply.</td></tr>'}`;

  // แก้ลิมิตของนโยบายที่มีอยู่แล้วในที่เดิม · ก่อนหน้านี้ทำได้ทางเดียวคือลบแล้วสร้างใหม่
  // ซึ่งครึ่งหลัง (สร้างใหม่) คือครึ่งที่คนลืม แล้วเป้าหมายที่เหลือไม่มีนโยบายก็ร่วงไป
  // ใช้ตัวที่กว้างกว่าเงียบ ๆ · scope/เป้าหมายแก้ไม่ได้ที่นี่ตั้งใจ — เปลี่ยนพวกนั้น
  // = คนละนโยบาย ให้สร้างใบใหม่แทน
  for (const btn of $('quota-table').querySelectorAll('[data-edit-policy]')) {
    btn.onclick = () => {
      const p = policies.data.find((x) => x.id === btn.dataset.editPolicy);
      if (!p) return;
      const fld = (id, label, val) => `
        <div class="field"><label for="${id}">${label}</label>
          <input type="number" id="${id}" min="0" step="1" value="${val || 0}"></div>`;
      modal(`แก้ลิมิต "${btn.dataset.name}"`, `
        <p class="hint">0 = ไม่จำกัด · นับใหม่ทุก window</p>
        <div class="row">
          ${fld('e-req', 'Max requests', p.max_requests)}
          ${fld('e-in', 'Max input tokens', p.max_input_tokens)}
          ${fld('e-out', 'Max output tokens', p.max_output_tokens)}
        </div>
        <div class="row">
          ${fld('e-img', 'Max images', p.max_images)}
          ${fld('e-rpm', 'Requests/นาที', p.max_requests_per_minute)}
          ${fld('e-tpm', 'Tokens/นาที', p.max_tokens_per_minute)}
        </div>
        <div class="row">
          <div class="field"><label for="e-window">Window</label>
            <select id="e-window">
              ${['day', 'month', 'term'].map((w) =>
                `<option value="${w}" ${p.window === w ? 'selected' : ''}>${w}</option>`).join('')}
            </select></div>
        </div>
        <p class="hint" id="e-note"></p>
        <button class="primary" id="e-save">บันทึก</button>`);
      $('e-save').onclick = async () => {
        try {
          await patch(`/admin/quota-policies/${p.id}`, {
            max_requests: Number($('e-req').value) || 0,
            max_input_tokens: Number($('e-in').value) || 0,
            max_output_tokens: Number($('e-out').value) || 0,
            max_images: Number($('e-img').value) || 0,
            max_requests_per_minute: Number($('e-rpm').value) || 0,
            max_tokens_per_minute: Number($('e-tpm').value) || 0,
            window: $('e-window').value,
          });
          $('modal').close();
          await loadQuota();
          banner('error', 'ok', 'แก้ลิมิตแล้ว');
        } catch (e) { $('e-note').textContent = e.message; }
      };
    };
  }

  // นโยบายที่เจาะจงกว่าชนะเสมอ (user > workspace > global) การลบตัวหนึ่งจึงไม่ได้แค่
  // หายไป แต่ทำให้คนที่เคยอยู่ใต้มันเลื่อนไปใช้ตัวถัดไป — ต้องบอกให้เห็นก่อนกด
  for (const btn of $('quota-table').querySelectorAll('[data-extend-policy]')) {
    btn.onclick = async () => {
      const answer = prompt(`ต่ออายุนโยบาย "${btn.dataset.name}" อีกกี่วัน?\n\n`
        + 'นับจากวันนี้ · เว้นว่างแล้วกด OK = ไม่มีวันหมดอายุ', '7');
      if (answer === null) return;
      const days = answer.trim() === '' ? null : Number(answer);
      if (days !== null && (!Number.isFinite(days) || days < 1)) {
        showError('ใส่จำนวนวันเป็นตัวเลขตั้งแต่ 1 ขึ้นไป'); return;
      }
      try {
        await patch(`/admin/quota-policies/${btn.dataset.extendPolicy}`, { days });
        await loadQuota();
      } catch (e) { showError(e.message); }
    };
  }
  for (const btn of $('quota-table').querySelectorAll('[data-del-policy]')) {
    btn.onclick = async () => {
      if (!confirm('ลบนโยบายนี้?\n\nคนที่เคยอยู่ใต้นโยบายนี้จะเลื่อนไปใช้ตัวที่กว้างกว่า '
        + '(user → workspace → global) โควตาที่ใช้ได้จริงจะเปลี่ยนทันที')) return;
      try { await del(`/admin/quota-policies/${btn.dataset.delPolicy}`); await loadQuota(); }
      catch (e) { showError(e.message); }
    };
  }

  // A leaderboard reads better with a bar than a column of digits: the tokens
  // column carries an inline share-of-the-top bar so the heaviest users show at
  // a glance. The width is relative to the top user, not the total.
  const maxTok = Math.max(1, ...top.data.map((r) => r.total_tokens || 0));
  $('top-users').innerHTML = `
    <tr><th>User</th><th>Name</th><th class="num">Requests</th>
        <th>Total tokens</th><th class="num">Images</th></tr>
    ${top.data.map((r) => `<tr>
      <td><code>${esc(r.external_id || r.user_id || '—')}</code></td>
      <td>${esc(r.display_name || '')}</td>
      <td class="num">${num(r.requests)}</td>
      <td><div class="usebar"><span class="usebar-n">${num(r.total_tokens)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${
          Math.max(2, Math.round((r.total_tokens || 0) / maxTok * 100))
        }%;background:linear-gradient(90deg,var(--c1),var(--c3))"></div></div></div></td>
      <td class="num">${num(r.images)}</td></tr>`).join('')
      || '<tr><td class="empty">No usage yet.</td></tr>'}`;
}

$('create-quota').onclick = async () => {
  const scope = $('q-scope').value;
  try {
    await post('/admin/quota-policies', {
      scope,
      workspace_id: scope === 'workspace' ? $('q-workspace').value : null,
      user_id: scope === 'user' ? $('q-user').value : null,
      model_alias: $('q-model').value.startsWith('group:') ? null : ($('q-model').value || null),
      access_group_id: $('q-model').value.startsWith('group:')
        ? $('q-model').value.slice(6) : null,
      window: $('q-window').value,
      name: $('q-name').value.trim(),
      expires_in_days: Number($('q-days').value) || null,
      max_requests: Number($('q-req').value) || 0,
      max_input_tokens: Number($('q-in').value) || 0,
      max_output_tokens: Number($('q-outt').value) || 0,
      max_images: Number($('q-img').value) || 0,
      max_requests_per_minute: Number($('q-rpm').value) || 0,
      max_tokens_per_minute: Number($('q-tpm').value) || 0,
    });
    await loadQuota();
  } catch (e) { showError(e.message); }
};

/* -------------------------------------------------------- my account */
async function loadAccount() {
  const [me, keys, catalog] = await Promise.all([
    api('/v1/me'), api('/v1/me/api-keys'), api('/v1/catalog'),
  ]);
  state.me = me;
  state.cache.catalog = catalog;
  renderQuotaInto('my-quota', me);

  $('key-count').textContent = `${keys.active} of ${keys.limit} active`;
  $('new-key').disabled = keys.active >= keys.limit;

  $('my-keys').innerHTML = `
    <tr><th>Name</th><th>Prefix</th><th>Created</th><th>Expires</th>
        <th>Last used</th><th>State</th><th></th></tr>
    ${keys.data.map((k) => `<tr>
      <td>${esc(k.name || '—')}</td>
      <td><code>${esc(k.key_prefix)}…</code></td>
      <td class="hint">${k.created_at ? new Date(k.created_at).toLocaleDateString() : '—'}</td>
      <td class="hint">${k.expires_at ? new Date(k.expires_at).toLocaleDateString() : 'never'}</td>
      <td class="hint">${k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'never'}</td>
      <td><span class="pill ${k.revoked ? 'err' : 'ok'}">${k.revoked ? 'revoked' : 'active'}</span></td>
      <td>${k.revoked ? '' : `<button class="danger small" data-revoke-mine="${esc(k.id)}">Revoke</button>`}</td>
    </tr>`).join('') || '<tr><td class="empty">No keys yet. Create one to use the API.</td></tr>'}`;

  for (const btn of $('my-keys').querySelectorAll('[data-revoke-mine]')) {
    btn.onclick = async () => {
      if (!confirm('Revoke this key? Anything using it stops working immediately.')) return;
      try { await del(`/v1/me/api-keys/${btn.dataset.revokeMine}`); await loadAccount(); }
      catch (e) { showError(e.message); }
    };
  }

  // A model can sit in more than one section of the catalogue; here it is a
  // flat list of what this person may call, so each one appears once.
  const seen = new Set();
  const models = catalog.sections.flatMap((section) => section.models)
    .filter((m) => !seen.has(m.id) && seen.add(m.id));
  $('my-models').innerHTML = accessNote(catalog.access) + modelList(
    models, 'ยังไม่มีโมเดลที่เปิดให้คุณใช้ · ติดต่อผู้ดูแลของคุณ',
  );
}

$('new-key').onclick = async () => {
  const name = prompt('What is this key for? (e.g. laptop, CI, Claude Code)');
  if (name === null) return;
  try {
    const created = await post('/v1/me/api-keys', { name: name.trim() });
    $('new-key-out').innerHTML = `<div class="secret">
      <strong>Store this key now — it cannot be retrieved again.</strong>
      <code class="mono">${esc(created.api_key)}</code>
      <button class="ghost small" id="copy-new-key">Copy</button></div>`;
    $('copy-new-key').onclick = () => navigator.clipboard.writeText(created.api_key);
    await loadAccount();
  } catch (e) { showError(e.message); }
};

$('change-password').onclick = async () => {
  flash('pw-status', '', 'changing…');
  try {
    await post('/auth/password', {
      current_password: $('pw-current').value,
      new_password: $('pw-new').value,
    });
    $('pw-current').value = ''; $('pw-new').value = '';
    flash('pw-status', 'ok', 'changed — other sessions signed out');
  } catch (e) { flash('pw-status', 'err', e.message); }
};

/* ------------------------------------------------------------- sign in */
function showSignIn(needsSetup) {
  for (const sec of document.querySelectorAll('main > section')) sec.hidden = true;
  $('tab-signin').hidden = false;
  document.querySelector('#tabs').hidden = true;
  $('signout').hidden = true;
  $('whoami').hidden = false;
  $('whoami-chip').hidden = true;
  $('fleet').hidden = true;
  $('whoami').textContent = needsSetup ? 'first-run setup' : 'not signed in';
  $('chat-open').hidden = true;
  $('chat').hidden = true;
  $('signin-title').textContent = needsSetup ? 'Create the first administrator' : 'Sign in';
  $('signin-hint').textContent = needsSetup
    ? 'This instance has no accounts yet. The account you create here is the administrator.'
    : 'Use the account your administrator gave you.';
  $('signin').textContent = needsSetup ? 'Create administrator' : 'Sign in';
  $('display-name-field').hidden = !needsSetup;
  state.needsSetup = needsSetup;
}

$('signin').onclick = async () => {
  flash('signin-status', '', 'checking…');
  const body = {
    username: $('username').value.trim(),
    password: $('password').value,
  };
  if (state.needsSetup) body.display_name = $('display-name').value.trim();

  // Signing in and loading the page are two different things that fail for two
  // different reasons. They used to share a catch, so anything that went wrong
  // while loading was reported next to the password box - the console telling
  // someone their correct password was wrong.
  try {
    await post(state.needsSetup ? '/auth/setup' : '/auth/login', body);
  } catch (e) {
    flash('signin-status', 'err', e.message.replace(/^[A-Z_]+: /, ''));
    return;
  }
  $('password').value = '';

  if (!await sessionTook()) return;

  flash('signin-status', '', '');
  document.querySelector('#tabs').hidden = false;
  // ทำก่อนโหลดข้อมูล — ปุ่มพับเป็นเรื่องของโครงหน้า ไม่ได้ขึ้นกับว่าข้อมูลมาครบไหม
  setupFoldSections();
  $('fold-all').onclick = () => foldAllInTab(true);
  $('unfold-all').onclick = () => foldAllInTab(false);
  try {
    await load();
    showTab('dashboard');
  } catch (e) {
    showError(e.message);
  }
};

/** Did the browser actually keep the cookie the server just set?
 *
 * It can refuse, and say nothing. A gateway reachable at both https://host and
 * http://host:8080 sets one cookie name over both; the https one carries
 * `Secure`, and a browser will not let an insecure page overwrite a Secure
 * cookie of the same name. So the sign-in returns 200, the cookie is dropped on
 * the floor, and the next call comes back "No API key provided" - which reads
 * like the password was wrong, and is the one thing it was not.
 */
async function sessionTook() {
  let status;
  try {
    status = await api('/auth/status');
  } catch {
    return true;   // ตอบไม่ได้ว่าเกิดอะไร อย่าเดา ปล่อยให้ load() รายงานเอง
  }
  if (status.session) return true;

  const secure = location.protocol === 'https:';
  const other = secure
    ? `http://${location.hostname}:8080/console/`
    : `https://${location.hostname}/console/`;
  flash('signin-status', 'err',
    'รหัสผ่านถูกต้อง แต่เบราว์เซอร์ไม่ยอมเก็บคุกกี้ของรอบนี้ — มักเกิดเมื่อเคยเข้าผ่าน'
    + ` ${secure ? 'http' : 'https'} ของเครื่องเดียวกันมาก่อน ให้เข้าผ่าน ${other}`
    + ' หรือล้างคุกกี้ของโฮสต์นี้แล้วลองใหม่');
  return false;
}
for (const id of ['username', 'password']) {
  $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') $('signin').click(); });
}

$('signout').onclick = async () => {
  try { await post('/auth/logout'); } catch { /* signing out locally regardless */ }
  state.me = null;
  showSignIn(false);
};


/* -------------------------------------------------------- assistant */
/* History lives in this tab and nowhere else. The gateway stores no prompts
   (PRD 11), and the assistant must not be the exception that quietly starts. */
const CHAT_STORE = 'litegate_chat';

function chatHistory() {
  try { return JSON.parse(sessionStorage.getItem(CHAT_STORE) || '[]'); }
  catch { return []; }
}

function saveChat(messages) {
  try { sessionStorage.setItem(CHAT_STORE, JSON.stringify(messages.slice(-24))); }
  catch { /* private mode: the conversation just does not survive a reload */ }
}

function renderChat() {
  const log = $('chat-log');
  const messages = chatHistory();
  log.innerHTML = messages.length ? messages.map((m) => `
    <div class="chat-msg ${m.role === 'user' ? 'user' : 'bot'}">${formatReply(m.content)}</div>
  `).join('') : `<div class="hint">
      Ask about the models you can use, your quota, or an error you just hit.
      Answers come from this deployment's current state.
    </div>`;
  log.scrollTop = log.scrollHeight;
}

function stripThinking(text) {
  // Some models narrate before answering even when told not to. The real fix is
  // server-side - vLLM's --reasoning-parser puts the chain of thought in
  // reasoning_content instead of content, and the probe now reports when it is
  // missing. Until an operator acts on that, salvage what we can here.
  //
  // Never strip to nothing: an earlier version allowed "to the end of the text"
  // as a terminator and a numbered thinking list swallowed the whole reply,
  // leaving an empty bubble. Every branch falls back to the raw text.
  const raw = String(text);

  // The closing tag is the reliable one. Qwen3 and DeepSeek-R1 chat templates
  // prefill `<think>` themselves, so it is already in the prompt and never
  // reaches the reply - content starts mid-thought and ends with `</think>`.
  // Match on the last close tag and ignore whether an opening tag exists.
  const closed = raw.lastIndexOf('</think>');
  if (closed !== -1) {
    const after = raw.slice(closed + '</think>'.length).trim();
    if (after) return after;
  }

  // "Final Answer:" is the model announcing the end of its own deliberation.
  // Take the last one - it often rehearses several candidates first.
  const finals = [...raw.matchAll(/(?:^|\n)\s*(?:\*\*)?Final Answer:?(?:\*\*)?\s*/gi)];
  if (finals.length) {
    const last = finals[finals.length - 1];
    const answer = raw.slice(last.index + last[0].length).trim()
      // The announced answer usually arrives quoted; the quotes are punctuation
      // from the narration, not part of what the model means to say.
      .replace(/^["'\u201c\u2018]|["'\u201d\u2019]$/g, '')
      .trim();
    if (answer) return answer;
  }

  // Greedy on purpose: the answer is after the *last* blank line, not the
  // first, because the narration itself contains blank lines between steps.
  const narrated = raw.match(/^\s*Thinking Process:[\s\S]*\n\s*\n([\s\S]*)$/i);
  if (narrated) return narrated[1].trimStart() || raw;

  return raw;
}

function formatReply(text) {
  text = stripThinking(text);
  // Fenced code becomes a block; everything else is escaped text. No markdown
  // renderer: the assistant's output is untrusted input to this page.
  const parts = String(text).split(/```(?:[a-z]*)\n?/);
  return parts.map((part, index) => (index % 2
    ? `<pre>${esc(part.replace(/\n$/, ''))}</pre>`
    : esc(part).replace(/\n/g, '<br>'))).join('');
}

async function sendChat() {
  const input = $('chat-text');
  const question = input.value.trim();
  if (!question) return;

  const messages = chatHistory();
  messages.push({ role: 'user', content: question });
  saveChat(messages);
  input.value = '';
  renderChat();

  const log = $('chat-log');
  const bubble = document.createElement('div');
  bubble.className = 'chat-msg bot';
  bubble.textContent = '…';
  log.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
  $('chat-send').disabled = true;

  try {
    const response = await fetch('/v1/assistant/chat', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error((body.error || {}).message || `request failed (${response.status})`);
    }

    // Stream the reply so a slow local model still feels responsive.
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let reply = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        try {
          const chunk = JSON.parse(payload);
          if (chunk.error) throw new Error(chunk.error.message || 'stream failed');
          const piece = chunk.choices?.[0]?.delta?.content;
          if (piece) {
            reply += piece;
            const shown = formatReply(reply);
            // While the model is still narrating, show that it is working
            // rather than an empty bubble.
            bubble.innerHTML = shown.trim() ? shown : '<span class="hint">thinking…</span>';
            log.scrollTop = log.scrollHeight;
          }
        } catch (err) {
          if (err instanceof Error && err.message !== 'Unexpected end of JSON input') throw err;
        }
      }
    }

    messages.push({ role: 'assistant', content: reply || '(no answer)' });
    saveChat(messages);
    renderChat();
  } catch (e) {
    bubble.className = 'chat-msg err';
    bubble.textContent = e.message;
  } finally {
    $('chat-send').disabled = false;
    input.focus();
  }
}

async function initAssistant() {
  try {
    const status = await api('/v1/assistant/status');
    // Hidden, not disabled: an assistant with no model to answer with is not a
    // feature people should be looking at.
    $('chat-open').hidden = !status.available;
    if (status.available) $('chat-model').textContent = status.display_name || status.model;
  } catch {
    $('chat-open').hidden = true;
  }
}

$('chat-open').onclick = () => { $('chat').hidden = false; $('chat-open').hidden = true; renderChat(); $('chat-text').focus(); };
$('chat-close').onclick = () => { $('chat').hidden = true; $('chat-open').hidden = false; };
$('chat-clear').onclick = () => { saveChat([]); renderChat(); };
$('chat-send').onclick = sendChat;
$('chat-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

/* ----------------------------------------------------------------- boot */
async function load() {
  showError('');
  const me = await api('/v1/me');
  state.me = me;
  showWho(me);
  $('signout').hidden = false;
  applyRole(me.role);
  await initAssistant();
  renderQuota(me);
  const catalog = await api('/v1/catalog');
  renderCatalog(catalog);
  const models = catalog.sections.flatMap((s) => s.models);
  let health = null;
  if (me.role === 'admin') {
    health = (await api('/v1/health/endpoints')).data;
    renderHealth(health);
  }
  showFleet(new Set(models.map((m) => m.id)).size, health);
  if (me.role === 'admin' || me.role === 'manager') {
    const [summary, daily] = await Promise.all([
      api('/admin/usage/summary?days=14'),
      api('/admin/usage/daily?days=14'),
    ]);
    renderUsage(summary, daily);
    loadSavings().catch(() => { /* รายงานเสริม ล้มแล้วไม่ควรทำให้หน้าแดชบอร์ดพัง */ });
    const autoPanel = $('auto-panel');
    if (autoPanel) {
      autoPanel.hidden = false;
      $('auto-refresh').onclick = () => loadAutoPreview().catch((e) => showError(e.message));
      loadAutoPreview().catch(() => { /* เสริมเหมือนกัน */ });
    }
  }
}

function showWho(me) {
  const name = me.display_name || me.external_id;
  $('whoami').hidden = true;
  $('whoami-chip').hidden = false;
  $('whoami-initials').textContent = name.trim().slice(0, 2).toUpperCase();
  $('whoami-name').textContent = name;
  $('whoami-role').textContent = me.role;
}

// A gateway console should say what the fleet is doing in the frame, not only
// on whichever page happens to be open. Members see how many models they can
// reach; admins also see how many machines are answering, because that is the
// number they came to look at.
function showFleet(modelCount, health) {
  const chip = $('fleet');
  if (!modelCount && !health) { chip.hidden = true; return; }

  let dot = 'ok';
  let backends = '';
  if (health) {
    const rows = Object.values(health);
    const up = rows.filter((r) => r.healthy).length;
    backends = ` · ${up}/${rows.length} backends`;
    dot = up === rows.length ? 'ok' : up ? 'warn' : 'err';
  }
  chip.hidden = false;
  chip.className = `status ${dot}`;
  chip.innerHTML = `<span class="status-dot"></span>${modelCount} models${esc(backends)}`;
}

$('refresh').onclick = () => {
  const active = document.querySelector('#tabs button[aria-selected="true"]');
  load()
    .then(() => { if (active && active.dataset.tab !== 'dashboard') showTab(active.dataset.tab); })
    .catch((e) => showError(e.message));
};

(async function boot() {
  paintIcons();
  let status;
  try {
    status = await api('/auth/status');
  } catch (e) {
    showSignIn(false);
    flash('signin-status', 'err', e.message);
    return;
  }
  if (!status.session) { showSignIn(status.needs_setup); return; }

  // Signed in already. If a panel fails to load, say so on the page rather than
  // throwing the sign-in card back up — being asked to log in again is a bad
  // way to learn that one endpoint was unhappy.
  document.querySelector('#tabs').hidden = false;
  try {
    await load();
    showTab('dashboard');
  } catch (e) {
    showError(e.message);
  }
})();
