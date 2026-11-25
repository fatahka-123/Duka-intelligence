/* Duka Intelligence v0.3 — smart features UI
 * Quick entry (type or speak), live M-Pesa, shifts & cash-up, Duka Score, automation centre,
 * loss watch and market prices. Loaded after app.js; registers pages into PAGES/NAV.
 */
'use strict';

const mpesaOn = () => ['simulator', 'sandbox', 'production'].includes(state.business?.mpesa_mode);

// ------------------------------------------------------------------ navigation additions
NAV.splice(NAV.findIndex(n => n[0] === 'expenses'), 0, ['mpesa', '◉', 'M-Pesa', 'all']);
NAV.splice(NAV.findIndex(n => n[0] === 'staff'), 0, ['score', '★', 'Duka Score', 'owner'], ['automation', '⟳', 'Automation', 'owner']);

// ------------------------------------------------------------------ quick entry (type or speak)
let quickAction = null;
function quickEntryBox() {
  const speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  return `<section class="panel quick"><div class="quick-row">
    <span class="quick-ico" aria-hidden="true">✳</span>
    <input id="quickInput" class="quick-input" maxlength="300" autocomplete="off"
      placeholder="Tell Duka what happened… e.g. “sold 2 unga 1 sukari mpesa” · “nimelipa stima 1500” · “Kamau paid 500”">
    ${speech ? `<button class="btn light mic" id="micBtn" title="Speak (English or Kiswahili)" onclick="listen()">🎤</button>` : ''}
    <button class="btn" onclick="quickParse()">Go</button></div>
    <div id="quickPreview"></div></section>`;
}
function wireQuickEntry() {
  const i = $('#quickInput'); if (!i) return;
  i.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); quickParse(); } };
}
function listen() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new SR(); rec.lang = 'sw-KE'; rec.interimResults = false; rec.maxAlternatives = 1;
  const btn = $('#micBtn'); btn.classList.add('listening'); btn.textContent = '●';
  rec.onresult = e => { $('#quickInput').value = e.results[0][0].transcript; quickParse(); };
  rec.onend = () => { btn.classList.remove('listening'); btn.textContent = '🎤'; };
  rec.onerror = () => toast('Could not hear that — try typing instead');
  rec.start();
}
async function quickParse() {
  const text = $('#quickInput').value.trim(); if (!text) return;
  const box = $('#quickPreview'); box.innerHTML = '<div class="muted" style="font-size:12px;padding:10px 0">Understanding…</div>';
  try { drawQuick(await post('/quick-entry/parse', { text })); }
  catch (e) { box.innerHTML = ''; toast(e.message); }
}
function quickSummary(a) {
  if (a.intent === 'sale') return 'Sell ' + a.lines.map(l => `${l.quantity} × ${l.name}`).join(', ') + ` = ${money(a.lines.reduce((s, l) => s + l.unit_price * l.quantity, 0))}`;
  if (a.intent === 'receive') return 'Receive stock: ' + a.lines.map(l => `${l.quantity} × ${l.name}`).join(', ');
  return a.summary;
}
function drawQuick(a, keep = false) {
  const box = $('#quickPreview');
  const prev = keep ? { method: $('#qeMethod')?.value, cust: $('#qeCustomer')?.value, ref: $('#qeRef')?.value } : null;
  quickAction = a;
  if (a.intent === 'error') {
    box.innerHTML = `<div class="quick-card warn"><b>Hmm.</b> ${esc(a.message)}${(a.unclear || []).filter(u => u.options.length).map(u => `<div class="muted" style="margin-top:6px">“${esc(u.text)}” could be: ${u.options.map(o => esc(o.name)).join(', ')}</div>`).join('')}</div>`;
    return;
  }
  const icon = { sale: '↗', expense: '◈', receive: '▤', payment: '◉' }[a.intent];
  const lines = (a.lines || []).map((l, i) => `<div class="kv"><span>${(l.alternatives || []).length ? `<select class="alt" onchange="qeSwap(${i},this.value)" aria-label="Product">${[{ product_id: l.product_id, name: l.name }, ...l.alternatives].map(o => `<option value="${o.product_id}" ${o.product_id === l.product_id ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select>` : esc(l.name)} ${l.stock !== undefined && l.quantity > l.stock ? `<span class="badge red">Only ${l.stock} in stock</span>` : ''}</span>
    <span class="qty"><button class="btn light small" onclick="qeQty(${i},-1)" aria-label="Less">−</button><b>${l.quantity}</b><button class="btn light small" onclick="qeQty(${i},1)" aria-label="More">＋</button>
    ${l.unit_price !== undefined ? `<b style="min-width:90px;text-align:right">${money(l.unit_price * l.quantity)}</b>` : ''}</span></div>`).join('');
  const needCustomer = a.intent === 'sale' && (a.needs || []).includes('customer');
  box.innerHTML = `<div class="quick-card"><div class="row-flex" style="gap:10px;margin-bottom:8px"><span class="stat-icon">${icon}</span><b>${esc(quickSummary(a))}</b></div>
    ${lines}
    ${(a.unclear || []).length ? `<div class="muted" style="font-size:11px;margin-top:6px">Skipped: ${a.unclear.map(u => '“' + esc(u.text) + '”').join(', ')}</div>` : ''}
    ${a.intent === 'sale' ? `<div class="form-grid" style="margin-top:10px"><div class="field"><label>Payment</label><select id="qeMethod">${['cash', 'mpesa', 'credit', 'bank'].map(m => `<option value="${m}" ${a.payment_method === m ? 'selected' : ''}>${METHODS[m]}</option>`).join('')}</select></div>
      <div class="field"><label>Customer${needCustomer ? ' (required)' : ''}</label><select id="qeCustomer"><option value="">Walk-in</option>${state.customers.map(c => `<option value="${c.id}" ${a.customer_id === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></div>
      <div class="field full" id="qeRefBox" style="${a.payment_method === 'mpesa' ? '' : 'display:none'}"><label>M-Pesa code (optional — checked automatically)</label><input id="qeRef" maxlength="12" style="text-transform:uppercase"></div></div>` : ''}
    <div class="modal-actions" style="margin-top:12px"><button class="btn light" onclick="$('#quickPreview').innerHTML=''">Cancel</button><button class="btn" id="qeGo" onclick="quickExecute()">Confirm ✓</button></div></div>`;
  if (prev) { if (prev.method) $('#qeMethod').value = prev.method; if (prev.cust !== undefined) $('#qeCustomer').value = prev.cust; if (prev.ref) $('#qeRef').value = prev.ref; }
  const m = $('#qeMethod'); if (m) { const sync = () => { $('#qeRefBox').style.display = m.value === 'mpesa' ? '' : 'none'; }; m.onchange = sync; sync(); }
}
function qeSwap(i, pid) {
  const l = quickAction.lines[i], alt = l.alternatives.find(o => o.product_id === Number(pid));
  if (!alt) return;
  l.alternatives = [{ product_id: l.product_id, name: l.name, unit_price: l.unit_price, stock: l.stock }, ...l.alternatives.filter(o => o !== alt)];
  Object.assign(l, { product_id: alt.product_id, name: alt.name, unit_price: alt.unit_price, stock: alt.stock });
  drawQuick(quickAction, true);
}
function qeQty(i, d) { const l = quickAction.lines[i]; l.quantity = Math.max(1, l.quantity + d); drawQuick(quickAction, true); }
async function quickExecute() {
  const a = quickAction; if (!a) return;
  const btn = $('#qeGo'); btn.disabled = true;
  try {
    let body = { intent: a.intent };
    if (a.intent === 'sale') {
      const method = $('#qeMethod').value, customer = $('#qeCustomer').value ? Number($('#qeCustomer').value) : null;
      if (method === 'credit' && !customer) { btn.disabled = false; return toast('Choose the customer for a credit sale'); }
      body = { ...body, lines: a.lines.map(l => ({ product_id: l.product_id, quantity: l.quantity })), payment_method: method,
        customer_id: customer, payment_ref: ($('#qeRef')?.value || '').toUpperCase(), client_ref: uid() };
    } else if (a.intent === 'receive') body.lines = a.lines.map(l => ({ product_id: l.product_id, quantity: l.quantity }));
    else if (a.intent === 'expense') body.expense = a.expense;
    else if (a.intent === 'payment') body.payment = a.payment;
    const r = await post('/quick-entry/execute', body);
    quickAction = null;
    await loadData();
    if (state.page === 'dashboard') renderPage();
    $('#quickPreview').innerHTML = `<div class="quick-card ok">✓ ${esc(r.message)}${r.sale ? ` <button class="link" onclick="showSale(${r.sale.id})">Receipt →</button>` : ''}</div>`;
    $('#quickInput').value = ''; $('#quickInput').focus();
  } catch (e) { toast(e.message); btn.disabled = false; }
}

// ------------------------------------------------------------------ notification bell
async function refreshBell() {
  if (!isManager() || !$('#bellCount')) return;
  try { const r = await api('/notifications/unread'); $('#bellCount').textContent = r.count || ''; $('#bellCount').style.display = r.count ? '' : 'none'; } catch { /* offline */ }
}
setInterval(refreshBell, 60000);
const KIND_ICON = { daily_summary: '☀', debt_reminder: '♙', low_stock: '▤', price_alert: '↗', fraud_alert: '!', cash_variance: '◎', payment_received: '◉' };
async function openInbox() {
  try {
    const rows = await api('/notifications');
    openModal(`<h2>Alerts</h2><p>What Duka noticed and did for you automatically.</p>
      ${rows.map(n => `<div class="insight ${n.kind === 'fraud_alert' || n.kind === 'cash_variance' ? 'critical' : n.kind === 'price_alert' || n.kind === 'low_stock' ? 'warning' : 'good'}">
        <span class="dot">${KIND_ICON[n.kind] || 'i'}</span><div><b>${esc(n.title)}</b><p style="white-space:pre-wrap">${esc(n.body)}</p><span class="muted" style="font-size:10px">${fmtDateTime(n.created_at)}${n.status === 'sent' ? ' · new' : ''}</span></div></div>`).join('') || '<div class="empty">No alerts yet.</div>'}
      <div class="modal-actions"><button class="btn light" onclick="go('automation');closeModal()">Automation settings</button><button class="btn" onclick="closeModal()">Close</button></div>`);
    await post('/notifications/read-all', {}); refreshBell();
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ till: live M-Pesa, STK push, shifts
let mpesaPoll = null, stkPoll = null, currentShift = null;
function startLiveMpesa() {
  clearInterval(mpesaPoll);
  if (!mpesaOn()) return;
  const tick = async () => {
    const el = $('#liveMpesa'); if (!el || state.page !== 'pos') return clearInterval(mpesaPoll);
    try {
      const r = await api('/mpesa/feed?status=unmatched&hours=2');
      el.innerHTML = r.items.length ? `<div class="live-head"><span class="pulse"></span> M-Pesa received — tap to use for this sale</div>` +
        r.items.slice(0, 5).map(t => `<button class="live-tx" onclick="useMpesa('${esc(t.trans_id)}',${t.amount})"><b>${money(t.amount)}</b><span>${esc(t.payer_name || t.phone)} · ${esc(t.trans_id || '')} · ${fmtDateTime(t.created_at)}</span></button>`).join('') : '';
    } catch { /* offline */ }
  };
  tick(); mpesaPoll = setInterval(tick, 5000);
}
function useMpesa(code, amount) {
  $('#cartMethod').value = 'mpesa'; renderCart();
  $('#cartRef').value = code; $('#cartTender').value = Math.min(amount, cartTotals().total);
  renderCart(); verifyCode();
  toast(`Using ${code} · ${money(amount)}`);
}
async function verifyCode() {
  const el = $('#codeStatus'), code = ($('#cartRef')?.value || '').trim().toUpperCase(); if (!el) return;
  if (!code) { el.innerHTML = ''; return; }
  try {
    const r = await api(`/mpesa/check-code?code=${encodeURIComponent(code)}&amount=${cartTotals().total}`);
    const map = { verified: ['ok', '✓ Confirmed by M-Pesa'], unverified: ['warn', '⚠ Not confirmed by M-Pesa yet'], reused: ['bad', '✕ ' + (r.message || 'Code already used')],
      invalid: ['bad', '✕ ' + (r.message || 'Invalid code')], short: ['bad', '✕ ' + (r.message || 'Amount too low')], not_checked: ['', 'Recorded manually'] };
    const [cls, txt] = map[r.status] || ['', ''];
    el.innerHTML = `<span class="code-${cls}">${esc(txt)}</span>`;
  } catch { el.innerHTML = ''; }
}
async function sendPrompt() {
  const phone = $('#stkPhone').value.trim(), { total } = cartTotals();
  if (!cart.lines.length) return toast('Add products first');
  const btn = $('#stkBtn'); btn.disabled = true;
  try {
    const tx = await post('/mpesa/stk', { phone, amount: total });
    $('#stkStatus').innerHTML = state.business.mpesa_mode === 'simulator'
      ? `<span class="pulse"></span> Simulator: no real prompt goes to the phone — the payment will be faked in a few seconds. <button class="link" onclick="go('mpesa')">Set up real M-Pesa →</button>`
      : `<span class="pulse"></span> ${esc(tx.message)}`;
    let tries = 0;
    clearInterval(stkPoll);
    stkPoll = setInterval(async () => {
      tries++;
      try {
        const s = await api('/mpesa/stk/' + tx.id);
        if (s.status === 'unmatched' && s.trans_id) {
          clearInterval(stkPoll); $('#stkStatus').innerHTML = `<span class="code-ok">✓ Paid ${money(s.amount)} · ${esc(s.trans_id)}</span>`;
          $('#cartRef').value = s.trans_id; $('#cartTender').value = Math.min(s.amount, total); btn.disabled = false;
          checkout();
        } else if (s.status === 'failed') {
          clearInterval(stkPoll); btn.disabled = false; $('#stkStatus').innerHTML = `<span class="code-bad">✕ ${esc(s.result_desc || 'Payment not completed')}</span>`;
        } else if (tries > 45) { clearInterval(stkPoll); btn.disabled = false; $('#stkStatus').innerHTML = '<span class="code-warn">No response yet — check the live M-Pesa list</span>'; }
      } catch { /* keep polling */ }
    }, 2000);
  } catch (e) { toast(e.message); btn.disabled = false; }
}
async function loadShift() {
  try { currentShift = await api('/shifts/current'); } catch { currentShift = null; }
  const el = $('#shiftBox'); if (!el) return;
  el.innerHTML = currentShift ? `<button class="btn light" onclick="closeShiftModal()">◎ Shift open · ${currentShift.sales_count} sales · Close till</button>`
    : `<button class="btn light" onclick="openShiftModal()">◎ Open shift</button>`;
}
function openShiftModal() {
  openModal(`<h2>Open your shift</h2><p>Count the cash in the drawer before you start. Duka tracks what should be there.</p>
    <form id="openShift"><div class="field"><label>Opening cash (float) KSh</label><input name="opening_float" type="number" min="0" step="1" required value="0"></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Start shift</button></div></form>`);
  onSubmit('openShift', async x => { await post('/shifts/open', { opening_float: Number(x.opening_float) }); closeModal(); loadShift(); toast('Shift started'); });
}
async function closeShiftModal() {
  const s = await api('/shifts/current');
  openModal(`<h2>Close till</h2><p>Count the cash in the drawer <b>without looking at the expected figure first</b>.</p>
    <div class="kv"><span>Sales this shift</span><b>${s.sales_count}</b></div><div class="kv"><span>M-Pesa received</span><b>${money(s.mpesa)}</b></div>
    <form id="closeShift"><div class="form-grid" style="margin-top:12px"><div class="field"><label>Cash counted KSh</label><input name="counted_cash" type="number" min="0" step="1" required></div>
    <div class="field"><label>Cash removed / banked KSh</label><input name="cash_removed" type="number" min="0" step="1" value="0"></div>
    <div class="field full"><label>Notes</label><input name="notes" maxlength="200"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Close shift</button></div></form>`);
  onSubmit('closeShift', async x => {
    const r = await post('/shifts/close', { counted_cash: Number(x.counted_cash), cash_removed: Number(x.cash_removed || 0), notes: x.notes });
    const v = r.variance, ok = Math.abs(v) < 100;
    openModal(`<h2>${ok ? 'Till balanced ✓' : v < 0 ? 'Till is short' : 'Till is over'}</h2><p>${ok ? 'Great work.' : 'The owner has been notified.'}</p>
      <div class="kv"><span>Opening float</span><b>${money(r.opening_float)}</b></div><div class="kv"><span>+ Cash sales</span><b>${money(r.cash_sales)}</b></div>
      <div class="kv"><span>− Removed / banked</span><b>${money(r.cash_removed)}</b></div><div class="kv"><span>= Expected</span><b>${money(r.expected_cash)}</b></div>
      <div class="kv"><span>Counted</span><b>${money(r.counted_cash)}</b></div>
      <div class="change-due ${ok ? '' : 'bad'}" style="margin-top:10px"><span>Difference</span><b>${v >= 0 ? '+' : ''}${money(v)}</b></div>
      <div class="kv"><span>M-Pesa this shift</span><b>${money(r.mpesa)}</b></div><div class="modal-actions"><button class="btn" onclick="closeModal()">Done</button></div>`);
    loadShift();
  });
}

// ------------------------------------------------------------------ M-Pesa page
PAGES.mpesa = {
  render() {
    const b = state.business;
    return pageHead('M-Pesa', mpesaOn() ? `${b.mpesa_type === 'paybill' ? 'Paybill' : 'Till'} ${esc(b.mpesa_shortcode || '')} · ${b.mpesa_mode === 'simulator' ? 'Simulator mode (demo)' : b.mpesa_mode}` : 'Link your Till or Paybill so payments reconcile themselves.',
      isOwner() ? `<button class="btn light" onclick="mpesaSettings()">⚙ Settings</button>` : '') +
      (mpesaOn() ? `<div id="mpesaBody"><div class="empty">Loading…</div></div>` :
      `<section class="panel"><div class="hero-empty"><h3>Stop checking SMS at the counter</h3><p>When M-Pesa is linked, every payment to your Till or Paybill appears here and at the till within seconds. Duka matches debt repayments to the right customer automatically, and blocks fake or reused M-Pesa codes.</p>
      ${isOwner() ? `<button class="btn" onclick="mpesaSettings()">Link M-Pesa</button> <button class="btn light" onclick="quickSimulator()">Try the simulator</button>` : '<p class="muted">Ask the owner to link M-Pesa.</p>'}</div></section>`);
  },
  async after() {
    if (!mpesaOn()) return;
    try {
      const r = await api('/mpesa/feed?hours=168');
      const matched = r.items.filter(t => t.status === 'matched'), auto = matched.filter(t => t.matched_by.startsWith('auto') || t.matched_by === 'stk');
      const total = r.items.filter(t => ['matched', 'unmatched'].includes(t.status)).reduce((a, t) => a + t.amount, 0);
      $('#mpesaBody').innerHTML = `<div class="cards">${stat('Received (7 days)', money(total), '◉', `${r.items.length} payments`)}${stat('Matched automatically', auto.length, '✓', `${matched.length ? Math.round(auto.length / matched.length * 100) : 0}% needed no typing`)}
        ${stat('Waiting to match', r.unmatched_count, '…', money(r.unmatched_total))}${stat('Code checks', 'On', '!', 'Reused & fake codes blocked')}</div>
        ${state.business.mpesa_mode === 'simulator' ? `<section class="panel sim"><div class="panel-head"><div><h3>Simulator</h3><p>Pretend a customer paid your ${state.business.mpesa_type}. Use a customer's phone to see debt auto-matching, or a receipt number as the account (paybill).</p></div></div>
        <form id="simForm" class="form-grid"><div class="field"><label>Amount</label><input name="amount" type="number" min="1" value="500" required></div><div class="field"><label>Payer phone</label><input name="phone" placeholder="0712 345 678"></div>
        <div class="field"><label>Payer name</label><input name="name" value="JANE"></div><div class="field"><label>Account / reference</label><input name="bill_ref" placeholder="optional"></div>
        <div class="field full"><button class="btn" type="submit">Simulate payment</button></div></form></section>` : ''}
        <section class="panel"><div class="panel-head"><div><h3>Payments</h3><p>Confirmed by Safaricom — waiting-to-match first, then the latest 40</p></div></div><div class="table-wrap"><table><thead><tr><th>TIME</th><th>CODE</th><th>FROM</th><th>AMOUNT</th><th>STATUS</th><th></th></tr></thead><tbody>
        ${[...r.items.filter(t => t.status === 'unmatched'), ...r.items.filter(t => t.status !== 'unmatched')].slice(0, 40).map(t => `<tr><td>${fmtDateTime(t.created_at)}</td><td><b>${esc(t.trans_id || '—')}</b></td><td>${esc(t.payer_name || '')} <span class="muted">${esc(t.phone)}</span>${t.bill_ref ? `<div class="muted">Ref ${esc(t.bill_ref)}</div>` : ''}</td><td><b>${money(t.amount)}</b></td>
          <td>${t.status === 'matched' ? `<span class="badge">✓ ${({ auto_phone: 'Auto · customer', auto_ref: 'Auto · receipt', stk: 'Prompt', cashier: 'Till' })[t.matched_by] || 'Matched'}</span>` : t.status === 'unmatched' ? '<span class="badge warn">Unmatched</span>' : `<span class="badge red">${esc(t.status)}</span>`}${t.result_desc ? `<div class="muted">${esc(t.result_desc)}</div>` : ''}</td>
          <td style="text-align:right">${t.status === 'unmatched' ? `<button class="btn light small" onclick="attachMpesa(${t.id},${t.amount})">Match</button>` : t.sale_id ? `<button class="btn light small" onclick="showSale(${t.sale_id})">Sale</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">No payments yet.</td></tr>'}</tbody></table></div></section>`;
      if ($('#simForm')) onSubmit('simForm', async x => { const t = await post('/mpesa/simulate', { ...x, amount: Number(x.amount) }); toast(t.status === 'matched' ? `Auto-matched ✓ ${t.result_desc || ''}` : 'Payment received — waiting to match'); await loadData(); renderPage(); });
    } catch (e) { toast(e.message); }
  },
};
function attachMpesa(id, amount) {
  const owing = state.customers.filter(c => c.balance > 0);
  openModal(`<h2>Match ${money(amount)}</h2><p>Who paid? The money will clear their oldest debts first.</p>
    <form id="attachForm"><div class="field"><label>Customer</label><select name="customer_id" required>${owing.map(c => `<option value="${c.id}">${esc(c.name)} — owes ${money(c.balance)}</option>`).join('')}</select></div>
    <div class="modal-actions">${isManager() ? `<button type="button" class="btn light" onclick="ignoreMpesa(${id})">Not a debt — dismiss</button>` : ''}<button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit" ${owing.length ? '' : 'disabled'}>Match</button></div></form>`);
  onSubmit('attachForm', async x => { await post(`/mpesa/${id}/attach`, { customer_id: Number(x.customer_id) }); closeModal(); await loadData(); renderPage(); toast('Payment matched'); });
}
async function ignoreMpesa(id) { try { await post(`/mpesa/${id}/ignore`, {}); closeModal(); renderPage(); } catch (e) { toast(e.message); } }
async function quickSimulator() {
  try { await post('/mpesa/settings', { mode: 'simulator', shortcode: '123456', type: 'till' }, 'PUT'); const me = await api('/auth/me'); state.business = me.business; appShell(); renderPage(); toast('Simulator on — try a payment'); }
  catch (e) { toast(e.message); }
}
async function mpesaSettings() {
  let s; try { s = await api('/mpesa/settings'); } catch (e) { return toast(e.message); }
  const modeNote = { off: 'M-Pesa is recorded by hand — no prompts.', simulator: '<b>Simulator:</b> payments are pretend. Nothing is sent to Safaricom or to any real phone.',
    sandbox: '<b>Sandbox:</b> real prompts on real Safaricom phones using Safaricom\'s free test system. Test payments only.', production: '<b>Live:</b> real prompts, real money into your Till/Paybill.' };
  openModal(`<h2>M-Pesa settings</h2>
  <div class="quick-card" style="margin-bottom:12px" id="mpModeNote">${modeNote[s.mode] || ''}</div>
  <details style="margin-bottom:12px;font-size:12px"><summary style="cursor:pointer"><b>How to get your keys (10 minutes)</b></summary>
    <ol style="padding-left:18px;line-height:1.6;margin:8px 0">
      <li>Sign up free at <b>developer.safaricom.co.ke</b> and log in.</li>
      <li>Go to <b>My Apps → Create new app</b>, tick <b>M-Pesa Express</b> (and C2B), save.</li>
      <li>Open the app and copy the <b>Consumer Key</b> and <b>Consumer Secret</b> into the boxes below.</li>
      <li>For testing choose <b>Daraja sandbox</b> and tap <b>Fill sandbox test values</b>. Save, then <b>Test connection</b>.</li>
      <li>To go live, complete Safaricom's <b>Go Live</b> for your own Till/Paybill. You'll get live keys and a passkey by email — choose <b>Live</b> and enter them.</li>
    </ol></details>
  <form id="mpForm"><div class="form-grid">
    <div class="field"><label>Mode</label><select name="mode">${[['off', 'Off (record manually)'], ['simulator', 'Simulator (demo/training)'], ['sandbox', 'Daraja sandbox'], ['production', 'Live']].map(([v, l]) => `<option value="${v}" ${s.mode === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="field"><label>Type</label><select name="type"><option value="till" ${s.type === 'till' ? 'selected' : ''}>Buy Goods (Till)</option><option value="paybill" ${s.type === 'paybill' ? 'selected' : ''}>Paybill</option></select></div>
    <div class="field"><label>Till / Paybill number</label><input name="shortcode" value="${esc(s.shortcode)}" inputmode="numeric"></div>
    <div class="field"><label>Store number (tills)</label><input name="store_number" value="${esc(s.store_number)}" inputmode="numeric" placeholder="Head office number"></div>
    <div class="field full"><label>Consumer key</label><input name="consumer_key" placeholder="${esc(s.consumer_key || 'not set')}" autocomplete="off"></div>
    <div class="field full"><label>Consumer secret</label><input name="consumer_secret" placeholder="${esc(s.consumer_secret || 'not set')}" autocomplete="off"></div>
    <div class="field full"><label>Lipa na M-Pesa passkey (for prompts)</label><input name="passkey" placeholder="${esc(s.passkey || 'not set')}" autocomplete="off"></div></div>
    ${s.callbacks ? `<p class="footer-note">Callback URLs (registered automatically):<br><code>${esc(s.callbacks.confirmation)}</code></p>` : ''}
    <div id="mpTestResult" style="font-size:12px"></div>
    <div class="modal-actions" style="flex-wrap:wrap"><button type="button" class="btn light" id="mpSandboxFill">Fill sandbox test values</button>
      ${s.mode !== 'off' ? `<button type="button" class="btn light" onclick="testMpesa()">Test connection</button>` : ''}
      ${['sandbox', 'production'].includes(s.mode) ? `<button type="button" class="btn light" onclick="registerUrls()">Register C2B URLs</button>` : ''}<button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save</button></div></form>`);
  const form = $('#mpForm');
  form.mode.onchange = () => { $('#mpModeNote').innerHTML = modeNote[form.mode.value] || ''; $('#mpTestResult').innerHTML = ''; };
  $('#mpSandboxFill').onclick = () => {
    const d = s.sandbox_defaults; form.mode.value = 'sandbox'; form.type.value = d.type; form.shortcode.value = d.shortcode; form.store_number.value = '';
    form.passkey.value = d.passkey; form.mode.onchange();
    toast('Sandbox test values filled — now paste your Consumer Key and Secret, then Save');
  };
  onSubmit('mpForm', async x => {
    for (const k of ['consumer_key', 'consumer_secret', 'passkey']) if (!x[k]) delete x[k];
    await post('/mpesa/settings', x, 'PUT'); const me = await api('/auth/me'); state.business = me.business;
    toast('M-Pesa settings saved'); appShell(); renderPage(); await mpesaSettings();
    if (x.mode !== 'off') testMpesa();
  });
}
async function testMpesa() {
  const el = $('#mpTestResult'); if (el) el.innerHTML = '<span class="muted">Checking with Safaricom…</span>';
  try {
    const r = await post('/mpesa/test', {});
    if (el) el.innerHTML = `<div class="quick-card ok">✓ ${esc(r.message)}${(r.warnings || []).map(w => `<div class="muted" style="margin-top:4px">⚠ ${esc(w)}</div>`).join('')}</div>`;
  } catch (e) { if (el) el.innerHTML = `<div class="quick-card warn">✕ ${esc(e.message)}</div>`; else toast(e.message); }
}
async function registerUrls() { try { await post('/mpesa/register-urls', {}); toast('URLs registered with Safaricom'); } catch (e) { toast(e.message); } }

// ------------------------------------------------------------------ Duka Score
PAGES.score = {
  render() { return pageHead('Duka Score', 'Your business health — and proof of it for lenders, landlords and suppliers.', `<button class="btn" onclick="shareScore()">Share with a lender</button>`) + '<div id="scoreBody"><div class="empty">Calculating…</div></div>'; },
  async after() {
    let s; try { s = await api('/score'); } catch (e) { return toast(e.message); }
    const ring = (v) => { const r = 54, c = 2 * Math.PI * r; return `<svg viewBox="0 0 140 140" width="150" height="150" role="img" aria-label="Score ${v} of 100"><circle cx="70" cy="70" r="${r}" fill="none" stroke="#244a37" stroke-width="12"/><circle cx="70" cy="70" r="${r}" fill="none" stroke="#d9f36b" stroke-width="12" stroke-linecap="round" stroke-dasharray="${c * v / 100} ${c}" transform="rotate(-90 70 70)"/><text x="70" y="78" text-anchor="middle" font-size="36" font-weight="800" fill="#fff" font-family="Manrope">${v}</text></svg>`; };
    const pts = s.months.map(m => ({ value: Math.max(0, m.net_profit), label: m.month + (m.partial ? ' (partial)' : ''), axis: m.month.split(' ')[0], extra: `revenue ${money(m.revenue)}` }));
    $('#scoreBody').innerHTML = `<section class="score-hero">${ring(s.score)}<div><div class="eyebrow" style="color:#d9f36b">GRADE ${s.grade} · ${esc(s.status.toUpperCase())}</div>
      <h2>${s.ready ? (s.score >= 65 ? 'Your records tell a strong story.' : 'Good start — a few habits will lift your score.') : 'Building your track record.'}</h2>
      <p>${s.history_days} days of records · avg monthly revenue ${money(s.average_monthly_revenue)} · avg monthly net profit ${money(s.average_monthly_net_profit)}</p>
      ${s.indicative_loan ? `<div class="loan">Indicative affordable loan: <b>${money(s.indicative_loan)}</b> over 3 months (repayment ≈ ${money(s.indicative_repayment)}/month)</div>` : ''}</div></section>
      <div class="grid-main"><section class="panel"><div class="panel-head"><div><h3>What makes your score</h3><p>Every factor comes from your own records</p></div></div>
        ${Object.values(s.components).map(c => `<div class="comp"><div class="row-flex" style="justify-content:space-between"><b>${esc(c.label)}</b><span><b>${c.score}</b><span class="muted">/100 · weight ${c.weight}%</span></span></div><div class="bar"><span style="width:${c.score}%"></span></div><div class="muted" style="font-size:11px">${esc(c.detail)}</div></div>`).join('')}</section>
      <section class="panel"><div class="panel-head"><div><h3>How to raise it</h3><p>Your three biggest opportunities</p></div></div>${s.tips.map(t => `<div class="insight good"><span class="dot">↑</span><div><p>${esc(t)}</p></div></div>`).join('') || '<div class="empty">Excellent — keep it up.</div>'}
        <div class="footer-note">${esc(s.disclaimer)}</div></section></div>
      <section class="panel"><div class="panel-head"><div><h3>Monthly net profit</h3><p>What a lender sees first</p></div></div>${barChart(pts, { width: 900, height: 190 })}</section>`;
  },
};
async function shareScore() {
  openModal(`<h2>Share a verified statement</h2><p>Creates a private, read-only link showing your Duka Score and 6-month performance. It expires automatically and you can revoke all links at any time.</p>
    <form id="shareForm"><div class="field"><label>Link valid for</label><select name="days"><option value="7">7 days</option><option value="30" selected>30 days</option><option value="90">90 days</option></select></div>
    <div class="modal-actions"><button type="button" class="btn danger" onclick="revokeShares()">Revoke all links</button><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Create link</button></div></form>`);
  onSubmit('shareForm', async x => {
    const r = await post('/score/share', { days: Number(x.days) });
    openModal(`<h2>Link ready</h2><p>Verification code <b>${esc(r.verification_code)}</b> · expires in ${r.expires_in_days} days</p>
      <input class="search" style="width:100%" readonly value="${esc(r.url)}" onclick="this.select()">
      <div class="modal-actions"><a class="btn light" target="_blank" rel="noopener" href="${esc(r.url)}">Preview</a><a class="btn light" target="_blank" rel="noopener" href="${waLink('', 'Habari, here is my verified business statement from Duka Intelligence: ' + r.url)}">Send on WhatsApp</a><button class="btn" onclick="navigator.clipboard?.writeText('${esc(r.url)}');toast('Copied')">Copy link</button></div>`);
  });
}
async function revokeShares() { try { await post('/score/revoke', {}); closeModal(); toast('All shared links revoked'); } catch (e) { toast(e.message); } }

// ------------------------------------------------------------------ Automation centre
PAGES.automation = {
  render() { return pageHead('Automation', 'Duka works while you are away: summaries, reminders and alerts — sent automatically.') + '<div id="autoBody"><div class="empty">Loading…</div></div>'; },
  async after() {
    let s, preview, out;
    try { [s, preview, out] = await Promise.all([api('/automation/settings'), api('/automation/preview-summary'), api('/notifications?outbox=true')]); } catch (e) { return toast(e.message); }
    const prov = s.providers.whatsapp || s.providers.sms;
    $('#autoBody').innerHTML = `${prov ? '' : `<div class="quick-card warn" style="margin-bottom:16px"><b>Demo mode:</b> no WhatsApp/SMS provider is configured on the server, so messages are generated and shown in the outbox below but not delivered. Add WhatsApp Cloud API or Africa's Talking keys to go live.</div>`}
    <div class="grid-main"><section class="panel"><form id="autoForm">
      <h3 style="font:700 14px Manrope;margin-top:0">Owner updates</h3>
      <div class="form-grid"><div class="field"><label>Owner's phone</label><input name="owner_phone" value="${esc(s.owner_phone)}" placeholder="07XX XXX XXX"></div>
      <div class="field"><label>Send via</label><select name="notify_channel"><option value="whatsapp" ${s.notify_channel === 'whatsapp' ? 'selected' : ''}>WhatsApp</option><option value="sms" ${s.notify_channel === 'sms' ? 'selected' : ''}>SMS</option></select></div>
      <label class="toggle full"><input type="checkbox" name="daily_summary" ${s.daily_summary ? 'checked' : ''}> <span><b>Daily summary</b><br><span class="muted">Sales, profit, M-Pesa vs cash, what to restock, debts and fraud flags</span></span></label>
      <div class="field"><label>Send at</label><select name="summary_hour">${Array.from({ length: 24 }, (_, h) => `<option value="${h}" ${s.summary_hour === h ? 'selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('')}</select></div></div>
      <h3 style="font:700 14px Manrope;margin-top:18px">Customers</h3>
      <label class="toggle"><input type="checkbox" name="debt_reminders" ${s.debt_reminders ? 'checked' : ''}> <span><b>Automatic debt reminders</b><br><span class="muted">Bilingual, polite hours only (9am–6pm), max one a week, gets firmer: friendly → overdue → final. Then stops.</span></span></label>
      <div class="field" style="max-width:220px"><label>Start after (days unpaid)</label><input name="reminder_after_days" type="number" min="1" max="90" value="${s.reminder_after_days}"></div>
      <h3 style="font:700 14px Manrope;margin-top:18px">Intelligence</h3>
      <label class="toggle"><input type="checkbox" name="price_alerts" ${s.price_alerts ? 'checked' : ''}> <span><b>Price coach</b><br><span class="muted">When a supplier's price goes up, suggest a new selling price that protects your margin</span></span></label>
      <label class="toggle"><input type="checkbox" name="benchmark_opt_in" ${s.benchmark_opt_in ? 'checked' : ''}> <span><b>Market prices network</b><br><span class="muted">Share your prices anonymously and see the median price at other shops (shown only when 3+ shops sell the same barcode)</span></span></label>
      <div class="field" style="max-width:260px"><label>County</label><input name="county" value="${esc(s.county)}" placeholder="e.g. Nairobi" list="counties"><datalist id="counties">${['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru', 'Kiambu', 'Machakos', 'Uasin Gishu', 'Kakamega', 'Nyeri', 'Meru', 'Kisii', 'Kajiado'].map(c => `<option value="${c}">`).join('')}</datalist></div>
      <div class="modal-actions"><button type="button" class="btn light" onclick="runAutomation()">Send summary now</button><button class="btn" type="submit">Save</button></div></form></section>
    <section class="panel"><div class="panel-head"><div><h3>Tonight's summary</h3><p>Preview of what the owner receives</p></div></div><div class="phone"><div class="bubble">${esc(preview.text).replace(/\*(.+?)\*/g, '<b>$1</b>')}</div></div></section></div>
    <section class="panel"><div class="panel-head"><div><h3>Outbox</h3><p>Every message Duka sent or prepared</p></div></div><div class="table-wrap"><table><thead><tr><th>WHEN</th><th>TYPE</th><th>TO</th><th>MESSAGE</th><th>STATUS</th></tr></thead><tbody>
      ${out.map(n => `<tr><td>${fmtDateTime(n.created_at)}</td><td>${esc(n.kind.replace('_', ' '))}</td><td>${esc(n.recipient)}<div class="muted">${esc(n.channel)}</div></td><td style="white-space:normal;max-width:420px"><b>${esc(n.title)}</b><div class="muted" style="white-space:pre-wrap">${esc(n.body.slice(0, 220))}${n.body.length > 220 ? '…' : ''}</div></td>
        <td><span class="badge ${n.status === 'failed' ? 'red' : n.status === 'simulated' ? 'warn' : ''}">${esc(n.status)}</span>${n.error ? `<div class="muted">${esc(n.error)}</div>` : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">Nothing sent yet.</td></tr>'}</tbody></table></div></section>`;
    onSubmit('autoForm', async (x, form) => {
      const body = { ...x, summary_hour: Number(x.summary_hour), reminder_after_days: Number(x.reminder_after_days) };
      for (const k of ['daily_summary', 'debt_reminders', 'price_alerts', 'benchmark_opt_in']) body[k] = form.querySelector(`[name=${k}]`).checked;
      await post('/automation/settings', body, 'PUT'); toast('Automation saved'); renderPage();
    });
  },
};
async function runAutomation() { try { const r = await post('/automation/run?send_summary_now=true', {}); toast(`Done · ${r.summary} summary, ${r.reminders} reminder(s), ${r.alerts} alert(s)`); renderPage(); } catch (e) { toast(e.message); } }

// ------------------------------------------------------------------ Loss watch (inside Staff page) & market prices (Products page)
async function renderLossWatch() {
  const el = $('#lossWatch'); if (!el) return;
  try {
    const [lw, shifts] = await Promise.all([api('/loss-watch'), api('/shifts')]);
    el.innerHTML = `<section class="panel"><div class="panel-head"><div><h3>Loss watch</h3><p>Last ${lw.days} days · stock written off ${money(lw.shrinkage_value)} (${lw.shrinkage_pct}% of sales)</p></div></div>
      ${lw.unverified_codes.length ? `<div class="insight critical"><span class="dot">!</span><div><b>${lw.unverified_codes.length} M-Pesa code(s) never confirmed by M-Pesa</b><p>${lw.unverified_codes.slice(0, 5).map(c => `${esc(c.code)} · ${money(c.amount)} · ${esc(c.user)}`).join('<br>')}</p></div></div>` : ''}
      <div class="table-wrap"><table><thead><tr><th>STAFF</th><th>SALES</th><th>VOIDS</th><th>DISCOUNTS</th><th>CASH +/−</th><th>WRITE-OFFS</th><th>RISK</th></tr></thead><tbody>
      ${lw.staff.map(s => `<tr><td><b>${esc(s.name)}</b><div class="muted">${esc(s.role)}</div></td><td>${s.sales}<div class="muted">${money(s.revenue)}</div></td><td>${s.voids}${s.void_value ? `<div class="muted">${money(s.void_value)}</div>` : ''}</td><td>${s.discount_rate}%</td>
        <td class="${s.cash_variance < 0 ? 'delta down' : ''}">${money(s.cash_variance)}</td><td>${money(s.write_offs)}</td>
        <td><span class="badge ${s.level === 'high' ? 'red' : s.level === 'medium' ? 'warn' : ''}">${s.level}</span>${s.flags.map(f => `<div class="muted" style="font-size:10px;white-space:normal">• ${esc(f)}</div>`).join('')}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">No activity yet.</td></tr>'}</tbody></table></div></section>
      <section class="panel"><div class="panel-head"><div><h3>Shift cash-ups</h3><p>Expected vs counted cash per shift</p></div></div><div class="table-wrap"><table><thead><tr><th>OPENED</th><th>STAFF</th><th>SALES</th><th>EXPECTED</th><th>COUNTED</th><th>DIFFERENCE</th><th>M-PESA</th></tr></thead><tbody>
      ${shifts.map(s => `<tr><td>${fmtDateTime(s.opened_at)}</td><td>${esc(s.user)}</td><td>${s.sales_count}</td><td>${money(s.expected_cash)}</td><td>${s.counted_cash === null ? '<span class="badge">Open</span>' : money(s.counted_cash)}</td>
        <td>${s.variance === null ? '—' : `<b class="delta ${s.variance < -50 ? 'down' : 'up'}">${s.variance >= 0 ? '+' : ''}${money(s.variance)}</b>`}</td><td>${money(s.mpesa)}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">No shifts yet. Staff open a shift from the Point of sale screen.</td></tr>'}</tbody></table></div></section>`;
  } catch (e) { el.innerHTML = ''; }
}
async function renderBenchmarks() {
  const el = $('#benchmarks'); if (!el) return;
  try {
    const r = await api('/benchmarks');
    if (!r.opted_in) { el.innerHTML = isOwner() ? `<section class="panel"><div class="insight info"><span class="dot">i</span><div><b>See what other shops charge</b><p>Join the anonymous market prices network to compare your prices with the median at nearby shops.</p><button class="link" onclick="go('automation')">Turn it on →</button></div></div></section>` : ''; return; }
    el.innerHTML = r.items.length ? `<section class="panel"><div class="panel-head"><div><h3>Market prices</h3><p>Anonymous median from other Duka shops selling the same barcode</p></div></div><div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>YOUR PRICE</th><th>MARKET MEDIAN</th><th>DIFFERENCE</th><th>COMPARED WITH</th></tr></thead><tbody>
      ${r.items.map(i => `<tr><td><b>${esc(i.name)}</b></td><td>${money(i.your_price)}</td><td>${money(i.median)}</td><td><span class="badge ${i.position === 'above' ? 'warn' : i.position === 'below' ? '' : ''}">${i.position === 'in line' ? 'In line' : `${i.difference > 0 ? '+' : ''}${money(i.difference)} (${i.difference_pct > 0 ? '+' : ''}${i.difference_pct}%)`}</span></td><td class="muted">${i.shops} ${esc(i.scope)}</td></tr>`).join('')}</tbody></table></div></section>`
      : `<section class="panel"><div class="empty">You're in the market prices network. Comparisons appear once 3+ other shops sell the same barcoded products.</div></section>`;
  } catch { el.innerHTML = ''; }
}

// ------------------------------------------------------------------ hook into existing pages
const _dashRender = PAGES.dashboard.render;
PAGES.dashboard.render = function () { return _dashRender.call(this).replace('<div class="cards">', quickEntryBox() + '<div id="mpesaAlert"></div><div class="cards">'); };
PAGES.dashboard.after = async function () {
  wireQuickEntry();
  if (mpesaOn()) {
    try { const r = await api('/mpesa/feed?status=unmatched&hours=72'); if (r.unmatched_count) $('#mpesaAlert').innerHTML = `<div class="quick-card warn" style="margin-bottom:14px">◉ <b>${r.unmatched_count} M-Pesa payment(s)</b> (${money(r.unmatched_total)}) waiting to be matched. <button class="link" onclick="go('mpesa')">Review →</button></div>`; } catch { /* ignore */ }
  }
};
const _staffAfter = PAGES.staff.after, _staffRender = PAGES.staff.render;
PAGES.staff.render = function () { return _staffRender.call(this) + '<div id="lossWatch"></div>'; };
PAGES.staff.after = async function () { await _staffAfter.call(this); renderLossWatch(); };
const _prodRender = PAGES.products.render, _prodAfter = PAGES.products.after;
PAGES.products.render = function () { return _prodRender.call(this) + (isManager() ? '<div id="benchmarks"></div>' : ''); };
PAGES.products.after = async function () { await _prodAfter.call(this); if (isManager()) renderBenchmarks(); };
const _posAfter = PAGES.pos.after;
PAGES.pos.after = function () { _posAfter.call(this); loadShift(); startLiveMpesa(); };

// ------------------------------------------------------------------ purchase orders (Suppliers page)
const PO_BADGE = { draft: ['warn', 'Draft'], sent: ['', 'Sent'], received: ['', '✓ Received'], cancelled: ['red', 'Cancelled'] };
async function renderPurchaseOrders() {
  const el = $('#poList'); if (!el) return;
  try {
    const rows = await api('/purchase-orders');
    el.innerHTML = `<section class="panel"><div class="panel-head"><div><h3>Purchase orders</h3><p>Order from suppliers, send on WhatsApp, then receive the delivery into stock in one tap</p></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap"><button class="btn light small" onclick="newPO(true)">From restock plan</button><button class="btn small" onclick="newPO(false)">＋ New order</button></div></div>
      <div class="table-wrap"><table><thead><tr><th>ORDER</th><th>SUPPLIER</th><th>ITEMS</th><th>TOTAL</th><th>STATUS</th><th></th></tr></thead><tbody>
      ${rows.map(o => `<tr class="clickable" onclick="showPO(${o.id})"><td><b>${esc(o.reference)}</b><div class="muted">${fmtDate(o.created_at)}</div></td><td>${esc(o.supplier || '—')}</td>
        <td>${o.items.length}</td><td><b>${money(o.total)}</b></td><td><span class="badge ${PO_BADGE[o.status][0]}">${PO_BADGE[o.status][1]}</span></td><td style="text-align:right"><button class="btn light small">Open</button></td></tr>`).join('')
        || '<tr><td colspan="6" class="empty">No orders yet. Start one from the restock plan — Duka already knows what is running out.</td></tr>'}</tbody></table></div></section>`;
  } catch (e) { el.innerHTML = ''; }
}
async function newPO(fromPlan) {
  let lines = [];
  if (fromPlan) {
    try { lines = await api('/purchase-orders/suggest'); } catch (e) { return toast(e.message); }
    if (!lines.length) toast('Nothing is running low right now — add items by hand');
  }
  if (!lines.length) lines = [{ product_id: state.products[0]?.id, quantity: 1, unit_cost: state.products[0]?.cost_price || 0 }];
  window._poLines = lines;
  drawPOForm();
}
function drawPOForm(keep) {
  const lines = window._poLines;
  const sup = keep?.supplier_id ?? '', notes = keep?.notes ?? '';
  const opts = id => state.products.map(p => `<option value="${p.id}" ${p.id === id ? 'selected' : ''}>${esc(p.name)} (${p.stock_qty} in stock)</option>`).join('');
  openModal(`<h2>New purchase order</h2><p>Check quantities and buying prices, then save. You can send it to the supplier on WhatsApp next.</p>
    <form id="poForm"><div class="field"><label>Supplier</label><select name="supplier_id"><option value="">— choose —</option>${state.suppliers.map(s => `<option value="${s.id}" ${String(s.id) === String(sup) ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select></div>
    <div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>QTY</th><th>UNIT COST</th><th></th></tr></thead><tbody>
    ${lines.map((l, i) => `<tr><td><select data-i="${i}" data-k="product_id">${opts(l.product_id)}</select></td><td><input data-i="${i}" data-k="quantity" type="number" min="1" value="${l.quantity}" style="width:80px"></td>
      <td><input data-i="${i}" data-k="unit_cost" type="number" min="0" step="0.01" value="${l.unit_cost}" style="width:100px"></td><td><button type="button" class="btn light small" onclick="poRemove(${i})" aria-label="Remove">✕</button></td></tr>`).join('')}</tbody></table></div>
    <button type="button" class="btn light small" style="margin-top:8px" onclick="poAdd()">＋ Add line</button>
    <div class="field" style="margin-top:10px"><label>Notes for supplier</label><input name="notes" maxlength="200" value="${esc(notes)}" placeholder="e.g. Deliver by Friday"></div>
    <div class="kv"><span>Estimated total</span><b id="poTotal"></b></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Save order</button></div></form>`);
  const sync = () => {
    $$('#poForm [data-i]').forEach(inp => { const l = lines[+inp.dataset.i]; l[inp.dataset.k] = Number(inp.value); });
    $('#poTotal').textContent = money(lines.reduce((a, l) => a + l.quantity * l.unit_cost, 0));
  };
  $$('#poForm [data-i]').forEach(inp => inp.oninput = inp.onchange = () => {
    if (inp.dataset.k === 'product_id') { const p = state.products.find(x => x.id === Number(inp.value)); lines[+inp.dataset.i].unit_cost = p?.cost_price || 0; drawPOForm(poKeep()); return; }
    sync();
  });
  sync();
  onSubmit('poForm', async x => {
    sync();
    const items = lines.filter(l => l.product_id && l.quantity > 0).map(l => ({ product_id: l.product_id, quantity: Math.floor(l.quantity), unit_cost: l.unit_cost }));
    if (!items.length) return toast('Add at least one product');
    const po = await post('/purchase-orders', { supplier_id: x.supplier_id ? Number(x.supplier_id) : null, items, notes: x.notes });
    toast(`Order ${po.reference} saved`); renderPage(); showPO(po.id);
  });
}
const poKeep = () => ({ supplier_id: $('#poForm [name=supplier_id]')?.value, notes: $('#poForm [name=notes]')?.value });
function poAdd() { const k = poKeep(); const p = state.products[0]; window._poLines.push({ product_id: p?.id, quantity: 1, unit_cost: p?.cost_price || 0 }); drawPOForm(k); }
function poRemove(i) { const k = poKeep(); window._poLines.splice(i, 1); if (!window._poLines.length) poAdd(); else drawPOForm(k); }
async function showPO(id) {
  let o; try { o = (await api('/purchase-orders')).find(x => x.id === id); } catch (e) { return toast(e.message); }
  if (!o) return toast('Order not found');
  const open = ['draft', 'sent'].includes(o.status);
  openModal(`<h2>${esc(o.reference)} <span class="badge ${PO_BADGE[o.status][0]}" style="vertical-align:middle">${PO_BADGE[o.status][1]}</span></h2>
    <p>${esc(o.supplier || 'No supplier')} · created ${fmtDate(o.created_at)}${o.received_at ? ' · received ' + fmtDate(o.received_at) : ''}</p>
    <form id="poRecv"><div class="table-wrap"><table><thead><tr><th>PRODUCT</th><th>ORDERED</th><th>UNIT COST</th>${open ? '<th>ARRIVED</th>' : '<th>RECEIVED</th>'}</tr></thead><tbody>
    ${o.items.map(i => `<tr><td><b>${esc(i.name)}</b></td><td>${i.quantity}</td><td>${open ? `<input name="c_${i.id}" type="number" min="0" step="0.01" value="${i.unit_cost}" style="width:95px">` : money(i.unit_cost)}</td>
      <td>${open ? `<input name="q_${i.id}" type="number" min="0" value="${i.quantity}" style="width:80px">` : i.received_qty}</td></tr>`).join('')}</tbody></table></div>
    ${o.notes ? `<p class="muted">${esc(o.notes)}</p>` : ''}<div class="kv"><span>Total</span><b>${money(o.total)}</b></div>
    <div class="modal-actions" style="flex-wrap:wrap">${open ? `<button type="button" class="btn light" onclick="sendPO(${o.id})">Send on WhatsApp</button><button type="button" class="btn danger" onclick="cancelPO(${o.id})">Cancel order</button>` : ''}
      <button type="button" class="btn light" onclick="closeModal()">Close</button>${open ? '<button class="btn" type="submit">Receive into stock</button>' : ''}</div></form>`);
  if (open) onSubmit('poRecv', async x => {
    const items = o.items.map(i => ({ item_id: i.id, quantity: Math.floor(Number(x['q_' + i.id] || 0)), unit_cost: Number(x['c_' + i.id]) }));
    await post(`/purchase-orders/${o.id}/receive`, { items });
    await loadData(); closeModal(); renderPage(); toast('Delivery received — stock updated');
  });
}
async function sendPO(id) {
  try {
    const m = await api(`/purchase-orders/${id}/message`);
    window.open(waLink(m.phone, m.text), '_blank', 'noopener');
    await post(`/purchase-orders/${id}/sent`, {}); renderPage(); showPO(id);
  } catch (e) { toast(e.message); }
}
async function cancelPO(id) { try { await post(`/purchase-orders/${id}/cancel`, {}); closeModal(); renderPage(); toast('Order cancelled'); } catch (e) { toast(e.message); } }
const _supRender = PAGES.suppliers.render, _supAfter = PAGES.suppliers.after;
PAGES.suppliers.render = function () { return _supRender.call(this) + '<div id="poList"></div>'; };
PAGES.suppliers.after = async function () { if (_supAfter) await _supAfter.call(this); renderPurchaseOrders(); };

// ------------------------------------------------------------------ Platform admin (the provider's view of every client shop)
let adminFilter = '';
PAGES.admin = {
  render() {
    return pageHead('Admin · all shops', 'Your clients: create their shop and login, see who is using Duka, pause an account or reset a password.',
      `<button class="btn" onclick="newClient()">＋ New client</button>`) + '<div id="adminBody"><div class="empty">Loading…</div></div>';
  },
  async after() {
    let r; try { r = await api('/admin/businesses'); } catch (e) { $('#adminBody').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    window._adminRows = r.businesses;
    const s = r.summary;
    $('#adminBody').innerHTML = `<div class="cards">${stat('Client shops', s.shops, '▦', `${s.active} active · ${s.shops - s.active} paused`)}
      ${stat('Used this week', s.used_this_week, '↗', 'Signed in or sold in the last 7 days')}${stat('Sales (30 days)', s.sales_30d.toLocaleString(), '▣', 'Across all shops')}
      ${stat('Revenue (30 days)', money(s.revenue_30d), '◈', 'Across all shops')}</div>
      <section class="panel"><div class="toolbar"><input class="search" id="adminSearch" placeholder="Search shop, owner or email" value="${esc(adminFilter)}"></div>
      <div class="table-wrap"><table><thead><tr><th>SHOP</th><th>OWNER</th><th>STAFF</th><th>SALES 30D</th><th>LAST ACTIVE</th><th>M-PESA</th><th>STATUS</th><th></th></tr></thead><tbody id="adminRows"></tbody></table></div></section>
      <p class="footer-note">Only the emails in the server's PLATFORM_ADMINS setting can see this page. Every action here is written to that shop's activity log.</p>`;
    const search = $('#adminSearch'); search.oninput = () => { adminFilter = search.value; drawAdminRows(); }; drawAdminRows();
  },
};
function drawAdminRows() {
  const q = adminFilter.toLowerCase(), el = $('#adminRows'); if (!el) return;
  const rows = window._adminRows.filter(b => !q || [b.name, b.owner?.name, b.owner?.email, b.phone].join(' ').toLowerCase().includes(q));
  el.innerHTML = rows.map(b => `<tr><td><b>${esc(b.name)}</b><div class="muted">${esc(b.phone || '')}${b.county ? ' · ' + esc(b.county) : ''} · since ${fmtDate(b.created_at)}</div>${b.note ? `<div class="muted" style="white-space:normal;max-width:240px">${esc(b.note)}</div>` : ''}</td>
    <td>${b.owner ? `${esc(b.owner.name)}<div class="muted">${esc(b.owner.email)}</div>${b.owner.must_change_password ? '<span class="badge warn">Hasn’t set own password</span>' : ''}` : '—'}</td>
    <td>${b.staff_count}</td><td>${b.sales_30d}<div class="muted">${money(b.revenue_30d)}</div></td>
    <td>${b.last_activity ? fmtDateTime(b.last_activity) : '<span class="muted">Never</span>'}</td><td>${esc(b.mpesa_mode)}</td>
    <td><span class="badge ${b.active ? '' : 'red'}">${b.active ? 'Active' : 'Paused'}</span></td>
    <td style="text-align:right"><button class="btn light small" onclick="manageClient(${b.id})">Manage</button></td></tr>`).join('') || '<tr><td colspan="8" class="empty">No shops match.</td></tr>';
}
function loginCard(title, shop, login) {
  const url = location.origin;
  const msg = `Habari! Your Duka Intelligence account for ${shop} is ready.\n\nOpen: ${url}\nEmail: ${login.email}\nTemporary password: ${login.temporary_password}\n\nYou'll be asked to choose your own password the first time you sign in.`;
  openModal(`<h2>${title}</h2><p>Give these details to the client. The temporary password is shown <b>only now</b> — they must replace it at first sign-in.</p>
    <div class="quick-card ok" style="font-size:13px;line-height:1.8"><div>Website: <b>${esc(url)}</b></div><div>Email: <b>${esc(login.email)}</b></div><div>Temporary password: <b style="font-family:ui-monospace,monospace;font-size:15px">${esc(login.temporary_password)}</b></div></div>
    <div class="modal-actions" style="flex-wrap:wrap"><button class="btn light" id="copyLogin">Copy</button><a class="btn light" target="_blank" rel="noopener" href="${waLink(window._clientPhone || '', msg)}">Send on WhatsApp</a><button class="btn" onclick="closeModal();renderPage()">Done</button></div>`);
  $('#copyLogin').onclick = () => { navigator.clipboard?.writeText(msg); toast('Copied'); };
}
function newClient() {
  openModal(`<h2>New client</h2><p>Creates their own shop — completely separate from every other shop — and an owner login with a temporary password.</p>
    <form id="clientForm"><div class="form-grid"><div class="field full"><label>Shop name</label><input name="business_name" required minlength="2" placeholder="e.g. Baraka Mini Mart"></div>
    <div class="field"><label>Owner's name</label><input name="owner_name" required minlength="2"></div><div class="field"><label>Owner's phone</label><input name="phone" placeholder="07XX XXX XXX"></div>
    <div class="field full"><label>Owner's email (their login)</label><input name="email" type="email" required></div>
    <div class="field"><label>County</label><input name="county" list="counties2"><datalist id="counties2">${['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru', 'Kiambu', 'Machakos', 'Uasin Gishu', 'Kakamega', 'Nyeri', 'Meru', 'Kisii', 'Kajiado'].map(c => `<option value="${c}">`).join('')}</datalist></div>
    <div class="field"><label>Private note (only you see it)</label><input name="note" maxlength="200" placeholder="e.g. Paid 3 months"></div></div>
    <div class="modal-actions"><button type="button" class="btn light" onclick="closeModal()">Cancel</button><button class="btn" type="submit">Create shop & login</button></div></form>`);
  onSubmit('clientForm', async x => { const r = await post('/admin/businesses', x); window._clientPhone = x.phone; loginCard('Client account ready ✓', r.business.name, r.login); });
}
async function manageClient(id) {
  const b = window._adminRows.find(x => x.id === id); let users = [];
  try { users = await api(`/admin/businesses/${id}/users`); } catch (e) { return toast(e.message); }
  window._clientPhone = b.phone;
  openModal(`<h2>${esc(b.name)}</h2><p>${b.active ? 'Active' : '<b style="color:var(--red)">Paused</b> — nobody at this shop can sign in. Their data is kept.'} · since ${fmtDate(b.created_at)}</p>
    <div class="cards" style="grid-template-columns:repeat(3,1fr)">${stat('Sales 30d', b.sales_30d, '▣', money(b.revenue_30d))}${stat('Products', b.products, '▤', '')}${stat('Logins', b.staff_count, '☺', '')}</div>
    <div class="table-wrap"><table><thead><tr><th>LOGIN</th><th>ROLE</th><th>LAST SIGN-IN</th><th></th></tr></thead><tbody>
    ${users.map(u => `<tr><td><b>${esc(u.name)}</b><div class="muted">${esc(u.email)}</div></td><td>${esc(u.role)}${u.active ? '' : ' <span class="badge red">disabled</span>'}</td>
      <td>${u.last_login_at ? fmtDateTime(u.last_login_at) : '—'}</td><td style="text-align:right">${u.id === state.me.id ? '' : `<button class="btn light small" onclick="adminReset(${u.id},${b.id})">Reset password</button>`}</td></tr>`).join('')}</tbody></table></div>
    <form id="statusForm"><div class="field" style="margin-top:12px"><label>Private note</label><input name="note" maxlength="200" value="${esc(b.note)}"></div>
    <div class="modal-actions" style="flex-wrap:wrap">${b.active ? `<button type="button" class="btn danger" id="pauseBtn">Pause shop</button>` : `<button type="button" class="btn" id="pauseBtn">Resume shop</button>`}
      <button type="button" class="btn light" onclick="closeModal()">Close</button><button class="btn light" type="submit">Save note</button></div></form>`);
  onSubmit('statusForm', async x => { await post(`/admin/businesses/${id}/status`, { active: b.active, note: x.note }); toast('Saved'); closeModal(); renderPage(); });
  $('#pauseBtn').onclick = async () => {
    if (b.active && !confirmStep('#pauseBtn', 'Tap again to pause — they will be signed out')) return;
    try { await post(`/admin/businesses/${id}/status`, { active: !b.active, note: $('#statusForm [name=note]').value }); toast(b.active ? 'Shop paused' : 'Shop resumed'); closeModal(); renderPage(); }
    catch (e) { toast(e.message); }
  };
}
function confirmStep(sel, label) {  // two-tap confirm without a browser dialog
  const btn = $(sel); if (btn.dataset.armed) return true;
  btn.dataset.armed = '1'; btn.textContent = label; setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = 'Pause shop'; } }, 5000);
  return false;
}
async function adminReset(uid, bid) {
  const shop = window._adminRows.find(x => x.id === bid)?.name || '';
  try { const r = await post(`/admin/users/${uid}/reset-password`, {}); loginCard('New temporary password', shop, r); } catch (e) { toast(e.message); }
}
