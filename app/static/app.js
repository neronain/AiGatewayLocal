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

function renderCatalog(data) {
  $('catalog').innerHTML = data.sections.map((s) => `
    <h3 style="margin:18px 0 10px">${esc(s.title)}</h3>
    <div class="grid">
      ${s.models.map((m) => `
        <div class="card">
          <h3>${esc(m.name)}</h3>
          <div class="mono" style="color:var(--fg3)">${esc(m.id)}</div>
          ${m.description ? `<p class="hint">${esc(m.description)}</p>` : ''}
          <div class="badges">${m.badges.map((b) => `<span class="badge">${esc(b)}</span>`).join('')}</div>
          <div class="sub">${esc(m.context)}
            ${m.claude_code_ready ? ' <span class="pill ok">Claude Code Ready</span>' : ''}</div>
        </div>`).join('')}
    </div>`).join('') || '<div class="empty">No models available.</div>';
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

  $('model-table').innerHTML = `
    <tr><th>Alias</th><th>Upstream / backends</th><th>Capabilities</th>
        <th>Health</th><th>Test status</th><th></th></tr>
    ${registry.data.map((m, i) => {
      const healthy = m.endpoints.filter((e) => e.health?.healthy).length;
      const total = m.endpoints.length;
      const cls = healthy === total ? 'ok' : healthy ? 'warn' : 'err';
      const c = compat[i];
      const ccls = c.status === 'READY' ? 'ok' : c.status === 'DEGRADED' ? 'err' : 'mute';
      return `<tr>
        <td><code>${esc(m.alias)}</code><div class="hint">${esc(m.display_name)}</div></td>
        <td><code>${esc(m.upstream_model)}</code>
            ${m.endpoints.map((e) => `<div class="hint">${esc(e.server_type)} ·
              ${esc(e.base_url)} ${e.health?.healthy ? '' : '<span class="pill err">down</span>'}</div>`).join('')}</td>
        <td>${m.badges.map((b) => `<span class="badge">${esc(b)}</span>`).join(' ')}</td>
        <td><span class="pill ${cls}">${healthy}/${total} up</span></td>
        <td><span class="pill ${ccls}">${esc(c.status)}</span>
            <div class="hint" id="run-${esc(m.alias)}"></div></td>
        <td style="white-space:nowrap">
          <button class="ghost small" data-verify="${esc(m.alias)}">Verify</button>
          <button class="ghost small" data-test="${esc(m.alias)}">Run tests</button>
          <button class="ghost small" data-edit="${esc(m.alias)}">Edit</button>
          <button class="danger small" data-del="${esc(m.alias)}">Delete</button>
        </td></tr>`;
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

async function loadAccess() {
  const [users, workspaces, keys] = await Promise.all([
    api('/admin/users'), api('/admin/workspaces'), api('/admin/api-keys'),
  ]);
  state.cache.users = users.data;
  state.cache.workspaces = workspaces.data;

  const userOpts = users.data
    .map((u) => `<option value="${esc(u.id)}">${esc(u.external_id)} — ${esc(u.display_name || u.role)}</option>`)
    .join('');
  $('k-user').innerHTML = userOpts;
  $('q-user').innerHTML = userOpts;
  const workspaceOpts = workspaces.data
    .map((c) => `<option value="${esc(c.id)}">${esc(c.code)} — ${esc(c.name)}</option>`).join('');
  $('k-workspace').innerHTML = '<option value="">— none —</option>' + workspaceOpts;
  $('q-workspace').innerHTML = workspaceOpts;

  // ตาราง People — ก่อนหน้านี้ผู้ใช้โผล่แค่ใน dropdown ตอนออก key · role ตั้งได้
  // ตอนสร้างเท่านั้น แล้วไม่มีทางแก้จากหน้าเว็บอีกเลย
  const roles = ['member', 'manager', 'admin'];
  const adminCount = users.data.filter((u) => u.role === 'admin').length;
  $('user-table').innerHTML = `
    <tr><th>ID</th><th>Name</th><th>Email</th><th>Role</th><th>Status</th></tr>
    ${users.data.map((u) => {
      // admin คนสุดท้ายเปลี่ยน role ไม่ได้ — ไม่มี admin แปลว่าไม่มีใครออก key
      // ตั้ง quota หรือแก้ registry ได้อีก และไม่มีทางกลับผ่านหน้าเว็บ
      const locked = u.role === 'admin' && adminCount <= 1;
      return `<tr>
        <td><code>${esc(u.external_id)}</code></td>
        <td>${esc(u.display_name || '—')}</td>
        <td class="hint">${esc(u.email || '—')}</td>
        <td>${locked
          ? `<span class="pill ok" title="ผู้ดูแลคนเดียวที่เหลืออยู่ — ตั้งให้คนอื่นเป็น admin ก่อนถึงจะเปลี่ยนได้">admin (คนเดียว)</span>`
          : `<select class="small" data-role="${esc(u.id)}">${roles.map((r) =>
              `<option value="${r}"${u.role === r ? ' selected' : ''}>${r}</option>`).join('')}</select>`}</td>
        <td><span class="pill ${u.status === 'active' ? 'ok' : 'mute'}">${esc(u.status || 'active')}</span></td>
      </tr>`;
    }).join('') || '<tr><td class="empty">No people yet.</td></tr>'}`;

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
        <td>${esc(k.name || '—')}</td>
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
  $('workspace-table').innerHTML = `
    <tr><th>Code</th><th>Name</th><th>Term</th><th>Allowed models</th><th></th></tr>
    ${workspaces.data.map((c) => `<tr>
      <td><code>${esc(c.code)}</code></td><td>${esc(c.name)}</td><td>${esc(c.term)}</td>
      <td class="checks">${aliases.map((a) => `
        <label><input type="checkbox" data-workspace="${esc(c.id)}" value="${esc(a)}"> ${esc(a)}</label>
      `).join('') || '<span class="hint">open the Models tab first</span>'}</td>
      <td><button class="ghost small" data-saveworkspace="${esc(c.id)}">Save</button></td>
    </tr>`).join('') || '<tr><td class="empty">No workspaces yet.</td></tr>'}`;

  for (const btn of $('workspace-table').querySelectorAll('[data-saveworkspace]')) {
    btn.onclick = async () => {
      const id = btn.dataset.saveworkspace;
      const models = [...document.querySelectorAll(`input[data-workspace="${id}"]:checked`)]
        .map((i) => i.value);
      try {
        await post(`/admin/workspaces/${id}/models`, { models });
        banner('error', 'ok', `Updated allowed models: ${models.join(', ') || 'none'}`);
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

  $('q-model').innerHTML = '<option value="">— all models —</option>' +
    (state.cache.models || []).map((m) => `<option value="${esc(m.alias)}">${esc(m.alias)}</option>`).join('');

  const users = Object.fromEntries((state.cache.users || []).map((u) => [u.id, u.external_id]));
  const workspaces = Object.fromEntries((state.cache.workspaces || []).map((c) => [c.id, c.code]));
  const lim = (v) => (v ? num(v) : '∞');

  $('quota-table').innerHTML = `
    <tr><th>Scope</th><th>Applies to</th><th>Model</th><th>Window</th>
        <th class="num">Requests</th><th class="num">Input</th>
        <th class="num">Output</th><th class="num">Images</th><th></th></tr>
    ${policies.data.map((p) => `<tr>
      <td><span class="pill mute">${esc(p.scope)}</span></td>
      <td>${esc(users[p.user_id] || workspaces[p.workspace_id] || 'everyone')}</td>
      <td><code>${esc(p.model_alias || 'all')}</code></td>
      <td>${esc(p.window)}</td>
      <td class="num">${lim(p.max_requests)}</td><td class="num">${lim(p.max_input_tokens)}</td>
      <td class="num">${lim(p.max_output_tokens)}</td><td class="num">${lim(p.max_images)}</td>
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
      model_alias: $('q-model').value || null,
      window: $('q-window').value,
      max_requests: Number($('q-req').value) || 0,
      max_input_tokens: Number($('q-in').value) || 0,
      max_output_tokens: Number($('q-outt').value) || 0,
      max_images: Number($('q-img').value) || 0,
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

  const models = catalog.sections.flatMap((section) => section.models);
  $('my-models').innerHTML = models.length ? `<div class="grid">
    ${models.map((m) => `<div class="card">
      <h3>${esc(m.name)}</h3>
      <div class="mono" style="color:var(--fg3)">${esc(m.id)}</div>
      <div class="badges">${m.badges.map((b) => `<span class="badge">${esc(b)}</span>`).join('')}</div>
      <div class="sub">${esc(m.context)}${m.claude_code_ready ? ' · <span class="pill ok">Claude Code Ready</span>' : ''}</div>
    </div>`).join('')}</div>`
    : '<div class="empty">No models are enabled for you yet. Ask your manager.</div>';
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
  try {
    await post(state.needsSetup ? '/auth/setup' : '/auth/login', body);
    $('password').value = '';
    flash('signin-status', '', '');
    document.querySelector('#tabs').hidden = false;
    await load();
    showTab('dashboard');
  } catch (e) {
    flash('signin-status', 'err', e.message.replace(/^[A-Z_]+: /, ''));
  }
};
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
  $('whoami').textContent = `${me.display_name || me.external_id} · ${me.role}`;
  $('signout').hidden = false;
  applyRole(me.role);
  await initAssistant();
  renderQuota(me);
  renderCatalog(await api('/v1/catalog'));
  if (me.role === 'admin') renderHealth((await api('/v1/health/endpoints')).data);
  if (me.role === 'admin' || me.role === 'manager') {
    renderUsage(await api('/admin/usage/summary?days=7'));
  }
}

$('refresh').onclick = () => {
  const active = document.querySelector('#tabs button[aria-selected="true"]');
  load()
    .then(() => { if (active && active.dataset.tab !== 'dashboard') showTab(active.dataset.tab); })
    .catch((e) => showError(e.message));
};

(async function boot() {
  try {
    const status = await api('/auth/status');
    if (!status.session) { showSignIn(status.needs_setup); return; }
    document.querySelector('#tabs').hidden = false;
    await load();
    showTab('dashboard');
  } catch (e) {
    showSignIn(false);
    flash('signin-status', 'err', e.message);
  }
})();
