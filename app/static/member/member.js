/* LiteGate — หน้าตรวจสอบสิทธิ์สำหรับสมาชิก
 *
 * ต่างจากคอนโซลตรงที่ไม่มี session: ผู้ใช้วาง API key ของตัวเอง แล้วหน้านี้เรียก
 * endpoint ฝั่งสมาชิกด้วย key นั้นตรง ๆ · ทุกอย่างที่แสดงคือสิ่งที่ key ใบนั้นเข้าถึง
 * ได้อยู่แล้ว — หน้านี้ไม่ได้เปิดสิทธิ์อะไรใหม่ แค่ทำให้อ่านง่ายขึ้น
 *
 * ข้อจำกัดที่ตั้งใจ:
 *   - key อยู่ในตัวแปรของหน้า และ (ถ้าผู้ใช้เลือก) ใน sessionStorage เท่านั้น
 *     ไม่ใช่ localStorage — เครื่องในห้องแล็บที่นักเรียนใช้ต่อกันไม่ควรจำ key ข้ามคน
 *   - ไม่เคยใส่ key ลง URL · ไม่เคย log
 *   - ตรวจรูปแบบก่อนส่ง เพราะ header HTTP รับได้แค่ latin-1
 */
'use strict';

const $ = (id) => document.getElementById(id);
const KEY_STORE = 'litegate:member-key';

let apiKey = '';

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* header HTTP รับได้แค่ latin-1 · key ที่ก๊อปมาแล้วติดอักษรไทย ช่องว่าง หรืออีโมจิ
 * จะทำให้ fetch โยน TypeError ซึ่งอ่านแล้วไม่รู้เลยว่าเกิดอะไร — บอกให้ตรงดีกว่า */
function looksLikeKey(value) {
  return /^[\x21-\x7e]+$/.test(value) && value.length >= 12;
}

async function call(path) {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${apiKey}` },
    cache: 'no-store',
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error('key นี้ใช้ไม่ได้ — อาจถูกเพิกถอน หมดอายุ หรือพิมพ์ตกหล่น');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail?.error?.message || `เกตเวย์ตอบ ${response.status}`);
  }
  return response.json();
}

const fmt = (n) => Number(n || 0).toLocaleString();

function when(iso, fallback = '—') {
  if (!iso) return fallback;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? fallback : d.toLocaleString();
}

/* ── ส่วนแสดงผล ─────────────────────────────────────────────────────────── */

function renderWho(me) {
  $('whoami').innerHTML = `
    <div class="who">
      <div class="who-avatar">${esc((me.display_name || me.external_id || '?').trim().slice(0, 2).toUpperCase())}</div>
      <div>
        <div class="who-name">${esc(me.display_name || me.external_id)}</div>
        <div class="hint">${esc(me.external_id)} · บทบาท ${esc(me.role)}${
          me.workspace_id ? ' · วิชา ' + esc(me.workspace_id) : ''}</div>
      </div>
    </div>`;
}

function renderKey(info) {
  const key = info.key;
  if (!key) { $('keycard').hidden = true; return; }
  const expired = key.expires_at && new Date(key.expires_at) < new Date();
  const limits = [];
  if (key.limited_to_models?.length) {
    limits.push(`จำกัดเฉพาะโมเดล: ${key.limited_to_models.map(esc).join(', ')}`);
  }
  if (key.limited_to_groups?.length) {
    limits.push(`จำกัดตามชุด: ${key.limited_to_groups.map(esc).join(', ')}`);
  }
  $('keycard').innerHTML = `
    <h3>key ใบที่กำลังใช้</h3>
    <div class="kv">
      <span>ชื่อใบ</span><span>${esc(key.label || '— ไม่ได้ตั้งชื่อ —')}</span>
      <span>ขึ้นต้นด้วย</span><span class="mono">${esc(key.prefix)}…</span>
      <span>ออกให้เมื่อ</span><span>${when(key.issued_at)}</span>
      <span>หมดอายุ</span><span class="${expired ? 'bad' : ''}">${
        key.expires_at ? when(key.expires_at) : 'ไม่มีวันหมดอายุ'}${expired ? ' (หมดแล้ว)' : ''}</span>
      <span>ใช้ล่าสุด</span><span>${when(key.last_used_at, 'ยังไม่เคยใช้')}</span>
    </div>
    ${limits.length
      ? `<p class="hint">${limits.map((l) => esc(l)).join(' · ')}</p>`
      : '<p class="hint">ใบนี้ไม่ได้จำกัดโมเดลเพิ่ม — ใช้ได้เท่าที่วิชาและบทบาทอนุญาต</p>'}`;
}

function bar(used, limit) {
  if (!limit) return '<span class="hint">ไม่จำกัด</span>';
  const pct = Math.min(100, Math.round((used / limit) * 100));
  const tone = pct >= 90 ? 'bad' : pct >= 75 ? 'warn' : 'ok';
  return `<div class="meter"><span class="${tone}" style="width:${pct}%"></span></div>
    <span class="hint">${fmt(used)} / ${fmt(limit)} (${pct}%)</span>`;
}

function renderQuota(me) {
  const q = me.quota || {};
  const used = q.used || {};
  const limits = q.limits || {};
  $('quota').innerHTML = `
    <h3>โควตาของคุณ</h3>
    <p class="hint">รอบนี้: ${when(q.window_start)} — ${when(q.window_end)} (${esc(q.window || '')})</p>
    <div class="quota-grid">
      <div><div class="hint">คำขอ</div>${bar(used.requests, limits.max_requests)}</div>
      <div><div class="hint">token เข้า</div>${bar(used.input_tokens, limits.max_input_tokens)}</div>
      <div><div class="hint">token ออก</div>${bar(used.output_tokens, limits.max_output_tokens)}</div>
      <div><div class="hint">ภาพ</div>${bar(used.images, limits.max_images)}</div>
    </div>`;
}

function badges(model) {
  const caps = model.capabilities || {};
  const on = Object.entries(caps).filter(([, v]) => v === true).map(([k]) => k);
  const modes = (model.modalities?.input || []);
  return [...new Set([...modes, ...on])]
    .map((b) => `<span class="npill">${esc(b)}</span>`).join('');
}

function renderModels(catalog) {
  const note = catalog.access?.restricted
    ? `<p class="hint">เห็นเท่านี้เพราะถูกจำกัดโดย <strong>${esc(catalog.access.reason)}</strong> ·
       ต้องใช้ตัวอื่นให้ติดต่อผู้ดูแลของคุณ</p>` : '';
  const sections = (catalog.sections || []).map((s) => `
    <details class="model-section-box" open>
      <summary class="model-section">${esc(s.title)}
        <span class="hint">${s.models.length} โมเดล</span></summary>
      <div class="model-list">
        ${s.models.map((m) => `
          <div class="mrow">
            <div>
              <div class="mname">${esc(m.display_name || m.alias)}</div>
              <div class="hint mono">${esc(m.alias)}</div>
              ${m.description ? `<div class="hint">${esc(m.description)}</div>` : ''}
            </div>
            <div class="npills">${badges(m)}</div>
            <div class="hint num">${m.context_window ? fmt(m.context_window) + ' tok' : ''}</div>
          </div>`).join('')}
      </div>
    </details>`).join('');
  $('models').innerHTML = note + (sections
    || '<div class="card"><div class="empty">ยังไม่มีโมเดลที่เปิดให้คุณใช้ · ติดต่อผู้ดูแลของคุณ</div></div>');
}

function renderUsage(usage) {
  const daily = usage.daily || [];
  const peak = Math.max(1, ...daily.map((d) => d.input_tokens + d.output_tokens));
  const rows = daily.slice(-14).map((d) => {
    const total = d.input_tokens + d.output_tokens;
    return `<div class="uday">
      <span class="hint">${esc(d.date)}</span>
      <div class="meter"><span class="ok" style="width:${Math.round(total / peak * 100)}%"></span></div>
      <span class="num">${fmt(total)} tok</span>
      <span class="hint num">${fmt(d.requests)} ครั้ง</span>
    </div>`;
  }).join('');
  const models = (usage.by_model || []).map((m) => `
    <tr><td>${esc(m.model)}</td><td class="num">${fmt(m.requests)}</td>
        <td class="num">${fmt(m.input_tokens)}</td><td class="num">${fmt(m.output_tokens)}</td></tr>`).join('');
  $('usage').innerHTML = daily.length
    ? `<p class="hint">${usage.window_days} วันที่ผ่านมา</p>
       <div class="udays">${rows}</div>
       <table class="tbl"><thead><tr><th>โมเดล</th><th class="num">ครั้ง</th>
         <th class="num">token เข้า</th><th class="num">token ออก</th></tr></thead>
         <tbody>${models}</tbody></table>`
    : '<div class="empty">ยังไม่มีการใช้งานในช่วงนี้</div>';
}

/* ── ทางเดินหลัก ────────────────────────────────────────────────────────── */

async function load() {
  const [me, keyInfo, catalog, usage] = await Promise.all([
    call('/v1/me'), call('/v1/me/key'), call('/v1/catalog'), call('/v1/me/usage?days=14'),
  ]);
  renderWho(me);
  renderKey(keyInfo);
  renderQuota(me);
  renderModels(catalog);
  renderUsage(usage);
  $('gate').hidden = true;
  $('result').hidden = false;
  $('signout').hidden = false;
}

async function check() {
  const value = $('key').value.trim();
  const msg = $('gate-msg');
  msg.textContent = '';
  msg.className = 'hint';
  if (!looksLikeKey(value)) {
    msg.className = 'hint bad';
    msg.textContent = 'รูปแบบ key ไม่ถูกต้อง — ตรวจว่าไม่มีช่องว่างหรืออักษรไทยติดมาตอนก๊อป';
    return;
  }
  apiKey = value;
  $('check').disabled = true;
  try {
    await load();
    if ($('remember').checked) sessionStorage.setItem(KEY_STORE, apiKey);
  } catch (e) {
    apiKey = '';
    msg.className = 'hint bad';
    msg.textContent = e.message;
  } finally {
    $('check').disabled = false;
  }
}

function forget() {
  apiKey = '';
  sessionStorage.removeItem(KEY_STORE);
  $('key').value = '';
  $('result').hidden = true;
  $('gate').hidden = false;
  $('signout').hidden = true;
}

$('check').onclick = check;
$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') check(); });
$('signout').onclick = forget;
$('theme').onclick = () => {
  const root = document.documentElement;
  const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('litegate:theme', next); } catch { /* โหมดส่วนตัว */ }
};

(function boot() {
  try {
    const saved = localStorage.getItem('litegate:theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch { /* โหมดส่วนตัว */ }

  // key ที่จำไว้อยู่ใน sessionStorage เท่านั้น — ปิดแท็บแล้วหายไปเอง
  const saved = sessionStorage.getItem(KEY_STORE);
  if (!saved) return;
  apiKey = saved;
  $('remember').checked = true;
  load().catch(() => forget());
})();
