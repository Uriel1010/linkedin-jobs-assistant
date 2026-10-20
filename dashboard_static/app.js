'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={inbox:'M3 4h18v16H3z M3 13h5l2 3h4l2-3h5',bookmark:'M6 3h12v18l-6-4-6 4z',columns:'M3 4h18v16H3z M9 4v16 M15 4v16',archive:'M3 3h18v5H3z M5 8v13h14V8 M9 12h6',radar:'M12 12l8-8 M12 3a9 9 0 1 0 9 9 M12 7a5 5 0 1 0 5 5',logout:'M9 4H4v16h5 M10 12h11 M17 8l4 4-4 4',lock:'M6 10h12v11H6z M8 10V6a4 4 0 0 1 8 0v4',refresh:'M20 7a9 9 0 1 0 1 9 M20 2v6h-6',search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',filter:'M3 5h18 M6 12h12 M9 19h6 M8 2v6 M16 9v6 M11 16v6',download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',pin:'M12 21s7-7 7-12a7 7 0 0 0-14 0c0 5 7 12 7 12z M12 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6',close:'M6 6l12 12 M18 6 6 18',restore:'M4 4v6h6 M4 10a8 8 0 1 1 1 8',arrow:'M5 19 19 5 M6 5h13v13',check:'M4 12l5 5L20 6'};
const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.inbox}"/></svg>`;
document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
const labels={inbox:'Job inbox',saved:'Shortlist',dismissed:'Dismissed',applications:'Applications',searches:'Searches & health',settings:'Settings'};
const stages={saved:'Saved',reviewing:'Reviewing',applied:'Applied',interviewing:'Interviewing',offer:'Offer',rejected:'Rejected',withdrawn:'Withdrawn',archived:'Archived'};
const roles={embedded:'Embedded',software:'Software',backend:'Backend',frontend:'Frontend',fullstack:'Full stack',qa:'QA & testing',other:'Other',devops:'DevOps',data:'Data',ml:'AI / ML',research:'Research',security:'Security',support:'Support',management:'Management'};
const state={view:'inbox',offset:0,items:[],selected:null,checked:new Set(),apps:[],demo:false,listSeq:0,detailSeq:0};
let toastTimer,undoAction,editorAction,searchTimer;
function toast(text,undo=null){clearTimeout(toastTimer);$('toast').querySelector('span').textContent=text;$('toast').hidden=false;$('undo').hidden=!undo;undoAction=undo;toastTimer=setTimeout(()=>$('toast').hidden=true,undo?15000:6000)}
function error(err){$('global-error').textContent=err.message||'Something went wrong. Please try again.';$('global-error').hidden=false}
async function api(path,method='GET',payload){
  const response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-Dashboard-Request':'1'},body:payload===undefined?undefined:JSON.stringify(payload)});
  let data;try{data=await response.json()}catch{throw new Error('The server returned an unexpected response. Try again.')}
  if(!response.ok){if(response.status===401&&path!='/login'){showLogin()}throw new Error(typeof data.detail==='string'?data.detail:'Please check your input and try again.')}
  return data;
}
function showLogin(){$('app').hidden=true;$('login').hidden=false;$('access-key').focus()}
function filters(){const p=new URLSearchParams({view:state.view,offset:state.offset});['query','role','level','mode','days','sort'].forEach(id=>p.set(id==='query'?'q':id,$(id).value));if($('unread').checked)p.set('unread','1');return p}
function persistFilters(){const values={};['query','role','level','mode','days','sort'].forEach(id=>values[id]=$(id).value);values.unread=$('unread').checked;localStorage.setItem('jobspace.filters',JSON.stringify(values))}
function age(value){if(!value)return 'Date unknown';const n=(Date.now()-new Date(value))/3600000;if(!Number.isFinite(n))return 'Date unknown';if(n<1)return 'Just discovered';if(n<24)return `${Math.floor(n)}h ago`;return `${Math.floor(n/24)}d ago`}
function date(value){if(!value)return 'Not specified';const d=new Date(value);return Number.isNaN(d.getTime())?'Not specified':d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})}
function postingDate(job){if(/reposted|פורסם מחדש/i.test(job.posted_text||''))return 'Reposted · original date unknown';if(job.posted_at)return date(job.posted_at);return job.posted_text?`${job.posted_text} · observed ${date(job.posting_observed_at||job.first_seen)}`:'Not provided by LinkedIn'}
function clock(value){if(!value)return 'Waiting for first run';const d=new Date(value);return Number.isNaN(d.getTime())?'Not scheduled':d.toLocaleString(undefined,{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
function initials(company){return (company||'?').split(/\s+/).slice(0,2).map(s=>s[0]).join('').toUpperCase()}
function saved(job){return job.application&&!['archived','withdrawn','rejected'].includes(job.application.status)}
function level(value){return ({mid:'Mid-level',principal:'Staff / Principal',unknown:'',intern:'Intern / Student'})[value]??(value?value[0].toUpperCase()+value.slice(1):'')}
function validURL(value){try{const u=new URL(value);return u.protocol==='https:'&&['linkedin.com','www.linkedin.com'].includes(u.hostname)?u.href:null}catch{return null}}
function renderCards(){
  $('job-list').innerHTML=state.items.length?state.items.map(job=>`<article class="job-card ${state.selected===job.id?'selected':''}" data-job="${esc(job.id)}">
    <div class="card-top"><input type="checkbox" data-check="${esc(job.id)}" aria-label="Select ${esc(job.title)}" ${state.checked.has(job.id)?'checked':''}><span class="company-logo" aria-hidden="true">${esc(initials(job.company))}</span><span class="company-name" dir="auto">${esc(job.company||'Company not listed')}</span>${!job.seen?'<span class="new-dot">UNREAD</span>':''}<button class="icon-button ${saved(job)?'saved':''}" data-save="${esc(job.id)}" aria-label="${saved(job)?'Edit saved application':'Save job'}" title="${saved(job)?'Edit application':'Save to shortlist'}">${icon('bookmark')}</button></div>
    <button class="card-open" data-open="${esc(job.id)}" dir="auto">${esc(job.title)}</button>
    <div class="card-meta">${icon('pin')}<span dir="auto">${esc(job.location||'Location not listed')}${job.work_arrangement?' · '+esc(job.work_arrangement):''}</span></div>
    <div class="card-bottom"><span class="pill">${esc(roles[job.role]||job.role)}</span>${level(job.level)?`<span class="pill">${esc(level(job.level))}</span>`:''}${saved(job)?`<span class="pill stage">${esc(stages[job.application.status])}</span>`:''}<span class="card-age" title="Discovered ${esc(clock(job.first_seen))}">${esc(age(job.first_seen))}</span><button class="icon-button card-dismiss" data-dismiss="${esc(job.id)}" aria-label="${job.dismissed?'Restore job':'Dismiss job'}" title="${job.dismissed?'Restore to inbox':'Dismiss from inbox'}">${icon(job.dismissed?'restore':'close')}</button></div></article>`).join(''):`<div class="empty"><div class="large-symbol">${state.view==='dismissed'?'✓':'↗'}</div><h2>${state.view==='saved'?'Make room for the possibilities.':state.view==='dismissed'?'Nothing dismissed here.':'A fresh perspective?'}</h2><p>${state.view==='saved'?'Save an interesting job from your inbox to start your shortlist.':state.view==='dismissed'?'Dismissed jobs stay here until you restore them.':'No jobs match these filters. Try a broader search or reset your filters.'}</p><button class="secondary" data-reset>Reset filters</button></div>`;
  $('bulk-dismiss').hidden=!state.checked.size;
  $('bulk-dismiss').textContent=`${state.view==='dismissed'?'Restore':'Dismiss'} ${state.checked.size} selected`;
  $('select-page').textContent=state.items.length&&state.checked.size===state.items.length?'Clear selection':'Select page';
}
async function loadJobs(){
  const seq=++state.listSeq;
  $('global-error').hidden=true;$('job-list').setAttribute('aria-busy','true');
  if(!state.items.length)$('job-list').innerHTML='<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try{
    const data=await api('/jobs?'+filters());
    if(seq!==state.listSeq)return;
    if(data.total&&state.offset>=data.total){state.offset=Math.floor((data.total-1)/24)*24;return loadJobs()}
    state.items=data.items;state.checked.clear();
    for(const key of ['inbox','saved','dismissed'])$('count-'+key).textContent=data.counts[key].toLocaleString();
    $('stat-inbox').textContent=data.counts.inbox.toLocaleString();$('stat-saved').textContent=data.counts.saved.toLocaleString();
    $('results-count').textContent=`${data.total.toLocaleString()} ${data.total===1?'opportunity':'opportunities'}${state.view==='saved'?' on your shortlist':state.view==='dismissed'?' dismissed':''}`;
    renderCards();$('job-list').setAttribute('aria-busy','false');
    $('pagination').innerHTML=data.total>24?`<button class="secondary" data-page="${Math.max(0,state.offset-24)}" ${!state.offset?'disabled':''}>← Previous</button><span>${Math.floor(state.offset/24)+1} / ${Math.ceil(data.total/24)}</span><button class="secondary" data-page="${state.offset+24}" ${state.offset+24>=data.total?'disabled':''}>Next →</button>`:'';
  }catch(err){if(seq===state.listSeq){$('job-list').setAttribute('aria-busy','false');$('job-list').innerHTML='<div class="empty"><h2>Couldn’t load your jobs.</h2><p>Your work is saved on the server.</p><button class="secondary" data-retry>Try again</button></div>';error(err)}}
}
async function loadOverview(){try{const data=await api('/overview');$('stat-progress').textContent=['applied','interviewing','offer'].reduce((n,k)=>n+(data.applications_by_status[k]||0),0)}catch(err){error(err)}}
function closeDetail(){state.selected=null;state.detailSeq++;$('detail-panel').classList.remove('open');$('detail-panel').innerHTML='<div class="detail-placeholder"><span class="large-symbol">↗</span><h2>Good things ahead.</h2><p>Select an opportunity to read the details, take a note, or make your next move.</p><span class="pill">Your search, at your pace.</span></div>';renderCards()}
async function openJob(id){
  const seq=++state.detailSeq;state.selected=id;renderCards();
  const panel=$('detail-panel');panel.classList.add('open');panel.innerHTML='<div class="detail-body"><button class="icon-button" data-close-detail aria-label="Close job details">×</button><p>Loading job details…</p></div>';
  try{
    const job=await api('/jobs/'+encodeURIComponent(id));if(seq!==state.detailSeq)return;
    const item=state.items.find(j=>j.id===id)||{};job.application=item.application;
    const url=validURL(job.url);
    panel.innerHTML=`<div class="detail-head"><div class="detail-top"><span class="company-logo">${esc(initials(job.company))}</span><button class="icon-button" data-close-detail aria-label="Close job details">${icon('close')}</button></div><h2 dir="auto">${esc(job.title)}</h2><div class="detail-company" dir="auto">${esc(job.company)}<br>${esc(job.location||'Location not specified')}</div><div class="detail-tags">${[job.work_arrangement,job.employment_type,job.seniority].filter(Boolean).map(t=>`<span class="pill">${esc(t)}</span>`).join('')}</div><div class="detail-actions">${url&&!state.demo?`<a class="primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">View on LinkedIn ${icon('arrow')}</a>`:'<button class="primary" disabled>Example listing ↗</button>'}<button class="secondary" data-save="${esc(id)}">${icon('bookmark')} ${saved(job)?'Edit application':'Save job'}</button><button class="icon-button" data-dismiss="${esc(id)}" aria-label="${item.dismissed?'Restore':'Dismiss'} job" title="${item.dismissed?'Restore':'Dismiss'}">${icon(item.dismissed?'restore':'archive')}</button></div></div><div class="detail-body">${saved(job)?`<div class="application-strip"><span class="pill stage">${esc(stages[job.application.status])}</span><span>${job.application.follow_up_at?'Follow up '+esc(date(job.application.follow_up_at)):'On your shortlist'}</span><button class="text-button" data-edit-app="${job.application.id}">Notes & next step →</button></div>`:''}<dl class="detail-facts"><div><dt>Discovered</dt><dd>${esc(date(job.first_seen))}</dd></div><div><dt>Source posting date</dt><dd>${esc(postingDate(job))}</dd></div></dl><h3>About this opportunity</h3><div class="description" dir="auto">${esc(job.description||'The source description is not available yet. Open the original listing on LinkedIn for the full details.')}</div></div>`;
    if(item&&!item.seen){await api('/job-state','POST',{ids:[id],seen:true});item.seen=1;renderCards()}
    panel.scrollTop=0;
  }catch(err){if(seq===state.detailSeq){panel.innerHTML='<div class="detail-body"><button class="icon-button" data-close-detail aria-label="Close job details">×</button><p>Couldn’t load this opportunity. Please try again.</p></div>';error(err)}}
}
async function dismissJobs(ids,target){
  const previous=state.items.filter(j=>ids.includes(j.id)).map(j=>({id:j.id,dismissed:!!j.dismissed}));
  const dismissed=target===undefined?state.view!=='dismissed':target;
  await api('/job-state','POST',{ids,dismissed});
  if(ids.includes(state.selected))closeDetail();
  await loadJobs();
  toast(`${ids.length} ${ids.length===1?'job':'jobs'} ${dismissed?'dismissed':'restored'}.`,async()=>{
    for(const value of [false,true]){const group=previous.filter(j=>j.dismissed===value).map(j=>j.id);if(group.length)await api('/job-state','POST',{ids:group,dismissed:value})}
    await loadJobs();toast('Restored the previous state.');
  });
}
async function saveJob(id){
  const job=state.items.find(j=>j.id===id);
  if(saved(job)){return editApplication(job.application)}
  if(job.application)await api('/applications/'+job.application.id,'PATCH',{status:'saved'});
  else await api('/applications','POST',{job_id:id,status:'saved'});
  toast('Added to your shortlist.');await loadJobs();await loadOverview();if(state.selected===id)await openJob(id);
}
function showEditor(title,fields,action){$('editor-title').textContent=title;$('editor-fields').innerHTML=fields;editorAction=action;$('editor').showModal()}
function editApplication(application){
  const a=application;const follow=a.follow_up_at?new Date(new Date(a.follow_up_at)-new Date(a.follow_up_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  showEditor('Your next step',`<p dir="auto">${esc(a.title||'Saved opportunity')} · ${esc(a.company||'')}</p><label for="app-stage">Application stage</label><select id="app-stage">${Object.entries(stages).map(([key,title])=>`<option value="${key}" ${a.status===key?'selected':''}>${title}</option>`).join('')}</select><label for="app-notes">Private notes</label><textarea id="app-notes" maxlength="20000" placeholder="What stands out? What do you want to ask?">${esc(a.notes||'')}</textarea><label for="app-followup">Follow-up reminder (your local time)</label><input id="app-followup" type="datetime-local" value="${esc(follow)}"><label for="app-cv">CV version / reference</label><input id="app-cv" maxlength="300" placeholder="e.g. Embedded engineering CV" value="${esc(a.cv_version||'')}"><p>Follow-up dates are shared with your Telegram bot. Saving or changing a stage does not submit an application.</p>`,async()=>{
    await api('/applications/'+a.id,'PATCH',{status:$('app-stage').value,notes:$('app-notes').value,follow_up_at:$('app-followup').value?new Date($('app-followup').value).toISOString():null,cv_version:$('app-cv').value||null});
    toast('Application updated.');if(state.view==='applications')await loadBoard();else{await loadJobs();if(state.selected)await openJob(state.selected)}await loadOverview();
  });
}
async function loadBoard(){
  const data=await api('/applications');state.apps=data.items;
  const visible=$('board-filter').value==='all'?Object.keys(stages):['saved','reviewing','applied','interviewing','offer'];
  $('board').style.gridTemplateColumns=`repeat(${visible.length},minmax(190px,1fr))`;
  $('board').innerHTML=visible.map(stage=>{const list=data.items.filter(a=>a.status===stage);return `<section class="board-column"><h2>${esc(stages[stage])}<span>${list.length}</span></h2>${list.map(a=>`<button class="board-card" data-edit-app="${a.id}"><small dir="auto">${esc(a.company)}</small><h3 dir="auto">${esc(a.title)}</h3>${a.follow_up_at?`<p>Follow up · ${esc(date(a.follow_up_at))}</p>`:''}${a.notes?'<span class="pill">Has notes</span>':''}</button>`).join('')||'<p class="column-empty">Your next opportunity<br>could land here.</p>'}</section>`}).join('');
}
async function loadSearches(){
  const [searches,overview,alerts]=await Promise.all([api('/searches'),api('/overview'),api('/alerts')]);
  const network=overview.collector.network||{};
  const status=network.blocked_session?'LinkedIn sign-in required':network.retry_after>Date.now()/1000?'Collection cooling down':searches.some(s=>s.last_success)?'Collection has completed successfully':'Waiting for first collection';
  $('health-summary').innerHTML=`<span class="pill">${esc(status)}</span><span class="pill">${searches.filter(s=>s.enabled).length} active searches</span><span class="pill">${overview.jobs} stored jobs</span><span class="pill">${overview.follow_ups_due} follow-ups due</span>`;
  $('search-list').innerHTML=searches.map(s=>`<article class="search-card"><span class="pill ${s.enabled?'stage':''}">${s.enabled?'Active':'Paused'} · Every ${s.interval_minutes} min</span><h3 dir="auto">${esc(s.keywords)}</h3><p>${esc(s.location||'Any location')}<br>Last success · ${esc(clock(s.last_success))}<br>Next run · ${s.enabled?esc(clock(s.next_due)):'Paused'}</p><div class="search-actions"><button class="secondary" data-poll="${s.id}">${icon('refresh')} Collect now</button><button class="text-button" data-toggle-search="${s.id}" data-enabled="${!s.enabled}">${s.enabled?'Pause search':'Resume search'}</button></div></article>`).join('')||'<div class="empty"><h2>Start with a search.</h2><p>Add a role and location. Your server will check for new opportunities hourly.</p></div>';
  $('alert-list').innerHTML=alerts.length?alerts.map(a=>`<article class="notice"><p>${esc(a.message)}<small>${esc(clock(a.created_at))}${a.acknowledged_at?' · Acknowledged':''}</small></p>${a.acknowledged_at?'':`<button class="secondary" data-ack="${a.id}">Acknowledge</button>`}</article>`).join(''):'<div class="notice"><span class="pill stage">All clear</span><p>No open service notices.</p></div>';
}
async function changeView(view){
  if(state.view==='settings' && settingsDirty && !confirm('Leave without saving your settings?'))return;
  settingsDirty=false;
  state.view=view;state.offset=0;state.checked.clear();state.listSeq++;closeDetail();$('global-error').hidden=true;
  document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  $('breadcrumb').textContent=labels[view];
  $('page-title').textContent={inbox:'Your next chapter.',saved:'The ones worth a closer look.',dismissed:'A little less noise.',applications:'Keep your next move moving.',settings:'A search that fits your life.',searches:'Your search, on autopilot.'}[view];
  $('page-subtitle').textContent={inbox:'A focused place to find, consider, and make your next move.',saved:'Your considered shortlist. Give each possibility a little space.',dismissed:'Set aside for now. Every opportunity can be restored.',applications:'From first interest to the next conversation. One step at a time.',settings:'Choose what reaches you, and when. You’re in control.',searches:'Set the direction. Your server keeps an eye on new opportunities.'}[view];
  const list=['inbox','saved','dismissed'].includes(view);
  $('settings-view').hidden=view!=='settings';$('jobs-view').hidden=!list;$('applications-view').hidden=view!=='applications';$('searches-view').hidden=view!=='searches';$('export').hidden=!list;
  if(list){state.items=[];await loadJobs();await loadOverview()}else if(view==='applications')await loadBoard();else if(view==='settings')await loadSettings();else await loadSearches();
}
async function run(action,button){if(button)button.disabled=true;try{await action()}catch(err){error(err);toast(err.message)}finally{if(button)button.disabled=false}}
document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.view)return run(()=>changeView(button.dataset.view),button);
  if(button.dataset.open)return run(()=>openJob(button.dataset.open));
  if(button.dataset.save)return run(()=>saveJob(button.dataset.save),button);
  if(button.dataset.dismiss){const job=state.items.find(j=>j.id===button.dataset.dismiss);return run(()=>dismissJobs([button.dataset.dismiss],!job.dismissed),button)}
  if(button.hasAttribute('data-close-detail'))return closeDetail();
  if(button.dataset.editApp){const a=[...state.apps,...state.items.map(j=>j.application).filter(Boolean)].find(a=>String(a.id)===button.dataset.editApp);if(a)editApplication(a);return}
  if(button.dataset.page!==undefined){state.offset=Number(button.dataset.page);return run(async()=>{await loadJobs();$('results-count').scrollIntoView({block:'start'})},button)}
  if(button.hasAttribute('data-reset'))return resetFilters();
  if(button.hasAttribute('data-retry'))return run(loadJobs,button);
  if(button.dataset.poll)return run(async()=>{await api('/searches/'+button.dataset.poll+'/poll','POST');toast('Collection requested. Results will arrive when it completes.');await loadSearches()},button);
  if(button.dataset.toggleSearch)return run(async()=>{await api('/searches/'+button.dataset.toggleSearch,'PATCH',{enabled:button.dataset.enabled==='true'});await loadSearches()},button);
  if(button.dataset.ack)return run(async()=>{await api('/alerts/'+button.dataset.ack+'/acknowledge','POST');await loadSearches()},button);
});
$('job-list').addEventListener('change',e=>{if(e.target.dataset.check){if(e.target.checked)state.checked.add(e.target.dataset.check);else state.checked.delete(e.target.dataset.check);renderCards()}});
$('select-page').onclick=()=>{if(state.checked.size===state.items.length)state.checked.clear();else state.items.forEach(j=>state.checked.add(j.id));renderCards()};
$('bulk-dismiss').onclick=e=>run(()=>dismissJobs([...state.checked]),e.currentTarget);
$('undo').onclick=e=>{const fn=undoAction;undoAction=null;if(fn)run(fn,e.currentTarget)};
function resetFilters(){['role','level','mode','days'].forEach(id=>$(id).value='all');$('query').value='';$('sort').value='newest';$('unread').checked=false;state.offset=0;persistFilters();run(loadJobs)}
function applyFilters(){state.offset=0;persistFilters();run(loadJobs)}
$('clear-filters').onclick=resetFilters;
['role','level','mode','days','sort','unread'].forEach(id=>$(id).onchange=applyFilters);
$('query').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(applyFilters,300)};
$('filter-toggle').onclick=()=>{const open=$('filters').classList.toggle('open');$('filter-toggle').setAttribute('aria-expanded',String(open))};
$('refresh').onclick=e=>run(()=>changeView(state.view),e.currentTarget);
$('export').onclick=()=>{window.location.href='/api/export?'+filters()};
$('board-filter').onchange=()=>run(loadBoard);
$('logout').onclick=()=>run(async()=>{await api('/logout','POST');showLogin()});
$('close-editor').onclick=$('cancel-editor').onclick=()=>$('editor').close();
$('editor-form').onsubmit=e=>{e.preventDefault();run(async()=>{await editorAction();$('editor').close()},e.submitter)};
$('add-search').onclick=()=>showEditor('Find your next opportunity','<p>Add a title and location. Collection runs hourly on your server.</p><label for="search-title">Job title or keywords</label><input id="search-title" maxlength="200" required placeholder="e.g. Embedded Software Engineer"><label for="search-location">Location</label><input id="search-location" maxlength="200" placeholder="e.g. Israel">',async()=>{await api('/searches','POST',{keywords:$('search-title').value.trim(),location:$('search-location').value.trim()});await loadSearches();toast('Search added. Hourly collection is enabled.')});
$('login-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('login-error').textContent='';try{await api('/login','POST',{key:$('access-key').value});$('access-key').value='';await boot()}catch(err){$('login-error').textContent=err.message}finally{button.disabled=false}};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('editor').open)closeDetail();if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)&&!$('editor').open){e.preventDefault();$('query').focus()}});
async function boot(){
  const session=await api('/session');state.demo=session.demo;$('demo-badge').hidden=!session.demo;
  if(!session.authenticated)return showLogin();
  $('login').hidden=true;$('app').hidden=false;
  try{const f=JSON.parse(localStorage.getItem('jobspace.filters')||'{}');['query','role','level','mode','days','sort'].forEach(id=>{if(typeof f[id]==='string')$(id).value=f[id];if(!$(id).value&&id!=='query')$(id).selectedIndex=0});$('unread').checked=!!f.unread}catch{}
  await changeView('inbox');
}
boot().catch(err=>{showLogin();$('login-error').textContent=err.message});
