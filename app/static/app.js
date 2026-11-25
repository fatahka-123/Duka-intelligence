/* Grovia — web client (v0.2)
 * Plain JS, no build step. Pages are rendered as HTML strings into #pageContent.
 * All user-supplied text MUST pass through esc() before being placed into HTML.
 */
'use strict';

// ------------------------------------------------------------------ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = n => 'KSh ' + Number(n || 0).toLocaleString('en-KE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const short = n => { n = Number(n || 0); return Math.abs(n) >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : Math.abs(n) >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k' : n.toFixed(0); };
const utc = s => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z');
const fmtDateTime = s => utc(s).toLocaleString('en-KE', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const fmtDate = s => utc(s).toLocaleDateString('en-KE', { day: 'numeric', month: 'short', year: 'numeric' });
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g, '');
const METHODS = { cash: 'Cash', mpesa: 'M-Pesa', bank: 'Bank', card: 'Card', credit: 'Credit', other: 'Other' };
const isManager = () => ['owner', 'manager'].includes(state.me?.role);
const isOwner = () => state.me?.role === 'owner';

function delta(pctValue, suffix = 'vs yesterday') {
  if (pctValue === null || pctValue === undefined) return `<span class="muted">No comparison yet</span>`;
  const up = pctValue >= 0;
  return `<span class="delta ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(pctValue)}%</span> <span class="muted">${suffix}</span>`;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

// ------------------------------------------------------------------ theme (light / dark / follow the phone)
const THEMES = { light: 'Light', dark: 'Dark', system: 'Same as device' };
// Colour themes: [label, accent, sidebar, highlight]
const ACCENTS = {
  forest: ['Forest', '#16734a', '#0f241b', '#d9f36b'], ocean: ['Ocean', '#1d5fd1', '#0c1730', '#7dd3fc'],
  sunset: ['Sunset', '#d1541d', '#26120d', '#fdc27a'], royal: ['Royal', '#6d3fd1', '#170f2e', '#c4b5fd'],
  rose: ['Rose', '#c2185b', '#270d18', '#f9a8d4'], graphite: ['Graphite', '#2f3b4d', '#0e1420', '#facc15'],
};
const themeChoice = () => store.get('duka_theme', 'light');
const accentChoice = () => { const a = store.get('duka_accent', 'forest'); return ACCENTS[a] ? a : 'forest'; };
function applyTheme(choice = themeChoice(), accent = accentChoice()) {
  const dark = choice === 'dark' || (choice === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  const root = document.documentElement;
  root.dataset.theme = dark ? 'dark' : 'light'; root.dataset.accent = accent;
  $('meta[name=theme-color]')?.setAttribute('content', ACCENTS[accent][2]);
}
function markPicked() {
  $$('[data-choice]').forEach(b => { const on = b.dataset.choice === themeChoice(); b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
  $$('[data-accent-choice]').forEach(b => { const on = b.dataset.accentChoice === accentChoice(); b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
}
function setTheme(choice) { store.set('duka_theme', choice); applyTheme(); markPicked(); toast(`Mode: ${THEMES[choice]}`); }
function setAccent(accent) { store.set('duka_accent', accent); applyTheme(); markPicked(); toast(`Theme: ${ACCENTS[accent][0]}`); }
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { if (themeChoice() === 'system') applyTheme(); });
applyTheme();

class NetworkError extends Error {}

async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch('/api' + path, { credentials: 'same-origin', ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  } catch {
    throw new NetworkError('You appear to be offline');
  }
  let data = {};
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && !path.startsWith('/auth/')) { authView(); throw new Error('Please sign in again'); }
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong');
  return data;
}
const post = (path, body, method = 'POST') => api(path, { method, body: JSON.stringify(body) });

function toast(msg, ms = 3200) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), ms);
}
function openModal(html) { $('#modal').innerHTML = html; $('#modalBack').classList.add('show'); setTimeout(() => $('#modal input:not([type=hidden]),#modal select')?.focus(), 30); }
function closeModal() { $('#modalBack').classList.remove('show'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
$('#modalBack').addEventListener('click', e => { if (e.target.id === 'modalBack') closeModal(); });

/** Wire a modal form: collects fields, calls handler, shows errors. */
function onSubmit(id, handler) {
  const form = $('#' + id);
  form.onsubmit = async e => {
    e.preventDefault();
    const btn = form.querySelector('button[type=submit],button:not([type])');
    if (btn) btn.disabled = true;
    try { await handler(Object.fromEntries(new FormData(form)), form); }
    catch (err) { toast(err.message); }
    finally { if (btn) btn.disabled = false; }
  };
}

// ------------------------------------------------------------------ state
let state = { me: null, business: null, page: 'dashboard', products: [], customers: [], suppliers: [], dash: null };
let cart = { lines: [], ref: uid() };
let reportPeriod = 'month';
let salesFilter = { period: '7d', unpaid: false };
let chatLog = [];

// ------------------------------------------------------------------ offline sale queue
// Sales made while offline are stored on this device and replayed when the connection returns.
// Each carries a client_ref, so the server records it exactly once even if a replay is repeated.
const queueKey = () => `duka_queue_${state.business?.id}`;
const getQueue = () => store.get(queueKey(), []);
const setQueue = q => { store.set(queueKey(), q); renderOfflineBar(); };

async function syncQueue() {
  const q = getQueue();
  if (!q.length || !navigator.onLine) return;
  const remaining = [];
  let synced = 0;
  for (const item of q) {
    try { await post('/sales', item.body); synced++; }
    catch (e) {
      if (e instanceof NetworkError) { remaining.push(item); continue; }
      remaining.push({ ...item, error: e.message });
    }
  }
  setQueue(remaining);
  if (synced) { toast(`Synced ${synced} offline sale(s)`); await loadData(); renderPage(); }
}

function renderOfflineBar() {
  const el = $('#offlineBar'); if (!el) return;
  const q = getQueue(), failed = q.filter(x => x.error);
  if (navigator.onLine && !q.length) { el.innerHTML = ''; return; }
  el.innerHTML = `<div class="offline-bar"><span>${navigator.onLine ? '' : '● Offline — sales will be saved on this device. '}${q.length ? `${q.length} sale(s) waiting to sync${failed.length ? `, ${failed.length} need attention` : ''}.` : ''}</span>
    ${q.length ? `<span><button class="btn light small" onclick="syncQueue()">Sync now</button> ${failed.length ? `<button class="btn light small" onclick="showQueue()">Review</button>` : ''}</span>` : ''}</div>`;
}
function showQueue() {
  const q = getQueue();
  openModal(`<h2>Offline sales</h2><p>These sales were recorded without a connection. Items marked with an error were rejected by the server (for example not enough stock).</p>
  ${q.map((x, i) => `<div class="kv"><span>${fmtDateTime(x.at)} · ${x.body.items.length} item(s) · ${money(x.total)}${x.error ? `<br><span style="color:var(--red)">${esc(x.error)}</span>` : ''}</span>
    <button class="btn danger small" onclick="dropQueued(${i})">Discard</button></div>`).join('') || '<div class="empty">Nothing waiting.</div>'}
  <div class="modal-actions"><button class="btn light" onclick="closeModal()">Close</button><button class="btn" onclick="closeModal();syncQueue()">Retry sync</button></div>`);
}
function dropQueued(i) { const q = getQueue(); q.splice(i, 1); setQueue(q); showQueue(); }
window.addEventListener('online', () => { renderOfflineBar(); syncQueue(); });
window.addEventListener('offline', renderOfflineBar);

// ------------------------------------------------------------------ auth
function authView(mode = 'login') {
  const reg = mode === 'register';
  $('#root').innerHTML = `<div class="auth"><section class="auth-art"><div class="brand"><div class="brand-mark">G</div><div><b>GROVIA</b><small>BUSINESS, MADE CLEAR.</small></div></div>
  <div><div class="eyebrow" style="color:var(--highlight)">THE SMARTER WAY TO RUN RETAIL</div><h1>Know your numbers.<br>Grow with confidence.</h1>
  <p>Sales, stock, credit customers and plain-language business insights in one simple workspace — built for African shops, in English and Kiswahili.</p></div>
  <div style="font-size:11px;color:#90a99a">© ${new Date().getFullYear()} Grovia · Early access</div></section>
  <section class="auth-form"><form class="auth-box" id="authForm"><div class="eyebrow">GROVIA</div>
  <h2>${reg ? 'Create your workspace' : 'Welcome back'}</h2><p>${reg ? 'Set up your business account in less than a minute.' : 'Sign in to continue managing your business.'}</p>
  ${reg ? `<div class="field"><label>Business name</label><input name="business_name" required minlength="2" placeholder="e.g. Umoja Mini Mart"></div>
  <div class="field"><label>Your name</label><input name="name" required minlength="2" placeholder="Full name" autocomplete="name"></div>
  <div class="field"><label>Business phone (optional)</label><input name="phone" placeholder="07XX XXX XXX" autocomplete="tel"></div>` : ''}
  <div class="field"><label>Email address</label><input name="email" type="email" required placeholder="you@example.com" autocomplete="email"></div>
  <div class="field"><label>Password</label><input name="password" type="password" required minlength="${reg ? 10 : 1}" autocomplete="${reg ? 'new-password' : 'current-password'}" placeholder="${reg ? 'At least 10 characters' : 'Your password'}"></div>
  ${reg ? '<p style="font-size:10px;color:#77857c">Use a unique password. Your data is isolated from other businesses.</p>' : '<p style="font-size:10px;color:#77857c">Staff accounts are created by the shop owner under Staff.</p>'}
  <button class="btn" style="width:100%;padding:13px" type="submit">${reg ? 'Create business account' : 'Sign in'} →</button>
  <div class="auth-toggle">${reg ? 'Already have an account?' : 'New to Grovia?'} <button class="link" type="button" id="switchAuth">${reg ? 'Sign in' : 'Create an account'}</button></div>
  ${reg ? '' : '<div class="auth-toggle" style="margin-top:6px"><button class="link" type="button" id="forgotLink">Forgot password?</button></div>'}
  <div id="authError" role="alert" style="color:#c74646;font-size:11px;margin-top:12px"></div></form></section></div>`;
  $('#switchAuth').onclick = () => authView(reg ? 'login' : 'register');
  if ($('#forgotLink')) $('#forgotLink').onclick = () => forgotView($('#authForm [name=email]').value);
  demoBanner();
  $('#authForm').onsubmit = async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]'); btn.disabled = true;
    try {
      const d = await post('/auth/' + (reg ? 'register' : 'login'), Object.fromEntries(new FormData(e.target)));
      state.me = d.user; state.business = d.business; state.page = d.user.role === 'cashier' ? 'pos' : 'dashboard';
      store.set('duka_session_hint', d);
      await loadApp();
    } catch (err) { const box = $('#authError'); box.style.color = '#c74646'; box.dataset.done = '1'; box.textContent = err.message; btn.disabled = false; }
  };
}

// First sign-in with a temporary password (set by the provider or the shop owner): choose your own before going further
function firstPasswordView() {
  $('#root').innerHTML = `<div class="auth"><section class="auth-art"><div class="brand"><div class="brand-mark">G</div><div><b>GROVIA</b><small>BUSINESS, MADE CLEAR.</small></div></div>
    <div><div class="eyebrow" style="color:var(--highlight)">KARIBU</div><h1>Welcome to ${esc(state.business.name)}.</h1><p>One quick step: replace the temporary password you were given with your own. Only you will know it.</p></div><div></div></section>
    <section class="auth-form"><form class="auth-box" id="firstPw"><div class="eyebrow">GROVIA</div><h2>Choose your password</h2>
    <p>Signed in as <b>${esc(state.me.email)}</b>.</p>
    <div class="field"><label>Temporary password</label><input name="current_password" type="password" required autocomplete="current-password"></div>
    <div class="field"><label>New password</label><input name="new_password" type="password" required minlength="10" autocomplete="new-password" placeholder="At least 10 characters"></div>
    <div class="field"><label>Repeat new password</label><input name="repeat" type="password" required minlength="10" autocomplete="new-password"></div>
    <button class="btn" style="width:100%;padding:13px" type="submit">Save and continue →</button>
    <div class="auth-toggle"><button class="link" type="button" onclick="logout()">Sign out</button></div>
    <div id="authError" role="alert" style="color:#c74646;font-size:11px;margin-top:12px"></div></form></section></div>`;
  $('#firstPw').onsubmit = async e => {
    e.preventDefault(); const x = Object.fromEntries(new FormData(e.target)), btn = e.target.querySelector('button[type=submit]');
    if (x.new_password !== x.repeat) { $('#authError').textContent = 'The new passwords do not match'; return; }
    btn.disabled = true;
    try {
      await post('/auth/password', { current_password: x.current_password, new_password: x.new_password });
      const d = await api('/auth/me'); state.me = d.user; store.set('duka_session_hint', d);
      await loadApp(); toast('Password saved — welcome!');
    } catch (err) { $('#authError').textContent = err.message; btn.disabled = false; }
  };
}

function forgotView(email = '') {
  const box = $('.auth-form');
  box.innerHTML = `<form class="auth-box" id="forgotForm"><div class="eyebrow">GROVIA</div><h2>Reset your password</h2>
    <p>We'll send a 6-digit code to the phone number on your account.</p>
    <div class="field"><label>Email address</label><input name="email" type="email" required value="${esc(email)}" autocomplete="email"></div>
    <button class="btn" style="width:100%;padding:13px" type="submit">Send code →</button>
    <div id="forgotMsg" role="status" class="muted" style="font-size:12px;margin-top:12px"></div>
    <div id="resetStep" style="display:none;margin-top:12px">
      <div class="field"><label>6-digit code</label><input name="code" inputmode="numeric" maxlength="6" pattern="\\d{6}" autocomplete="one-time-code"></div>
      <div class="field"><label>New password</label><input name="new_password" type="password" minlength="10" autocomplete="new-password" placeholder="At least 10 characters"></div>
      <button class="btn" style="width:100%;padding:13px" type="button" id="resetBtn">Set new password</button></div>
    <div class="auth-toggle"><button class="link" type="button" onclick="authView()">← Back to sign in</button></div></form>`;
  const f = $('#forgotForm');
  f.onsubmit = async e => {
    e.preventDefault(); const btn = f.querySelector('button[type=submit]'); btn.disabled = true;
    try { const r = await post('/auth/forgot', { email: f.email.value }); $('#forgotMsg').textContent = r.message; $('#resetStep').style.display = ''; }
    catch (err) { $('#forgotMsg').textContent = err.message; }
    finally { btn.disabled = false; }
  };
  $('#resetBtn').onclick = async () => {
    try {
      const r = await post('/auth/reset', { email: f.email.value, code: f.code.value.trim(), new_password: f.new_password.value });
      authView(); const m = $('#authError'); m.style.color = 'var(--accent)'; m.dataset.done = '1'; m.textContent = r.message;
    } catch (err) { $('#forgotMsg').textContent = err.message; }
  };
}

async function demoBanner() {
  // On demo deployments, tell people when the sample shop is still being prepared (it takes a minute or two on small servers)
  let h; try { h = await (await fetch('/health')).json(); } catch { return; }
  const box = $('#authError'); if (!box || !h.demo || h.demo === 'off') return;
  if (h.demo === 'seeding') {
    box.style.color = '#7a4c08';
    box.textContent = '⏳ The demo shop is being prepared (1–3 minutes on the free server). Demo login: demo@duka.app / demo-password-123';
    setTimeout(() => { if ($('#authForm')) demoBanner(); }, 8000);
  } else if (h.demo === 'ready' && !box.dataset.done) {
    box.style.color = 'var(--accent)'; box.dataset.done = '1';
    box.textContent = '✓ Demo ready — sign in with demo@duka.app / demo-password-123';
  }
}

// ------------------------------------------------------------------ shell & navigation
const NAV = [
  ['dashboard', '▦', 'Overview', 'all'],
  ['pos', '▣', 'Point of sale', 'all'],
  ['products', '▤', 'Products & stock', 'all'],
  ['sales', '↗', 'Sales history', 'all'],
  ['customers', '♙', 'Customers & credit', 'all'],
  ['expenses', '◈', 'Expenses', 'manager'],
  ['suppliers', '⇄', 'Suppliers', 'manager'],
  ['reports', '▥', 'Reports', 'manager'],
  ['assistant', '✳', 'Ask Grovia', 'all'],
  ['messaging', '✉', 'Messaging', 'manager'],
  ['staff', '☺', 'Staff', 'owner'],
  ['settings', '⚙', 'Settings', 'all'],
  ['admin', '⛭', 'Admin · all shops', 'platform'],
];
const allowed = ([, , , who]) => who === 'all' || (who === 'manager' && isManager()) || (who === 'owner' && isOwner()) || (who === 'platform' && !!state.me?.platform_admin);

function appShell() {
  const initials = state.me.name.split(' ').map(x => x[0]).slice(0, 2).join('').toUpperCase();
  $('#root').innerHTML = `<div class="shell"><aside class="sidebar" id="sidebar"><div class="brand"><div class="brand-mark">G</div><div><b>GROVIA</b><small>BUSINESS, MADE CLEAR.</small></div></div>
  <div class="nav-label">Workspace</div><nav class="nav">${NAV.filter(allowed).map(([id, ico, label]) => `<button data-page="${id}"><span class="ico">${ico}</span>${label}</button>`).join('')}</nav>
  <div class="side-bottom"><strong>${esc(state.business.name)}</strong><span>${esc(state.me.name)} · <span class="role-tag">${esc(state.me.role)}</span></span>
  <div style="margin-top:15px"><button style="background:none;border:0;color:#b7c8bd;padding:0;font-size:12px" onclick="logout()">↪ Sign out</button></div></div></aside>
  <main class="main"><header class="topbar"><div style="display:flex;align-items:center"><button class="mobile-menu" aria-label="Menu" onclick="$('#sidebar').classList.toggle('open')">☰</button><span class="crumb" id="crumb"></span></div>
  <div class="top-right">${isManager() ? `<button class="bell" aria-label="Alerts" onclick="openInbox()">🔔<span id="bellCount" style="display:none"></span></button>` : ''}<button class="btn small" onclick="go('pos')">＋ New sale</button><div class="avatar" title="${esc(state.me.name)}">${esc(initials)}</div></div></header>
  <div id="offlineBar"></div><div class="content" id="pageContent"></div></main></div>`;
  $$('[data-page]').forEach(b => b.onclick = () => go(b.dataset.page));
  renderOfflineBar();
  if (typeof refreshBell === 'function') refreshBell();
}

async function loadApp() {
  if (state.me?.must_change_password) return firstPasswordView();
  appShell();
  if (!location.hash) location.hash = state.page;
  await loadData();
  routeFromHash();
  syncQueue();
}

async function loadData() {
  try {
    const [d, p, c, s] = await Promise.all([api('/dashboard'), api('/products'), api('/customers'), isManager() ? api('/suppliers') : Promise.resolve([])]);
    Object.assign(state, { dash: d, products: p, customers: c, suppliers: s });
    store.set(`duka_cache_${state.business.id}`, { products: p, customers: c });
  } catch (e) {
    if (e instanceof NetworkError) {
      const cached = store.get(`duka_cache_${state.business.id}`, null);
      if (cached) Object.assign(state, cached);
      toast('Offline — showing saved product list');
    } else toast(e.message);
  }
}

function go(page) { if (location.hash.slice(1) === page) renderPage(); else location.hash = page; }
function routeFromHash() {
  const page = location.hash.slice(1) || 'dashboard';
  const item = NAV.find(n => n[0] === page);
  state.page = item && allowed(item) ? page : (isManager() ? 'dashboard' : 'pos');
  renderPage();
}
window.addEventListener('hashchange', () => { if (state.me) routeFromHash(); });

function pageHead(title, sub, actions = '') {
  return `<div class="welcome"><div><div class="eyebrow">${esc(state.business.name)}</div><h1>${title}</h1><div class="sub">${sub}</div></div><div style="display:flex;gap:8px;flex-wrap:wrap">${actions}</div></div>`;
}
function stat(label, value, icon, foot) {
  return `<div class="card stat"><div class="stat-top"><span>${label}</span><span class="stat-icon">${icon}</span></div><div class="stat-value">${value}</div><div class="stat-foot">${foot}</div></div>`;
}
const PAGES = {};
function renderPage() {
  const p = state.page;
  $$('[data-page]').forEach(b => b.classList.toggle('active', b.dataset.page === p));
  $('#crumb').textContent = NAV.find(n => n[0] === p)?.[2] || p;
  $('#sidebar').classList.remove('open');
  const page = PAGES[p] || PAGES.dashboard;
  $('#pageContent').innerHTML = page.render();
  page.after?.();
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------------ charts (single series, one hue)
function barChart(points, { width = 640, height = 170, fmt = money, label = d => d.label } = {}) {
  if (!points.length) return '<div class="empty">No data for this period.</div>';
  const W = width, H = height, padL = 44, padB = 22, padT = 8;
  const max = Math.max(...points.map(p => p.value), 1);
  const nice = (() => { const e = Math.pow(10, Math.floor(Math.log10(max))); return Math.ceil(max / e) * e; })();
  const slot = (W - padL) / points.length, bw = Math.max(2, Math.min(34, slot - 2));
  const y = v => padT + (H - padB - padT) * (1 - v / nice);
  const grid = [0, 0.5, 1].map(t => `<line x1="${padL}" x2="${W}" y1="${y(nice * t)}" y2="${y(nice * t)}" style="stroke:var(--line)"/><text x="${padL - 6}" y="${y(nice * t) + 3}" text-anchor="end" font-size="10" fill="#849188">${short(nice * t)}</text>`).join('');
  const every = Math.ceil(points.length / 10);
  const bars = points.map((p, i) => {
    const x = padL + i * slot + (slot - bw) / 2, top = y(p.value), h = Math.max(0, H - padB - top);
    const r = Math.min(4, bw / 2, h);
    const path = h > 0 ? `M${x},${H - padB}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${H - padB}Z` : '';
    const tip = esc(`${label(p)} · ${fmt(p.value)}${p.extra ? ' · ' + p.extra : ''}`);
    return `<g data-tip="${tip}"><rect x="${padL + i * slot}" y="${padT}" width="${slot}" height="${H - padB - padT}" fill="transparent"/>${path ? `<path d="${path}" style="fill:${p.highlight ? 'var(--accent)' : 'var(--chart)'}"/>` : ''}
      ${i % every === 0 ? `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle" font-size="10" fill="#849188">${esc(p.axis ?? label(p))}</text>` : ''}</g>`;
  }).join('');
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Bar chart">${grid}${bars}</svg><div class="tip"></div></div>`;
}
document.addEventListener('mousemove', e => {
  const g = e.target.closest?.('[data-tip]'), chart = e.target.closest?.('.chart');
  $$('.chart .tip').forEach(t => { if (!chart || !chart.contains(t)) t.style.display = 'none'; });
  if (!g || !chart) return;
  const tip = $('.tip', chart), box = chart.getBoundingClientRect();
  tip.textContent = g.dataset.tip; tip.style.display = 'block';
  tip.style.left = (e.clientX - box.left) + 'px'; tip.style.top = (e.clientY - box.top) + 'px';
});
const dailyPoints = days => days.map(d => {
  const dt = new Date(d.date + 'T00:00:00');
  return { value: d.revenue, label: dt.toLocaleDateString('en-KE', { weekday: 'short', day: 'numeric', month: 'short' }),
    axis: days.length <= 8 ? dt.toLocaleDateString('en-KE', { weekday: 'short' }) : dt.getDate() + '', extra: `${d.sales} sale(s)` };
});
function hbars(rows, total) {
  const max = Math.max(...rows.map(r => r.value), 1);
  return rows.map(r => `<div class="hbar"><span>${esc(r.label)}</span><div class="bar"><span style="width:${(r.value / max * 100).toFixed(1)}%"></span></div><b>${money(r.value)}${total ? ` <span class="muted" style="font-weight:400">${Math.round(r.value / total * 100)}%</span>` : ''}</b></div>`).join('');
}

// ------------------------------------------------------------------ dashboard
const ICONS = { critical: '!', warning: '△', good: '✓', info: 'i' };
function insightList(items) {
  return items.map(i => `<div class="insight ${i.level}"><span class="dot" aria-label="${i.level}">${ICONS[i.level]}</span><div><b>${esc(i.title)}</b><p>${esc(i.detail)}</p>
    ${i.action && i.page ? `<button class="link" onclick="go('${i.page}')">${esc(i.action)} →</button>` : ''}</div></div>`).join('');
}
PAGES.dashboard = {
  render() {
    const d = state.dash || {};
    const hour = new Date().getHours();
    const greet = hour < 12 ? 'Habari ya asubuhi' : hour < 17 ? 'Habari ya mchana' : 'Habari ya jioni';
    const week = (d.last_7_days || []);
    const pts = dailyPoints(week); if (pts.length) pts[pts.length - 1].highlight = true;
    return pageHead(`${greet}, ${esc(state.me.name.split(' ')[0])}.`, `Here is what's happening in ${esc(state.business.name)} today.`) +
    `<div class="cards">
      ${stat("Today's sales", money(d.today_revenue), '↗', `${d.today_sales || 0} sale(s) · ${delta(d.revenue_change)}`)}
      ${isManager() ? stat('Gross profit today', money(d.today_gross_profit), '◈', `Expenses today ${money(d.today_expenses)}`) : stat('Cash collected today', money(d.today_collected), '◈', 'Incl. debt repayments')}
      ${isManager() ? stat('Cash collected today', money(d.today_collected), '◎', 'Incl. debt repayments') : stat('Products', d.product_count || 0, '▤', 'Active in catalogue')}
      ${stat('Owed by customers', money(d.outstanding_balance), '♙', `<button class="link" style="padding:0;font-size:10px" onclick="go('customers')">See who owes →</button>`)}
    </div>
    <div class="grid-main">
      <section class="panel"><div class="panel-head"><div><h3>Last 7 days</h3><p>Daily sales revenue · today highlighted</p></div><button class="btn light small" onclick="go('${isManager() ? 'reports' : 'sales'}')">Details</button></div>${barChart(pts)}</section>
      ${isManager() ? `<section class="panel"><div class="panel-head"><div><h3>Grovia noticed</h3><p>Things that need your attention</p></div><button class="btn light small" onclick="go('assistant')">Ask Grovia</button></div>${insightList(d.insights || [])}</section>`
        : `<section class="panel"><div class="panel-head"><div><h3>Stock attention</h3><p>At or below reorder level</p></div></div>${lowStockList(d.low_stock)}</section>`}
    </div>
    <div class="grid-main">
      <section class="panel"><div class="panel-head"><div><h3>Recent transactions</h3><p>Tap a sale to view or reprint the receipt</p></div><button class="btn light small" onclick="go('sales')">View all</button></div>
      ${(d.recent_sales || []).length ? `<div class="table-wrap"><table><thead><tr><th>REFERENCE</th><th>TIME</th><th>PAYMENT</th><th>AMOUNT</th></tr></thead><tbody>
      ${d.recent_sales.map(s => `<tr class="clickable" onclick="showSale(${s.id})"><td><b>${esc(s.reference)}</b></td><td>${fmtDateTime(s.created_at)}</td><td>${payBadge(s)}</td><td><b>${money(s.total)}</b></td></tr>`).join('')}</tbody></table></div>`
        : '<div class="empty">No sales yet. Record your first sale to see activity here.</div>'}</section>
      ${isManager() ? `<section class="panel"><div class="panel-head"><div><h3>Stock attention</h3><p>At or below reorder level</p></div><button class="btn light small" onclick="go('products')">Restock plan</button></div>${lowStockList(d.low_stock)}</section>` : ''}
    </div>`;
  },
};
function lowStockList(items = []) {
  return items.length ? items.map(x => `<div class="low-row"><div class="row-flex"><div class="product-dot">▤</div><div><b>${esc(x.name)}</b><div class="muted" style="font-size:10px">Reorder at ${x.reorder_level}</div></div></div><span class="badge ${x.stock_qty <= 0 ? 'red' : 'warn'}">${x.stock_qty <= 0 ? 'Out of stock' : x.stock_qty + ' left'}</span></div>`).join('')
    : '<div class="empty">All products are above their reorder levels.</div>';
}
function payBadge(s) {
  const map = { paid: ['', 'Paid'], part_paid: ['warn', 'Part paid'], unpaid: ['warn', 'Unpaid'], voided: ['red', 'Voided'] };
  const [cls, label] = map[s.payment_status] || ['', s.payment_status];
  return `<span class="badge ${cls}">${label}</span> <span class="muted" style="font-size:10px">${METHODS[s.method] || esc(s.method)}</span>`;
}

// ------------------------------------------------------------------ point of sale
let posCategory = '';
PAGES.pos = {
  render() {
    const cats = [...new Set(state.products.map(p => p.category))].sort();
    return pageHead('Point of sale', 'Scan or tap products, take payment, share the receipt.', '<span id="shiftBox"></span>') +
    `<div class="grid-main"><section class="panel"><div class="scan">
      <input class="search" id="scanInput" placeholder="Scan barcode or type product name / SKU, then Enter" autocomplete="off" aria-label="Scan or search products">
    </div>
    ${cats.length > 1 ? `<div class="chips" style="margin-bottom:12px"><button class="chip ${posCategory ? '' : 'active'}" onclick="posCategory='';filterPOS()">All</button>${cats.map(c => `<button class="chip ${posCategory === c ? 'active' : ''}" data-cat="${esc(c)}" onclick="posCategory=this.dataset.cat;filterPOS()">${esc(c)}</button>`).join('')}</div>` : ''}
    <div id="posProducts" class="pos-grid"></div></section>
    <section class="panel"><div class="panel-head"><div><h3>Current sale</h3><p id="cartCount">0 items</p></div><button class="btn light small" onclick="clearCart()">Clear</button></div>
      <div id="cartLines"></div>
      <div style="border-top:1px solid var(--line);padding-top:14px;margin-top:12px">
        <div class="kv"><span class="muted">Subtotal</span><b id="cartSubtotal">KSh 0.00</b></div>
        <div class="form-grid" style="margin:10px 0">
          <div class="field"><label>Discount (KSh)</label><input type="number" min="0" step="0.01" id="cartDiscount" value="0" oninput="renderCart()"></div>
          <div class="field"><label>Payment</label><select id="cartMethod" onchange="renderCart()">${Object.entries(METHODS).filter(([k]) => k !== 'other').map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
          <div class="field full"><label>Customer <span class="muted">(required for credit)</span></label>
            <div style="display:flex;gap:6px"><select id="cartCustomer" style="flex:1" onchange="renderCart()"><option value="">Walk-in customer</option>${state.customers.map(c => `<option value="${c.id}">${esc(c.name)}${c.balance > 0 ? ` (owes ${money(c.balance)})` : ''}</option>`).join('')}</select>
            <button type="button" class="btn light small" onclick="openCustomer(null, true)" title="Add customer">＋</button></div></div>
          <div class="field full" id="stkBox" style="display:none"><label>Send M-Pesa prompt to customer's phone</label>
            <div style="display:flex;gap:6px"><input id="stkPhone" placeholder="07XX XXX XXX" inputmode="tel" style="flex:1"><button type="button" class="btn" id="stkBtn" onclick="sendPrompt()">Send prompt</button></div>
            <div id="stkStatus" class="muted" style="font-size:11px;margin-top:4px"></div></div>
          <div class="field" id="refField" style="display:none"><label>M-Pesa code</label><input id="cartRef" placeholder="e.g. SKL2AB3CD4" maxlength="20" style="text-transform:uppercase" onchange="typeof verifyCode==='function'&&verifyCode()"><div id="codeStatus" style="font-size:11px"></div></div>
          <div class="field" id="tenderField"><label id="tenderLabel">Cash received</label><input type="number" min="0" step="0.01" id="cartTender" placeholder="Exact amount" oninput="renderCart()"></div>
        </div>
        <div id="changeDue"></div><div id="liveMpesa"></div>
        <div class="row-flex" style="justify-content:space-between;margin-bottom:15px"><b>Total</b><b id="cartTotal" style="font:800 24px Manrope">KSh 0.00</b></div>
        <button class="btn" id="checkoutBtn" style="width:100%;padding:13px;font-size:13px" onclick="checkout()">Complete sale</button>
        <div class="mobile-cart" id="mobileCart" onclick="$('#cartLines').scrollIntoView({behavior:'smooth'})"><span id="mobileCartCount">0 items</span><b id="mobileCartTotal">KSh 0.00</b><span>Checkout ↓</span></div>
        <div class="footer-note">Tip: barcode scanners work out of the box — scan into the box above. Shortcut: <b>F2</b> focuses the scan box.</div>
      </div></section></div>`;
  },
  after() {
    filterPOS(); renderCart();
    const input = $('#scanInput');
    input.focus();
    input.oninput = filterPOS;
    input.onkeydown = async e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const code = input.value.trim(); if (!code) return;
      const exact = state.products.find(p => p.barcode === code || p.sku.toLowerCase() === code.toLowerCase());
      const matches = visibleProducts();
      const p = exact || (matches.length === 1 ? matches[0] : null);
      if (p) { addCart(p.id); input.value = ''; filterPOS(); }
      else toast(matches.length ? 'Several matches — tap the product' : `No product matches “${code}”`);
    };
  },
};
document.addEventListener('keydown', e => { if (e.key === 'F2' && state.page === 'pos') { e.preventDefault(); $('#scanInput')?.focus(); } });
function visibleProducts() {
  const q = ($('#scanInput')?.value || '').toLowerCase().trim();
  return state.products.filter(p => (!posCategory || p.category === posCategory) &&
    (!q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q) || p.barcode === q));
}
function filterPOS() {
  const el = $('#posProducts'); if (!el) return;
  $$('.chip').forEach(c => c.classList.toggle('active', (c.dataset.cat || '') === posCategory));
  const list = visibleProducts();
  el.innerHTML = list.map(p => {
    const inCart = cart.lines.find(l => l.id === p.id)?.qty || 0;
    const left = p.stock_qty - inCart;
    return `<button class="pos-card" onclick="addCart(${p.id})" ${left <= 0 ? 'disabled' : ''}><b style="font-size:12px">${esc(p.name)}</b>
      <div class="muted" style="font-size:10px;margin:5px 0">${left <= 0 ? 'Out of stock' : `${left} ${esc(p.unit || '')} left`}${inCart ? ` · ${inCart} in cart` : ''}</div>
      <strong style="color:var(--green);font-size:12px">${money(p.selling_price)}</strong></button>`;
  }).join('') || `<div class="empty">${state.products.length ? 'No matching products.' : 'Add products before recording sales.'}</div>`;
}
function addCart(id) {
  const p = state.products.find(x => x.id === id), line = cart.lines.find(x => x.id === id);
  if ((line?.qty || 0) + 1 > p.stock_qty) return toast(`Only ${p.stock_qty} ${p.name} in stock`);
  if (line) line.qty++; else cart.lines.push({ id, qty: 1 });
  renderCart(); filterPOS();
}
function setQty(id, qty) {
  const p = state.products.find(x => x.id === id);
  qty = Math.floor(Number(qty) || 0);
  if (qty > p.stock_qty) { toast(`Only ${p.stock_qty} in stock`); qty = p.stock_qty; }
  if (qty <= 0) cart.lines = cart.lines.filter(l => l.id !== id);
  else cart.lines.find(l => l.id === id).qty = qty;
  renderCart(); filterPOS();
}
function clearCart() { cart = { lines: [], ref: uid() }; ['#stkStatus', '#codeStatus'].forEach(x => { const el = $(x); if (el) el.innerHTML = ''; }); const sp = $('#stkPhone'); if (sp) sp.value = ''; const d = $('#cartDiscount'); if (d) d.value = 0; const t = $('#cartTender'); if (t) t.value = ''; renderCart(); filterPOS(); }
function cartTotals() {
  const sub = cart.lines.reduce((s, l) => s + state.products.find(p => p.id === l.id).selling_price * l.qty, 0);
  const discount = Number($('#cartDiscount')?.value || 0);
  return { sub, discount, total: Math.max(0, Math.round((sub - discount) * 100) / 100) };
}
function renderCart() {
  const el = $('#cartLines'); if (!el) return;
  el.innerHTML = cart.lines.length ? cart.lines.map(l => {
    const p = state.products.find(y => y.id === l.id);
    return `<div class="cart-line"><div><b>${esc(p.name)}</b><div class="muted">${money(p.selling_price)} each</div></div>
      <div class="qty"><button class="btn light small" onclick="setQty(${p.id},${l.qty - 1})" aria-label="Decrease">−</button><input type="number" min="0" value="${l.qty}" onchange="setQty(${p.id},this.value)" aria-label="Quantity"><button class="btn light small" onclick="setQty(${p.id},${l.qty + 1})" aria-label="Increase">＋</button></div>
      <b style="text-align:right">${money(p.selling_price * l.qty)}</b></div>`;
  }).join('') : '<div class="empty">Scan or tap products to begin.</div>';
  const { sub, total } = cartTotals();
  const method = $('#cartMethod').value;
  $('#cartSubtotal').textContent = money(sub);
  $('#cartTotal').textContent = money(total);
  const count = cart.lines.reduce((s, l) => s + l.qty, 0);
  $('#cartCount').textContent = count + ' item(s)';
  $('#mobileCartCount').textContent = count + ' item(s)'; $('#mobileCartTotal').textContent = money(total);
  $('#mobileCart').classList.toggle('show', count > 0);
  $('#refField').style.display = method === 'mpesa' ? '' : 'none';
  $('#stkBox').style.display = method === 'mpesa' && typeof mpesaOn === 'function' && mpesaOn() ? '' : 'none';
  const stkLabel = $('#stkBox label');
  if (stkLabel) stkLabel.textContent = state.business?.mpesa_mode === 'simulator' ? "Send M-Pesa prompt (SIMULATOR — no real phone prompt)" : "Send M-Pesa prompt to customer's phone";
  const cust = state.customers.find(c => c.id === Number($('#cartCustomer').value));
  if (cust?.phone && !$('#stkPhone').value) $('#stkPhone').value = cust.phone;
  $('#tenderLabel').textContent = method === 'cash' ? 'Cash received' : method === 'credit' ? 'Deposit paid now' : 'Amount paid';
  $('#cartTender').placeholder = method === 'credit' ? '0' : 'Exact amount';
  const tender = $('#cartTender').value === '' ? null : Number($('#cartTender').value);
  let html = '';
  if (tender !== null && cart.lines.length) {
    if (method === 'cash' && tender > total) html = `<div class="change-due"><span>Change to give</span><b>${money(tender - total)}</b></div>`;
    else if (tender < total) html = `<div class="change-due warn"><span>Balance on credit</span><b>${money(total - tender)}</b></div>`;
  }
  $('#changeDue').innerHTML = html;
}
async function checkout() {
  if (!cart.lines.length) return toast('Add products first');
  const { sub, discount, total } = cartTotals();
  if (discount > sub) return toast('Discount cannot exceed subtotal');
  const method = $('#cartMethod').value;
  const tenderRaw = $('#cartTender').value;
  let paid = tenderRaw === '' ? (method === 'credit' ? 0 : total) : Math.min(Number(tenderRaw), total);
  const customerId = $('#cartCustomer').value ? Number($('#cartCustomer').value) : null;
  if (paid < total && !customerId) return toast('Choose the customer for a credit or part-paid sale');
  const body = { items: cart.lines.map(l => ({ product_id: l.id, quantity: l.qty })), customer_id: customerId, discount, amount_paid: paid,
    payment_method: method, payment_ref: ($('#cartRef')?.value || '').toUpperCase(), client_ref: cart.ref };
  const btn = $('#checkoutBtn'); btn.disabled = true;
  try {
    const sale = await post('/sales', body);
    const cashGiven = method === 'cash' && tenderRaw !== '' ? Number(tenderRaw) : null;
    clearCart();
    await loadData(); renderPage();
    showReceipt(sale, cashGiven);
  } catch (e) {
    if (e instanceof NetworkError) {
      // keep the sale on this device; decrement local stock so the till stays accurate
      setQueue([...getQueue(), { body, total, at: new Date().toISOString() }]);
      body.items.forEach(i => { const p = state.products.find(x => x.id === i.product_id); if (p) p.stock_qty -= i.quantity; });
      clearCart(); renderPage();
      toast('Offline: sale saved on this device and will sync automatically', 4500);
    } else toast(e.message);
  } finally { btn.disabled = false; }
}

// ------------------------------------------------------------------ receipts
function receiptHTML(s, cashGiven) {
  const b = state.business;
  const row = (l, r, bold) => `<div class="r"${bold ? ' style="font-weight:700"' : ''}><span>${l}</span><span>${r}</span></div>`;
  return `<div class="receipt-paper print-area"><div class="c"><b style="font-size:14px">${esc(b.name)}</b>${b.address ? `<br>${esc(b.address)}` : ''}${b.phone ? `<br>Tel ${esc(b.phone)}` : ''}${b.kra_pin ? `<br>PIN ${esc(b.kra_pin)}` : ''}</div><hr>
    ${row('Receipt', esc(s.reference))}${row('Date', fmtDateTime(s.created_at))}${s.cashier ? row('Served by', esc(s.cashier)) : ''}${s.customer_name ? row('Customer', esc(s.customer_name)) : ''}<hr>
    ${s.items.filter(i => i.quantity > 0).map(i => `<div>${esc(i.name)}</div>${row(`&nbsp;&nbsp;${i.quantity} × ${Number(i.unit_price).toFixed(2)}`, Number(i.line_total).toFixed(2))}`).join('')}<hr>
    ${row('Subtotal', money(s.subtotal))}${s.discount ? row('Discount', '− ' + money(s.discount)) : ''}${row('TOTAL', money(s.total), true)}
    ${s.payments.map(p => p.amount < 0 ? row(`Refund (${METHODS[p.method] || esc(p.method)})`, '− ' + money(-p.amount)) : row(`Paid (${METHODS[p.method] || esc(p.method)}${p.ref ? ' ' + esc(p.ref) : ''})`, money(p.amount))).join('')}
    ${cashGiven ? row('Cash given', money(cashGiven)) + row('Change', money(cashGiven - s.paid)) : ''}
    ${(s.returns || []).length ? `<hr><div><b>Returned</b></div>${s.returns.map(r => `<div class="muted" style="font-size:10px">${fmtDateTime(r.created_at)} · ${r.quantity || ''} ${esc(r.note.replace(/^Return [^:]+: /, ''))}${r.restocked ? '' : ' (not restocked)'}</div>`).join('')}` : ''}
    ${s.balance > 0 ? row('BALANCE DUE', money(s.balance), true) : ''}${s.status === 'voided' ? `<hr><div class="c"><b>*** VOIDED ***</b><br>${esc(s.void_reason || '')}</div>` : ''}
    <hr><div class="c">${esc(b.receipt_footer || 'Asante! Karibu tena.')}</div><div class="c muted" style="font-size:10px;margin-top:6px">Not a tax invoice</div></div>`;
}
function receiptText(s) {
  const b = state.business;
  const lines = [`*${b.name}*`, `Receipt ${s.reference} · ${fmtDateTime(s.created_at)}`, '',
    ...s.items.filter(i => i.quantity > 0).map(i => `${i.quantity} × ${i.name} — ${money(i.line_total)}`), '',
    ...(s.discount ? [`Discount: −${money(s.discount)}`] : []), `*Total: ${money(s.total)}*`, `Paid: ${money(s.paid)}`,
    ...(s.balance > 0 ? [`*Balance due: ${money(s.balance)}*`] : []), '', b.receipt_footer || 'Asante! Karibu tena.'];
  return lines.join('\n');
}
function waLink(phone, text) {
  let p = String(phone || '').replace(/[^\d]/g, '');
  if (p.startsWith('0')) p = '254' + p.slice(1);
  if (p.length === 9 && /^[17]/.test(p)) p = '254' + p;
  return `https://wa.me/${p}?text=${encodeURIComponent(text)}`;
}
function showReceipt(s, cashGiven = null) {
  const phone = state.customers.find(c => c.id === s.customer_id)?.phone || '';
  window._lastSale = s;
  openModal(`<h2>${s.status === 'voided' ? 'Voided sale' : s.duplicate ? 'Sale already recorded' : 'Sale complete ✓'}</h2><p>${esc(s.reference)} · ${money(s.total)}</p>
    ${receiptHTML(s, cashGiven)}
    <div class="modal-actions no-print" style="flex-wrap:wrap">
      <button class="btn light" onclick="sendReceiptModal(${s.id},'${esc(phone)}')">Send by SMS / WhatsApp</button>
      <a class="btn light" target="_blank" rel="noopener" href="${waLink(phone, receiptText(s))}">Share via my WhatsApp</a>
      <button class="btn light" onclick="window.print()">Print</button>
      ${s.balance > 0 && s.status !== 'voided' ? `<button class="btn light" onclick="recordPayment(${s.id},${s.balance})">Add payment</button>` : ''}
      ${isManager() && s.status !== 'voided' && s.items.some(i => i.quantity > 0) ? `<button class="btn light" onclick="returnItems(${s.id})">Return items</button>` : ''}
      ${isManager() && s.status !== 'voided' ? `<button class="btn danger" onclick="voidSale(${s.id})">Void</button>` : ''}
      <button class="btn" onclick="closeModal();${state.page === 'pos' ? "$('#scanInput')?.focus()" : ''}">${state.page === 'pos' ? 'Next sale' : 'Close'}</button></div>`);
}
async function showSale(id) { try { showReceipt(await api('/sales/' + id)); } catch (e) { toast(e.message); } }

function recordPayment(id, balance) {
  openModal(`<h2>Record payment</h2><p>Outstanding balance: <b>${money(balance)}</b></p><form id="paymentForm"><div class="form-grid">
    <div class="field"><label>Amount received (KSh)</label><input name="amount" type="number" min="0.01" max="${balance}" step="0.01" value="${balance}" required></div>
    <div class="field"><label>Method</label><select name="payment_method">${['cash', 'mpesa', 'bank', 'card', 'other'].map(m => `<option value="${m}">${METHODS[m]}</option>`).join('')}</select></div>
    <div class="field full"><label>Reference (e.g. M-Pesa code)</label><input name="payment_ref" maxlength="60"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save payment</button></div></form>`);
  onSubmit('paymentForm', async x => {
    const s = await post(`/sales/${id}/payment`, { ...x, amount: Number(x.amount), payment_ref: (x.payment_ref || '').toUpperCase() });
    await loadData(); renderPage(); toast('Payment recorded'); showReceipt(s);
  });
}
function returnItems(id) {
  const s = window._lastSale; if (!s || s.id !== id) return showSale(id);
  const lines = s.items.filter(i => i.quantity > 0);
  openModal(`<h2>Return items</h2><p>${esc(s.reference)} · choose how many of each item came back. The sale and reports are reduced, and any money already paid above the new total is refunded.</p>
    <form id="returnForm">${lines.map(i => `<div class="kv"><span>${esc(i.name)} <span class="muted">(${i.quantity} sold · ${money(i.unit_price)} each)</span></span>
      <input name="q_${i.product_id}" type="number" min="0" max="${i.quantity}" value="0" style="width:80px"></div>`).join('')}
    <div class="form-grid" style="margin-top:12px"><div class="field full"><label>Reason</label><input name="reason" required minlength="3" placeholder="e.g. Customer changed mind / expired"></div>
    <div class="field"><label>Refund by</label><select name="refund_method">${['cash', 'mpesa', 'bank', 'card', 'other'].map(m => `<option value="${m}">${METHODS[m]}</option>`).join('')}</select></div>
    <label class="toggle"><input type="checkbox" name="restock" checked> <span><b>Put back in stock</b><br><span class="muted">Untick for damaged or expired goods</span></span></label></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="showSale(${id})">Back</button><button class="btn" type="submit">Record return</button></div></form>`);
  onSubmit('returnForm', async (x, form) => {
    const items = lines.map(i => ({ product_id: i.product_id, quantity: Math.floor(Number(x['q_' + i.product_id] || 0)) })).filter(i => i.quantity > 0);
    if (!items.length) return toast('Enter how many items came back');
    const r = await post(`/sales/${id}/return`, { items, reason: x.reason, refund_method: x.refund_method, restock: form.querySelector('[name=restock]').checked });
    await loadData(); renderPage(); showReceipt(r);
    toast(r.refund > 0 ? `Return recorded. Give the customer ${money(r.refund)} back (${METHODS[x.refund_method]}).` : 'Return recorded — the balance owed went down', 6000);
  });
}
function voidSale(id) {
  openModal(`<h2>Void this sale?</h2><p>Items go back into stock and the sale is removed from reports. Refund any money taken to the customer. This is recorded in the audit log.</p>
    <form id="voidForm"><div class="field"><label>Reason</label><input name="reason" required minlength="3" placeholder="e.g. Wrong item rung up"></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Keep sale</button><button class="btn danger" type="submit">Void sale</button></div></form>`);
  onSubmit('voidForm', async x => {
    const s = await post(`/sales/${id}/void`, x);
    await loadData(); renderPage();
    toast(s.refund_due > 0 ? `Voided. Refund ${money(s.refund_due)} to the customer.` : 'Sale voided', 5000);
    closeModal();
  });
}

// ------------------------------------------------------------------ products
let productQuery = '', inventorySummary = null;
PAGES.products = {
  render() {
    const actions = isManager() ? `<a class="btn light" href="/api/export/products.csv">Export CSV</a><button class="btn" onclick="openProduct()">＋ Add product</button>` : '';
    return pageHead('Products & inventory', 'Catalogue, prices, stock levels and what to reorder.', actions) +
      (isManager() ? `<div id="invSummary"></div>` : '') +
      `<section class="panel"><div class="toolbar"><div class="toolbar-left"><input class="search" id="productSearch" placeholder="Search name, SKU or barcode…" value="${esc(productQuery)}" oninput="productQuery=this.value;filterProducts()"><span class="muted" style="font-size:11px">${state.products.length} products</span></div></div>
      <div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>SKU</th>${isManager() ? '<th>COST</th>' : ''}<th>PRICE</th><th>STOCK</th>${isManager() ? '<th>MARGIN</th>' : ''}<th>STATUS</th><th></th></tr></thead><tbody id="productRows"></tbody></table></div></section>`;
  },
  async after() {
    filterProducts();
    if (!isManager()) return;
    try { inventorySummary = await api('/products/summary'); } catch (e) { return toast(e.message); }
    const s = inventorySummary, el = $('#invSummary'); if (!el) return;
    el.innerHTML = `<div class="cards">${stat('Stock value at cost', money(s.cost_value), '▤', `${s.units.toLocaleString()} units on hand`)}${stat('Stock value at price', money(s.retail_value), '↗', `${money(s.potential_profit)} potential profit`)}
      ${stat('Need restocking', s.restock.length, '△', s.restock.length ? `≈ ${money(s.restock.reduce((a, r) => a + r.estimated_cost, 0))} to restock` : 'Nothing urgent')}${stat('Slow movers', s.dead_stock.length, '◌', s.dead_stock.length ? `${money(s.dead_stock.reduce((a, r) => a + r.capital_tied, 0))} tied up` : 'All stock is moving')}</div>
      ${s.restock.length ? `<section class="panel"><div class="panel-head"><div><h3>Smart restock plan</h3><p>Based on the last 4 weeks of sales — enough to cover about 2 weeks</p></div><span style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn light small" onclick="shareRestock()">Send to supplier (WhatsApp)</button><button class="btn small" onclick="go('suppliers');setTimeout(()=>newPO(true),300)">Create purchase order</button></span></div>
      <div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>IN STOCK</th><th>SELLS / DAY</th><th>RUNS OUT IN</th><th>SUGGESTED ORDER</th><th>EST. COST</th><th></th></tr></thead><tbody>
      ${s.restock.map(r => `<tr><td><b>${esc(r.name)}</b></td><td>${r.stock_qty}</td><td>${r.daily_sales || '—'}</td><td>${r.stock_qty <= 0 ? '<span class="badge red">Out now</span>' : r.days_left !== null ? `<span class="badge ${r.days_left <= 3 ? 'red' : 'warn'}">~${Math.round(r.days_left)} days</span>` : '—'}</td><td><b>${r.suggested_order}</b></td><td>${money(r.estimated_cost)}</td><td><button class="btn light small" onclick="openStock(${r.product_id},${r.suggested_order})">Receive</button></td></tr>`).join('')}</tbody></table></div></section>` : ''}`;
  },
};
function shareRestock() {
  const lines = inventorySummary.restock.filter(r => r.suggested_order > 0).map(r => `• ${r.name} — ${r.suggested_order}`);
  window.open(waLink('', `Habari, ${state.business.name} would like to order:\n${lines.join('\n')}\n\nAsante.`), '_blank', 'noopener');
}
function filterProducts() {
  const q = productQuery.toLowerCase(), el = $('#productRows'); if (!el) return;
  const list = state.products.filter(p => p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q) || p.barcode === productQuery || p.category.toLowerCase().includes(q));
  const cols = isManager() ? 8 : 6;
  el.innerHTML = list.length ? list.map(p => `<tr><td><b>${esc(p.name)}</b><div class="muted">${esc(p.category)}</div></td><td>${esc(p.sku || '—')}</td>
    ${isManager() ? `<td>${money(p.cost_price)}</td>` : ''}<td>${money(p.selling_price)}</td><td><b>${p.stock_qty}</b> <span class="muted">${esc(p.unit)}</span></td>
    ${isManager() ? `<td><span class="${p.margin_pct < 0 ? 'delta down' : ''}">${p.margin_pct}%</span></td>` : ''}
    <td><span class="badge ${p.stock_qty <= 0 ? 'red' : p.low_stock ? 'warn' : ''}">${p.stock_qty <= 0 ? 'Out' : p.low_stock ? 'Reorder' : 'In stock'}</span></td>
    <td style="text-align:right">${isManager() ? `<button class="btn light small" onclick="openStock(${p.id})">＋ Stock</button> <button class="btn light small" onclick="productMenu(${p.id})">⋯</button>` : ''}</td></tr>`).join('')
    : `<tr><td colspan="${cols}" class="empty">${state.products.length ? 'No matches.' : 'No products yet. Add your first product.'}</td></tr>`;
}
function productMenu(id) {
  const p = state.products.find(x => x.id === id);
  openModal(`<h2>${esc(p.name)}</h2><p>${p.stock_qty} ${esc(p.unit)} in stock · ${money(p.selling_price)}</p>
    <div style="display:grid;gap:8px"><button class="btn light" onclick="openProduct(${id})">Edit details & prices</button><button class="btn light" onclick="openStock(${id})">Receive stock</button>
    <button class="btn light" onclick="openAdjust(${id})">Stock count / write-off</button><button class="btn light" onclick="showMovements(${id})">Stock history</button>
    <button class="btn danger" onclick="archiveProduct(${id})">Archive product</button></div>
    <div class="modal-actions"><button class="btn light" onclick="closeModal()">Close</button></div>`);
}
function openProduct(id) {
  const p = id ? state.products.find(x => x.id === id) : { category: 'General', unit: 'pcs', reorder_level: 5, stock_qty: 0 };
  const cats = [...new Set(state.products.map(x => x.category))];
  openModal(`<h2>${id ? 'Edit product' : 'Add a product'}</h2><p>${id ? 'Stock is changed through “Receive stock” or “Stock count” so every change is recorded.' : 'Accurate cost and selling prices power your margin and profit reports.'}</p>
  <form id="productForm"><div class="form-grid">
    <div class="field full"><label>Product name</label><input name="name" required value="${esc(p.name || '')}" placeholder="e.g. Maize flour 2kg"></div>
    <div class="field"><label>Category</label><input name="category" list="catList" value="${esc(p.category)}"><datalist id="catList">${cats.map(c => `<option value="${esc(c)}">`).join('')}</datalist></div>
    <div class="field"><label>Unit</label><select name="unit">${['pcs', 'kg', 'g', 'litre', 'ml', 'pack', 'bale', 'crate', 'dozen', 'metre'].map(u => `<option ${p.unit === u ? 'selected' : ''}>${u}</option>`).join('')}</select></div>
    <div class="field"><label>Cost price (KSh)</label><input name="cost_price" type="number" min="0" step="0.01" required value="${p.cost_price ?? ''}" oninput="marginHint()"></div>
    <div class="field"><label>Selling price (KSh)</label><input name="selling_price" type="number" min="0.01" step="0.01" required value="${p.selling_price ?? ''}" oninput="marginHint()"></div>
    <div class="field full muted" id="marginHint" style="font-size:11px"></div>
    ${id ? '' : `<div class="field"><label>Opening stock</label><input name="stock_qty" type="number" min="0" value="0" required></div>`}
    <div class="field"><label>Reorder level</label><input name="reorder_level" type="number" min="0" value="${p.reorder_level}"></div>
    <div class="field"><label>SKU / code</label><input name="sku" value="${esc(p.sku || '')}"></div>
    <div class="field"><label>Barcode</label><input name="barcode" value="${esc(p.barcode || '')}" placeholder="Scan here"></div>
  </div><div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save product</button></div></form>`);
  marginHint();
  onSubmit('productForm', async x => {
    ['cost_price', 'selling_price', 'stock_qty', 'reorder_level'].forEach(k => { if (k in x) x[k] = Number(x[k] || 0); });
    await post(id ? `/products/${id}` : '/products', x, id ? 'PUT' : 'POST');
    closeModal(); await loadData(); renderPage(); toast(id ? 'Product updated' : 'Product added');
  });
}
function marginHint() {
  const c = Number($('[name=cost_price]')?.value), s = Number($('[name=selling_price]')?.value), el = $('#marginHint');
  if (!el || !c || !s) { if (el) el.textContent = ''; return; }
  const m = (s - c) / s * 100;
  el.innerHTML = m < 0 ? `<span class="delta down">⚠ Selling below cost — you lose ${money(c - s)} per unit</span>` : `Profit ${money(s - c)} per unit · ${m.toFixed(1)}% margin`;
}
function openStock(id, suggested = '') {
  const p = state.products.find(x => x.id === id);
  openModal(`<h2>Receive stock</h2><p>${esc(p.name)} · currently ${p.stock_qty} ${esc(p.unit)} · cost ${money(p.cost_price)}</p><form id="stockForm"><div class="form-grid">
    <div class="field"><label>Quantity received</label><input name="quantity" type="number" min="1" required value="${suggested}"></div>
    <div class="field"><label>Buying price per unit (KSh)</label><input name="unit_cost" type="number" min="0" step="0.01" value="${p.cost_price}"></div>
    <div class="field full"><label>Supplier</label><select name="supplier_id"><option value="">—</option>${state.suppliers.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></div>
    <div class="field full"><label>Note</label><input name="note" value="Stock received" maxlength="240"></div></div>
    <p class="footer-note">If the buying price changed, the product cost is updated to a weighted average so profit stays accurate.</p>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Update stock</button></div></form>`);
  onSubmit('stockForm', async x => {
    await post(`/products/${id}/stock`, { quantity: Number(x.quantity), unit_cost: x.unit_cost === '' ? null : Number(x.unit_cost), supplier_id: x.supplier_id ? Number(x.supplier_id) : null, note: x.note });
    closeModal(); await loadData(); renderPage(); toast('Stock updated');
  });
}
function openAdjust(id) {
  const p = state.products.find(x => x.id === id);
  openModal(`<h2>Stock count / write-off</h2><p>${esc(p.name)} · system says ${p.stock_qty} ${esc(p.unit)}</p><form id="adjForm"><div class="form-grid">
    <div class="field"><label>Reason</label><select name="reason" onchange="$('#adjLabel').textContent=this.value==='count'?'Counted on shelf':'Quantity to remove'"><option value="count">Physical count</option><option value="damaged">Damaged</option><option value="expired">Expired</option><option value="theft">Missing / theft</option><option value="own_use">Own use</option><option value="returned_to_supplier">Returned to supplier</option><option value="other">Other</option></select></div>
    <div class="field"><label id="adjLabel">Counted on shelf</label><input name="qty" type="number" min="0" required></div>
    <div class="field full"><label>Note</label><input name="note" maxlength="200"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save</button></div></form>`);
  onSubmit('adjForm', async x => {
    const body = x.reason === 'count' ? { counted_qty: Number(x.qty) } : { change: -Number(x.qty) };
    const r = await post(`/products/${id}/adjust`, { ...body, reason: x.reason, note: x.note });
    closeModal(); await loadData(); renderPage();
    const diff = r.stock_qty - p.stock_qty;
    toast(diff === 0 ? 'Count matches the system ✓' : `Stock ${diff > 0 ? 'increased' : 'reduced'} by ${Math.abs(diff)} — logged`);
  });
}
async function showMovements(id) {
  const p = state.products.find(x => x.id === id);
  try {
    const rows = await api(`/products/${id}/movements`);
    const label = { opening: 'Opening', purchase: 'Received', sale: 'Sold', void: 'Void return', adjustment: 'Adjustment' };
    openModal(`<h2>Stock history</h2><p>${esc(p.name)} · now ${p.stock_qty}</p><div class="table-wrap"><table><thead><tr><th>DATE</th><th>TYPE</th><th>QTY</th><th>BALANCE</th><th>BY</th><th>NOTE</th></tr></thead><tbody>
    ${rows.map(m => `<tr><td>${fmtDateTime(m.created_at)}</td><td>${label[m.type] || esc(m.type)}</td><td><b class="delta ${m.quantity > 0 ? 'up' : 'down'}">${m.quantity > 0 ? '+' : ''}${m.quantity}</b></td><td>${m.balance_after ?? '—'}</td><td>${esc(m.user || '—')}</td><td>${esc(m.note)}</td></tr>`).join('')}</tbody></table></div>
    <div class="modal-actions"><button class="btn light" onclick="closeModal()">Close</button></div>`);
  } catch (e) { toast(e.message); }
}
async function archiveProduct(id) {
  const p = state.products.find(x => x.id === id);
  openModal(`<h2>Archive ${esc(p.name)}?</h2><p>It disappears from the till and product list. Past sales and reports are kept.</p>
    <div class="modal-actions"><button class="btn light" onclick="closeModal()">Cancel</button><button class="btn danger" id="confirmArchive">Archive</button></div>`);
  $('#confirmArchive').onclick = async () => { try { await api(`/products/${id}`, { method: 'DELETE' }); closeModal(); await loadData(); renderPage(); toast('Product archived'); } catch (e) { toast(e.message); } };
}

// ------------------------------------------------------------------ sales history
PAGES.sales = {
  render() {
    const periods = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['month', 'This month'], ['365d', 'Year']];
    return pageHead('Sales history', 'Every sale, payment and void — tap a row for the receipt.', isManager() ? `<a class="btn light" href="/api/export/sales.csv?period=${salesFilter.period}">Export CSV</a>` : '') +
      `<section class="panel"><div class="toolbar"><div class="chips">${periods.map(([k, v]) => `<button class="chip ${salesFilter.period === k ? 'active' : ''}" onclick="salesFilter.period='${k}';renderPage()">${v}</button>`).join('')}</div>
      <label style="font-size:12px;display:flex;gap:6px;align-items:center"><input type="checkbox" ${salesFilter.unpaid ? 'checked' : ''} onchange="salesFilter.unpaid=this.checked;renderPage()"> Unpaid only</label></div>
      <div id="salesTable"><div class="empty">Loading…</div></div></section>`;
  },
  async after() {
    try {
      const rows = await api(`/sales?${salesFilter.unpaid ? 'unpaid=true' : 'period=' + salesFilter.period}`);
      const live = rows.filter(s => s.status === 'completed');
      const total = live.reduce((a, s) => a + s.total, 0), owed = live.reduce((a, s) => a + s.balance, 0);
      $('#salesTable').innerHTML = `<p class="muted" style="font-size:11px;margin:0 0 10px">${live.length} sale(s) · ${money(total)}${owed ? ` · ${money(owed)} unpaid` : ''}</p>
      <div class="table-wrap"><table><thead><tr><th>REFERENCE</th><th>DATE</th><th>CUSTOMER</th><th>BY</th><th>TOTAL</th><th>BALANCE</th><th>STATUS</th><th></th></tr></thead><tbody>
      ${rows.map(s => `<tr class="clickable" onclick="showSale(${s.id})"><td><b>${esc(s.reference)}</b></td><td>${fmtDateTime(s.created_at)}</td><td>${esc(s.customer_name || 'Walk-in')}</td><td>${esc(s.cashier || '—')}</td>
        <td style="${s.status === 'voided' ? 'text-decoration:line-through' : ''}">${money(s.total)}</td><td>${s.balance > 0 && s.status === 'completed' ? `<b>${money(s.balance)}</b>` : '—'}</td><td>${payBadge(s)}</td>
        <td>${s.balance > 0 && s.status === 'completed' ? `<button class="btn light small" onclick="event.stopPropagation();recordPayment(${s.id},${s.balance})">Add payment</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No sales in this period.</td></tr>'}</tbody></table></div>`;
    } catch (e) { toast(e.message); }
  },
};

// ------------------------------------------------------------------ customers & credit
PAGES.customers = {
  render() {
    const owing = state.customers.filter(c => c.balance > 0).sort((a, b) => b.balance - a.balance);
    const total = owing.reduce((a, c) => a + c.balance, 0);
    return pageHead('Customers & credit', 'Know who owes you, and follow up politely.', `<button class="btn" onclick="openCustomer()">＋ Add customer</button>`) +
      `<div class="cards">${stat('Customers', state.customers.length, '♙', 'Saved contacts')}${stat('Owed to you', money(total), '◈', `${owing.length} customer(s) with a balance`)}</div>
      <section class="panel"><div class="toolbar"><input class="search" placeholder="Search customers…" oninput="filterCustomers(this.value)"></div>
      <div class="table-wrap"><table><thead><tr><th>NAME</th><th>PHONE</th><th>BALANCE</th><th>CREDIT LIMIT</th><th></th></tr></thead><tbody id="custRows"></tbody></table></div></section>`;
  },
  after() { filterCustomers(''); },
};
function filterCustomers(q) {
  q = q.toLowerCase();
  const list = state.customers.filter(c => c.name.toLowerCase().includes(q) || c.phone.includes(q)).sort((a, b) => b.balance - a.balance || a.name.localeCompare(b.name));
  $('#custRows').innerHTML = list.map(c => `<tr class="clickable" onclick="showStatement(${c.id})"><td><b>${esc(c.name)}</b>${c.notes ? `<div class="muted">${esc(c.notes)}</div>` : ''}</td><td>${esc(c.phone || '—')}</td>
    <td>${c.balance > 0 ? `<b style="color:var(--amber)">${money(c.balance)}</b>` : '—'}</td><td>${c.credit_limit ? money(c.credit_limit) : '<span class="muted">No limit</span>'}</td>
    <td style="text-align:right">${c.balance > 0 && c.phone ? `<a class="btn light small" onclick="event.stopPropagation()" target="_blank" rel="noopener" href="${reminderLink(c)}">WhatsApp reminder</a>` : ''}</td></tr>`).join('')
    || '<tr><td colspan="5" class="empty">No customers yet.</td></tr>';
}
function reminderLink(c) {
  return waLink(c.phone, `Habari ${c.name}, hii ni ukumbusho wa kirafiki kutoka ${state.business.name}: salio lako ni ${money(c.balance)}. Unaweza kulipa kwa M-Pesa au dukani. Asante!\n\nHi ${c.name}, a friendly reminder from ${state.business.name}: your balance is ${money(c.balance)}. Thank you!`);
}
function openCustomer(id, fromPOS = false) {
  const c = id ? state.customers.find(x => x.id === id) : {};
  openModal(`<h2>${id ? 'Edit customer' : 'Add customer'}</h2><p>Save contacts to sell on credit and send reminders.</p><form id="custForm"><div class="form-grid">
    <div class="field full"><label>Name</label><input name="name" required value="${esc(c.name || '')}"></div>
    <div class="field"><label>Phone</label><input name="phone" value="${esc(c.phone || '')}" placeholder="07XX XXX XXX"></div>
    ${isManager() ? `<div class="field"><label>Credit limit (0 = none)</label><input name="credit_limit" type="number" min="0" step="1" value="${c.credit_limit || 0}"></div>` : ''}
    <div class="field full"><label>Notes</label><input name="notes" value="${esc(c.notes || '')}" maxlength="240" placeholder="e.g. Pays on Fridays"></div>
    <label class="toggle full"><input type="checkbox" name="marketing_opt_out" ${c.marketing_opt_out ? 'checked' : ''}> <span><b>No promotional messages</b><br><span class="muted">Leave them out of broadcasts (receipts and debt reminders still go)</span></span></label></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save customer</button></div></form>`);
  onSubmit('custForm', async (x, form) => {
    x.credit_limit = Number(x.credit_limit || 0);
    x.marketing_opt_out = form.querySelector('[name=marketing_opt_out]').checked;
    const saved = await post(id ? `/customers/${id}` : '/customers', x, id ? 'PUT' : 'POST');
    closeModal(); await loadData();
    if (fromPOS) { renderPage(); renderCart(); $('#cartCustomer').value = saved.id; }
    else renderPage();
    toast('Customer saved');
  });
}
async function showStatement(id) {
  try {
    const s = await api(`/customers/${id}/statement`), c = s.customer;
    openModal(`<h2>${esc(c.name)}</h2><p>${esc(c.phone || 'No phone')} · ${s.visits} purchase(s) · lifetime ${money(s.lifetime_spend)}${s.last_visit ? ` · last ${fmtDate(s.last_visit)}` : ''}</p>
      <div class="change-due ${c.balance > 0 ? 'warn' : ''}"><span>Balance owed</span><b>${money(c.balance)}</b></div>
      <div class="table-wrap"><table><thead><tr><th>DATE</th><th>REF</th><th>TOTAL</th><th>BALANCE</th><th></th></tr></thead><tbody>
      ${s.sales.map(x => `<tr><td>${fmtDate(x.created_at)}</td><td>${esc(x.reference)}</td><td>${money(x.total)}</td><td>${x.balance > 0 && x.status === 'completed' ? money(x.balance) : payBadge(x)}</td>
        <td>${x.balance > 0 && x.status === 'completed' ? `<button class="btn light small" onclick="recordPayment(${x.id},${x.balance})">Pay</button>` : `<button class="btn light small" onclick="showSale(${x.id})">View</button>`}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No purchases yet.</td></tr>'}</tbody></table></div>
      <div class="modal-actions">${c.balance > 0 && c.phone ? `<a class="btn light" target="_blank" rel="noopener" href="${reminderLink(c)}">WhatsApp reminder</a>` : ''}${isManager() ? `<button class="btn light" onclick="openCustomer(${id})">Edit</button>` : ''}<button class="btn" onclick="closeModal()">Close</button></div>`);
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ expenses
const EXPENSE_CATS = ['Rent', 'Wages', 'Transport', 'Electricity', 'Water', 'Internet & airtime', 'Licences & permits', 'Repairs', 'Packaging', 'Security', 'Other'];
PAGES.expenses = {
  render() {
    return pageHead('Expenses', 'Record running costs so your profit figure is real.', `<a class="btn light" href="/api/export/expenses.csv?period=month">Export CSV</a><button class="btn" onclick="openExpense()">＋ Add expense</button>`) +
      `<div id="expBody"><div class="empty">Loading…</div></div>`;
  },
  async after() {
    try {
      const rows = await api('/expenses');
      const now = new Date(), month = rows.filter(x => { const d = utc(x.created_at); return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear(); });
      const byCat = {}; month.forEach(x => byCat[x.category] = (byCat[x.category] || 0) + x.amount);
      const total = month.reduce((a, x) => a + x.amount, 0);
      $('#expBody').innerHTML = `<div class="grid-main"><section class="panel"><div class="panel-head"><div><h3>All expenses</h3><p>Most recent first</p></div></div><div class="table-wrap"><table><thead><tr><th>DATE</th><th>CATEGORY</th><th>DESCRIPTION</th><th>AMOUNT</th><th></th></tr></thead><tbody>
        ${rows.map(x => `<tr><td>${fmtDate(x.created_at)}</td><td>${esc(x.category)}</td><td>${esc(x.description)}</td><td><b>${money(x.amount)}</b></td><td><button class="btn light small" aria-label="Delete" onclick="deleteExpense(${x.id})">✕</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty">No expenses recorded.</td></tr>'}</tbody></table></div></section>
        <section class="panel"><div class="panel-head"><div><h3>This month</h3><p>${money(total)} total</p></div></div>${Object.keys(byCat).length ? hbars(Object.entries(byCat).sort((a, b) => b[1] - a[1]).map(([label, value]) => ({ label, value })), total) : '<div class="empty">Nothing this month yet.</div>'}</section></div>`;
    } catch (e) { toast(e.message); }
  },
};
function openExpense() {
  openModal(`<h2>Record an expense</h2><p>Capture running costs for realistic profit estimates.</p><form id="expenseForm"><div class="form-grid">
    <div class="field"><label>Category</label><select name="category">${EXPENSE_CATS.map(c => `<option>${c}</option>`).join('')}</select></div>
    <div class="field"><label>Amount (KSh)</label><input name="amount" type="number" min="0.01" step="0.01" required></div>
    <div class="field full"><label>Description</label><input name="description" placeholder="Optional details" maxlength="240"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save expense</button></div></form>`);
  onSubmit('expenseForm', async x => { await post('/expenses', { ...x, amount: Number(x.amount) }); closeModal(); await loadData(); renderPage(); toast('Expense saved'); });
}
async function deleteExpense(id) {
  openModal(`<h2>Delete this expense?</h2><p>This is recorded in the audit log.</p><div class="modal-actions"><button class="btn light" onclick="closeModal()">Cancel</button><button class="btn danger" id="delExp">Delete</button></div>`);
  $('#delExp').onclick = async () => { try { await api(`/expenses/${id}`, { method: 'DELETE' }); closeModal(); renderPage(); toast('Expense deleted'); } catch (e) { toast(e.message); } };
}

// ------------------------------------------------------------------ suppliers
PAGES.suppliers = {
  render() {
    return pageHead('Suppliers', 'Who you buy from, and what you bought.', `<button class="btn" onclick="openSupplier()">＋ Add supplier</button>`) +
      `<section class="panel"><div class="table-wrap"><table><thead><tr><th>SUPPLIER</th><th>PHONE</th><th>NOTES</th><th></th></tr></thead><tbody>
      ${state.suppliers.map(s => `<tr><td><b>${esc(s.name)}</b></td><td>${esc(s.phone || '—')}</td><td class="muted">${esc(s.notes || '')}</td>
        <td style="text-align:right"><button class="btn light small" onclick="supplierPurchases(${s.id})">Purchases</button> <button class="btn light small" onclick="openSupplier(${s.id})">Edit</button></td></tr>`).join('') || '<tr><td colspan="4" class="empty">No suppliers added.</td></tr>'}</tbody></table></div></section>`;
  },
};
function openSupplier(id) {
  const s = id ? state.suppliers.find(x => x.id === id) : {};
  openModal(`<h2>${id ? 'Edit' : 'Add'} supplier</h2><p>Link deliveries to suppliers when receiving stock.</p><form id="supForm"><div class="form-grid">
    <div class="field full"><label>Name</label><input name="name" required value="${esc(s.name || '')}"></div><div class="field"><label>Phone</label><input name="phone" value="${esc(s.phone || '')}"></div>
    <div class="field"><label>Notes</label><input name="notes" value="${esc(s.notes || '')}" placeholder="e.g. Delivers Tuesdays"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save</button></div></form>`);
  onSubmit('supForm', async x => { await post(id ? `/suppliers/${id}` : '/suppliers', x, id ? 'PUT' : 'POST'); closeModal(); await loadData(); renderPage(); toast('Supplier saved'); });
}
async function supplierPurchases(id) {
  const s = state.suppliers.find(x => x.id === id);
  try {
    const rows = await api(`/suppliers/${id}/purchases`);
    const total = rows.reduce((a, r) => a + r.total, 0);
    openModal(`<h2>${esc(s.name)}</h2><p>${rows.length} deliveries · ${money(total)}</p><div class="table-wrap"><table><thead><tr><th>DATE</th><th>PRODUCT</th><th>QTY</th><th>UNIT COST</th><th>TOTAL</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${fmtDate(r.created_at)}</td><td>${esc(r.product)}</td><td>${r.quantity}</td><td>${money(r.unit_cost)}</td><td>${money(r.total)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No deliveries recorded. Pick this supplier when receiving stock.</td></tr>'}</tbody></table></div>
      <div class="modal-actions">${s.phone ? `<a class="btn light" target="_blank" rel="noopener" href="${waLink(s.phone, 'Habari, ')}">WhatsApp</a>` : ''}<button class="btn" onclick="closeModal()">Close</button></div>`);
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ reports
PAGES.reports = {
  render() {
    const periods = [['today', 'Today'], ['yesterday', 'Yesterday'], ['week', 'This week'], ['month', 'This month'], ['last_month', 'Last month'], ['30d', '30 days'], ['90d', '90 days']];
    return pageHead('Business reports', 'Profit & loss, trends, best sellers and where the money went.', `<button class="btn light" onclick="window.print()">Print</button>`) +
      `<div class="chips" style="margin-bottom:16px">${periods.map(([k, v]) => `<button class="chip ${reportPeriod === k ? 'active' : ''}" onclick="reportPeriod='${k}';renderPage()">${v}</button>`).join('')}</div><div id="reportData" class="print-area"><div class="empty">Loading your report…</div></div>`;
  },
  async after() {
    let r;
    try { r = await api('/reports/summary?period=' + reportPeriod); } catch (e) { return toast(e.message); }
    const vs = `vs previous ${r.period.days === 1 ? 'day' : r.period.days + ' days'}`;
    const pmTotal = r.payment_methods.reduce((a, m) => a + m.amount, 0);
    $('#reportData').innerHTML = `<p class="muted" style="font-size:12px;margin-top:0">${esc(r.period.label)} · ${fmtDate(r.period.start)} – ${fmtDate(r.period.end)}</p>
    <div class="cards">${stat('Sales revenue', money(r.revenue), '↗', delta(r.change.revenue, vs))}${stat('Gross profit', money(r.gross_profit), '◈', `${r.gross_margin_pct}% margin · ${delta(r.change.gross_profit, '')}`)}
      ${stat('Estimated net profit', money(r.estimated_net_operating_profit), '◎', delta(r.change.net_profit, vs))}${stat('Transactions', r.transactions, '▣', `Avg sale ${money(r.average_sale)}`)}</div>
    ${r.period.days > 1 ? `<section class="panel"><div class="panel-head"><div><h3>Daily revenue</h3><p>Hover a bar for the day's figures</p></div></div>${barChart(dailyPoints(r.daily), { width: 1100, height: 230 })}</section>` : ''}
    <div class="grid-main"><section class="panel"><div class="panel-head"><div><h3>Top products</h3><p>By revenue, with the profit each one earned</p></div><a class="btn light small" href="/api/export/sales.csv?period=${reportPeriod}">Export sales</a></div>
      ${r.top_products.length ? `<div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>UNITS</th><th>REVENUE</th><th>GROSS PROFIT</th></tr></thead><tbody>${r.top_products.map(p => `<tr><td><b>${esc(p.name)}</b></td><td>${p.quantity}</td><td>${money(p.revenue)}</td><td class="${p.gross_profit < 0 ? 'delta down' : ''}">${money(p.gross_profit)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No sales in this period.</div>'}</section>
      <section class="panel"><h3 style="font:700 14px Manrope;margin-top:0">Profit & loss</h3>
        <div class="kv"><span>Revenue</span><b>${money(r.revenue)}</b></div><div class="kv"><span class="muted">incl. discounts given</span><span class="muted">${money(r.discounts_given)}</span></div>
        <div class="kv"><span>Cost of goods sold</span><b>− ${money(r.cost_of_goods_sold)}</b></div><div class="kv"><span>Gross profit</span><b>${money(r.gross_profit)}</b></div>
        <div class="kv"><span>Operating expenses</span><b>− ${money(r.operating_expenses)}</b></div><div class="kv" style="font-size:14px"><b>Estimated net profit</b><b class="delta ${r.estimated_net_operating_profit >= 0 ? 'up' : 'down'}">${money(r.estimated_net_operating_profit)}</b></div>
        <div class="kv"><span class="muted">Sold on credit in period</span><span class="muted">${money(r.credit_issued)}</span></div>
        <div class="footer-note">Based only on what is recorded in Grovia. Not a statutory financial statement.</div></section></div>
    <div class="grid-main"><section class="panel"><div class="panel-head"><div><h3>How customers paid</h3><p>Money received in this period</p></div><a class="btn light small" href="/api/export/payments.csv?period=${reportPeriod}">Export</a></div>${r.payment_methods.length ? hbars(r.payment_methods.sort((a, b) => b.amount - a.amount).map(m => ({ label: METHODS[m.method] || m.method, value: m.amount })), pmTotal) : '<div class="empty">No payments.</div>'}</section>
      <section class="panel"><div class="panel-head"><div><h3>Expenses by category</h3><p>${money(r.operating_expenses)} total</p></div></div>${r.expenses_by_category.length ? hbars(r.expenses_by_category.map(c => ({ label: c.category, value: c.amount })), r.operating_expenses) : '<div class="empty">No expenses recorded.</div>'}</section></div>`;
  },
};

// ------------------------------------------------------------------ assistant
const SUGGESTIONS = ['What did I sell today?', 'What should I restock?', 'Who owes me money?', 'What is my profit this month?', 'Which products are not selling?', 'Faida yangu wiki hii?', 'Mauzo ya leo?', 'Nani anadaiwa?'];
PAGES.assistant = {
  render() {
    const sugg = SUGGESTIONS.filter(q => isManager() || !/profit|faida|not selling/i.test(q));
    return pageHead('Ask Grovia', 'Ask about your business in English or Kiswahili. Answers come only from your own records.') +
      `<section class="panel"><div class="chat" id="chat">${chatLog.length ? chatLog.map(chatBubble).join('') : `<div class="msg bot">Habari! I can tell you about sales, profit, top sellers, what to restock, who owes you and slow-moving stock. Try a question below.</div>`}</div>
      <div class="chips" style="margin:14px 0">${sugg.map(q => `<button class="chip" onclick="ask(this.textContent)">${esc(q)}</button>`).join('')}</div>
      <form id="askForm" style="display:flex;gap:8px"><input class="search" id="askInput" style="flex:1" placeholder="Ask about your business…" required maxlength="500" autocomplete="off"><button class="btn" type="submit">Ask</button></form>
      <div class="footer-note">Grovia's assistant uses fixed rules over your data — it does not send anything to an outside AI service.</div></section>`;
  },
  after() {
    $('#askForm').onsubmit = e => { e.preventDefault(); ask($('#askInput').value); $('#askInput').value = ''; };
    const c = $('#chat'); c.scrollTop = c.scrollHeight;
  },
};
const chatBubble = m => `<div class="msg ${m.me ? 'me' : 'bot'}">${esc(m.text)}</div>`;
async function ask(q) {
  q = q.trim(); if (!q) return;
  chatLog.push({ me: true, text: q });
  const c = $('#chat'); c.innerHTML = chatLog.map(chatBubble).join('') + '<div class="msg bot muted">Analysing your records…</div>'; c.scrollTop = c.scrollHeight;
  try { const d = await post('/assistant', { question: q }); chatLog.push({ text: d.answer }); }
  catch (e) { chatLog.push({ text: e.message }); }
  chatLog = chatLog.slice(-40);
  c.innerHTML = chatLog.map(chatBubble).join(''); c.scrollTop = c.scrollHeight;
}

// ------------------------------------------------------------------ staff
PAGES.staff = {
  render() {
    return pageHead('Staff', 'Give each person their own login. Cashiers can sell but cannot see costs, profit or delete records.', `<button class="btn" onclick="openStaff()">＋ Add staff</button>`) +
      `<section class="panel"><div id="staffRows"><div class="empty">Loading…</div></div></section>
      <section class="panel"><div class="panel-head"><div><h3>Activity log</h3><p>Voids, stock write-offs, price changes and other sensitive actions</p></div></div><div id="auditRows"><div class="empty">Loading…</div></div></section>`;
  },
  async after() {
    try {
      const [staff, log] = await Promise.all([api('/staff'), api('/audit?limit=60')]);
      $('#staffRows').innerHTML = `<div class="table-wrap"><table><thead><tr><th>NAME</th><th>EMAIL</th><th>ROLE</th><th>LAST SIGN-IN</th><th>STATUS</th><th></th></tr></thead><tbody>
        ${staff.map(u => `<tr><td><b>${esc(u.name)}</b></td><td>${esc(u.email)}</td><td>${esc(u.role)}</td><td>${u.last_login_at ? fmtDateTime(u.last_login_at) : '—'}</td><td><span class="badge ${u.active ? '' : 'red'}">${u.active ? 'Active' : 'Disabled'}</span></td>
        <td style="text-align:right">${u.role === 'owner' ? '' : `<button class="btn light small" onclick="editStaff(${u.id},'${u.role}',${u.active},'${esc(u.phone)}')">Manage</button>`}</td></tr>`).join('')}</tbody></table></div>`;
      $('#auditRows').innerHTML = log.length ? `<div class="table-wrap"><table><thead><tr><th>WHEN</th><th>WHO</th><th>ACTION</th><th>DETAIL</th></tr></thead><tbody>
        ${log.map(a => `<tr><td>${fmtDateTime(a.created_at)}</td><td>${esc(a.user)}</td><td>${esc(a.action)}</td><td style="white-space:normal">${esc(a.detail)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No activity yet.</div>';
    } catch (e) { toast(e.message); }
  },
};
function openStaff() {
  openModal(`<h2>Add staff member</h2><p>Share the email and temporary password with them in person. They can change it under Settings.</p><form id="staffForm"><div class="form-grid">
    <div class="field full"><label>Name</label><input name="name" required minlength="2"></div><div class="field full"><label>Email</label><input name="email" type="email" required></div>
    <div class="field"><label>Role</label><select name="role"><option value="cashier">Cashier — sell & take payments</option><option value="manager">Manager — + stock, expenses, reports</option></select></div>
    <div class="field"><label>Temporary password</label><input name="password" required minlength="10" value="${uid().slice(0, 12)}"></div>
    <div class="field full"><label>Phone (optional — for password reset codes)</label><input name="phone" placeholder="07XX XXX XXX"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Add staff</button></div></form>`);
  onSubmit('staffForm', async x => { await post('/staff', x); closeModal(); renderPage(); toast('Staff member added'); });
}
function editStaff(id, role, active, phone = '') {
  openModal(`<h2>Manage staff</h2><p>Changes sign them out so new permissions apply immediately.</p><form id="editStaffForm"><div class="form-grid">
    <div class="field"><label>Role</label><select name="role"><option value="cashier" ${role === 'cashier' ? 'selected' : ''}>Cashier</option><option value="manager" ${role === 'manager' ? 'selected' : ''}>Manager</option></select></div>
    <div class="field"><label>Status</label><select name="active"><option value="true" ${active ? 'selected' : ''}>Active</option><option value="false" ${active ? '' : 'selected'}>Disabled</option></select></div>
    <div class="field full"><label>Phone (for password reset codes)</label><input name="phone" value="${esc(phone)}" placeholder="07XX XXX XXX"></div>
    <div class="field full"><label>Reset password (optional)</label><input name="password" minlength="10" placeholder="Leave blank to keep"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save</button></div></form>`);
  onSubmit('editStaffForm', async x => { await post(`/staff/${id}`, { role: x.role, active: x.active === 'true', password: x.password || null, phone: x.phone || '' }, 'PATCH'); closeModal(); renderPage(); toast('Staff updated'); });
}

// ------------------------------------------------------------------ settings
PAGES.settings = {
  render() {
    const b = state.business;
    const t = themeChoice();
    return pageHead('Settings', 'Your business profile, receipts and account security.') + `
      <section class="panel"><div class="panel-head"><div><h3>Appearance</h3><p>Make Grovia yours. Saved on this device — each phone or computer can have its own look.</p></div></div>
      <div class="pick-label">Mode</div>
      <div class="theme-pick" role="radiogroup" aria-label="Mode">${Object.entries(THEMES).map(([k, v]) => `<button type="button" role="radio" aria-checked="${t === k}" class="theme-opt ${t === k ? 'active' : ''}" data-choice="${k}" onclick="setTheme('${k}')">
        <div class="swatch sw-${k}">${k === 'system' ? '' : '<i></i><i></i>'}</div><b>${v}</b></button>`).join('')}</div>
      <div class="pick-label" style="margin-top:18px">Colour theme</div>
      <div class="accent-pick" role="radiogroup" aria-label="Colour theme">${Object.entries(ACCENTS).map(([k, [label, accent, side, hi]]) => `<button type="button" role="radio" aria-checked="${accentChoice() === k}" class="theme-opt accent-opt ${accentChoice() === k ? 'active' : ''}" data-accent-choice="${k}" onclick="setAccent('${k}')">
        <div class="dots"><i style="background:${side}"></i><i style="background:${accent}"></i><i style="background:${hi}"></i></div><b>${label}</b></button>`).join('')}</div></section>
      <div class="grid-main">
      <section class="panel"><div class="panel-head"><div><h3>Business profile</h3><p>Shown on receipts</p></div></div>
      ${isOwner() ? `<form id="bizForm"><div class="form-grid">
        <div class="field full"><label>Business name</label><input name="name" required value="${esc(b.name)}"></div>
        <div class="field"><label>Phone</label><input name="phone" value="${esc(b.phone)}"></div><div class="field"><label>KRA PIN</label><input name="kra_pin" value="${esc(b.kra_pin)}" maxlength="20"></div>
        <div class="field full"><label>Address / location</label><input name="address" value="${esc(b.address)}" placeholder="e.g. Shop 4, Tom Mboya St, Nairobi"></div>
        <div class="field full"><label>Receipt footer</label><input name="receipt_footer" value="${esc(b.receipt_footer)}" maxlength="240"></div>
        <div class="field full"><label>Timezone</label><select name="timezone">${['Africa/Nairobi', 'Africa/Kampala', 'Africa/Dar_es_Salaam', 'Africa/Kigali', 'Africa/Addis_Ababa', 'Africa/Lagos', 'Africa/Accra', 'Africa/Johannesburg', 'Africa/Cairo', 'UTC'].map(t => `<option ${b.timezone === t ? 'selected' : ''}>${t}</option>`).join('')}</select></div></div>
        <div class="modal-actions"><button class="btn" type="submit">Save profile</button></div></form>` : `<div class="kv"><span>Name</span><b>${esc(b.name)}</b></div><div class="kv"><span>Phone</span><b>${esc(b.phone || '—')}</b></div><p class="footer-note">Only the owner can edit the business profile.</p>`}</section>
      <section class="panel"><div class="panel-head"><div><h3>Your account</h3><p>${esc(state.me.email)} · ${esc(state.me.role)}</p></div></div>
        <form id="pwForm"><div class="field" style="margin-bottom:10px"><label>Current password</label><input name="current_password" type="password" required autocomplete="current-password"></div>
        <div class="field"><label>New password</label><input name="new_password" type="password" required minlength="10" autocomplete="new-password"></div>
        <div class="modal-actions"><button class="btn" type="submit">Change password</button></div></form>
        <div style="border-top:1px solid var(--line);margin-top:18px;padding-top:14px"><b style="font-size:12px">Lost a phone?</b><p class="muted" style="font-size:11px">Sign out of Grovia on every device, including this one.</p><button class="btn danger" onclick="logoutAll()">Sign out everywhere</button></div>
        ${isOwner() ? `<div style="border-top:1px solid var(--line);margin-top:18px;padding-top:14px"><b style="font-size:12px">Back up your data</b><p class="muted" style="font-size:11px">Download everything Grovia has recorded for your shop — sales, stock, customers, M-Pesa, expenses — as one file. Keep it somewhere safe, e.g. once a week.</p><a class="btn light" href="/api/export/backup.json">Download backup</a></div>` : ''}
      </section></div>`;
  },
  after() {
    if ($('#bizForm')) onSubmit('bizForm', async x => { state.business = await post('/business', x, 'PUT'); appShell(); renderPage(); toast('Profile saved'); });
    onSubmit('pwForm', async (x, form) => { await post('/auth/password', x); form.reset(); toast('Password changed. Other devices have been signed out.'); });
  },
};

// ------------------------------------------------------------------ session
function resetState() { try { localStorage.removeItem('duka_session_hint'); } catch { /* ignore */ } state = { me: null, business: null, page: 'dashboard', products: [], customers: [], suppliers: [], dash: null }; cart = { lines: [], ref: uid() }; chatLog = []; }
async function logout() { try { await post('/auth/logout', {}); } catch { /* ignore */ } resetState(); history.replaceState(null, '', '/'); authView(); }
async function logoutAll() { try { await post('/auth/logout-all', {}); } catch (e) { return toast(e.message); } resetState(); history.replaceState(null, '', '/'); authView(); toast('Signed out on all devices'); }

async function boot() {
  try {
    const d = await api('/auth/me');
    store.set('duka_session_hint', d);
    state.me = d.user; state.business = d.business; state.page = d.user.role === 'cashier' ? 'pos' : 'dashboard';
    await loadApp();
  } catch (e) {
    const hint = store.get('duka_session_hint', null);
    if (e instanceof NetworkError && hint) {  // reopen the till offline with the last known session
      state.me = hint.user; state.business = hint.business; state.page = 'pos';
      await loadApp();
    } else authView();
  }
}
if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
boot();
