const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (u, opt) => {
  const r = await fetch(u, opt);
  if (!r.ok) throw new Error(await r.text());
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const toast = m => {
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = m;
  document.body.appendChild(t); setTimeout(() => t.remove(), 3200);
};
const bandClass = b => 'b-' + String(b || '').split(' ')[0];
const fmt = n => n === null || n === undefined ? '—' : n;

let META = { statuses: [], archetypes: {}, bands: [] }, STATE = { sort: 'final_score' };

// ---------------------------------------------------------------- tabs
$$('#tabs button').forEach(b => b.onclick = () => {
  $$('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  $$('main > section').forEach(s => s.classList.toggle('hidden', s.id !== 'tab-' + b.dataset.tab));
  ({ apply: loadApply, pipeline: loadJobs, followups: loadAlerts, keywords: loadKeywords,
     funnel: loadFunnel, targets: loadTargets, runs: loadRuns }[b.dataset.tab] || (() => {}))();
});

// ---------------------------------------------------------------- pipeline
async function loadJobs() {
  const p = new URLSearchParams({ sort: STATE.sort });
  if ($('#q').value) p.set('q', $('#q').value);
  if ($('#f-band').value) p.set('band', $('#f-band').value);
  if ($('#f-status').value) p.set('status', $('#f-status').value);
  if ($('#f-arch').value) p.set('archetype', $('#f-arch').value);
  if ($('#f-rej').checked) p.set('include_rejected', 'true');
  const { jobs } = await api('/api/jobs?' + p);

  const n = b => jobs.filter(j => j.band === b).length;
  $('#pipestats').innerHTML = [
    ['Logged', jobs.length], ['Strong', n('Strong')], ['Worth a shot', n('Worth a shot')],
    ['Applied', jobs.filter(j => j.status !== 'Not Applied' && j.status !== 'Do Not Apply').length],
    ['Interviewing', jobs.filter(j => ['Recruiter Screen', 'Interviewing', 'Offer'].includes(j.status)).length],
  ].map(([l, v]) => `<div class="stat"><div class="n">${v}</div><div class="l">${l}</div></div>`).join('');

  $('#jobs tbody').innerHTML = jobs.map(j => `
    <tr data-id="${j.id}">
      <td><span class="score">${fmt(j.final_score)}</span><div class="tiny muted">fit ${fmt(j.fit_score)} ×${fmt(j.location_multiplier)}</div></td>
      <td><span class="badge ${bandClass(j.band)}">${esc(j.band)}</span>
          ${j.rejected_reason ? `<div class="tiny muted">auto-rejected</div>` : ''}</td>
      <td><b>${esc(j.company)}</b><div class="tiny muted">${esc(j.source || '')}</div></td>
      <td>${j.apply_url ? `<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">${esc(j.role_title)}</a>` : esc(j.role_title)}
          <div class="tiny muted">${esc(j.experience_asked || '')}</div></td>
      <td>${esc(j.archetype || '—')}</td>
      <td>${esc(j.location || '—')}<div class="tiny muted">T${fmt(j.location_tier)}${j.work_mode ? ' · ' + esc(j.work_mode) : ''}</div></td>
      <td class="small">${esc(j.comp_raw || (j.comp_min ? `${j.comp_currency||''} ${j.comp_min}${j.comp_max ? '-'+j.comp_max : ''}` : '—'))}<div class="tiny muted">${esc(j.comp_confidence || '')}</div></td>
      <td class="small">${esc(j.date_posted || '—')}<div class="tiny muted">${esc(j.date_confidence || '')}</div></td>
      <td><select class="js-status" data-id="${j.id}">${META.statuses.map(s =>
          `<option ${s === j.status ? 'selected' : ''}>${s}</option>`).join('')}</select>
          ${j.date_applied ? `<div class="tiny muted">${esc(j.date_applied)}</div>` : ''}</td>
      <td class="small">${esc(j.why_this_score || '')}</td>
      <td><button class="btn tiny js-open" data-id="${j.id}">open</button></td>
    </tr>`).join('') || '<tr><td colspan="11" class="muted" style="padding:20px">Nothing here. Import a run on the Import tab.</td></tr>';

  $$('.js-status').forEach(s => s.onchange = async e => {
    await api('/api/jobs/' + e.target.dataset.id, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ status: e.target.value })
    });
    toast('Status updated'); loadJobs(); loadAlertCount();
  });
  $$('.js-open').forEach(b => b.onclick = () => openDrawer(b.dataset.id));
}

$$('#jobs th[data-sort]').forEach(th => th.onclick = () => { STATE.sort = th.dataset.sort; loadJobs(); });
['#q', '#f-band', '#f-status', '#f-arch', '#f-rej'].forEach(s => {
  const el = $(s); el.oninput = el.onchange = () => loadJobs();
});
$('#exp-csv').onclick = () => location.href = '/api/export.csv';
$('#exp-json').onclick = () => location.href = '/api/export.json';
$('#btn-rescore').onclick = async () => {
  await api('/api/config/reload', { method: 'POST' });
  const r = await api('/api/rescore', { method: 'POST' });
  toast(`Re-scored ${r.rescored} postings`); loadJobs();
};
$('#btn-brief').onclick = async () => {
  const t = await api('/api/agent-brief');
  await navigator.clipboard.writeText(t).catch(() => {});
  $('#drawer').classList.remove('hidden');
  $('#drawer').innerHTML = `<button class="btn" onclick="document.getElementById('drawer').classList.add('hidden')">close</button>
    <h3>Agent brief — copied to clipboard</h3>
    <p class="small muted">Paste this at the end of the research prompt so the agent skips what you already have.</p>
    <pre>${esc(t)}</pre>`;
};

// ---------------------------------------------------------------- drawer
async function openDrawer(id) {
  const j = await api('/api/jobs/' + id);
  const d = $('#drawer');
  d.classList.remove('hidden');
  const sub = [['Archetype', j.s_archetype, 25], ['Domain', j.s_domain, 20],
    ['Seniority', j.s_seniority, 15], ['Req. gaps', j.s_gaps, 15],
    ['Stage', j.s_stage, 10], ['Comp', j.s_comp, 10], ['Location', j.s_location, 5],
    ['Like ★ roles', j.s_affinity || 0, 10]];
  d.innerHTML = `
    <div class="row"><button class="btn" id="dclose">close</button><div class="spacer"></div>
      <button class="btn" id="ddel">delete</button></div>
    <h2 style="margin:12px 0 2px">${esc(j.role_title)}</h2>
    <div class="muted">${esc(j.company)} · ${esc(j.location || '')} · ${esc(j.job_id)}</div>
    <p><span class="badge ${bandClass(j.band)}">${esc(j.band)}</span>
       <span class="score" style="margin-left:8px">${fmt(j.final_score)}</span>
       <span class="muted small">final · fit ${fmt(j.fit_score)} × ${fmt(j.location_multiplier)} location</span></p>
    ${j.rejected_reason ? `<p class="small" style="color:var(--stretch)"><b>Auto-rejected:</b> ${esc(j.rejected_reason)}</p>` : ''}
    <p><b>Biggest gap:</b> ${esc(j.why_this_score || '—')}</p>
    <p><button class="btn primary" id="d-resume">Create resume for this role</button>
       <button class="btn primary" id="d-cover">Create cover letter</button>
       <button class="btn" id="d-star">${j.favourite ? '★ Starred — unstar' : '☆ Star: find more like this'}</button></p>
    <table style="font-size:13px">${sub.map(([l, v, m]) => `<tr>
      <td style="width:110px;border:0;padding:3px 0">${l}</td>
      <td style="border:0;padding:3px 0"><div class="bar"><i style="width:${(v / m) * 100}%"></i></div></td>
      <td style="border:0;padding:3px 0;width:60px" class="tiny muted">${fmt(v)}/${m}</td></tr>`).join('')}</table>
    <dl class="kv">
      <dt>Experience asked</dt><dd>${esc(j.experience_asked || '—')}</dd>
      <dt>Compensation</dt><dd>${esc(j.comp_raw || '—')} <span class="tiny muted">(${esc(j.comp_confidence)})</span></dd>
      <dt>Company stage</dt><dd>${esc(j.company_stage || '—')}</dd>
      <dt>Domains</dt><dd>${(j.domains || []).map(x => `<span class="chip">${esc(x)}</span>`).join('') || '—'}</dd>
      <dt>Unmet mandatories</dt><dd>${(j.unmet_mandatories || []).map(x => `<span class="chip">${esc(x)}</span>`).join('') || 'none'}</dd>
      <dt>Missing keywords</dt><dd>${(j.missing_keywords || []).map(x => `<span class="chip">${esc(x)}</span>`).join('') || '—'}</dd>
      <dt>Posted</dt><dd>${esc(j.date_posted || '—')} <span class="tiny muted">(${esc(j.date_confidence)})</span></dd>
      <dt>Source</dt><dd>${esc(j.source || '—')}</dd>
      <dt>Link</dt><dd>${j.apply_url ? `<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">${esc(j.apply_url)}</a>` : '—'}</dd>
    </dl>
    <div class="row">
      <select id="d-status">${META.statuses.map(s => `<option ${s === j.status ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <input type="date" id="d-applied" value="${esc(j.date_applied || '')}">
    </div>
    <p><input id="d-ref" placeholder="Referral path — who do you know there?" value="${esc(j.referral_path || '')}" style="width:100%"></p>
    <p><textarea id="d-notes" style="min-height:90px" placeholder="Notes">${esc(j.notes || '')}</textarea></p>
    <button class="btn primary" id="dsave">Save</button>
    ${j.description ? `<h4>Job description</h4><pre>${esc(j.description)}</pre>` : ''}`;
  $('#dclose').onclick = () => d.classList.add('hidden');
  $('#d-resume').onclick = () => openResume(id);
  $('#d-cover').onclick = () => openCoverLetter(id);
  $('#d-star').onclick = async () => { await toggleStar(id, !j.favourite); openDrawer(id); };
  $('#ddel').onclick = async () => {
    if (!confirm('Delete this posting?')) return;
    await api('/api/jobs/' + id, { method: 'DELETE' });
    d.classList.add('hidden'); loadJobs();
  };
  $('#dsave').onclick = async () => {
    await api('/api/jobs/' + id, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        status: $('#d-status').value, date_applied: $('#d-applied').value || null,
        referral_path: $('#d-ref').value, notes: $('#d-notes').value
      })
    });
    toast('Saved'); d.classList.add('hidden'); loadJobs(); loadAlertCount();
  };
}

// ---------------------------------------------------------------- alerts
async function loadAlerts() {
  const a = await api('/api/alerts');
  const list = (rows, head, tone) => rows.length ? `
    <h3 style="color:${tone}">${head}</h3>
    <table><tbody>${rows.map(r => `<tr>
      <td style="width:60px"><span class="score">${r.days_since_applied}d</span></td>
      <td><b>${esc(r.company)}</b> — ${esc(r.role_title)}
          <div class="tiny muted">applied ${esc(r.date_applied)} · ${esc(r.band)}</div></td>
      <td style="width:150px">${r.referral_path ? esc(r.referral_path) : '<span class="tiny muted">no referral path logged</span>'}</td>
      <td style="width:70px"><button class="btn tiny js-open" data-id="${r.id}">open</button></td>
    </tr>`).join('')}</tbody></table>` : '';
  $('#alerts').innerHTML =
    list(a.follow_up, `Follow up now (${a.followup_days}+ days, no response)`, 'var(--worth)') +
    list(a.stale, `Likely dead (${a.stale_days}+ days) — mark Ghosted and move on`, 'var(--stretch)') ||
    '<p class="muted">Nothing needs chasing. Either you are up to date or nothing is marked Applied yet.</p>';
  $$('#alerts .js-open').forEach(b => b.onclick = () => openDrawer(b.dataset.id));
}
async function loadAlertCount() {
  const a = await api('/api/alerts');
  const n = a.follow_up.length + a.stale.length;
  $('#alertcount').textContent = n ? `(${n})` : '';
  $('#alertcount').style.color = n ? 'var(--stretch)' : '';
}

// ---------------------------------------------------------------- keywords
async function loadKeywords() {
  const k = await api('/api/analytics/keywords');
  $('#kwstats').innerHTML = [
    ['CV coverage', k.coverage_score + '%'], ['Quick wins', k.cv_quick_wins.length],
    ['Real gaps', k.real_gaps.length], ['Terms tracked', k.all.length],
  ].map(([l, v]) => `<div class="stat"><div class="n">${v}</div><div class="l">${l}</div></div>`).join('');
  const tbl = rows => rows.length ? `<table><thead><tr><th>Term</th><th>Category</th>
      <th>Freq</th><th>% of postings</th><th>Criticality</th><th>Note</th></tr></thead>
      <tbody>${rows.map(r => `<tr><td><b>${esc(r.term)}</b></td><td class="small">${esc(r.category || '—')}</td>
      <td class="score">${r.frequency}</td><td>${fmt(r.avg_pct)}${r.avg_pct != null ? '%' : ''}</td>
      <td class="small">${esc(r.criticality || '—')}</td><td class="small muted">${esc(r.note || '')}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted small">Nothing yet — import a run with a <code>keywords</code> block.</p>';
  $('#kw-wins').innerHTML = tbl(k.cv_quick_wins);
  $('#kw-gaps').innerHTML = tbl(k.real_gaps);
  $('#kw-all').innerHTML = tbl(k.all.slice(0, 60));
}

// ---------------------------------------------------------------- funnel
async function loadFunnel() {
  const f = await api('/api/analytics/funnel');
  const o = f.overall;
  $('#funstats').innerHTML = [
    ['Logged', o.logged], ['Applied', o.applied], ['Screens', o.screen],
    ['Interviews', o.interview], ['Offers', o.offer],
    ['Screen rate', o.screen_rate == null ? '—' : o.screen_rate + '%'],
  ].map(([l, v]) => `<div class="stat"><div class="n">${v}</div><div class="l">${l}</div></div>`).join('');
  $('#fun-arch').innerHTML = f.by_archetype.length ? `<table><thead><tr>
      <th>Archetype</th><th>Logged</th><th>Applied</th><th>Screens</th><th>Interviews</th>
      <th>Offers</th><th>Screen rate</th></tr></thead><tbody>
      ${f.by_archetype.map(b => `<tr>
        <td><b>${esc(b.archetype)}</b> <span class="small muted">${esc(f.archetype_labels[b.archetype] || '')}</span></td>
        <td>${b.logged}</td><td>${b.applied}</td><td>${b.screen}</td><td>${b.interview}</td><td>${b.offer}</td>
        <td>${b.screen_rate == null ? '<span class="muted">—</span>' :
          `<span class="score">${b.screen_rate}%</span><div class="bar"><i style="width:${Math.min(b.screen_rate, 100)}%"></i></div>`}</td>
      </tr>`).join('')}</tbody></table>` : '<p class="muted">No data yet.</p>';
  $('#fun-status').innerHTML = `<table><tbody>${Object.entries(f.by_status).map(([s, n]) =>
    `<tr><td style="width:170px">${esc(s)}</td><td class="score" style="width:50px">${n}</td>
     <td><div class="bar"><i style="width:${o.logged ? (n / o.logged) * 100 : 0}%"></i></div></td></tr>`).join('')}</tbody></table>`;
}

// ---------------------------------------------------------------- targets
async function loadTargets() {
  const { targets } = await api('/api/targets');
  $('#targets tbody').innerHTML = targets.map(t => `<tr>
    <td><b>${esc(t.company)}</b></td><td class="small">${esc(t.why || '')}</td>
    <td class="small">${t.careers_url ? `<a href="${esc(t.careers_url)}" target="_blank" rel="noopener">careers ↗</a>` : '—'}</td>
    <td class="small">${esc(t.region || '')}</td><td class="small">${esc(t.last_checked || 'never')}</td>
    <td class="score">${t.open_roles_found}</td>
    <td><button class="btn tiny js-tdel" data-id="${t.id}">×</button></td></tr>`).join('')
    || '<tr><td colspan="7" class="muted" style="padding:20px">Build a 40-company watchlist here. The agent checks them every run whether or not they are hiring.</td></tr>';
  $$('.js-tdel').forEach(b => b.onclick = async () => {
    await api('/api/targets/' + b.dataset.id, { method: 'DELETE' }); loadTargets();
  });
}
$('#t-add').onclick = async () => {
  const company = $('#t-company').value.trim();
  if (!company) return;
  await api('/api/targets', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ company, careers_url: $('#t-url').value, region: $('#t-region').value, why: $('#t-why').value })
  });
  ['#t-company', '#t-url', '#t-region', '#t-why'].forEach(s => $(s).value = '');
  loadTargets();
};

// ---------------------------------------------------------------- runs
async function loadRuns() {
  const { runs } = await api('/api/runs');
  $('#runs tbody').innerHTML = runs.map(r => `<tr>
    <td>${esc(r.run_date)}</td><td class="small">${esc(r.agent || '—')}</td>
    <td>${r.scanned}</td><td>${r.passed_filter}</td><td class="score">${r.added}</td>
    <td>${r.duplicates}</td><td>${r.rejected}</td>
    <td class="small muted">${(r.sources_empty || []).join(', ') || '—'}</td></tr>`).join('')
    || '<tr><td colspan="8" class="muted" style="padding:20px">No runs yet.</td></tr>';
}

// ---------------------------------------------------------------- import
$('#importfile').onchange = async e => {
  const f = e.target.files[0]; if (f) $('#importbox').value = await f.text();
};
$('#btn-import').onclick = async () => {
  let payload;
  try { payload = JSON.parse($('#importbox').value); }
  catch (err) { return toast('Not valid JSON: ' + err.message); }
  try {
    const r = await api('/api/import', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    });
    $('#importresult').innerHTML = `
      <div class="stats" style="margin-top:16px">
        ${[['Added', r.added], ['Duplicates skipped', r.duplicates],
           ['Auto-rejected', r.auto_rejected], ['Errors', r.errors.length]]
          .map(([l, v]) => `<div class="stat"><div class="n">${v}</div><div class="l">${l}</div></div>`).join('')}
      </div>
      ${r.top.length ? `<h4>Top of this run</h4><table><tbody>${r.top.map(t =>
        `<tr><td class="score" style="width:60px">${t.final_score}</td>
         <td><span class="badge ${bandClass(t.band)}">${esc(t.band)}</span></td>
         <td><b>${esc(t.company)}</b> — ${esc(t.role_title)}</td></tr>`).join('')}</tbody></table>` : ''}
      ${r.rejected_detail.length ? `<h4>Auto-rejected</h4><ul class="small muted">${r.rejected_detail.map(x =>
        `<li>${esc(x.company)} — ${esc(x.role_title)}: ${esc(x.rejected_reason)}</li>`).join('')}</ul>` : ''}
      ${r.duplicate_detail.length ? `<h4>Already in the tracker</h4><ul class="small muted">${r.duplicate_detail.map(x =>
        `<li>${esc(x.company)} — ${esc(x.role_title)} (${esc(x.status)})</li>`).join('')}</ul>` : ''}
      ${r.errors.length ? `<h4 style="color:var(--stretch)">Errors</h4><pre>${esc(JSON.stringify(r.errors, null, 2))}</pre>` : ''}`;
    toast(`Imported: ${r.added} added, ${r.duplicates} dupes`);
    loadAlertCount(); refreshHeadline();
  } catch (err) { toast('Import failed: ' + err.message); }
};

// ---------------------------------------------------------------- linkedin
let liTimer = null;
async function liStatus() {
  const st = await api('/api/linkedin/status');
  $('#li-meta').textContent = st.token_configured
    ? `token ✓ · ${st.search_count} searches · cap ${st.max_results_per_run} results`
    : 'no APIFY_TOKEN in .env';
  $('#li-run').disabled = $('#li-ds').disabled = st.running || !st.token_configured;
  if (st.log.length) { $('#li-log').classList.remove('hidden'); $('#li-log').textContent = st.log.join('\n'); }
  if (st.running) { liTimer = setTimeout(liStatus, 4000); return; }
  if (liTimer && st.result) {
    toast(`LinkedIn: ${st.result.added} added, ${st.result.duplicates} already tracked`);
    loadAlertCount(); refreshHeadline();
  }
  if (liTimer && st.error) toast('LinkedIn sync failed — see log');
  liTimer = null;
}
async function liStart(body) {
  try {
    await api('/api/linkedin/sync', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) });
    liTimer = 1; liStatus();
  } catch (e) { toast(e.message.slice(0, 160)); }
}
$('#li-run').onclick = () => liStart({});
$('#li-ds').onclick = () => { const id = $('#li-dataset').value.trim(); if (id) liStart({ dataset_id: id }); };
$('#li-urls').onclick = async () => {
  const { urls } = await api('/api/linkedin/searches');
  $('#li-log').classList.remove('hidden');
  $('#li-log').textContent = urls.join('\n');
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', liStatus);

// ---------------------------------------------------------------- indeed
let idTimer = null;
async function idStatus() {
  const st = await api('/api/indeed/status');
  $('#id-meta').textContent = st.token_configured
    ? `token ✓ · ${st.search_count} searches · cap ${st.max_results_per_run}/search`
    : 'no APIFY_TOKEN in .env';
  $('#id-run').disabled = st.running || !st.token_configured;
  if (st.log.length) { $('#id-log').classList.remove('hidden'); $('#id-log').textContent = st.log.join('\n'); }
  if (st.running) { idTimer = setTimeout(idStatus, 4000); return; }
  if (idTimer && st.result) {
    toast(`Indeed: ${st.result.added} added, ${st.result.duplicates} already tracked`);
    loadAlertCount(); refreshHeadline();
  }
  if (idTimer && st.error) toast('Indeed sync failed — see log');
  idTimer = null;
}
$('#id-run').onclick = async () => {
  try { await api('/api/indeed/sync', { method: 'POST' }); idTimer = 1; idStatus(); }
  catch (e) { toast(e.message.slice(0, 160)); }
};
$('#id-urls').onclick = async () => {
  const { specs } = await api('/api/indeed/searches');
  $('#id-log').classList.remove('hidden');
  $('#id-log').textContent = specs.map(s => JSON.stringify(s)).join('\n');
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', idStatus);

// ---------------------------------------------------------------- naukri
let nkTimer = null;
async function nkStatus() {
  const st = await api('/api/naukri/status');
  $('#nk-meta').textContent = st.token_configured
    ? `token ✓ · ${st.search_count} searches · cap ${st.max_results_per_run}/search`
    : 'no APIFY_TOKEN in .env';
  $('#nk-run').disabled = st.running || !st.token_configured;
  if (st.log.length) { $('#nk-log').classList.remove('hidden'); $('#nk-log').textContent = st.log.join('\n'); }
  if (st.running) { nkTimer = setTimeout(nkStatus, 4000); return; }
  if (nkTimer && st.result) {
    toast(`Naukri: ${st.result.added} added, ${st.result.duplicates} already tracked`);
    loadAlertCount(); refreshHeadline();
  }
  if (nkTimer && st.error) toast('Naukri sync failed — see log');
  nkTimer = null;
}
$('#nk-run').onclick = async () => {
  try { await api('/api/naukri/sync', { method: 'POST' }); nkTimer = 1; nkStatus(); }
  catch (e) { toast(e.message.slice(0, 160)); }
};
$('#nk-urls').onclick = async () => {
  const { specs } = await api('/api/naukri/searches');
  $('#nk-log').classList.remove('hidden');
  $('#nk-log').textContent = specs.map(s => JSON.stringify(s)).join('\n');
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', nkStatus);

// ---------------------------------------------------------------- instahyre
let ihTimer = null;
async function ihStatus() {
  const st = await api('/api/instahyre/status');
  $('#ih-meta').textContent = st.token_configured
    ? `token ✓ · ${st.search_count} keyword×location combos · cap ${st.max_results_per_run}`
    : 'no APIFY_TOKEN in .env';
  $('#ih-run').disabled = st.running || !st.token_configured;
  if (st.log.length) { $('#ih-log').classList.remove('hidden'); $('#ih-log').textContent = st.log.join('\n'); }
  if (st.running) { ihTimer = setTimeout(ihStatus, 4000); return; }
  if (ihTimer && st.result) {
    toast(`Instahyre: ${st.result.added} added, ${st.result.duplicates} already tracked`);
    loadAlertCount(); refreshHeadline();
  }
  if (ihTimer && st.error) toast('Instahyre sync failed — see log');
  ihTimer = null;
}
$('#ih-run').onclick = async () => {
  try { await api('/api/instahyre/sync', { method: 'POST' }); ihTimer = 1; ihStatus(); }
  catch (e) { toast(e.message.slice(0, 160)); }
};
$('#ih-urls').onclick = async () => {
  const { input } = await api('/api/instahyre/searches');
  $('#ih-log').classList.remove('hidden');
  $('#ih-log').textContent = JSON.stringify(input, null, 2);
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', ihStatus);

// ---------------------------------------------------------------- wellfound
let wfTimer = null;
async function wfStatus() {
  const st = await api('/api/wellfound/status');
  $('#wf-meta').textContent = st.token_configured
    ? `token ✓ · ${st.search_count} searches · ${st.max_results_per_run} pages/search`
    : 'no APIFY_TOKEN in .env';
  $('#wf-run').disabled = st.running || !st.token_configured;
  if (st.log.length) { $('#wf-log').classList.remove('hidden'); $('#wf-log').textContent = st.log.join('\n'); }
  if (st.running) { wfTimer = setTimeout(wfStatus, 4000); return; }
  if (wfTimer && st.result) {
    toast(`Wellfound: ${st.result.added} added, ${st.result.duplicates} already tracked`);
    loadAlertCount(); refreshHeadline();
  }
  if (wfTimer && st.error) toast('Wellfound sync failed — see log');
  wfTimer = null;
}
$('#wf-run').onclick = async () => {
  try { await api('/api/wellfound/sync', { method: 'POST' }); wfTimer = 1; wfStatus(); }
  catch (e) { toast(e.message.slice(0, 160)); }
};
$('#wf-urls').onclick = async () => {
  const { specs } = await api('/api/wellfound/searches');
  $('#wf-log').classList.remove('hidden');
  $('#wf-log').textContent = specs.map(s => JSON.stringify(s)).join('\n');
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', wfStatus);

// ---------------------------------------------------------------- recruiter emails
async function oeStatus() {
  const st = await api('/api/outreach/status');
  $('#oe-meta').textContent = `${st.with_email} of ${st.total} postings have an address · `
    + `${st.drafted} drafted · ${st.sent} sent · `
    + (st.gmail_configured ? 'Gmail ✓' : 'no Gmail credentials in .env');
}
$('#oe-harvest').onclick = async () => {
  $('#oe-harvest').disabled = true;
  try {
    const r = await api('/api/outreach/harvest', { method: 'POST' });
    $('#oe-log').classList.remove('hidden');
    $('#oe-log').textContent = `Scanned ${r.scanned} descriptions, found ${r.found} new address(es).`;
    toast(`Found ${r.found} address(es)`); oeStatus(); loadApply();
  } catch (e) { toast(e.message.slice(0, 160)); }
  $('#oe-harvest').disabled = false;
};
document.querySelector('#tabs button[data-tab="import"]').addEventListener('click', oeStatus);

// ---------------------------------------------------------------- apply list
let COMPILER = null;
async function loadApply() {
  const p = new URLSearchParams();
  if ($('#a-stretch').checked) p.set('include_stretch', 'true');
  const { jobs, compiler } = await api('/api/apply-list?' + p);
  COMPILER = compiler;
  $('#applycount').textContent = jobs.length ? `(${jobs.length})` : '';
  $('#applylist').innerHTML = jobs.map(j => `
    <div class="card">
      <div><div class="score" style="font-size:22px">${fmt(j.final_score)}</div>
           <span class="badge ${bandClass(j.band)}">${esc(j.band)}</span></div>
      <div>
        <h4>${j.favourite ? '<span title="Starred: the kind of role you want" style="color:var(--worth)">★</span> ' : ''}${esc(j.role_title)}
          ${!j.favourite && j.like_favourite ? `<span class="chip ok" title="Resembles a starred role">like your ★ roles · ${Math.round((j.affinity||0)*100)}%</span>` : ''}</h4>
        <div class="small"><b>${esc(j.company)}</b> · ${esc(j.location || '')}${j.work_mode ? ' · ' + esc(j.work_mode) : ''}
          · ${esc(j.experience_asked || 'exp n/a')} · ${esc(j.comp_raw || j.comp_confidence || '')}</div>
        <div class="small muted" style="margin-top:4px">${esc(j.why_this_score || '')}</div>
        <div class="tiny muted" style="margin-top:4px">Posted ${esc(j.date_posted || '?')} (${esc(j.date_confidence)}) · ${esc(j.source || '')}
          ${j.resume_exists ? ' · <span style="color:var(--strong)">resume created</span>' : ''}
          ${j.cover_letter_exists ? ' · <span style="color:var(--strong)">cover letter created</span>' : ''}</div>
      </div>
      <div class="acts">
        <button class="btn primary js-res" data-id="${j.id}">Create resume</button>
        <button class="btn primary js-cov" data-id="${j.id}">Create cover letter</button>
        <button class="btn js-mail" data-id="${j.id}" title="${j.recruiter_email ? esc(j.recruiter_email) : 'no address found — you can paste one'}">${j.email_status === 'sent' ? '✓ Emailed' : 'Email recruiter'}</button>
        ${j.apply_url ? `<a class="btn" href="${esc(j.apply_url)}" target="_blank" rel="noopener">Apply ↗</a>` : ''}
        <button class="btn js-applied" data-id="${j.id}">Mark applied</button>
        <button class="btn js-not" data-id="${j.id}" title="Drops it off this list. Reversible — change the status back in Details.">Not interested</button>
        <button class="btn js-open" data-id="${j.id}">Details</button>
        <button class="btn js-star" data-id="${j.id}" data-on="${j.favourite ? 1 : 0}" title="Star = find more roles like this">${j.favourite ? '★ Starred' : '☆ Star'}</button>
      </div>
    </div>`).join('') || `<div class="panel muted">Nothing to apply for right now. Run a LinkedIn sync or import a research run — or tick “include Stretch”.</div>`;
  $$('#applylist .js-res').forEach(b => b.onclick = () => openResume(b.dataset.id));
  $$('#applylist .js-cov').forEach(b => b.onclick = () => openCoverLetter(b.dataset.id));
  $$('#applylist .js-mail').forEach(b => b.onclick = () => openEmail(b.dataset.id));
  $$('#applylist .js-open').forEach(b => b.onclick = () => openDrawer(b.dataset.id));
  $$('#applylist .js-star').forEach(b => b.onclick = () => toggleStar(b.dataset.id, b.dataset.on !== '1'));
  $$('#applylist .js-not').forEach(b => b.onclick = async () => {
    await api('/api/jobs/' + b.dataset.id, { method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ status: 'Do Not Apply' }) });
    toast('Marked not interested — it stays in Pipeline, change the status back any time');
    loadApply(); refreshHeadline();
  });
  $$('#applylist .js-applied').forEach(b => b.onclick = async () => {
    await api('/api/jobs/' + b.dataset.id, { method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ status: 'Applied' }) });
    toast('Marked applied — follow-up reminder starts in 10 days'); loadApply(); loadAlertCount(); refreshHeadline();
  });
}
$('#a-stretch').onchange = loadApply;

async function toggleStar(id, on) {
  const r = await api(`/api/jobs/${id}/favourite`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ favourite: on }) });
  toast(on ? `Starred — ${r.rescored} roles re-scored for similarity` : 'Unstarred');
  loadApply(); if (!$('#tab-pipeline').classList.contains('hidden')) loadJobs();
}

// ---------------------------------------------------------------- resume drawer
async function openResume(id) {
  const d = $('#drawer');
  d.classList.remove('hidden');
  d.innerHTML = '<p class="muted">Tailoring resume…</p>';
  const job = await api('/api/jobs/' + id);
  const r = await api(`/api/jobs/${id}/resume`, { method: 'POST' });
  const chips = (xs, cls) => xs.length ? xs.map(x => `<span class="chip ${cls}">${esc(x)}</span>`).join('') : '<span class="tiny muted">none</span>';
  d.innerHTML = `
    <div class="row"><button class="btn" id="rclose">close</button><div class="spacer"></div>
      <span class="tiny muted">${esc(r.filename)}</span></div>
    <h3 style="margin:12px 0 2px">Resume for ${esc(job.role_title)}</h3>
    <div class="small muted">${esc(job.company)} · archetype ${esc(r.archetype || 'default')} · headline: ${esc(r.headline.replace(/\\&/g, '&'))}</div>
    <p class="small" style="margin-top:12px"><b>JD terms your resume covers</b><br>${chips(r.coverage.covered, 'ok')}</p>
    <p class="small"><b>You have these — consider wording them in</b> <span class="tiny muted">(edit resume/master.json)</span><br>${chips(r.coverage.honest_additions, 'add')}</p>
    <p class="small"><b>Real gaps — not added, prepare an answer</b><br>${chips(r.coverage.real_gaps, 'gap')}</p>
    <div class="row" style="margin:12px 0">
      <button class="btn primary" id="r-dl">Download .tex</button>
      <button class="btn" id="r-copy">Copy LaTeX</button>
      ${r.compiler ? '<button class="btn" id="r-pdf">Download PDF</button>' : '<span class="tiny muted">no local LaTeX — use Overleaf</span>'}
      <form id="r-ol" action="https://www.overleaf.com/docs" method="post" target="_blank" style="display:inline">
        <input type="hidden" name="encoded_snip" id="r-snip"><input type="hidden" name="snip_name" value="${esc(r.filename)}">
        <button class="btn" type="submit">Open in Overleaf ↗</button>
      </form>
    </div>
    <details><summary class="small">Bullets selected per role</summary>
      ${Object.entries(r.selected).map(([co, bs]) => `<p class="tiny"><b>${esc(co)}</b><br>${bs.map(b => '• ' + esc(b) + '…').join('<br>')}</p>`).join('')}
    </details>
    <textarea id="r-tex" style="min-height:360px;margin-top:10px" spellcheck="false">${esc(r.tex)}</textarea>
    <p class="tiny muted">Every line comes from resume/master.json (your CV + facts you've stated). The generator selects and orders — it never writes new claims. Edit here before downloading if you want a one-off tweak.</p>`;
  $('#rclose').onclick = () => d.classList.add('hidden');
  $('#r-snip').value = encodeURIComponent(r.tex);
  $('#r-ol').onsubmit = () => { $('#r-snip').value = encodeURIComponent($('#r-tex').value); };
  $('#r-copy').onclick = async () => { await navigator.clipboard.writeText($('#r-tex').value).catch(() => {}); toast('LaTeX copied'); };
  $('#r-dl').onclick = () => {
    const blob = new Blob([$('#r-tex').value], { type: 'application/x-tex' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = r.filename; a.click();
  };
  if ($('#r-pdf')) $('#r-pdf').onclick = () => window.open(`/api/jobs/${id}/resume.pdf`, '_blank');
  if ($('#tab-apply') && !$('#tab-apply').classList.contains('hidden')) loadApply();
}

// ---------------------------------------------------------------- cover letter drawer
async function openCoverLetter(id) {
  const d = $('#drawer');
  d.classList.remove('hidden');
  d.innerHTML = '<p class="muted">Drafting cover letter…</p>';
  const job = await api('/api/jobs/' + id);
  const r = await api(`/api/jobs/${id}/cover-letter`, { method: 'POST' });
  const chips = (xs, cls) => xs.length ? xs.map(x => `<span class="chip ${cls}">${esc(x)}</span>`).join('') : '<span class="tiny muted">none</span>';
  d.innerHTML = `
    <div class="row"><button class="btn" id="clclose">close</button><div class="spacer"></div>
      <span class="tiny muted">${esc(r.filename)}</span></div>
    <h3 style="margin:12px 0 2px">Cover letter for ${esc(job.role_title)}</h3>
    <div class="small muted">${esc(job.company)} · archetype ${esc(r.archetype || 'default')}</div>
    <p class="small"><b>Gaps the JD scan flagged</b> <span class="tiny muted">(not written into the letter — the scan
      throws false positives; add a line yourself if one is genuinely worth naming)</span><br>${chips(r.real_gaps, 'gap')}</p>
    <div class="row" style="margin:12px 0">
      <button class="btn primary" id="cl-dl">Download .tex</button>
      <button class="btn" id="cl-copy">Copy LaTeX</button>
      ${r.compiler ? '<button class="btn" id="cl-pdf">Download PDF</button>' : '<span class="tiny muted">no local LaTeX — use Overleaf</span>'}
      <form id="cl-ol" action="https://www.overleaf.com/docs" method="post" target="_blank" style="display:inline">
        <input type="hidden" name="encoded_snip" id="cl-snip"><input type="hidden" name="snip_name" value="${esc(r.filename)}">
        <button class="btn" type="submit">Open in Overleaf ↗</button>
      </form>
    </div>
    <details open><summary class="small">Paragraphs generated</summary>
      ${r.paragraphs.map(p => `<p class="tiny">${esc(p)}</p>`).join('')}
    </details>
    <textarea id="cl-tex" style="min-height:360px;margin-top:10px" spellcheck="false">${esc(r.tex)}</textarea>
    <p class="tiny muted">Opening paragraph is the archetype summary from resume/master.json; body paragraphs are
      the roles whose bullets actually scored against this JD, turned into first-person sentences — nothing here is
      invented, and anything that didn't clear the relevance bar is left out rather than padded in. Read it before
      sending; mechanical composition reads more generic than a hand-written letter. Edit here before downloading
      if you want a one-off tweak.</p>`;
  $('#clclose').onclick = () => d.classList.add('hidden');
  $('#cl-snip').value = encodeURIComponent(r.tex);
  $('#cl-ol').onsubmit = () => { $('#cl-snip').value = encodeURIComponent($('#cl-tex').value); };
  $('#cl-copy').onclick = async () => { await navigator.clipboard.writeText($('#cl-tex').value).catch(() => {}); toast('LaTeX copied'); };
  $('#cl-dl').onclick = () => {
    const blob = new Blob([$('#cl-tex').value], { type: 'application/x-tex' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = r.filename; a.click();
  };
  if ($('#cl-pdf')) $('#cl-pdf').onclick = () => window.open(`/api/jobs/${id}/cover-letter.pdf`, '_blank');
  if ($('#tab-apply') && !$('#tab-apply').classList.contains('hidden')) loadApply();
}

// ---------------------------------------------------------------- email the recruiter
async function openEmail(id) {
  const d = $('#drawer');
  d.classList.remove('hidden');
  d.innerHTML = '<p class="muted">Composing — building the resume PDF…</p>';
  const job = await api('/api/jobs/' + id);
  let p;
  try {
    p = await api(`/api/jobs/${id}/email/preview`, { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({}) });
  } catch (e) { d.innerHTML = `<p style="color:var(--stretch)">Could not compose: ${esc(e.message)}</p>`; return; }

  d.innerHTML = `
    <div class="row"><button class="btn" id="mclose">close</button><div class="spacer"></div>
      <span class="tiny muted">${esc(job.company)}</span></div>
    <h3 style="margin:12px 0 2px">Email about ${esc(job.role_title)}</h3>
    ${p.already === 'sent' ? `<p class="small" style="color:var(--strong)">Already sent ${esc(p.sent_at || '')} — sending again needs a second confirmation.</p>` : ''}
    ${p.gmail_configured ? '' : `<p class="small" style="color:var(--stretch)">No Gmail credentials in <code>.env</code> yet —
      add <code>GMAIL_ADDRESS</code> and <code>GMAIL_APP_PASSWORD</code> (a Google
      <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">app password</a>, not your real one)
      to enable drafting and sending.</p>`}
    <p class="small" style="margin-top:10px"><b>To</b> ${p.to ? '' : '<span class="tiny muted">— none found for this posting; paste one</span>'}<br>
      <input id="m-to" style="width:100%" placeholder="recruiter@company.com" value="${esc(p.to || '')}"></p>
    <p class="small"><b>Subject</b><br><input id="m-subj" style="width:100%" value="${esc(p.subject)}"></p>
    <p class="small"><b>Attachments</b> ${p.attachments.map(a => `<span class="chip ok">${esc(a)}</span>`).join('')}
      <label class="tiny muted" style="margin-left:8px"><input type="checkbox" id="m-cl"> also attach the cover letter PDF</label></p>
    <textarea id="m-body" style="min-height:300px" spellcheck="true">${esc(p.body)}</textarea>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="m-queue">Queue for Claude to send</button>
      <button class="btn" id="m-draft" ${p.gmail_configured ? '' : 'disabled'}>Save to Gmail Drafts</button>
      <button class="btn" id="m-send" ${p.gmail_configured ? '' : 'disabled'}>Send now</button>
    </div>
    <p class="tiny muted"><b>Queue for Claude</b> writes this message (with the PDFs) to <code>data/outbox/</code>;
      ask Claude to send the outbox and it goes out through the Gmail connector — no app password needed, but Claude
      has to be the one to push it. The other two go straight from your own Gmail and need
      <code>GMAIL_APP_PASSWORD</code> in <code>.env</code>: <b>Drafts</b> lands in Gmail for you to send yourself,
      <b>Send now</b> delivers immediately. Read the body first — it is composed mechanically from your master
      resume, and a recruiter is a person, not an endpoint.</p>`;

  $('#mclose').onclick = () => d.classList.add('hidden');
  const payload = () => ({ to: $('#m-to').value.trim(), subject: $('#m-subj').value,
    body: $('#m-body').value, attach_cover_letter: $('#m-cl').checked });

  $('#m-queue').onclick = async () => {
    if (!$('#m-to').value.trim()) return toast('Add a recipient address first');
    $('#m-queue').disabled = true;
    try {
      const r = await api(`/api/jobs/${id}/email/queue`, { method: 'POST',
        headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload()) });
      toast(`Queued as ${r.file} — ask Claude to "send the outbox"`);
      d.classList.add('hidden'); loadApply();
    } catch (e) { toast(e.message.slice(0, 200)); $('#m-queue').disabled = false; }
  };

  $('#m-draft').onclick = async () => {
    if (!$('#m-to').value.trim()) return toast('Add a recipient address first');
    $('#m-draft').disabled = true;
    try {
      await api(`/api/jobs/${id}/email/draft`, { method: 'POST',
        headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload()) });
      toast('Saved to Gmail Drafts — review and send it from Gmail');
      d.classList.add('hidden'); loadApply();
    } catch (e) { toast(e.message.slice(0, 200)); $('#m-draft').disabled = false; }
  };

  $('#m-send').onclick = async () => {
    const to = $('#m-to').value.trim();
    if (!to) return toast('Add a recipient address first');
    if (!confirm(`Send this email to ${to} now?\n\nIt goes straight to their inbox from your Gmail — this cannot be undone.`)) return;
    $('#m-send').disabled = true;
    try {
      await api(`/api/jobs/${id}/email/send`, { method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...payload(), confirm: true, force: p.already === 'sent' }) });
      toast('Sent'); d.classList.add('hidden'); loadApply(); refreshHeadline();
    } catch (e) { toast(e.message.slice(0, 200)); $('#m-send').disabled = false; }
  };
}

// ---------------------------------------------------------------- boot
async function refreshHeadline() {
  const f = await api('/api/analytics/funnel');
  const cfg = await api('/api/config');
  $('#whoami').textContent = '· ' + cfg.profile.name;
  $('#headline').textContent =
    `${f.overall.logged} logged · ${f.overall.applied} applied · ${f.overall.screen} screens · ` +
    `${f.overall.offer} offers · floor INR ${(cfg.comp_floors.INR.hard_floor / 100000)} LPA`;
}
(async () => {
  META = await api('/api/meta');
  $('#f-status').insertAdjacentHTML('beforeend', META.statuses.map(s => `<option>${s}</option>`).join(''));
  $('#f-band').insertAdjacentHTML('beforeend', META.bands.map(s => `<option>${s}</option>`).join(''));
  $('#f-arch').insertAdjacentHTML('beforeend',
    Object.entries(META.archetypes).map(([k, v]) => `<option value="${k}">${k} — ${v}</option>`).join(''));
  await Promise.all([loadApply(), loadAlertCount(), refreshHeadline()]);
})();
