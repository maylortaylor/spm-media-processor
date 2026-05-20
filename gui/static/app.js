'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════

const state = {
  yearFolder: localStorage.getItem('yearFolder') || '',
  outputDir: '',
  events: [],       // [{folder, folder_name, workspace, scan_result, status, uiState}]
  knownBands: [],
  config: {},
  thresholds: {
    gap_db: 12,
    gap_sec: 30,
    single_band_threshold: 75,
  },
  activeFilter: 'all',
  batchJobId: null,
};

// ═══════════════════════════════════════════════════════════════════════════
// API helpers
// ═══════════════════════════════════════════════════════════════════════════

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`${path}: ${res.status} ${err}`);
  }
  return res.json();
}

const GET = (path) => api('GET', path);
const POST = (path, body) => api('POST', path, body);
const PATCH = (path, body) => api('PATCH', path, body);
const DELETE = (path) => api('DELETE', path);

// ═══════════════════════════════════════════════════════════════════════════
// SSE job streaming
// ═══════════════════════════════════════════════════════════════════════════

function streamJob(jobId, { onLog, onEvent, onDone }) {
  const es = new EventSource(`/api/job/${jobId}/stream`);
  es.onmessage = (e) => {
    const raw = e.data;
    if (raw === '__DONE__') {
      es.close();
      if (onDone) onDone();
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      if (parsed.type === 'log') {
        if (onLog) onLog(parsed.line);
      } else {
        if (onEvent) onEvent(parsed);
      }
    } catch {
      if (onLog) onLog(raw);
    }
  };
  es.onerror = () => { es.close(); if (onDone) onDone(); };
  return es;
}

// ═══════════════════════════════════════════════════════════════════════════
// Known bands management
// ═══════════════════════════════════════════════════════════════════════════

async function loadBands() {
  const { bands } = await GET('/api/bands');
  state.knownBands = bands;
  updateBandsDatalist();
  return bands;
}

function updateBandsDatalist() {
  ['bands-datalist', 'bands-datalist-settings'].forEach(id => {
    const dl = document.getElementById(id);
    if (!dl) return;
    dl.innerHTML = state.knownBands.map(b => `<option value="${escHtml(b)}">`).join('');
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Screen switching
// ═══════════════════════════════════════════════════════════════════════════

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ═══════════════════════════════════════════════════════════════════════════
// Entry screen
// ═══════════════════════════════════════════════════════════════════════════

function initEntry() {
  const input = document.getElementById('year-folder-input');
  input.value = state.yearFolder;

  // Sliders
  ['gap-db', 'gap-sec', 'single'].forEach(key => {
    const sl = document.getElementById(`sl-${key}`);
    const val = document.getElementById(`val-${key}`);
    if (!sl) return;
    sl.addEventListener('input', () => { val.textContent = sl.value; });
    // Load from config
  });

  document.getElementById('threshold-toggle').addEventListener('click', function () {
    this.classList.toggle('open');
    document.getElementById('threshold-body').classList.toggle('open');
  });

  document.getElementById('btn-browse').addEventListener('click', async () => {
    const { path } = await GET('/api/browse');
    if (path) input.value = path;
  });

  document.getElementById('btn-load').addEventListener('click', () => loadYearFolder());

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') loadYearFolder(); });
}

async function loadYearFolder() {
  const folder = document.getElementById('year-folder-input').value.trim();
  if (!folder) return;

  state.yearFolder = folder;
  localStorage.setItem('yearFolder', folder);

  state.thresholds = {
    gap_db: parseFloat(document.getElementById('sl-gap-db').value),
    gap_sec: parseFloat(document.getElementById('sl-gap-sec').value),
    single_band_threshold: parseFloat(document.getElementById('sl-single').value),
  };

  // Discover event folders
  let discoverResult;
  try {
    discoverResult = await POST('/api/discover', { year_folder: folder, output_dir: state.outputDir || null });
  } catch (err) {
    alert(`Error: ${err.message}`);
    return;
  }

  state.events = discoverResult.events.map(e => ({
    ...e,
    uiState: deriveUiState(e),
  }));

  document.getElementById('header-path').textContent = folder;

  showScreen('screen-dashboard');
  renderDashboard();

  // Scan any folders that don't have scan results yet
  const needsScan = state.events.filter(e => !e.scan_result);
  if (needsScan.length > 0) {
    startScanAll(false);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════════════════════════════════════

function deriveUiState(event) {
  if (!event.scan_result) return 'scanning';
  const sr = event.scan_result;
  if (sr.skipped) return 'skipped';
  const st = event.status || {};
  if (st.exported) return 'exported';
  if (sr.confirmed) return 'approved';
  return 'pending';
}

function renderDashboard() {
  updateDashSummary();
  renderEventGrid();
}

function updateDashSummary() {
  const total = state.events.length;
  const approved = state.events.filter(e => ['approved', 'exported'].includes(e.uiState)).length;
  const needsReview = state.events.filter(e => e.status && e.status.needs_review).length;
  const exported = state.events.filter(e => e.uiState === 'exported').length;
  document.getElementById('dash-summary').innerHTML =
    `<strong>${total}</strong> events &nbsp;·&nbsp; <strong>${approved}</strong> approved &nbsp;·&nbsp; <strong>${needsReview}</strong> needs review &nbsp;·&nbsp; <strong>${exported}</strong> exported`;
}

function renderEventGrid() {
  const grid = document.getElementById('event-grid');
  const filter = state.activeFilter;

  const visible = state.events.filter(e => {
    if (filter === 'all') return true;
    if (filter === 'pending') return e.uiState === 'pending';
    if (filter === 'scanning') return e.uiState === 'scanning';
    if (filter === 'approved') return e.uiState === 'approved';
    if (filter === 'exported') return e.uiState === 'exported';
    if (filter === 'skipped') return e.uiState === 'skipped';
    return true;
  });

  grid.innerHTML = visible.map(e => renderCard(e)).join('');
  bindCardEvents();
}

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatSize(gb) {
  if (gb == null) return '';
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${(gb * 1024).toFixed(0)} MB`;
}

function fileRowHtml(f) {
  const cat = f.category;
  const willProcess = ['short_set', 'medium_stream', 'full_stream', 'audio_file'].includes(cat);
  const isImage = !['video', 'audio'].includes(f.type);
  const dotClass = willProcess ? 'dot-process' : isImage ? 'dot-rename' : 'dot-skip';
  const actionClass = willProcess ? 'action-process' : isImage ? 'action-rename' : 'action-skip';
  const actionText = willProcess ? '→ will analyze' : isImage ? '→ rename only' : '→ skip';
  return `
    <div class="file-row">
      <span class="file-row-dot ${dotClass}"></span>
      <span class="file-row-name" title="${escHtml(f.relative_path || f.name)}">${escHtml(f.name)}</span>
      <span class="file-row-size">${formatSize(f.size_gb)}</span>
      <span class="file-row-cat text-muted">${escHtml(cat)}</span>
      <span class="file-row-action ${actionClass}">${actionText}</span>
    </div>`;
}

function pipelineBadgeHtml(label, state, folderPath, stage) {
  const icons = { scanned: '✓', analyzed: '✓', reviewed: '✓', exported: '✓' };
  const icon = icons[stage] || '—';
  let cls = 'pipe-badge';
  let content = `${icon} ${label}`;
  let onclick = '';

  if (state === 'done') {
    cls += ' done';
  } else if (state === 'running') {
    cls += ' running';
    content = `<span class="spin">⟳</span> ${label}`;
  } else if (state === 'pending') {
    cls += ' pending';
    content = `⏳ ${label}`;
    onclick = `onclick="runStage('${escHtml(folderPath)}', '${escHtml(stage)}')"`;
  }

  return `<span class="${cls}" ${onclick}>${content}</span>`;
}

function cardStateBadgeHtml(uiState) {
  const labels = {
    pending: 'NEEDS APPROVAL',
    scanning: 'SCANNING…',
    approved: 'APPROVED',
    error: 'ERROR',
    skipped: 'SKIPPED',
    exported: 'EXPORTED',
  };
  return `<span class="card-state-badge state-badge-${uiState}">${labels[uiState] || uiState.toUpperCase()}</span>`;
}

function renderCard(event) {
  const sr = event.scan_result;
  const st = event.status || {};
  const fp = escHtml(event.folder);
  const fn = escHtml(event.folder_name);
  const uiState = event.uiState;

  // Pipeline stage states
  const pipeScanned   = sr ? 'done' : (uiState === 'scanning' ? 'running' : 'not-started');
  const pipeAnalyzed  = st.analyzed ? 'done' : (sr && sr.confirmed ? 'pending' : 'not-started');
  const pipeReviewed  = st.all_approved ? 'done' : (st.needs_review ? 'pending' : (st.analyzed ? 'done' : 'not-started'));
  const pipeExported  = st.exported ? 'done' : (st.all_approved ? 'pending' : 'not-started');

  const bands = sr ? (sr.bands || []) : [];
  const eventName = sr ? (sr.event_name || '') : '';
  const eventDate = sr ? (sr.event_date || '') : '';
  const notes = sr ? (sr.notes || '') : '';

  const bandsHtml = bands.map(b => `
    <span class="band-badge" data-folder="${fp}">
      ${escHtml(b)}
      <button class="remove-band" data-folder="${fp}" data-band="${escHtml(b)}" title="Remove">×</button>
    </span>
  `).join('') + `
    <span class="add-band-wrap" data-folder="${fp}">
      <input type="text" class="add-band-input" data-folder="${fp}" placeholder="+ band" list="bands-datalist" style="display:none">
      <button class="add-band-btn" data-folder="${fp}">+ Add</button>
    </span>`;

  // Files (show max 6, then "+ N more")
  const files = sr ? (sr.files || []) : [];
  const showFiles = files.slice(0, 6);
  const moreFiles = files.length - showFiles.length;
  const filesHtml = showFiles.map(fileRowHtml).join('') +
    (moreFiles > 0 ? `<div class="file-row" style="color:#555;font-style:italic">  + ${moreFiles} more file(s)</div>` : '');

  const scanningHtml = uiState === 'scanning'
    ? '<div class="scanning-pulse text-muted" style="font-size:0.82rem;padding:4px 0">Scanning with Claude…</div>'
    : '';

  // Prominent review call-to-action
  const reviewUrl = `/open-review?folder=${encodeURIComponent(event.folder)}`;
  const reviewBannerHtml = st.needs_review ? `
    <div class="review-banner">
      <span>⚠ Segment review needed — verify set boundaries and band labels</span>
      <a class="btn btn-sm" href="${reviewUrl}" target="_blank">Open Waveform Review →</a>
    </div>` : '';

  // Next-step hint when approved but not yet analyzed
  const nextStepHtml = (!st.analyzed && sr && sr.confirmed && uiState === 'approved') ? `
    <div class="next-step-hint">Next: click <strong>Analyze Approved</strong> above to detect set boundaries</div>` : '';

  const beforeAfterHtml = renderBeforeAfter(event);
  const metaHtml = renderMetadataSection(event);

  return `
  <div class="event-card state-${uiState}" id="card-${fp.replace(/[^a-z0-9]/gi, '_').slice(-30)}" data-folder="${fp}">
    <div class="card-header">
      <span class="card-date">${escHtml(eventDate)}</span>
      <span class="card-folder" title="${fn}">📁 ${fn}</span>
      ${cardStateBadgeHtml(uiState)}
    </div>

    ${sr ? `
    <div class="card-body">
      <div class="card-field">
        <span class="card-field-label">Event</span>
        <input type="text" class="event-name-input" data-folder="${fp}" value="${escHtml(eventName)}" placeholder="Event name">
      </div>
      <div class="card-field">
        <span class="card-field-label">Bands</span>
        <div class="bands-container" data-folder="${fp}">${bandsHtml}</div>
      </div>
      ${notes ? `<div class="card-field"><span class="card-field-label">Notes</span><span style="font-size:0.8rem;color:#aaa;flex:1">${escHtml(notes)}</span></div>` : ''}
      <div class="card-field">
        <span class="card-field-label">Files</span>
        <div class="file-rows">${filesHtml || '<span class="text-muted">No video/audio files found</span>'}</div>
      </div>
      <div class="card-field">
        <span class="card-field-label">Pipeline</span>
        <div class="pipeline-row">
          ${pipelineBadgeHtml('Scanned', pipeScanned, event.folder, 'scanned')}
          ${pipelineBadgeHtml('Analyzed', pipeAnalyzed, event.folder, 'analyze')}
          ${pipelineBadgeHtml('Reviewed', pipeReviewed, event.folder, 'review')}
          ${pipelineBadgeHtml('Exported', pipeExported, event.folder, 'export')}
        </div>
      </div>
    </div>
    ` : scanningHtml ? `<div class="card-body">${scanningHtml}</div>` : ''}

    ${nextStepHtml}
    ${reviewBannerHtml}

    <div class="log-panel hidden" id="log-${fp.replace(/[^a-z0-9]/gi, '_').slice(-30)}"></div>

    ${beforeAfterHtml}
    ${metaHtml}

    <div class="card-footer">
      ${uiState !== 'skipped' ? `<button class="btn btn-xs btn-danger" data-action="skip" data-folder="${fp}">Skip</button>` : `<button class="btn btn-xs" data-action="unskip" data-folder="${fp}">Unskip</button>`}
      ${sr ? `<button class="btn btn-xs" data-action="rescan" data-folder="${fp}">Re-scan</button>` : ''}
      ${st.exported ? `<button class="btn btn-xs" data-action="metadata" data-folder="${fp}">Generate Metadata</button>` : ''}
      <span style="flex:1"></span>
      ${sr && !sr.confirmed && uiState !== 'skipped' ? `<button class="btn btn-sm btn-green" data-action="approve" data-folder="${fp}">Approve ✓</button>` : ''}
      ${sr && sr.confirmed ? `<button class="btn btn-xs" data-action="unapprove" data-folder="${fp}">Edit</button>` : ''}
    </div>
  </div>`;
}

function renderBeforeAfter(event) {
  const st = event.status || {};
  if (!st.exported || !event.scan_result) return '';

  const sr = event.scan_result;
  const processable = (sr.files || []).filter(f =>
    ['short_set', 'medium_stream', 'full_stream'].includes(f.category)
  );

  const beforeRows = processable.map(f =>
    `<div class="ba-row ba-before"><span class="ba-icon">📼</span> ${escHtml(f.name)} (${formatSize(f.size_gb)})</div>`
  ).join('');

  return `
  <div class="before-after">
    <div class="before-after-label">Before → After</div>
    ${beforeRows}
    <div class="ba-after" id="exports-${escHtml(event.folder).replace(/[^a-z0-9]/gi,'_').slice(-30)}">
      <em class="text-muted" style="font-size:0.78rem">Loading exports…</em>
    </div>
  </div>`;
}

function renderMetadataSection(event) {
  const st = event.status || {};
  if (!st.exported) return '';
  return `<div class="metadata-section" id="meta-${escHtml(event.folder).replace(/[^a-z0-9]/gi,'_').slice(-30)}"></div>`;
}

function cardIdSuffix(folder) {
  return folder.replace(/[^a-z0-9]/gi, '_').slice(-30);
}

// ═══════════════════════════════════════════════════════════════════════════
// Card event binding
// ═══════════════════════════════════════════════════════════════════════════

function bindCardEvents() {
  // Approve button
  document.querySelectorAll('[data-action="approve"]').forEach(btn => {
    btn.addEventListener('click', () => approveEvent(btn.dataset.folder));
  });

  // Unapprove
  document.querySelectorAll('[data-action="unapprove"]').forEach(btn => {
    btn.addEventListener('click', () => unapproveEvent(btn.dataset.folder));
  });

  // Skip / unskip
  document.querySelectorAll('[data-action="skip"]').forEach(btn => {
    btn.addEventListener('click', () => skipEvent(btn.dataset.folder));
  });
  document.querySelectorAll('[data-action="unskip"]').forEach(btn => {
    btn.addEventListener('click', () => unskipEvent(btn.dataset.folder));
  });

  // Re-scan
  document.querySelectorAll('[data-action="rescan"]').forEach(btn => {
    btn.addEventListener('click', () => rescanEvent(btn.dataset.folder));
  });

  // Generate metadata
  document.querySelectorAll('[data-action="metadata"]').forEach(btn => {
    btn.addEventListener('click', () => runStage(btn.dataset.folder, 'metadata'));
  });

  // Event name inline edit
  document.querySelectorAll('.event-name-input').forEach(input => {
    input.addEventListener('blur', () => saveEventName(input.dataset.folder, input.value));
    input.addEventListener('keydown', e => { if (e.key === 'Enter') input.blur(); });
  });

  // Band remove
  document.querySelectorAll('.remove-band').forEach(btn => {
    btn.addEventListener('click', () => removeBand(btn.dataset.folder, btn.dataset.band));
  });

  // Add band — show input
  document.querySelectorAll('.add-band-btn').forEach(btn => {
    btn.addEventListener('click', function () {
      const wrap = this.closest('.add-band-wrap');
      const inp = wrap.querySelector('.add-band-input');
      inp.style.display = 'block';
      this.style.display = 'none';
      inp.focus();
    });
  });

  // Add band — commit on Enter/blur
  document.querySelectorAll('.add-band-input').forEach(inp => {
    const commit = () => {
      const val = inp.value.trim();
      if (val) addBand(inp.dataset.folder, val);
      inp.value = '';
      inp.style.display = 'none';
      const wrap = inp.closest('.add-band-wrap');
      wrap.querySelector('.add-band-btn').style.display = '';
    };
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') { inp.value=''; commit(); } });
    inp.addEventListener('blur', commit);
  });

  // Load export details for exported events
  state.events.filter(e => e.status && e.status.exported).forEach(e => {
    loadExports(e.folder);
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Card actions
// ═══════════════════════════════════════════════════════════════════════════

function findEvent(folder) {
  return state.events.find(e => e.folder === folder);
}

async function approveEvent(folder) {
  const ev = findEvent(folder);
  if (!ev || !ev.scan_result) return;
  await PATCH('/api/workspace/scan', { folder, confirmed: true, bands: ev.scan_result.bands, event_name: ev.scan_result.event_name, notes: ev.scan_result.notes });
  ev.scan_result.confirmed = true;
  ev.uiState = 'approved';
  refreshCard(folder);
  updateDashSummary();
}

async function unapproveEvent(folder) {
  const ev = findEvent(folder);
  if (!ev || !ev.scan_result) return;
  await PATCH('/api/workspace/scan', { folder, confirmed: false });
  ev.scan_result.confirmed = false;
  ev.uiState = 'pending';
  refreshCard(folder);
}

async function skipEvent(folder) {
  const ev = findEvent(folder);
  if (!ev) return;
  if (ev.scan_result) await PATCH('/api/workspace/scan', { folder, skipped: true });
  ev.uiState = 'skipped';
  refreshCard(folder);
  updateDashSummary();
}

async function unskipEvent(folder) {
  const ev = findEvent(folder);
  if (!ev) return;
  if (ev.scan_result) await PATCH('/api/workspace/scan', { folder, skipped: false });
  ev.uiState = ev.scan_result && ev.scan_result.confirmed ? 'approved' : 'pending';
  refreshCard(folder);
  updateDashSummary();
}

async function rescanEvent(folder) {
  const ev = findEvent(folder);
  if (!ev) return;
  ev.uiState = 'scanning';
  ev.scan_result = null;
  refreshCard(folder);

  const { job_id } = await POST('/api/scan', { folder, output_dir: state.outputDir || null });
  const logEl = document.getElementById(`log-${cardIdSuffix(folder)}`);
  if (logEl) { logEl.classList.remove('hidden'); logEl.innerHTML = ''; }

  streamJob(job_id, {
    onLog: (line) => { if (logEl) appendLog(logEl, line); },
    onDone: async () => {
      const ws = await GET(`/api/workspace?folder=${encodeURIComponent(folder)}`);
      ev.scan_result = ws.scan_result;
      ev.status = ws.status;
      ev.uiState = deriveUiState(ev);
      refreshCard(folder);
      updateDashSummary();
    },
  });
}

async function saveEventName(folder, name) {
  const ev = findEvent(folder);
  if (!ev || !ev.scan_result) return;
  if (ev.scan_result.event_name === name) return;
  ev.scan_result.event_name = name;
  await PATCH('/api/workspace/scan', { folder, event_name: name });
}

async function removeBand(folder, band) {
  const ev = findEvent(folder);
  if (!ev || !ev.scan_result) return;
  ev.scan_result.bands = (ev.scan_result.bands || []).filter(b => b !== band);
  await PATCH('/api/workspace/scan', { folder, bands: ev.scan_result.bands });
  refreshCard(folder);
}

async function addBand(folder, band) {
  const ev = findEvent(folder);
  if (!ev || !ev.scan_result) return;
  const bands = ev.scan_result.bands || [];
  if (!bands.includes(band)) {
    bands.push(band);
    ev.scan_result.bands = bands;
    await PATCH('/api/workspace/scan', { folder, bands });
  }
  await loadBands();
  refreshCard(folder);
}

async function openReview(folder) {
  const { url } = await POST('/api/review/start', { folder, output_dir: state.outputDir || null });
  window.open(url, '_blank');

  // Poll for completion every 3s
  const ev = findEvent(folder);
  const poll = setInterval(async () => {
    const { all_approved } = await GET(`/api/workspace/segments?folder=${encodeURIComponent(folder)}`);
    if (all_approved) {
      clearInterval(poll);
      if (ev) {
        const ws = await GET(`/api/workspace?folder=${encodeURIComponent(folder)}`);
        ev.status = ws.status;
        ev.uiState = deriveUiState(ev);
        refreshCard(folder);
      }
    }
  }, 3000);
}

async function runStage(folder, stage) {
  const endpoints = {
    analyze: '/api/analyze',
    export: '/api/export',
    metadata: '/api/metadata',
  };
  const ep = endpoints[stage];
  if (!ep) return;

  const body = { folder, output_dir: state.outputDir || null, ...state.thresholds };
  const { job_id } = await POST(ep, body);

  const logEl = document.getElementById(`log-${cardIdSuffix(folder)}`);
  if (logEl) { logEl.classList.remove('hidden'); logEl.innerHTML = ''; }

  streamJob(job_id, {
    onLog: (line) => { if (logEl) appendLog(logEl, line); },
    onDone: async () => {
      const ws = await GET(`/api/workspace?folder=${encodeURIComponent(folder)}`);
      const ev = findEvent(folder);
      if (ev) {
        ev.status = ws.status;
        ev.uiState = deriveUiState(ev);
        refreshCard(folder);
        updateDashSummary();
        if (stage === 'export') loadExports(folder);
        if (stage === 'metadata') loadMetadata(folder);
      }
    },
  });
}

async function loadExports(folder) {
  const { exports } = await GET(`/api/workspace/exports?folder=${encodeURIComponent(folder)}`);
  const el = document.getElementById(`exports-${cardIdSuffix(folder)}`);
  if (!el) return;
  if (!exports || exports.length === 0) {
    el.innerHTML = '<em class="text-muted" style="font-size:0.78rem">No exports yet</em>';
    return;
  }
  el.innerHTML = exports.map(exp =>
    `<div class="ba-row ba-after"><span class="ba-icon">✓</span> ${escHtml(exp.filename)} (${exp.size_mb} MB)</div>`
  ).join('');
}

async function loadMetadata(folder) {
  const { exports } = await GET(`/api/workspace/exports?folder=${encodeURIComponent(folder)}`);
  const el = document.getElementById(`meta-${cardIdSuffix(folder)}`);
  if (!el) return;
  const withMeta = exports.filter(e => e.metadata);
  if (!withMeta.length) return;
  el.innerHTML = withMeta.map(exp => metadataItemHtml(exp, folder)).join('');
  bindMetadataSave(el, folder, exports);
}

function metadataItemHtml(exp, folder) {
  const m = exp.metadata || {};
  const safeId = cardIdSuffix(exp.filename);
  return `
  <div class="metadata-item" data-meta-file="${escHtml(exp.filename)}">
    <div class="metadata-item-filename">${escHtml(exp.filename)}</div>
    <div class="metadata-field">
      <label>Title</label>
      <input type="text" class="meta-title" maxlength="100" value="${escHtml(m.title || '')}">
      <div class="char-count ${(m.title||'').length > 70 ? 'over' : ''}">${(m.title||'').length}/70</div>
    </div>
    <div class="metadata-field">
      <label>Description</label>
      <textarea class="meta-description" rows="3">${escHtml(m.description || '')}</textarea>
    </div>
    <div class="metadata-field">
      <label>Tags</label>
      <input type="text" class="meta-tags" value="${escHtml((m.tags || []).join(', '))}">
    </div>
    <div class="metadata-field">
      <label>Thumbnail time</label>
      <input type="text" class="meta-thumb" value="${escHtml(m.thumbnail_time || '')}">
    </div>
    <button class="btn btn-sm" data-save-meta="${escHtml(exp.filename)}">Save</button>
  </div>`;
}

function bindMetadataSave(el, folder, exports) {
  el.querySelectorAll('.meta-title').forEach(inp => {
    inp.addEventListener('input', function () {
      const cc = this.closest('.metadata-item').querySelector('.char-count');
      const len = this.value.length;
      cc.textContent = `${len}/70`;
      cc.className = `char-count ${len > 70 ? 'over' : len > 60 ? 'warn' : ''}`;
    });
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Refresh a single card without full re-render
// ═══════════════════════════════════════════════════════════════════════════

function refreshCard(folder) {
  const ev = findEvent(folder);
  if (!ev) return;
  const suffix = cardIdSuffix(folder);
  const oldCard = document.querySelector(`[data-folder="${CSS.escape(folder)}"].event-card`);
  if (!oldCard) {
    // Card not visible (filtered out) — just re-render grid
    renderEventGrid();
    return;
  }
  const newHtml = renderCard(ev);
  const tmp = document.createElement('div');
  tmp.innerHTML = newHtml;
  const newCard = tmp.firstElementChild;
  oldCard.replaceWith(newCard);
  bindCardEvents();
}

function appendLog(el, line) {
  const div = document.createElement('div');
  div.className = 'log-line' + (line.toLowerCase().includes('error') ? ' error' : '');
  div.textContent = line;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════════════════
// Batch scan all
// ═══════════════════════════════════════════════════════════════════════════

async function startScanAll(force = false) {
  if (!state.yearFolder) return;
  const { job_id } = await POST('/api/scan-all', {
    year_folder: state.yearFolder,
    output_dir: state.outputDir || null,
    force,
  });

  streamJob(job_id, {
    onLog: (line) => console.log('[scan-all]', line),
    onEvent: (ev) => {
      if (ev.type === 'folder_done') {
        const event = state.events.find(e => e.folder_name === ev.folder || e.folder === ev.folder_path);
        if (event) {
          event.scan_result = ev.scan_result;
          event.uiState = deriveUiState(event);
          refreshCard(event.folder);
        }
        updateDashSummary();
      }
    },
    onDone: () => updateDashSummary(),
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Bulk actions
// ═══════════════════════════════════════════════════════════════════════════

async function runBatchJob(title, endpoint, folders, extra = {}) {
  if (!folders.length) { alert('No eligible events.'); return; }
  const { job_id } = await POST(endpoint, { folders, output_dir: state.outputDir || null, ...extra, ...state.thresholds });
  state.batchJobId = job_id;
  openBatchProgress(title, folders);

  streamJob(job_id, {
    onLog: (line) => console.log(`[${endpoint}]`, line),
    onEvent: async (ev) => {
      if (ev.type === 'folder_done') {
        updateBatchItem(ev.folder, 'done');
        // Refresh event status
        const event = state.events.find(e => e.folder_name === ev.folder);
        if (event) {
          const ws = await GET(`/api/workspace?folder=${encodeURIComponent(event.folder)}`);
          event.status = ws.status;
          event.uiState = deriveUiState(event);
          refreshCard(event.folder);
          if (ev.stage === 'export') loadExports(event.folder);
        }
        updateDashSummary();
      } else if (ev.type === 'folder_error') {
        updateBatchItem(ev.folder, 'error');
      } else if (ev.type === 'progress') {
        updateBatchProgress(ev.done, ev.total);
      }
    },
    onDone: () => {
      updateBatchProgress(folders.length, folders.length);
      document.getElementById('batch-title').textContent = title + ' — Done';
    },
  });
}

function openBatchProgress(title, folders) {
  document.getElementById('batch-progress').classList.add('open');
  document.getElementById('batch-title').textContent = title;
  document.getElementById('batch-count').textContent = `0/${folders.length}`;
  document.getElementById('batch-fill').style.width = '0%';
  const list = document.getElementById('batch-folder-list');
  list.innerHTML = folders.map(f => {
    const name = f.split('/').pop();
    return `<span class="batch-folder-item" id="batch-item-${CSS.escape(name)}" data-name="${name}">${escHtml(name)}</span>`;
  }).join('');
}

function updateBatchItem(folderName, status) {
  const el = document.getElementById(`batch-item-${CSS.escape(folderName)}`);
  if (el) el.className = `batch-folder-item ${status}`;
}

function updateBatchProgress(done, total) {
  document.getElementById('batch-count').textContent = `${done}/${total}`;
  document.getElementById('batch-fill').style.width = `${Math.round(done / total * 100)}%`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Settings panel
// ═══════════════════════════════════════════════════════════════════════════

async function openSettings() {
  const cfg = await GET('/api/config');
  state.config = cfg;
  document.getElementById('cfg-ffmpeg').value = cfg.ffmpeg_path || '';
  document.getElementById('cfg-ffprobe').value = cfg.ffprobe_path || '';
  document.getElementById('cfg-output-dir').value = cfg.default_output_dir || '';
  document.getElementById('cfg-gap-db').value = cfg.gap_db ?? 12;
  document.getElementById('cfg-gap-sec').value = cfg.gap_sec ?? 30;
  document.getElementById('cfg-single-band').value = cfg.single_band_threshold_min ?? 75;
  document.getElementById('cfg-min-seg').value = cfg.min_segment_min ?? 5;
  document.getElementById('cfg-calendar').value = cfg.google_calendar_id || '';

  const bands = await loadBands();
  renderSettingsBands(bands);

  document.getElementById('settings-overlay').classList.add('open');
}

function renderSettingsBands(bands) {
  const list = document.getElementById('settings-bands-list');
  list.innerHTML = bands.map(b => `
    <span class="band-manager-item">
      ${escHtml(b)}
      <button data-band="${escHtml(b)}" class="settings-remove-band">×</button>
    </span>`).join('');

  list.querySelectorAll('.settings-remove-band').forEach(btn => {
    btn.addEventListener('click', async () => {
      await DELETE(`/api/bands/${encodeURIComponent(btn.dataset.band)}`);
      const bands = await loadBands();
      renderSettingsBands(bands);
    });
  });
}

async function saveSettings() {
  const cfg = {
    ffmpeg_path: document.getElementById('cfg-ffmpeg').value.trim(),
    ffprobe_path: document.getElementById('cfg-ffprobe').value.trim(),
    default_output_dir: document.getElementById('cfg-output-dir').value.trim() || null,
    gap_db: parseFloat(document.getElementById('cfg-gap-db').value),
    gap_sec: parseFloat(document.getElementById('cfg-gap-sec').value),
    single_band_threshold_min: parseFloat(document.getElementById('cfg-single-band').value),
    min_segment_min: parseFloat(document.getElementById('cfg-min-seg').value),
    google_calendar_id: document.getElementById('cfg-calendar').value.trim(),
  };
  await POST('/api/config', cfg);
  document.getElementById('settings-overlay').classList.remove('open');
}

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════

async function init() {
  // Load config for threshold defaults
  try {
    const cfg = await GET('/api/config');
    state.config = cfg;
    document.getElementById('sl-gap-db').value = cfg.gap_db ?? 12;
    document.getElementById('val-gap-db').textContent = cfg.gap_db ?? 12;
    document.getElementById('sl-gap-sec').value = cfg.gap_sec ?? 30;
    document.getElementById('val-gap-sec').textContent = cfg.gap_sec ?? 30;
    document.getElementById('sl-single').value = cfg.single_band_threshold_min ?? 75;
    document.getElementById('val-single').textContent = cfg.single_band_threshold_min ?? 75;

    if (cfg._warnings && cfg._warnings.length) {
      const el = document.getElementById('entry-warnings');
      el.classList.remove('hidden');
      el.innerHTML = cfg._warnings.map(w => `⚠ ${escHtml(w)}`).join('<br>');
    }
  } catch { /* server may still be starting */ }

  await loadBands();
  initEntry();

  // Header buttons
  document.getElementById('btn-change-folder').addEventListener('click', () => showScreen('screen-entry'));
  document.getElementById('btn-settings').addEventListener('click', openSettings);
  document.getElementById('btn-close-settings').addEventListener('click', () =>
    document.getElementById('settings-overlay').classList.remove('open'));
  document.getElementById('settings-overlay').addEventListener('click', (e) => {
    if (e.target === document.getElementById('settings-overlay'))
      document.getElementById('settings-overlay').classList.remove('open');
  });
  document.getElementById('btn-save-settings').addEventListener('click', saveSettings);

  document.getElementById('btn-browse-output').addEventListener('click', async () => {
    const { path } = await GET('/api/browse');
    if (path) document.getElementById('cfg-output-dir').value = path;
  });

  document.getElementById('btn-add-band').addEventListener('click', async () => {
    const inp = document.getElementById('settings-new-band');
    const name = inp.value.trim();
    if (!name) return;
    await POST('/api/bands', { name });
    inp.value = '';
    const bands = await loadBands();
    renderSettingsBands(bands);
  });

  // Batch progress bar
  document.getElementById('btn-close-batch').addEventListener('click', () =>
    document.getElementById('batch-progress').classList.remove('open'));
  document.getElementById('btn-cancel-batch').addEventListener('click', () =>
    document.getElementById('batch-progress').classList.remove('open'));

  // Filter chips
  document.querySelectorAll('.filter-chip').forEach(chip => {
    chip.addEventListener('click', function () {
      document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
      this.classList.add('active');
      state.activeFilter = this.dataset.filter;
      renderEventGrid();
    });
  });

  // Bulk action buttons
  document.getElementById('btn-scan-pending').addEventListener('click', () => startScanAll(false));

  document.getElementById('btn-analyze-all').addEventListener('click', () => {
    const folders = state.events
      .filter(e => e.uiState === 'approved' && e.scan_result && !(e.status && e.status.analyzed))
      .map(e => e.folder);
    runBatchJob('Analyzing…', '/api/analyze-batch', folders);
  });

  document.getElementById('btn-export-all').addEventListener('click', () => {
    const folders = state.events
      .filter(e => e.status && e.status.all_approved && !e.status.exported)
      .map(e => e.folder);
    runBatchJob('Exporting…', '/api/export-batch', folders);
  });

  document.getElementById('btn-metadata-all').addEventListener('click', () => {
    const folders = state.events
      .filter(e => e.status && e.status.exported)
      .map(e => e.folder);
    runBatchJob('Generating metadata…', '/api/metadata-batch', folders);
  });

  // If we have a saved folder, auto-load
  if (state.yearFolder) {
    document.getElementById('year-folder-input').value = state.yearFolder;
  }
}

document.addEventListener('DOMContentLoaded', init);
