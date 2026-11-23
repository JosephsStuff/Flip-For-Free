// ---------- sync button ----------
(function () {
  const btn = document.getElementById('syncBtn');
  const text = document.getElementById('syncText');
  const msg = document.getElementById('syncMsg');
  if (!btn) return;

  function show(m, cls) {
    if (!msg) return;
    msg.textContent = m; msg.className = 'flash ' + (cls || ''); msg.hidden = !m;
  }
  async function poll() {
    try {
      const r = await fetch('/sync/status'); const s = await r.json();
      if (s.running) {
        text.textContent = 'Syncing… ' + (s.progress || '');
        btn.disabled = true;
        setTimeout(poll, 2000);
      } else {
        btn.disabled = false;
        if (btn.dataset.waiting) {
          delete btn.dataset.waiting;
          show(s.message, s.last_result === 'ok' ? 'ok' : (s.last_result === 'error' ? 'error' : 'warn'));
          text.textContent = 'Synced just now';
          if (s.last_result !== 'error') setTimeout(() => location.reload(), 1200);
        }
      }
    } catch (e) { setTimeout(poll, 4000); }
  }
  async function start(full) {
    const body = new FormData(); if (full) body.append('full', '1');
    const r = await fetch('/sync', { method: 'POST', body });
    const j = await r.json();
    if (!j.started && !/already/.test(j.message)) { show(j.message, 'warn'); return; }
    btn.dataset.waiting = '1'; show('Syncing with eBay…', '');
    poll();
  }
  btn.addEventListener('click', () => start(false));
  const full = document.getElementById('fullSyncBtn');
  if (full) full.addEventListener('click', () => {
    if (confirm('Re-download all orders and fees from eBay? Your costs and bulk buys are kept.')) start(true);
  });
  if (/Syncing/.test(text.textContent)) { btn.dataset.waiting = '1'; poll(); }
})();

// ---------- setup: folder browser ----------
function initSetup() {
  const input = document.getElementById('dbPath');
  const mode = document.getElementById('dbMode');
  const box = document.getElementById('browser');
  const list = document.getElementById('dirList');
  const cur = document.getElementById('curPath');
  if (!input) return;
  let here = null, parent = null;

  async function load(path) {
    const r = await fetch('/api/browse?path=' + encodeURIComponent(path || ''));
    const j = await r.json();
    here = j.path; parent = j.parent; cur.textContent = here;
    list.innerHTML = '';
    if (j.error) list.innerHTML = '<li class="muted">' + j.error + '</li>';
    j.dbs.forEach(d => {
      const li = document.createElement('li'); li.className = 'db';
      li.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>'; li.append(d); li.title = 'Open this database';
      li.onclick = () => { input.value = here + sep(here) + d; mode.value = 'file'; box.hidden = true; };
      list.appendChild(li);
    });
    j.dirs.forEach(d => {
      const li = document.createElement('li'); li.innerHTML = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>'; li.append(d);
      li.onclick = () => load(here + sep(here) + d);
      list.appendChild(li);
    });
    if (!j.dirs.length && !j.dbs.length && !j.error) list.innerHTML = '<li class="muted">No sub-folders</li>';
  }
  const sep = p => (p.includes('\\') ? (p.endsWith('\\') ? '' : '\\') : (p.endsWith('/') ? '' : '/'));
  document.getElementById('browseBtn').onclick = () => { box.hidden = false; load(input.value); };
  document.getElementById('upBtn').onclick = () => load(parent);
  document.getElementById('useBtn').onclick = () => { input.value = here; mode.value = 'folder'; box.hidden = true; };
  document.querySelectorAll('.chip').forEach(c => c.onclick = () => {
    if (c.dataset.file) { input.value = c.dataset.file; mode.value = 'file'; }
    else { input.value = c.dataset.path; mode.value = 'folder'; box.hidden = false; load(c.dataset.path); }
  });
  input.addEventListener('input', () => { mode.value = /\.db$/i.test(input.value) ? 'file' : 'folder'; });
  document.querySelectorAll('code.copy').forEach(c => {
    c.title = 'Click to copy';
    c.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(c.textContent); c.classList.add('copied'); setTimeout(() => c.classList.remove('copied'), 900); };
  });
}

// ---------- sales list: selection + action bar ----------
function initSales() {
  const form = document.getElementById('salesForm');
  const bar = document.getElementById('actionBar');
  const count = document.getElementById('selCount');
  const all = document.getElementById('checkAll');
  const bulkSel = document.getElementById('bulkSel');
  const newName = document.getElementById('newBulkName');
  const boxes = () => [...form.querySelectorAll('input[name=ids]')];
  let last = null;
  function update() {
    const n = boxes().filter(b => b.checked).length;
    bar.hidden = n === 0; count.textContent = n + ' selected';
    boxes().forEach(b => b.closest('tr').classList.toggle('sel', b.checked));
  }
  form.addEventListener('click', e => {
    const b = e.target;
    if (b.name !== 'ids') return;
    if (e.shiftKey && last) {  // shift-click selects a range
      const list = boxes(); const [a, z] = [list.indexOf(last), list.indexOf(b)].sort((x, y) => x - y);
      list.slice(a, z + 1).forEach(x => x.checked = b.checked);
    }
    last = b; update();
  });
  all.addEventListener('change', () => { boxes().forEach(b => b.checked = all.checked); update(); });
  bulkSel.addEventListener('change', () => { newName.hidden = bulkSel.value !== 'new'; if (!newName.hidden) newName.focus(); });
  // clicking a row (not a link/input) toggles it
  form.querySelectorAll('tbody tr').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a,input,button,select')) return;
    const cb = tr.querySelector('input[name=ids]'); if (!cb) return;
    cb.checked = !cb.checked; last = cb; update();
  }));
  update();
}

// ---------- sale detail ----------
function initSale() {
  const sel = document.getElementById('saleBulk');
  const field = document.getElementById('costField');
  if (!sel) return;
  const f = () => field.classList.toggle('dim', !!sel.value);
  sel.addEventListener('change', f); f();
}

// ---------- charts ----------
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function gbp(v) { return (v < 0 ? '-£' : '£') + Math.abs(v).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 }); }
function baseOpts() {
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--line');
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
      tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + gbp(c.parsed.y) } } },
    scales: { y: { ticks: { callback: v => gbp(v) }, grid: { color: css('--line') } }, x: { grid: { display: false } } },
  };
}
function monthChart(rows) {
  const el = document.getElementById('monthChart'); if (!el || !window.Chart) return;
  const label = m => new Date(m + '-01').toLocaleDateString('en-GB', { month: 'short', year: '2-digit' });
  new Chart(el, {
    data: {
      labels: rows.map(r => label(r.month)),
      datasets: [
        { type: 'bar', label: 'Money made', data: rows.map(r => r.money_made), backgroundColor: css('--accent-soft'), borderRadius: 4, order: 2 },
        { type: 'bar', label: 'Stock costs', data: rows.map(r => -r.costs), backgroundColor: css('--neg-soft'), borderRadius: 4, order: 3 },
        { type: 'line', label: 'Profit', data: rows.map(r => r.profit), borderColor: css('--ink'), backgroundColor: css('--ink'), tension: .3, pointRadius: 3, order: 1 },
      ],
    },
    options: { ...baseOpts(), scales: { ...baseOpts().scales, x: { stacked: true, grid: { display: false } }, y: { ...baseOpts().scales.y, stacked: false } } },
  });
}
function bulkChart(curve, cost) {
  const el = document.getElementById('bulkChart'); if (!el || !window.Chart) return;
  new Chart(el, {
    type: 'line',
    data: {
      labels: curve.map(p => p.d),
      datasets: [
        { label: 'Money made so far', data: curve.map(p => p.v), borderColor: css('--accent'), backgroundColor: css('--accent-soft'), fill: true, tension: .25, pointRadius: 2 },
        { label: 'Cost', data: curve.map(() => cost), borderColor: css('--neg'), borderDash: [6, 4], pointRadius: 0 },
      ],
    },
    options: baseOpts(),
  });
}
function priceChart(dates, prices) {
  const el = document.getElementById('priceChart'); if (!el || !window.Chart) return;
  new Chart(el, {
    type: 'line',
    data: { labels: dates, datasets: [{ label: 'Price each', data: prices, borderColor: css('--accent'), backgroundColor: css('--accent'), tension: .2, pointRadius: 3 }] },
    options: baseOpts(),
  });
}

// ---------- generic tick-to-select table with action bar ----------
function initSelectable(formId) {
  const form = document.getElementById(formId); if (!form) return;
  const bar = form.querySelector('#actionBar'), count = form.querySelector('#selCount'), all = form.querySelector('#checkAll');
  const boxes = () => [...form.querySelectorAll('input[name=ids]')];
  let last = null;
  function update() {
    const n = boxes().filter(b => b.checked).length;
    bar.hidden = n === 0; count.textContent = n + ' selected';
    boxes().forEach(b => b.closest('tr').classList.toggle('sel', b.checked));
  }
  form.addEventListener('click', e => {
    const b = e.target; if (b.name !== 'ids') return;
    if (e.shiftKey && last) { const l = boxes(); const [a, z] = [l.indexOf(last), l.indexOf(b)].sort((x, y) => x - y); l.slice(a, z + 1).forEach(x => x.checked = b.checked); }
    last = b; update();
  });
  if (all) all.addEventListener('change', () => { boxes().forEach(b => b.checked = all.checked); update(); });
  form.querySelectorAll('tbody tr').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a,input,button,select')) return;
    const cb = tr.querySelector('input[name=ids]'); if (!cb) return; cb.checked = !cb.checked; last = cb; update();
  }));
  update();
}

// ---------- insights ----------
function insightsCharts(weeks) {
  if (!window.Chart) return;
  const labels = weeks.map(w => new Date(w.week).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }));
  const a = document.getElementById('lvsChart');
  if (a) new Chart(a, { type: 'bar', data: { labels, datasets: [
      { label: 'New listings', data: weeks.map(w => w.listed), backgroundColor: css('--line'), borderRadius: 3 },
      { label: 'Items sold', data: weeks.map(w => w.sold), backgroundColor: css('--accent'), borderRadius: 3 }] },
    options: { ...baseOpts(), plugins: { ...baseOpts().plugins, tooltip: {} }, scales: { y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: css('--line') } }, x: { grid: { display: false } } } } });
  const b = document.getElementById('trendChart');
  if (b) new Chart(b, { data: { labels, datasets: [
      { type: 'bar', label: 'Sales', data: weeks.map(w => w.gross), backgroundColor: css('--accent-soft'), borderRadius: 3, yAxisID: 'y' },
      { type: 'line', label: 'Profit', data: weeks.map(w => w.profit), borderColor: css('--ink'), backgroundColor: css('--ink'), tension: .3, pointRadius: 2, yAxisID: 'y' },
      { type: 'line', label: 'Margin %', data: weeks.map(w => w.margin), borderColor: css('--warn'), borderDash: [5, 4], pointRadius: 0, tension: .3, yAxisID: 'y2' }] },
    options: { ...baseOpts(), plugins: { ...baseOpts().plugins, tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + (c.dataset.yAxisID === 'y2' ? (c.parsed.y ?? '—') + '%' : gbp(c.parsed.y)) } } },
      scales: { y: { ticks: { callback: v => gbp(v) }, grid: { color: css('--line') } }, y2: { position: 'right', grid: { display: false }, ticks: { callback: v => v + '%' } }, x: { grid: { display: false } } } } });
}

const COUNTRY_CENTRES = { GB: [54.5, -2.5], IE: [53.3, -8], US: [39.5, -98.4], CA: [56, -96], AU: [-25, 134], NZ: [-41, 174], DE: [51.2, 10.4],
  FR: [46.6, 2.4], ES: [40.2, -3.6], IT: [42.8, 12.6], NL: [52.2, 5.3], BE: [50.6, 4.6], AT: [47.6, 14.1], CH: [46.8, 8.2], SE: [62, 15], NO: [61, 9],
  DK: [56, 10], FI: [64, 26], PL: [52, 19.4], PT: [39.6, -8], GR: [39, 22], CZ: [49.8, 15.5], HU: [47.2, 19.4], RO: [45.9, 25], JP: [36.5, 138],
  CN: [35, 104], HK: [22.3, 114.2], SG: [1.35, 103.8], KR: [36.5, 127.9], IL: [31.4, 35], AE: [24, 54], ZA: [-29, 24], BR: [-10, -52], MX: [23.6, -102.5],
  IN: [22, 79], MT: [35.9, 14.4], CY: [35, 33], LU: [49.8, 6.1], IS: [65, -18], JE: [49.2, -2.1], GG: [49.45, -2.58], IM: [54.2, -4.5] };

async function buyerMap(geo, countries) {
  const el = document.getElementById('buyerMap'); if (!el || !window.L) return;
  const map = L.map(el, { scrollWheelZoom: false }).setView([54.5, -3], 5);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 12, attribution: '© OpenStreetMap' }).addTo(map);
  const accent = css('--accent'), max = Math.max(1, ...Object.values(geo), ...Object.values(countries));
  const dot = (lat, lon, n, label) => L.circleMarker([lat, lon], { radius: 5 + 14 * Math.sqrt(n / max), color: accent, weight: 1, fillColor: accent, fillOpacity: .45 })
    .bindTooltip(label + ': ' + n + ' sale' + (n > 1 ? 's' : '')).addTo(map);
  Object.entries(countries).forEach(([c, n]) => { if (c !== 'GB' && COUNTRY_CENTRES[c]) dot(...COUNTRY_CENTRES[c], n, c); });
  let cache = {}; try { cache = JSON.parse(localStorage.getItem('outcodes') || '{}'); } catch (e) {}
  const todo = Object.keys(geo).filter(o => !(o in cache));
  for (let i = 0; i < todo.length; i += 8) {
    await Promise.all(todo.slice(i, i + 8).map(o => fetch('https://api.postcodes.io/outcodes/' + encodeURIComponent(o))
      .then(r => r.ok ? r.json() : null).then(j => { cache[o] = j && j.result ? [j.result.latitude, j.result.longitude] : null; }).catch(() => {})));
  }
  try { localStorage.setItem('outcodes', JSON.stringify(cache)); } catch (e) {}
  const pts = [];
  Object.entries(geo).forEach(([o, n]) => { const p = cache[o]; if (p) { dot(p[0], p[1], n, o); pts.push(p); } });
  if (!Object.keys(countries).some(c => c !== 'GB') && pts.length > 1) map.fitBounds(pts, { padding: [20, 20], maxZoom: 9 });
}

// ---------- notifications: badge, toasts, desktop alerts ----------
(function () {
  const badge = document.getElementById('bellCount'); if (!badge) return;
  const box = document.getElementById('toasts');
  let lastId = null; try { lastId = parseInt(localStorage.getItem('lastNotif') || '', 10) || null; } catch (e) {}
  function toast(n) {
    const t = document.createElement('div'); t.className = 'toast';
    t.innerHTML = '<b></b><span></span>'; t.querySelector('b').textContent = n.title; t.querySelector('span').textContent = n.body || '';
    if (n.link) { t.style.cursor = 'pointer'; t.onclick = () => location.href = n.link; }
    box.appendChild(t); setTimeout(() => t.remove(), 9000);
    if (window.Notification && Notification.permission === 'granted' && document.hidden) {
      try { const dn = new Notification(n.title, { body: n.body || '' }); dn.onclick = () => { window.focus(); if (n.link) location.href = n.link; }; } catch (e) {}
    }
  }
  async function poll() {
    try {
      const r = await fetch('/notifications/latest?after=' + (lastId || 0)); if (!r.ok) return;
      const j = await r.json();
      badge.textContent = j.unread; badge.hidden = !j.unread;
      if (lastId !== null) j.items.filter(n => n.kind === 'sale' || n.kind === 'issue').slice(-3).forEach(toast);
      lastId = j.max_id; try { localStorage.setItem('lastNotif', String(lastId)); } catch (e) {}
    } catch (e) {}
  }
  poll(); setInterval(poll, 60000);
  const btn = document.getElementById('enableNotify');
  if (btn && window.Notification && Notification.permission === 'default') {
    btn.hidden = false; btn.onclick = () => Notification.requestPermission().then(() => btn.hidden = true);
  }
})();

// ---------- period picker: highlight "Custom" and open the date fields ----------
function showCustomRange(btn) {
  const form = btn.closest('form');
  form.querySelectorAll('.seg button').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  const panel = form.querySelector('.custom');
  panel.hidden = false;
  const first = panel.querySelector('input[type=date]');
  if (first) first.focus();
}

function filterLinkable(input) {
  const q = input.value.toLowerCase();
  input.closest('form').querySelectorAll('tbody tr').forEach(tr => { tr.hidden = q && !tr.textContent.toLowerCase().includes(q); });
}

// Enter in the period picker: in the name box it saves the range, in a date box it applies the dates
// (otherwise the browser submits the first button, "Today", and nothing is saved).
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' || !e.target.closest) return;
  const panel = e.target.closest('.periods .custom, .pnl-range');
  if (!panel) return;
  e.preventDefault();
  const want = e.target.name === 'range_name' && e.target.value.trim() ? '.save-range' : '.apply-range';
  const btn = panel.querySelector(want);
  if (btn) btn.click();
});
