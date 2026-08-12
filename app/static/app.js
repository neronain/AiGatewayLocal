/* EduLLM Gateway console.
   Plain ES modules-free JS: the page must run from a static mount with no build
   step and no network access beyond the gateway itself. */

const KEY_STORE = 'edullm_key';
const $ = (id) => document.getElementById(id);
const state = { key: sessionStorage.getItem(KEY_STORE) || '', me: null, cache: {} };

/* ---------------------------------------------------------------- utils */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
const num = (v) => (v || 0).toLocaleString();

function banner(target, kind, message) {
  $(target).innerHTML = message
    ? `<div class="banner ${kind}">${esc(message)}</div>` : '';
}
const showError = (m) => banner('error', 'err', m);

function flash(target, kind, message) {
  const el = $(target);
  el.className = 'sub';
  el.style.color = kind === 'err' ? 'var(--err)' : kind === 'ok' ? 'var(--ok)' : 'var(--muted)';
  el.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${state.key}`,
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
  const loaders = { models: loadModels, access: loadAccess, quota: loadQuota };
  if (loaders[name]) loaders[name]().catch((e) => showError(e.message));
}
for (const btn of document.querySelectorAll('#tabs button')) {
  btn.onclick = () => showTab(btn.dataset.tab);
}

function applyRole(role) {
  const admin = role === 'admin';
  const staff = admin || role === 'instructor';
  for (const btn of document.querySelectorAll('#tabs button')) {
    if (btn.hasAttribute('data-admin')) btn.hidden = !admin;
    if (btn.hasAttribute('data-staff')) btn.hidden = !staff;
  }
  $('health-wrap').hidden = !admin;
  $('usage-wrap').hidden = !staff;
}

/* ------------------------------------------------------------ dashboard */
function renderQuota(me) {
  const { used, limits } = me.quota;
  const row = (label, u, limit) => {
    const pct = limit ? Math.min(100, Math.round((u / limit) * 100)) : 0;
    const cls = !limit ? 'mute' : pct >= 100 ? 'err' : pct >= 80 ? 'warn' : 'ok';
    return `<tr><td>${label}</td><td class="num">${num(u)}</td>
      <td class="num">${limit ? num(limit) : 'unlimited'}</td>
      <td><span class="pill ${cls}">${limit ? pct + '%' : 'n/a'}</span></td></tr>`;
  };
  $('quota').innerHTML = `
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
          <div class="sub mono">${esc(m.id)}</div>
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
  const [registry, status] = await Promise.all([
    api('/admin/models'), api('/admin/registry/status'),
  ]);
  state.cache.models = registry.data;

  if (!status.writable) {
    banner('registry-note', 'warn',
      `The registry at ${status.config_dir} is read-only, so "Save to registry" is unavailable. ` +
      'Use Preview YAML and commit the file to git.');
  } else {
    banner('registry-note', '', '');
  }
  if (registry.errors?.length) {
    banner('registry-note', 'err', 'Registry errors: ' + registry.errors.join(' | '));
  }

  const compat = await Promise.all(
    registry.data.map((m) => api(`/admin/models/${encodeURIComponent(m.alias)}/compatibility`)
      .catch(() => ({ status: 'NOT TESTED', results: [] }))),
  );

  $('model-table').innerHTML = `
    <tr><th>Alias</th><th>Upstream / backend</th><th>Capabilities</th>
        <th>Health</th><th>Test status</th><th></th></tr>
    ${registry.data.map((m, i) => {
      const ep = m.endpoints[0] || {};
      const healthy = m.endpoints.every((e) => e.health?.healthy);
      const c = compat[i];
      const cls = c.status === 'READY' ? 'ok' : c.status === 'DEGRADED' ? 'err' : 'mute';
      return `<tr>
        <td><code>${esc(m.alias)}</code><div class="sub">${esc(m.display_name)}</div></td>
        <td><code>${esc(m.upstream_model)}</code>
            <div class="sub">${esc(ep.server_type || '')} · ${esc(ep.base_url || '')}</div></td>
        <td>${m.badges.map((b) => `<span class="badge">${esc(b)}</span>`).join(' ')}</td>
        <td><span class="pill ${healthy ? 'ok' : 'err'}">${healthy ? 'healthy' : 'down'}</span></td>
        <td><span class="pill ${cls}">${esc(c.status)}</span>
            <div class="sub" id="run-${esc(m.alias)}"></div></td>
        <td style="white-space:nowrap">
          <button class="ghost small" data-test="${esc(m.alias)}">Run tests</button>
          <button class="ghost small" data-edit="${esc(m.alias)}">Edit</button>
          <button class="danger small" data-del="${esc(m.alias)}">Delete</button>
        </td></tr>`;
    }).join('')}`;

  for (const btn of $('model-table').querySelectorAll('[data-test]')) {
    btn.onclick = () => runTests(btn.dataset.test, btn);
  }
  for (const btn of $('model-table').querySelectorAll('[data-edit]')) {
    btn.onclick = () => openEditor(state.cache.models.find((m) => m.alias === btn.dataset.edit));
  }
  for (const btn of $('model-table').querySelectorAll('[data-del]')) {
    btn.onclick = async () => {
      if (!confirm(`Delete the registry file for "${btn.dataset.del}"?\n\n` +
                   'Students calling this alias will get MODEL_NOT_FOUND.')) return;
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
          <td class="num">${r.latency_ms}</td><td class="empty">${esc(r.notes)}</td></tr>`).join('');
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

function editorValues() {
  const checked = (id) => $(id).checked;
  const purposes = ['general', 'coding', 'vision', 'reasoning', 'agent', 'fast']
    .filter((p) => checked(`p-${p}`));
  const vision = checked('c-vision');
  const anthropic = checked('x-anthropic');

  const definition = {
    apiVersion: 'edullm.gateway/v1',
    kind: 'Model',
    metadata: {
      alias: $('m-alias').value.trim(),
      display_name: $('m-name').value.trim() || $('m-alias').value.trim(),
      description: $('m-desc').value.trim(),
      visibility: $('m-visibility').value,
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
      protocols: { openai: checked('x-openai'), anthropic },
      endpoints: [{
        name: ($('m-url').value.replace(/^https?:\/\//, '').replace(/[^\w.-]/g, '-') || 'backend').slice(0, 40),
        server_type: $('m-server').value,
        base_url: $('m-url').value.trim(),
        api_key_env: $('m-keyenv').value.trim(),
        priority: 100, weight: 1,
        max_concurrency: Number($('m-conc').value) || 8,
        health_path: '/health',
        protocols: { openai: true, anthropic: false },
        modalities: { text: true, image: vision, audio: false, video: false },
        enabled: true,
      }],
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
  banner('detect-out', '', '');
  flash('save-status', '', '');
  flash('detect-status', '', '');

  const set = (id, v) => { $(id).value = v; };
  const check = (id, v) => { $(id).checked = !!v; };

  if (!model) {
    ['m-alias', 'm-name', 'm-desc', 'm-url', 'm-keyenv', 'm-upstream'].forEach((i) => set(i, ''));
    set('m-ctx', 131072); set('m-out', 8192); set('m-conc', 16);
    ['c-vision', 'c-tools', 'c-coding', 'c-reasoning', 'c-agentic', 'x-anthropic', 'x-claudecode']
      .forEach((i) => check(i, false));
    ['c-chat', 'c-streaming', 'x-openai', 'p-general'].forEach((i) => check(i, true));
    ['p-coding', 'p-vision', 'p-reasoning', 'p-agent', 'p-fast'].forEach((i) => check(i, false));
    $('m-alias').disabled = false;
  } else {
    const ep = model.endpoints[0] || {};
    set('m-alias', model.alias); $('m-alias').disabled = true;
    set('m-name', model.display_name); set('m-desc', '');
    set('m-visibility', model.visibility);
    set('m-upstream', model.upstream_model);
    set('m-server', ep.server_type || 'vllm'); set('m-url', ep.base_url || '');
    set('m-keyenv', ''); set('m-conc', ep.max_concurrency || 16);
    set('m-ctx', model.limits.context_tokens); set('m-out', model.limits.max_output_tokens);
    for (const [k, v] of Object.entries(model.capabilities)) check(`c-${k}`, v);
    check('x-openai', model.protocols.openai); check('x-anthropic', model.protocols.anthropic);
    check('x-claudecode', !!model.agent_clients?.claude_code?.enabled);
    ['general', 'coding', 'vision', 'reasoning', 'agent', 'fast']
      .forEach((p) => check(`p-${p}`, model.purpose.includes(p)));
  }
  $('editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('new-model').onclick = () => openEditor(null);
$('editor-close').onclick = () => { $('editor').hidden = true; };
$('reload-registry').onclick = async () => {
  try { await post('/admin/registry/reload'); await loadModels(); }
  catch (e) { showError(e.message); }
};

$('detect').onclick = async () => {
  const base_url = $('m-url').value.trim();
  if (!base_url) { flash('detect-status', 'err', 'Enter a base URL first'); return; }
  flash('detect-status', '', 'probing…');
  $('detect').disabled = true;
  try {
    const { suggestion } = await post('/admin/models/detect', {
      base_url, upstream_model: $('m-upstream').value.trim(),
      api_key_env: $('m-keyenv').value.trim(),
    });
    if (!suggestion.reachable) {
      banner('detect-out', 'err', 'Backend not reachable. ' + suggestion.notes.join(' '));
      flash('detect-status', 'err', 'failed');
      return;
    }
    if (!$('m-upstream').value.trim() && suggestion.upstream_model) {
      $('m-upstream').value = suggestion.upstream_model;
    }
    if (suggestion.context_tokens) $('m-ctx').value = suggestion.context_tokens;
    for (const [cap, on] of Object.entries(suggestion.capabilities)) {
      if ($(`c-${cap}`)) $(`c-${cap}`).checked = on;
    }
    if (suggestion.protocols?.anthropic) $('x-anthropic').checked = true;

    const yes = (v) => (v ? '<span class="pill ok">yes</span>' : '<span class="pill err">no</span>');
    $('detect-out').innerHTML = `<div class="banner ok">
      Detected — please confirm before saving. Detection is a suggestion, not a decision.
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
    flash('detect-status', 'ok', 'done');
  } catch (e) {
    flash('detect-status', 'err', 'failed');
    banner('detect-out', 'err', e.message);
  } finally {
    $('detect').disabled = false;
  }
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

/* --------------------------------------------------------- access & keys */
async function loadAccess() {
  const [users, courses, keys] = await Promise.all([
    api('/admin/users'), api('/admin/courses'), api('/admin/api-keys'),
  ]);
  state.cache.users = users.data;
  state.cache.courses = courses.data;

  const userOpts = users.data
    .map((u) => `<option value="${esc(u.id)}">${esc(u.external_id)} — ${esc(u.display_name || u.role)}</option>`)
    .join('');
  $('k-user').innerHTML = userOpts;
  $('q-user').innerHTML = userOpts;
  const courseOpts = courses.data
    .map((c) => `<option value="${esc(c.id)}">${esc(c.code)} — ${esc(c.name)}</option>`).join('');
  $('k-course').innerHTML = '<option value="">— none —</option>' + courseOpts;
  $('q-course').innerHTML = courseOpts;

  const byId = Object.fromEntries(users.data.map((u) => [u.id, u]));
  $('key-table').innerHTML = `
    <tr><th>Prefix</th><th>User</th><th>Course</th><th>Label</th>
        <th>Expires</th><th>Last used</th><th>State</th><th></th></tr>
    ${keys.data.map((k) => {
      const u = byId[k.user_id];
      const course = courses.data.find((c) => c.id === k.course_id);
      return `<tr>
        <td><code>${esc(k.key_prefix)}…</code></td>
        <td>${esc(u ? u.external_id : k.user_id)}</td>
        <td>${esc(course ? course.code : '—')}</td>
        <td>${esc(k.name || '—')}</td>
        <td class="sub">${k.expires_at ? new Date(k.expires_at).toLocaleDateString() : 'never'}</td>
        <td class="sub">${k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'never'}</td>
        <td><span class="pill ${k.revoked ? 'err' : 'ok'}">${k.revoked ? 'revoked' : 'active'}</span></td>
        <td>${k.revoked ? '' : `<button class="danger small" data-revoke="${esc(k.id)}">Revoke</button>`}</td>
      </tr>`;
    }).join('') || '<tr><td class="empty">No keys issued yet.</td></tr>'}`;

  for (const btn of $('key-table').querySelectorAll('[data-revoke]')) {
    btn.onclick = async () => {
      if (!confirm('Revoke this key? Any client using it stops working immediately.')) return;
      try { await del(`/admin/api-keys/${btn.dataset.revoke}`); await loadAccess(); }
      catch (e) { showError(e.message); }
    };
  }

  const aliases = (state.cache.models || []).map((m) => m.alias);
  $('course-table').innerHTML = `
    <tr><th>Code</th><th>Name</th><th>Term</th><th>Allowed models</th><th></th></tr>
    ${courses.data.map((c) => `<tr>
      <td><code>${esc(c.code)}</code></td><td>${esc(c.name)}</td><td>${esc(c.term)}</td>
      <td class="checks" id="cm-${esc(c.id)}">${aliases.map((a) => `
        <label><input type="checkbox" data-course="${esc(c.id)}" value="${esc(a)}"> ${esc(a)}</label>
      `).join('')}</td>
      <td><button class="ghost small" data-savecourse="${esc(c.id)}">Save</button></td>
    </tr>`).join('') || '<tr><td class="empty">No courses yet.</td></tr>'}`;

  for (const btn of $('course-table').querySelectorAll('[data-savecourse]')) {
    btn.onclick = async () => {
      const id = btn.dataset.savecourse;
      const models = [...document.querySelectorAll(`input[data-course="${id}"]:checked`)]
        .map((i) => i.value);
      try {
        await post(`/admin/courses/${id}/models`, { models });
        flash('error', 'ok', '');
        banner('error', 'ok', `Updated allowed models for the course: ${models.join(', ') || 'none'}`);
      } catch (e) { showError(e.message); }
    };
  }
}

$('issue-key').onclick = async () => {
  try {
    const result = await post('/admin/api-keys', {
      user_id: $('k-user').value,
      course_id: $('k-course').value || null,
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

$('create-course').onclick = async () => {
  try {
    await post('/admin/courses', {
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
  $('q-course-wrap').hidden = $('q-scope').value !== 'course';
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
  const courses = Object.fromEntries((state.cache.courses || []).map((c) => [c.id, c.code]));
  const lim = (v) => (v ? num(v) : '∞');

  $('quota-table').innerHTML = `
    <tr><th>Scope</th><th>Applies to</th><th>Model</th><th>Window</th>
        <th class="num">Requests</th><th class="num">Input</th>
        <th class="num">Output</th><th class="num">Images</th></tr>
    ${policies.data.map((p) => `<tr>
      <td><span class="pill mute">${esc(p.scope)}</span></td>
      <td>${esc(users[p.user_id] || courses[p.course_id] || 'everyone')}</td>
      <td><code>${esc(p.model_alias || 'all')}</code></td>
      <td>${esc(p.window)}</td>
      <td class="num">${lim(p.max_requests)}</td><td class="num">${lim(p.max_input_tokens)}</td>
      <td class="num">${lim(p.max_output_tokens)}</td><td class="num">${lim(p.max_images)}</td>
    </tr>`).join('') || '<tr><td class="empty">No policies — the gateway.yaml defaults apply.</td></tr>'}`;

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
      course_id: scope === 'course' ? $('q-course').value : null,
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

/* ----------------------------------------------------------------- boot */
async function load() {
  if (!state.key) { showError('Enter an API key to connect.'); return; }
  showError('');
  try {
    const me = await api('/v1/me');
    state.me = me;
    $('whoami').textContent = `${me.display_name || me.external_id} · ${me.role}`;
    applyRole(me.role);
    renderQuota(me);
    renderCatalog(await api('/v1/catalog'));
    if (me.role === 'admin') renderHealth((await api('/v1/health/endpoints')).data);
    if (me.role === 'admin' || me.role === 'instructor') {
      renderUsage(await api('/admin/usage/summary?days=7'));
    }
  } catch (err) {
    showError(err.message);
    $('whoami').textContent = 'not connected';
  }
}

$('connect').onclick = () => {
  state.key = $('key').value.trim();
  try { sessionStorage.setItem(KEY_STORE, state.key); } catch { /* private mode */ }
  load();
};
$('refresh').onclick = () => {
  const active = document.querySelector('#tabs button[aria-selected="true"]');
  load().then(() => { if (active && active.dataset.tab !== 'dashboard') showTab(active.dataset.tab); });
};
$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('connect').click(); });

if (state.key) { $('key').value = state.key; load(); }
