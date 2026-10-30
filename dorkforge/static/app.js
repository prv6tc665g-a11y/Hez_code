/* DorkForge Lead Agent — frontend (no framework, no build step) */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  jobId: null,
  socket: null,
  eventCount: 0,
  leads: [],
  health: null,
  scope: { domains: [], enforce: false },
};

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove('show'), 2600);
}

/* ------------------------------------------------------------------ tabs */
function showTab(name) {
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  $$('.panel').forEach((panel) => panel.classList.toggle('active', panel.id === `panel-${name}`));
  if (name === 'leads') loadLeads();
  if (name === 'live') loadHistory();
}

$$('.tab').forEach((tab) => tab.addEventListener('click', () => showTab(tab.dataset.tab)));

/* --------------------------------------------------------------- startup */
async function boot() {
  try {
    state.health = await api('/api/health');
  } catch (error) {
    toast(`Backend nicht erreichbar: ${error.message}`);
    return;
  }
  renderPills();
  await Promise.all([loadScope(), loadLibrary(), loadLeads()]);
}

function renderPills() {
  const h = state.health || {};
  const pills = [
    { text: h.demo ? 'Demo-Modus' : 'Live-Netz', cls: h.demo ? 'warn' : 'ok' },
    { text: `Scope ${h.enforce_scope ? 'AN' : 'AUS'} · ${(h.scope || []).length} Domain(s)`, cls: h.enforce_scope ? 'ok' : 'warn' },
    { text: `${h.library_size || 0} Rezepte`, cls: '' },
    { text: `v${h.version || '?'}`, cls: '' },
  ];
  $('#pills').innerHTML = pills
    .map((p) => `<span class="pill ${p.cls}">${esc(p.text)}</span>`)
    .join('');
  $('#scope-sub').textContent = (h.scope || []).length
    ? `Harvesting erlaubt für: ${h.scope.join(', ')}`
    : 'Kein Scope gesetzt — bitte unter „Scope“ eine Domain eintragen.';
}

/* ---------------------------------------------------------------- scope */
async function loadScope() {
  state.scope = await api('/api/scope');
  const list = $('#scope-list');
  list.innerHTML = (state.scope.domains.length
    ? state.scope.domains.map((d) => `<li><span>${esc(d)}</span><button class="mini" data-remove="${esc(d)}">entfernen</button></li>`).join('')
    : '<li><span class="hint">noch keine Domain</span></li>');
  list.querySelectorAll('[data-remove]').forEach((btn) => btn.addEventListener('click', async () => {
    await api(`/api/scope?domain=${encodeURIComponent(btn.dataset.remove)}`, { method: 'DELETE' });
    await loadScope();
    state.health = await api('/api/health');
    renderPills();
    toast('Domain entfernt');
  }));
  $('#scope-warn').hidden = state.scope.enforce;
}

$('#btn-add-scope').addEventListener('click', async () => {
  const domain = $('#scope-input').value.trim();
  if (!domain) return;
  try {
    await api('/api/scope', { method: 'POST', body: JSON.stringify({ domain, enforce: true }) });
    $('#scope-input').value = '';
    await loadScope();
    state.health = await api('/api/health');
    renderPills();
    toast(`Scope: ${domain}`);
  } catch (error) { toast(error.message); }
});

/* -------------------------------------------------------------- library */
async function loadLibrary() {
  const data = await api('/api/library');
  const categorySelect = $('#lib-category');
  const forgeSelect = $('#f-category');
  data.categories.forEach((category) => {
    categorySelect.insertAdjacentHTML('beforeend', `<option>${esc(category)}</option>`);
    forgeSelect.insertAdjacentHTML('beforeend', `<option value="${esc(category)}">${esc(category)}</option>`);
  });
  renderLibrary(data.entries);
  $$('#lib-category, #lib-risk').forEach((el) => el.addEventListener('change', () => refreshLibrary()));
}

async function refreshLibrary() {
  const category = $('#lib-category').value;
  const risk = $('#lib-risk').value;
  const params = new URLSearchParams();
  if (category) params.set('category', category);
  if (risk) params.set('risk', risk);
  const data = await api(`/api/library?${params}`);
  renderLibrary(data.entries);
}

function renderLibrary(entries) {
  $('#lib-count').textContent = `${entries.length} Rezepte`;
  $('#lib-list').innerHTML = entries.map((entry) => `
    <div class="lib-row">
      <span class="risk ${esc(entry.risk)}">${esc(entry.risk)}</span>
      <div>
        <code>${esc(entry.query)}</code>
        <div class="cat">${esc(entry.category)} · ${esc(entry.description)}</div>
      </div>
      <button class="mini" data-copy="${esc(entry.query)}">kopieren</button>
    </div>`).join('');
  $('#lib-list').querySelectorAll('[data-copy]').forEach((btn) => btn.addEventListener('click', () => {
    navigator.clipboard.writeText(btn.dataset.copy).then(() => toast('kopiert'));
  }));
}

/* ---------------------------------------------------------------- forge */
function readForm() {
  return {
    prompt: $('#prompt').value.trim(),
    domain: $('#f-domain').value.trim(),
    keywords: $('#f-keywords').value.split(',').map((k) => k.trim()).filter(Boolean),
    place: $('#f-place').value.trim(),
    name: $('#f-name').value.trim(),
    filetype: $('#f-filetype').value,
    category: $('#f-category').value,
    risk_ceiling: $('#f-risk').value,
    limit: Number($('#f-limit').value) || 24,
    engine: $('#f-engine').value,
    max_pages: Number($('#f-maxpages').value) || 12,
    follow_links: $('#f-follow').checked,
    demo: $('#f-demo').checked,
    seeds: $('#seeds').value.split('\n').map((s) => s.trim()).filter(Boolean),
  };
}

function fillFields(target) {
  $('#f-domain').value = target.domain || '';
  $('#f-keywords').value = (target.keywords || []).join(', ');
  $('#f-place').value = (target.places || [])[0] || '';
  $('#f-name').value = (target.names || [])[0] || '';
  $('#f-filetype').value = target.filetype || '';
  $('#f-risk').value = target.risk_ceiling || 'medium';
  if (target.category) $('#f-category').value = target.category;
  $('#parsed').open = true;
}

$('#btn-parse').addEventListener('click', async () => {
  const prompt = $('#prompt').value.trim();
  if (!prompt) return toast('Erst einen Satz eingeben');
  try {
    const target = await api('/api/parse', { method: 'POST', body: JSON.stringify({ prompt }) });
    fillFields(target);
    toast(`Domain: ${target.domain || '—'} · Keywords: ${(target.keywords || []).length}`);
  } catch (error) { toast(error.message); }
});

$('#btn-forge').addEventListener('click', async () => {
  const form = readForm();
  if (!form.prompt && !form.domain) return toast('Prompt oder Domain angeben');
  try {
    const data = await api('/api/forge', { method: 'POST', body: JSON.stringify(form) });
    fillFields(data.target);
    renderDorks(data.dorks, form.engine);
    toast(`${data.count} Dorks gebaut${data.skipped ? `, ${data.skipped} übersprungen` : ''}`);
  } catch (error) { toast(error.message); }
});

function renderDorks(dorks, engine) {
  if (!dorks.length) {
    $('#dorks').innerHTML = '<div class="card empty">Keine Dorks — Slot im Rezept leer?</div>';
    return;
  }
  const grouped = {};
  dorks.forEach((dork) => (grouped[dork.category] ||= []).push(dork));
  $('#dorks').innerHTML = Object.entries(grouped).map(([category, items]) => `
    <div class="cat-head">${esc(category)} <span class="badge">${items.length}</span></div>
    <div class="dork-grid">
      ${items.map((dork) => `
        <div class="dork">
          <div class="meta">
            <span class="risk ${esc(dork.risk)}">${esc(dork.risk)}</span>
            <span class="desc">${esc(dork.description)}</span>
          </div>
          <code>${esc(dork.query)}</code>
          <div class="row" style="margin:0">
            <button class="mini" data-copy="${esc(dork.query)}">kopieren</button>
            <a class="mini" href="${esc(dork.engine_url)}" target="_blank" rel="noopener noreferrer">öffnen ↗</a>
            <button class="mini" data-variants="${esc(dork.query)}">Varianten</button>
          </div>
        </div>`).join('')}
    </div>`).join('');

  $('#dorks').querySelectorAll('[data-copy]').forEach((btn) => btn.addEventListener('click', () => {
    navigator.clipboard.writeText(btn.dataset.copy).then(() => toast('kopiert'));
  }));
  $('#dorks').querySelectorAll('[data-variants]').forEach((btn) => btn.addEventListener('click', async () => {
    const data = await api('/api/variants', {
      method: 'POST', body: JSON.stringify({ query: btn.dataset.variants, engine }),
    });
    navigator.clipboard.writeText(data.variants.map((v) => v.query).join('\n'))
      .then(() => toast(`${data.variants.length} Varianten kopiert`));
  }));
}

/* ------------------------------------------------------------------ run */
$('#btn-run').addEventListener('click', async () => {
  const form = readForm();
  if (!form.prompt && !form.domain && !form.seeds.length) {
    return toast('Prompt, Domain oder Seed-URL angeben');
  }
  if (!form.demo && !form.seeds.length) {
    toast('Live-Modus ohne Seeds: der Agent startet mit /kontakt, /team … der Domain');
  }
  try {
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify(form) });
    state.jobId = job.id;
    state.eventCount = 0;
    $('#log').innerHTML = '';
    $('#job-stats').innerHTML = '';
    $('#job-status').textContent = `Job ${job.id} gestartet …`;
    $('#btn-cancel').disabled = false;
    showTab('live');
    connect(job.id);
  } catch (error) { toast(error.message); }
});

function connect(jobId) {
  if (state.socket) { try { state.socket.close(); } catch (_) { /* ignore */ } }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${proto}://${location.host}/ws/jobs/${jobId}`);
  state.socket = socket;

  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'stream_closed') {
      $('#job-status').textContent = `Fertig (${event.status})`;
      $('#btn-cancel').disabled = true;
      loadLeads();
      return;
    }
    state.eventCount += 1;
    appendEvent(event);
    handleEvent(event);
  };
  socket.onerror = () => toast('WebSocket-Fehler — Fallback auf Polling');
  socket.onclose = () => { $('#btn-cancel').disabled = true; };
}

function appendEvent(event) {
  const log = $('#log');
  const level = event.type === 'page_failed' || event.type === 'job_failed' ? 'err'
    : event.type === 'notice' ? 'warn'
    : event.type === 'lead_found' || event.type === 'job_finished' ? 'ok' : '';
  const time = new Date((event.ts || Date.now() / 1000) * 1000)
    .toLocaleTimeString('de-CH', { hour12: false });
  const line = document.createElement('div');
  line.className = `line ${level}`;
  line.innerHTML = `<span class="t">${time}</span><span class="ev">${esc(event.type)}</span>
    <span class="msg">${esc(describe(event))}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function describe(event) {
  switch (event.type) {
    case 'job_started': return `mode=${event.mode} scope=${JSON.stringify(event.scope)} gate=${event.enforce_scope}`;
    case 'target_parsed': return `domain=${event.domain || '—'} kw=${(event.keywords || []).join(', ') || '—'}`;
    case 'dorks_ready': return `${event.count} Dorks für ${event.target_domain || '—'}`;
    case 'crawl_started': return `${event.seeds.length} Seed(s), max ${event.max_pages} Seiten`;
    case 'page_started': return `${event.index}/${event.total} ${event.url}`;
    case 'page_failed': return `${event.url} → ${event.reason}`;
    case 'page_done': return `${event.url} · ${event.emails} Mail, ${event.phones} Tel, ${event.people} Person(en) → ${event.leads} Lead(s)`;
    case 'lead_found': return `${event.is_new ? 'NEU' : 'update'} · ${event.lead.email || event.lead.phone || event.lead.contact_name || event.lead.company} [${event.lead.score}]`;
    case 'notice': return event.message;
    case 'job_finished': return `status=${event.status} · ${event.new_leads ?? 0} neue Leads · ${event.pages_ok ?? 0} Seiten`;
    case 'job_failed': return event.error;
    default: return JSON.stringify(event, (k, v) => (k === 'ts' || k === 'type' ? undefined : v)).slice(0, 240);
  }
}

function handleEvent(event) {
  if (event.type === 'dorks_ready') {
    renderDorks(event.dorks, $('#f-engine').value);
  }
  if (event.type === 'job_finished' || event.type === 'job_failed') {
    const stats = event.type === 'job_failed' ? { error: event.error } : event;
    $('#job-stats').innerHTML = Object.entries(stats)
      .filter(([key]) => ['pages_ok', 'pages_failed', 'pages_visited', 'new_leads',
        'updated_leads', 'dorks', 'seconds', 'error'].includes(key))
      .map(([key, value]) => `<div class="stat"><div class="v">${esc(value)}</div><div class="k">${esc(key)}</div></div>`)
      .join('');
    $('#job-status').textContent = event.type === 'job_failed'
      ? `Fehler: ${event.error}` : `Fertig — ${event.new_leads ?? 0} neue Leads`;
    $('#btn-cancel').disabled = true;
    refreshLeadBadge();
  }
  if (event.type === 'lead_found') refreshLeadBadge();
}

$('#btn-cancel').addEventListener('click', async () => {
  if (!state.jobId) return;
  await api(`/api/jobs/${state.jobId}/cancel`, { method: 'POST' }).catch(() => {});
  toast('Abbruch angefordert');
});
$('#btn-clear-log').addEventListener('click', () => { $('#log').innerHTML = ''; });

async function loadHistory() {
  const data = await api('/api/jobs');
  $('#history').innerHTML = data.jobs.length
    ? data.jobs.map((job) => `<button data-job="${esc(job.id)}">
        ${esc(job.id)} · ${esc(job.status)} · ${job.stats?.new_leads ?? 0} Leads · ${esc(job.prompt.slice(0, 46) || '—')}
      </button>`).join('')
    : '<div class="hint">noch keine Runs</div>';
  $('#history').querySelectorAll('[data-job]').forEach((btn) => btn.addEventListener('click', () => {
    state.jobId = btn.dataset.job;
    showTab('live');
    connect(btn.dataset.job);
  }));
}

/* ---------------------------------------------------------------- leads */
async function loadLeads() {
  const min = Number($('#lead-min').value) || 0;
  const query = $('#lead-search').value.trim();
  const params = new URLSearchParams({ min_score: min });
  if (query) params.set('q', query);
  const data = await api(`/api/leads?${params}`);
  state.leads = data.leads;
  renderLeads(data.leads, data.stats);
}

function refreshLeadBadge() {
  api('/api/leads?limit=1').then((data) => {
    $('#lead-badge').textContent = data.stats.total;
  }).catch(() => {});
}

function renderLeads(leads, stats) {
  $('#lead-badge').textContent = stats.total;
  $('#lead-stats').innerHTML = [
    ['Leads', stats.total], ['mit E-Mail', stats.with_email], ['mit Telefon', stats.with_phone],
    ['namentlich', stats.named], ['Domains', stats.domains], ['Ø Score', stats.avg_score],
  ].map(([key, value]) => `<div class="stat"><div class="v">${esc(value)}</div><div class="k">${esc(key)}</div></div>`).join('');

  const body = $('#lead-table tbody');
  if (!leads.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">Noch keine Leads — Agent starten oder Seeds harvesten.</td></tr>';
    return;
  }
  body.innerHTML = leads.map((lead) => {
    const cls = lead.score >= 70 ? 's-high' : lead.score >= 40 ? 's-mid' : 's-low';
    return `<tr>
      <td><span class="score ${cls}">${lead.score}</span></td>
      <td>${esc(lead.company || lead.domain || '—')}</td>
      <td>${esc(lead.contact_name || '—')}</td>
      <td>${esc(lead.role || '—')}</td>
      <td>${lead.email ? `<a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a>` : '—'}</td>
      <td>${esc(lead.phone || '—')}</td>
      <td>${esc(lead.city || '—')}</td>
      <td>${lead.source_url ? `<a href="${esc(lead.source_url)}" target="_blank" rel="noopener noreferrer">Quelle ↗</a>` : '—'}</td>
    </tr>`;
  }).join('');
}

$('#btn-refresh-leads').addEventListener('click', loadLeads);
$('#lead-min').addEventListener('input', () => {
  $('#lead-min-val').textContent = $('#lead-min').value;
  loadLeads();
});
$('#lead-search').addEventListener('input', () => {
  clearTimeout(loadLeads._timer);
  loadLeads._timer = setTimeout(loadLeads, 250);
});

['csv', 'jsonl', 'md'].forEach((fmt) => {
  const id = `#btn-export-${fmt === 'md' ? 'md' : fmt}`;
  const button = $(id);
  if (button) button.addEventListener('click', () => {
    window.location.href = `/api/leads/export?fmt=${fmt}`;
  });
});

$('#btn-clear-leads').addEventListener('click', async () => {
  if (!confirm('Alle Leads löschen?')) return;
  await api('/api/leads', { method: 'DELETE' });
  await loadLeads();
  toast('Leads gelöscht');
});

/* ----------------------------------------------------------- demo button */
$('#btn-demo').addEventListener('click', () => {
  $('#prompt').value = 'Kontakte und Ansprechpartner von acme-demo.ch in Aarau, am liebsten mit E-Mail und Telefon';
  $('#f-domain').value = 'acme-demo.ch';
  $('#f-maxpages').value = '6';
  $('#seeds').value = [
    'https://acme-demo.ch/',
    'https://acme-demo.ch/kontakt',
    'https://acme-demo.ch/team',
    'https://acme-demo.ch/karriere',
    'https://acme-demo.ch/impressum',
  ].join('\n');
  $('#f-demo').checked = true;
  $('#parsed').open = true;
  toast('Demo-Prompt geladen — „Agent starten“ klicken');
});

boot();
