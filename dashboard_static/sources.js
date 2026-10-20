'use strict';
let publicSources=[];
const sourceNames={greenhouse:'Greenhouse',lever:'Lever',lever_eu:'Lever EU',ashby:'Ashby'};
function sourceTime(value){return value?clock(new Date(value*1000).toISOString()):'Not yet'}
async function loadSources(){
  const [data,alerts]=await Promise.all([api('/sources'),api('/alerts')]);publicSources=data.items;
  $('health-summary').innerHTML=`<span class="pill ${data.worker_alive?'stage':''}">${data.worker_alive?'Public-feed worker online':'Public-feed worker unavailable'}</span><span class="pill">LinkedIn automation off</span><span class="pill">Email not connected</span>`;
  $('search-list').innerHTML=data.items.map(s=>`<article class="search-card"><div class="source-heading"><span class="pill ${s.enabled&&!s.blocked?'stage':''}">${!s.enabled?'Paused':s.blocked?'Review required':esc(s.status)}</span><span class="pill">${esc(sourceNames[s.provider]||s.provider)}</span></div><h3>${esc(s.company)}</h3><p>${esc(s.location_filter||'All locations')} · Every ${s.interval_minutes} min<br>${s.jobs_count} current jobs · ${s.added} added on last success<br>Last success · ${esc(sourceTime(s.last_success))}<br>Next attempt · ${s.enabled&&!s.blocked?esc(sourceTime(Math.max(s.next_due,s.retry_after))):'Paused'}<br>${s.requests_today} / ${s.daily_budget} requests in the last 24h</p>${s.error?`<p class="source-warning" role="status">${esc(s.error.replaceAll('_',' '))}${s.blocked?' · Review before resuming. Existing jobs are preserved.':''}</p>`:''}<div class="search-actions"><button class="secondary" data-source-poll="${s.id}" ${!s.enabled||s.blocked?'disabled':''}>Collect now</button><button class="text-button" data-source-edit="${s.id}">Schedule & limits</button><button class="text-button" data-source-history="${s.id}">Run history</button>${s.blocked?`<button class="text-button" data-source-resume="${s.id}">Resume after review</button>`:`<button class="text-button" data-source-toggle="${s.id}">${s.enabled?'Pause':'Resume'}</button>`}</div></article>`).join('')||'<div class="empty"><h2>Connect an employer board.</h2><p>Add a public Greenhouse, Lever, or Ashby board to begin collecting without a LinkedIn session.</p></div>';
  $('alert-list').innerHTML=alerts.length?alerts.map(a=>`<article class="notice"><p>${esc(a.message)}<small>${esc(clock(a.created_at))}${a.acknowledged_at?' · Acknowledged':''}</small></p>${a.acknowledged_at?'':`<button class="secondary" data-ack="${a.id}">Acknowledge</button>`}</article>`).join(''):'<div class="notice"><span class="pill stage">All clear</span><p>No open service notices.</p></div>';
}
function sourceEditor(s){
  showEditor('Schedule & request limits',`<p>${esc(s.company)} · ${esc(sourceNames[s.provider])}</p><label for="source-interval">Collection interval (minutes)</label><input id="source-interval" type="number" min="60" max="10080" required value="${s.interval_minutes}"><label for="source-budget">Maximum requests per rolling 24 hours</label><input id="source-budget" type="number" min="1" max="200" required value="${s.daily_budget}"><label for="source-pages">Maximum requests in one collection</label><input id="source-pages" type="number" min="1" max="20" required value="${s.max_requests}"><p>Each HTTP page counts. Telegram delivery has its own interval in Settings. Provider cooldowns still apply.</p>`,async()=>{
    await api('/sources/'+s.id,'PATCH',{revision:s.revision,interval_minutes:Number($('source-interval').value),daily_budget:Number($('source-budget').value),max_requests:Number($('source-pages').value)});await loadSources();toast('Source settings saved.');
  });
}
$('add-source').onclick=()=>showEditor('Add an employer board',`<p>Use the board name from the employer’s public careers link. Example: jobs.eu.lever.co/mobileye → mobileye. No LinkedIn account is used.</p><label for="source-provider">Provider</label><select id="source-provider">${Object.entries(sourceNames).map(([v,n])=>`<option value="${v}">${n}</option>`).join('')}</select><label for="source-board">Board name</label><input id="source-board" required maxlength="100" pattern="[A-Za-z0-9_-]+" placeholder="e.g. mobileye"><label for="source-company">Company display name</label><input id="source-company" required maxlength="120" placeholder="Company name"><label for="source-location">Location contains</label><input id="source-location" maxlength="100" value="Israel"><p>The first successful collection establishes a baseline: jobs appear in the dashboard, but only later discoveries enter the notification queue. Starts with a two-hour schedule.</p>`,async()=>{
  await api('/sources','POST',{provider:$('source-provider').value,board:$('source-board').value.trim(),company:$('source-company').value.trim(),location_filter:$('source-location').value.trim()});await loadSources();toast('Employer source added.');
});
document.addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  const id=button.dataset.sourcePoll||button.dataset.sourceEdit||button.dataset.sourceToggle||button.dataset.sourceResume||button.dataset.sourceHistory;
  if(!id)return;
  const s=publicSources.find(s=>String(s.id)===id);if(!s)return;
  if(button.dataset.sourceEdit)return sourceEditor(s);
  if(button.dataset.sourceHistory)return run(async()=>{
    const data=await api('/sources/'+id+'/runs');
    showEditor('Recent collections',data.items.map(r=>`<p><strong>${esc(sourceTime(r.started))} · ${esc(r.status)}</strong><br>${r.jobs_count} jobs · ${r.added} added${r.error?'<br>'+esc(r.error.replaceAll('_',' ')):''}</p>`).join('')||'<p>No collections yet.</p>',async()=>{});
  },button);
  if(button.dataset.sourceResume&&!confirm('Resume this source after reviewing its failure? Cooldowns and request budgets remain in effect.'))return;
  run(async()=>{
    if(button.dataset.sourceToggle)await api('/sources/'+id,'PATCH',{revision:s.revision,enabled:!s.enabled});
    else await api('/sources/'+id+(button.dataset.sourceResume?'/resume':'/poll'),'POST');
    await loadSources();toast(button.dataset.sourceToggle?'Source updated.':'Collection queued. Check Run history for its result.');
  },button);
});
