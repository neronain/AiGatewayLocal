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

/* ไอคอนความสามารถ — วาดเอง ไม่ได้โหลด app.js มาทั้งก้อนเพื่อไอคอนไม่กี่ตัว
 * ผู้ใช้ที่เข้ามาดูว่า "ใช้อะไรได้บ้าง" อ่านสัญลักษณ์เร็วกว่าอ่านคำ */
const CAP_ICONS = {
  Text:      ['ข้อความ',   '<path d="M4 7h16M4 12h10M4 17h13"/>'],
  Image:     ['อ่านภาพได้', '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M4 16l4.5-4.5L15 18"/>'],
  Audio:     ['เสียง',     '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>'],
  Code:      ['เขียนโค้ด',  '<path d="M15.5 18L21 12l-5.5-6M8.5 6L3 12l5.5 6"/>'],
  Tools:     ['เรียกเครื่องมือ', '<path d="M14.7 6.3a4 4 0 0 1-5 5L5 16v3h3l4.7-4.7a4 4 0 0 1 5-5l-2.5 2.5 1.8 1.8L19.5 11a4 4 0 0 1-4.8-4.7z"/>'],
  Reasoning: ['คิดก่อนตอบ', '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9V16h7v-2.1A6 6 0 0 0 12 3z"/>'],
  Agent:     ['ทำงานเป็นเอเจนต์', '<rect x="5" y="8" width="14" height="12" rx="2"/><path d="M12 8V4M9 14h.01M15 14h.01"/>'],
};

function capIcon(badge) {
  const found = CAP_ICONS[badge];
  if (!found) return '';
  const [title, path] = found;
  return `<span class="cap" title="${esc(title)}" aria-label="${esc(title)}">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${path}</svg></span>`;
}

/* ── ส่วนแสดงผล ─────────────────────────────────────────────────────────── */

function renderWho(me) {
  $('whoami').innerHTML = `
    <div class="who">
      <div class="who-avatar">${esc((me.display_name || me.external_id || '?').trim().slice(0, 2).toUpperCase())}</div>
      <div>
        <div class="who-name">${esc(me.display_name || me.external_id)}</div>
        <div class="hint">${esc(me.external_id)} · บทบาท ${esc(me.role)}${
          workspaceLabel(me)}</div>
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

function renderAccessNote(catalog) {
  const box = $('access-note');
  if (!box) return;
  box.innerHTML = catalog.access?.restricted
    ? `<p class="hint">เห็นเท่านี้เพราะถูกจำกัดโดย <strong>${esc(accessReason(catalog.access))}</strong> ·
       ต้องใช้ตัวอื่นให้ติดต่อผู้ดูแลของคุณ</p>`
    : '';
}

/* ชื่อวิชาแบบที่คนอ่านออก — code ถ้ามี ไม่งั้นชื่อเต็ม · id ดิบเป็นทางเลือกสุดท้าย
 * เพราะเลขฐานสิบหก 32 ตัวไม่ได้บอกอะไรกับเจ้าของ key เลย */
function workspaceLabel(me) {
  const w = me.workspace;
  if (!w) return me.workspace_id ? ' · วิชา ' + esc(me.workspace_id) : '';
  // คั่นด้วย em dash ไม่ใช่ · เพราะชื่อวิชาเองก็มี · ได้ แล้วจะอ่านไม่ออกว่าอะไรเป็นอะไร
  const name = [w.code, w.name].filter(Boolean).join(' — ');
  return ' · วิชา ' + esc(w.term ? `${name} (${w.term})` : name);
}

/* แปลเหตุผลที่สิทธิ์ถูกจำกัดเป็นไทย — เซิร์ฟเวอร์ส่งประโยคอังกฤษมาด้วยเพราะ audit log
 * และคอนโซลผู้ดูแลอ่านมันตรง ๆ แต่หน้านี้เป็นไทยทั้งหน้า */
const ACCESS_REASONS = {
  workspace: 'วิชาที่ออก key ใบนี้ให้',
  membership: 'วิชาที่คุณอยู่',
  key: 'รายการโมเดลที่เขียนไว้บน key ใบนี้',
  'workspace+key': 'วิชาที่ออก key ใบนี้ให้ และรายการที่เขียนไว้บนใบนี้',
  'membership+key': 'วิชาที่คุณอยู่ และรายการที่เขียนไว้บนใบนี้',
};

function accessReason(access) {
  return ACCESS_REASONS[access.reason_code] || access.reason || 'สิทธิ์ที่ผู้ดูแลตั้งไว้';
}

/* โมเดลที่ใช้ได้ + ใช้ไปเท่าไร รวมเป็นตารางเดียว
 *
 * เดิมแยกเป็นสองส่วน: แค็ตตาล็อกยาว ๆ ข้างบน แล้วตัวเลขการใช้งานข้างล่าง · ผู้ใช้ที่เข้ามา
 * ถามอยู่สองอย่างเท่านั้น — "ฉันใช้อะไรได้" กับ "ฉันใช้ไปเท่าไร" — ซึ่งเป็นคำถามของ
 * โมเดลตัวเดียวกัน การแยกกันทำให้ต้องเลื่อนขึ้นลงเทียบเอง
 *
 * ตารางนี้แสดงเฉพาะโมเดลที่สิทธิ์ปัจจุบันเรียกได้ · โมเดลที่เคยใช้แต่ถูกถอนสิทธิ์ไปแล้ว
 * ไม่ต้องขึ้น เพราะผู้ใช้เข้ามาดูว่า "ตอนนี้" ใช้อะไรได้ ไม่ใช่ประวัติสิทธิ์ */
function renderUsage(usage, catalog) {
  const allowed = (catalog.sections || []).flatMap((s) => s.models);
  const byAlias = new Map(allowed.map((m) => [m.id, m]));
  const used = new Map((usage.by_model || []).map((r) => [r.model, r]));

  // เฉพาะโมเดลที่ *เรียกได้ตอนนี้* · โมเดลที่เคยใช้แต่ถูกถอนสิทธิ์ไปแล้วไม่ขึ้นในตาราง
  // — หน้านี้ตอบว่า "ตอนนี้ฉันใช้อะไรได้" การโชว์ของที่กดแล้วโดนปฏิเสธคือชวนให้ลอง
  //
  // ผลข้างเคียงที่ยอมรับ: ยอดรายวันด้านล่างเป็นยอดจริงทั้งหมด จึงอาจมากกว่าผลรวม
  // ของแถวในตารางถ้าเคยใช้โมเดลที่ตอนนี้ไม่มีสิทธิ์แล้ว
  const rows = [...byAlias.values()]
    .sort((a, b) => (used.get(b.id)?.requests || 0) - (used.get(a.id)?.requests || 0))
    .map((model) => ({ model, use: used.get(model.id) }));

  const body = rows.map(({ model, use }) => `
    <tr>
      <td>
        <div class="mname">${esc(model.name || model.id)}</div>
        <div class="hint mono">${esc(model.id)}</div>
      </td>
      <td class="caps">${(model.badges || []).map(capIcon).join('')}</td>
      <td class="num">${use ? fmt(use.requests) : '—'}</td>
      <td class="num">${use ? fmt(use.input_tokens) : '—'}</td>
      <td class="num">${use ? fmt(use.output_tokens) : '—'}</td>
    </tr>`).join('');

  const daily = usage.daily || [];
  const peak = Math.max(1, ...daily.map((d) => d.input_tokens + d.output_tokens));
  const spark = daily.slice(-14).map((d) => {
    const total = d.input_tokens + d.output_tokens;
    return `<div class="uday">
      <span class="hint">${esc(d.date)}</span>
      <div class="meter"><span class="ok" style="width:${Math.round(total / peak * 100)}%"></span></div>
      <span class="num">${fmt(total)} tok</span>
    </div>`;
  }).join('');

  $('usage').innerHTML = rows.length
    ? `<table class="tbl"><thead><tr>
         <th>โมเดล</th><th>ทำอะไรได้</th>
         <th class="num">ครั้ง</th><th class="num">token เข้า</th><th class="num">token ออก</th>
       </tr></thead><tbody>${body}</tbody></table>
       ${daily.length ? `<h3 class="uh">${usage.window_days} วันที่ผ่านมา</h3>
         <div class="udays">${spark}</div>` : ''}`
    : '<div class="empty">ยังไม่มีโมเดลที่เปิดให้คุณใช้ · ติดต่อผู้ดูแลของคุณ</div>';
}

/* ── ทางเดินหลัก ────────────────────────────────────────────────────────── */

async function load() {
  const [me, keyInfo, catalog, usage] = await Promise.all([
    call('/v1/me'), call('/v1/me/key'), call('/v1/catalog'), call('/v1/me/usage?days=14'),
  ]);
  renderWho(me);
  renderKey(keyInfo);
  renderQuota(me);
  renderAccessNote(catalog);
  renderUsage(usage, catalog);
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
