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
    access: loadAccess, quota: loadQuota,
  };
  if (loaders[name]) loaders[name]().catch((e) => showError(e.message));
}
for (const btn of document.querySelectorAll('#tabs button')) {
  btn.onclick = () => showTab(btn.dataset.tab);
}

function applyRole(role) {
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
function renderQuota(me) {
  renderQuotaInto('quota', me);
}

function renderQuotaInto(target, me) {
  const { used, limits } = me.quota;
  const row = (label, u, limit) => {
    const pct = limit ? Math.min(100, Math.round((u / limit) * 100)) : 0;
    const cls = !limit ? 'mute' : pct >= 100 ? 'err' : pct >= 80 ? 'warn' : 'ok';
    return `<tr><td>${label}</td><td class="num">${num(u)}</td>
      <td class="num">${limit ? num(limit) : 'unlimited'}</td>
      <td><span class="pill ${cls}">${limit ? pct + '%' : 'n/a'}</span></td></tr>`;
  };
  $(target).innerHTML = `
    <tr><th>Resource</th><th class="num">Used</th><th class="num">Limit</th><th>Status</th></tr>
    ${row('Requests', used.requests, limits.max_requests)}
    ${row('Input tokens', used.input_tokens, limits.max_input_tokens)}
    ${row('&nbsp;&nbsp;· text', used.text_input_tokens, 0)}
    ${row('&nbsp;&nbsp;· visual', used.visual_input_tokens, 0)}
    ${row('Output tokens', used.output_tokens, limits.max_output_tokens)}
    ${row('Images', used.images, limits.max_images)}
    <tr><td colspan="4" class="empty">Window: ${esc(me.quota.window)} ·
      resets ${new Date(me.quota.window_end).toLocaleString()}</td></tr>`;
}

// Inline because a gateway may run air-gapped: no icon font, no CDN, no build.
// Stroked rather than filled so one set reads correctly in both themes.
const ICONS = {
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
};

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

function renderCatalog(data) {
  const sections = data.sections.map((s) => `
    <h3 class="model-section">${esc(s.title)}
      <span class="hint">${s.models.length} โมเดล</span></h3>
    ${modelList(s.models, 'ไม่มีโมเดลในหมวดนี้')}`).join('');
  $('catalog').innerHTML = accessNote(data.access)
    + (sections || '<div class="empty">ยังไม่มีโมเดลที่เปิดให้คุณใช้ · ติดต่อผู้ดูแลของคุณ</div>');
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

function renderUsage(summary) {
  if (!summary.by_model.length) {
    $('usage').innerHTML = '<tr><td class="empty">No usage recorded yet.</td></tr>';
    return;
  }
  $('usage').innerHTML = `
    <tr><th>Model</th><th class="num">Requests</th><th class="num">Text in</th>
        <th class="num">Visual in</th><th class="num">Out</th><th class="num">Images</th>
        <th class="num">Avg ms</th><th class="num">TTFT ms</th></tr>
    ${summary.by_model.map((r) => `<tr>
      <td><code>${esc(r.model)}</code></td><td class="num">${num(r.requests)}</td>
      <td class="num">${num(r.text_input_tokens)}</td><td class="num">${num(r.visual_input_tokens)}</td>
      <td class="num">${num(r.output_tokens)}</td><td class="num">${num(r.images)}</td>
      <td class="num">${r.avg_latency_ms}</td><td class="num">${r.avg_ttft_ms ?? '-'}</td></tr>`).join('')}
    ${summary.errors.length ? `<tr><td colspan="8" class="empty">Errors: ${
      summary.errors.map((e) => esc(e.code) + ' ×' + e.count).join(', ')}</td></tr>` : ''}`;
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
  const endpointRow = (m, e, isLast) => {
    const dead = !e.health?.healthy;
    const speaks = [
      e.protocols?.openai ? 'OpenAI' : '',
      e.protocols?.anthropic ? 'Anthropic' : '',
      e.modalities?.image ? 'images' : '',
    ].filter(Boolean);
    return `<tr class="ep-row${isLast ? ' ep-last' : ''}">
      <td><span class="ep-mark">↳</span> <code>${esc(e.name)}</code>
          <div class="hint">${esc(e.server_type)}</div></td>
      <td class="mono">${esc(e.base_url)}</td>
      <td>${speaks.map((s) => `<span class="badge">${esc(s)}</span>`).join(' ')}</td>
      <td>${e.enabled === false
            ? '<span class="pill mute">ปิดไว้</span>'
            : `<span class="pill ${dead ? 'err' : 'ok'}">${dead ? 'down' : 'up'}</span>`}</td>
      <td class="ep-tune">
        <label>priority
          <input type="number" min="0" max="1000" value="${esc(e.priority)}"
            data-tune="priority" data-model="${esc(m.alias)}" data-ep="${esc(e.name)}"></label>
        <label>พร้อมกัน
          <input type="number" min="1" max="4096" value="${esc(e.max_concurrency)}"
            data-tune="max_concurrency" data-model="${esc(m.alias)}"
            data-ep="${esc(e.name)}"></label>
        ${e.health?.in_flight ? `<span class="hint">กำลังวิ่ง ${esc(e.health.in_flight)}</span>` : ''}
      </td>
      <td><button class="ghost small" data-ep-model="${esc(m.alias)}" data-ep="${esc(e.name)}"
            data-ep-to="${e.enabled === false ? '1' : '0'}"
            >${e.enabled === false ? 'เปิดเครื่องนี้' : 'ปิดเครื่องนี้'}</button></td>
    </tr>`;
  };

  $('model-table').innerHTML = `
    <tr><th>Alias / backend</th><th>Upstream / address</th><th>Capabilities · declared</th>
        <th>Health</th><th>Test status · measured</th><th></th></tr>
    ${registry.data.map((m, i) => {
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
      return `<tr class="alias-row">
        <td><code>${esc(m.alias)}</code>${m.enabled === false
              ? ' <span class="pill mute">off</span>' : ''}
            <div class="hint">${esc(m.display_name)}</div></td>
        <td><code>${esc(m.upstream_model)}</code></td>
        <td>${m.badges.map((b) => `<span class="badge">${esc(b)}</span>`).join(' ')}</td>
        <td><span class="pill ${cls}">${healthy}/${total} up</span>
            ${off ? `<div class="hint">ปิดไว้ ${off}</div>` : ''}</td>
        <td><span class="pill ${ccls}">${esc(c.status)}</span>
            <div class="hint">${last
              ? `วัดล่าสุด ${esc(last.toLocaleString())}`
              : 'ยังไม่เคยวัด — ป้ายทางซ้ายเป็นสิ่งที่ประกาศไว้เท่านั้น'}</div>
            ${failed.length
              ? `<div class="hint" style="color:var(--bad)">ไม่ผ่าน: ${failed.map(esc).join(', ')}</div>`
              : ''}
            <div class="hint" id="run-${esc(m.alias)}"></div></td>
        <td style="white-space:nowrap">
          <button class="ghost small" data-verify="${esc(m.alias)}">Verify</button>
          <button class="ghost small" data-test="${esc(m.alias)}">Run tests</button>
          <button class="ghost small" data-edit="${esc(m.alias)}">Edit</button>
          <button class="ghost small" data-enable="${esc(m.alias)}"
            data-to="${m.enabled === false ? '1' : '0'}"
            >${m.enabled === false ? 'Enable' : 'Disable'}</button>
          <button class="danger small" data-del="${esc(m.alias)}">Delete</button>
        </td></tr>
        ${m.endpoints.map((e, n) => endpointRow(m, e, n === m.endpoints.length - 1)).join('')}`;
    }).join('')}`;

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

async function loadAccess() {
  const [users, workspaces, keys, groups] = await Promise.all([
    api('/admin/users'), api('/admin/workspaces'), api('/admin/api-keys'),
    api('/admin/access-groups'),
  ]);
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
    <tr><th>ID</th><th>Name</th><th>Role</th><th>Workspaces</th><th>Status</th></tr>
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
            return `<span class="pill mute">${esc(code)}<button class="link small"
              data-leave="${esc(ws ? ws.id : '')}" data-who="${esc(u.id)}"
              title="เอา ${esc(u.external_id)} ออกจาก ${esc(code)}">×</button></span>`;
          }).join(' ') || '<span class="hint">—</span>'}
          ${spare.length ? `<select class="small" data-join="${esc(u.id)}">
            <option value="">+ add…</option>
            ${spare.map((c) => `<option value="${esc(c.id)}">${esc(c.code)}</option>`).join('')}
          </select>` : ''}
        </td>
        <td><span class="pill ${u.status === 'active' ? 'ok' : 'mute'}">${esc(u.status || 'active')}</span></td>
      </tr>`;
    }).join('') || '<tr><td class="empty">No people yet.</td></tr>'}`;

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
  $('key-table').innerHTML = `
    <tr><th>Prefix</th><th>User</th><th>Workspace</th><th>Label</th>
        <th>Expires</th><th>Last used</th><th>State</th><th></th></tr>
    ${keys.data.map((k) => {
      const u = byId[k.user_id];
      const workspace = workspaces.data.find((c) => c.id === k.workspace_id);
      return `<tr>
        <td><code>${esc(k.key_prefix)}…</code></td>
        <td>${esc(u ? u.external_id : k.user_id)}</td>
        <td>${esc(workspace ? workspace.code : '—')}</td>
        <td>${esc(k.name || '—')}${k.kind === 'service'
            ? ' <span class="pill mute">service</span>' : ''}${(k.models || []).length
          ? `<div class="hint">เฉพาะ ${(k.models || []).map(esc).join(', ')}</div>` : ''}${
          (k.access_groups || []).length
          ? `<div class="hint">มัด ${(k.access_groups || []).length} ชุด</div>` : ''}</td>
        <td class="hint">${k.expires_at ? new Date(k.expires_at).toLocaleDateString() : 'never'}</td>
        <td class="hint">${k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'never'}</td>
        <td><span class="pill ${k.revoked ? 'err' : 'ok'}">${k.revoked ? 'revoked' : 'active'}</span></td>
        <td>${k.revoked
          ? `<button class="small" data-purge="${esc(k.id)}" title="ลบแถวนี้ถาวร — ประวัติการใช้งานยังอยู่">Delete</button>`
          : `<button class="danger small" data-revoke="${esc(k.id)}">Revoke</button>`}</td>
      </tr>`;
    }).join('') || '<tr><td class="empty">No keys issued yet.</td></tr>'}`;

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
    <tr><th>Code</th><th>Name</th><th>Term</th><th>Allowed models</th><th></th></tr>
    ${workspaces.data.map((c) => {
      const held = c.status === 'suspended';
      return `<tr${held ? ' class="ws-suspended"' : ''}>
      <td><code>${esc(c.code)}</code>${held
        ? ' <span class="pill mute">ระงับอยู่</span>' : ''}</td>
      <td>${esc(c.name)}</td><td>${esc(c.term)}</td>
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
  paintIcons($('workspace-table'));
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
      <strong>Store this key now — it cannot be retrieved again.</strong>
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
      <td>${esc(p.window)}</td>
      <td class="num">${lim(p.max_requests)}</td><td class="num">${lim(p.max_input_tokens)}</td>
      <td class="num">${lim(p.max_output_tokens)}</td><td class="num">${lim(p.max_images)}</td>
      <td class="num">${p.max_requests_per_minute || p.max_tokens_per_minute
        ? `${lim(p.max_requests_per_minute)} req<div class="hint">${
            lim(p.max_tokens_per_minute)} tok</div>`
        : '<span class="hint">ไม่จำกัด</span>'}</td>
      <td><button class="danger small" data-del-policy="${esc(p.id)}">Delete</button></td>
    </tr>`).join('') || '<tr><td class="empty">No policies — the gateway.yaml defaults apply.</td></tr>'}`;

  // นโยบายที่เจาะจงกว่าชนะเสมอ (user > workspace > global) การลบตัวหนึ่งจึงไม่ได้แค่
  // หายไป แต่ทำให้คนที่เคยอยู่ใต้มันเลื่อนไปใช้ตัวถัดไป — ต้องบอกให้เห็นก่อนกด
  for (const btn of $('quota-table').querySelectorAll('[data-del-policy]')) {
    btn.onclick = async () => {
      if (!confirm('ลบนโยบายนี้?\n\nคนที่เคยอยู่ใต้นโยบายนี้จะเลื่อนไปใช้ตัวที่กว้างกว่า '
        + '(user → workspace → global) โควตาที่ใช้ได้จริงจะเปลี่ยนทันที')) return;
      try { await del(`/admin/quota-policies/${btn.dataset.delPolicy}`); await loadQuota(); }
      catch (e) { showError(e.message); }
    };
  }

  $('top-users').innerHTML = `
    <tr><th>User</th><th>Name</th><th class="num">Requests</th>
        <th class="num">Total tokens</th><th class="num">Images</th></tr>
    ${top.data.map((r) => `<tr>
      <td><code>${esc(r.external_id || r.user_id || '—')}</code></td>
      <td>${esc(r.display_name || '')}</td>
      <td class="num">${num(r.requests)}</td><td class="num">${num(r.total_tokens)}</td>
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
    renderUsage(await api('/admin/usage/summary?days=7'));
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
