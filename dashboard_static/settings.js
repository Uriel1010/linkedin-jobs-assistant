'use strict';
let settingsDirty=false,settingsSnapshot=null;
const settingChoices=(name,values,selected)=>`<div class="setting-choices">${Object.entries(values).map(([value,label])=>`<label><input type="checkbox" name="${name}" value="${esc(value)}" ${selected.includes(value)?'checked':''}><span>${esc(label)}</span></label>`).join('')}</div>`;
const settingOptions=(values,selected)=>Object.entries(values).map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${esc(label)}</option>`).join('');
const settingTime=n=>`${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
function renderSettings(data){
  const p=data.preferences,q=data.quiet_hours;
  $('settings-content').innerHTML=`
    <section class="settings-card"><div class="settings-section-title"><span class="settings-number">01</span><div><h2>What you’re looking for</h2><p>Choose the opportunities you want in your Telegram feed.</p></div></div>
      <fieldset><legend>Disciplines</legend>${settingChoices('pref-roles',{all:'All disciplines',...roles},p.roles)}</fieldset>
      <fieldset><legend>Experience</legend>${settingChoices('pref-levels',{all:'All levels',intern:'Intern / Student',junior:'Junior',mid:'Mid-level',senior:'Senior',lead:'Lead',principal:'Staff / Principal'},p.levels)}</fieldset>
      <div class="settings-fields"><label>Location scope<select id="pref-region">${settingOptions({israel:'Israel explicitly listed',broad:'Broader regions & remote'},p.region)}</select></label>
      <label>Maximum posting age (hours)<input id="pref-age" type="number" min="1" max="168" required value="${p.max_age_hours}"><small>1–168 hours. Applied when the source provides a usable posting age.</small></label>
      <label>Other titles<select id="pref-other">${settingOptions({technical:'Related technical roles',all:'Any uncategorized role'},p.other_scope)}</select></label>
      <label class="settings-check"><input id="pref-unknown" type="checkbox" ${p.keep_unknown_level?'checked':''}>Keep jobs with unspecified experience</label></div>
    </section>
    <section class="settings-card"><div class="settings-section-title"><span class="settings-number">02</span><div><h2>A little less noise</h2><p>Comma-separated keywords, in English or Hebrew. Up to 20 per field.</p></div></div>
      <div class="settings-fields"><label>Include keywords<textarea id="pref-include" dir="auto" maxlength="2019" placeholder="e.g. Linux, firmware">${esc(p.include.join(', '))}</textarea><small>Leave empty for no keyword requirement.</small></label>
      <label>Exclude keywords<textarea id="pref-exclude" dir="auto" maxlength="2019" placeholder="e.g. marketing, frontend">${esc(p.exclude.join(', '))}</textarea><small>Matching exclusions remove a job from the feed.</small></label></div>
      <p class="settings-hint">Changing filters rechecks previously filtered jobs. Already-delivered jobs stay deduplicated.</p>
    </section>
    <section class="settings-card"><div class="settings-section-title"><span class="settings-number">03</span><div><h2>On your schedule</h2><p>Collection continues while notifications are paused or quiet.</p></div></div>
      <label class="settings-check"><input id="pref-notify" type="checkbox" ${data.notify?'checked':''}>Send Telegram notifications</label>
      <div class="settings-fields"><label>New-job notification interval (minutes)<input id="pref-interval" type="number" min="15" max="1440" required value="${data.notification_interval_minutes}"><small>15–1440 minutes. Only changes Telegram job digests; searches keep their existing schedule. Important alerts and follow-up reminders are still checked every minute.</small></label></div>
      <label class="settings-check"><input id="quiet-enabled" type="checkbox" ${q.enabled?'checked':''}>Use quiet hours</label>
      <div class="settings-fields quiet-fields"><label>Quiet hours begin<input id="quiet-start" type="time" required value="${settingTime(q.start)}"></label><label>Resume notifications<input id="quiet-end" type="time" required value="${settingTime(q.end)}"></label>
      <label>Timezone<input id="quiet-zone" required maxlength="100" value="${esc(q.timezone)}" list="timezones"><datalist id="timezones"><option value="Asia/Jerusalem"><option value="Europe/London"><option value="Europe/Berlin"><option value="America/New_York"><option value="UTC"></datalist><small>Uses local time in this zone, including daylight saving.</small></label></div>
      <p class="settings-hint">When quiet hours end, the bot requests a fresh scan and sends a catch-up feed for the gap. You can still use Telegram commands during quiet hours.</p>
    </section>`;
  updateQuietInputs();
}
function updateQuietInputs(){document.querySelectorAll('.quiet-fields input').forEach(el=>el.disabled=!$('quiet-enabled').checked)}
async function loadSettings(){
  $('settings-error').textContent='';$('settings-save').disabled=true;
  $('settings-content').setAttribute('aria-busy','true');
  try{const data=await api('/settings');settingsSnapshot=data;renderSettings(data);settingsDirty=false;$('settings-status').textContent='Up to date · shared with Telegram'}
  catch(err){$('settings-error').textContent=err.message;$('settings-status').textContent='Couldn’t load settings';throw err}
  finally{$('settings-content').setAttribute('aria-busy','false')}
}
$('settings-form').addEventListener('input',()=>{settingsDirty=true;$('settings-save').disabled=false;$('settings-status').textContent='Unsaved changes';updateQuietInputs()});
$('settings-form').addEventListener('change',e=>{
  const el=e.target;
  if(!['pref-roles','pref-levels'].includes(el.name)||!el.checked)return;
  document.querySelectorAll(`[name="${el.name}"]`).forEach(other=>{
    if(other!==el&&(el.value==='all'||other.value==='all'))other.checked=false;
  });
});
$('settings-reload').onclick=e=>{if(settingsDirty&&!confirm('Discard your unsaved changes and reload settings?'))return;run(loadSettings,e.currentTarget)};
$('settings-form').onsubmit=async e=>{
  e.preventDefault();if(!settingsSnapshot)return;
  const selected=name=>Array.from(document.querySelectorAll(`[name="${name}"]:checked`),el=>el.value);
  const words=id=>$(id).value.split(',').map(v=>v.trim()).filter(Boolean);
  const minutes=id=>{const [h,m]=$(id).value.split(':').map(Number);return h*60+m};
  const p={roles:selected('pref-roles'),levels:selected('pref-levels'),region:$('pref-region').value,max_age_hours:Number($('pref-age').value),other_scope:$('pref-other').value,keep_unknown_level:$('pref-unknown').checked,include:words('pref-include'),exclude:words('pref-exclude')};
  if(!p.roles.length||!p.levels.length){$('settings-error').textContent='Choose at least one discipline and experience level.';return}
  if([p.include,p.exclude].some(items=>items.length>20||items.some(v=>v.length>100))){$('settings-error').textContent='Use up to 20 keywords per field, each at most 100 characters.';return}
  const q={enabled:$('quiet-enabled').checked,start:minutes('quiet-start'),end:minutes('quiet-end'),timezone:$('quiet-zone').value.trim()};
  if(q.enabled&&q.start===q.end){$('settings-error').textContent='Choose different start and end times, or turn quiet hours off.';return}
  const button=$('settings-save');button.disabled=true;$('settings-error').textContent='';
  // Keep the submitted form stable while its save is in flight.
  const controls=Array.from($('settings-content').querySelectorAll('input,select,textarea'));
  controls.forEach(el=>el.disabled=true);$('settings-reload').disabled=true;
  try{settingsSnapshot=await api('/settings','PUT',{revision:settingsSnapshot.revision,preferences:p,quiet_hours:q,notify:$('pref-notify').checked,notification_interval_minutes:Number($('pref-interval').value)});settingsDirty=false;renderSettings(settingsSnapshot);$('settings-status').textContent='Saved · shared with Telegram';toast('Settings saved. The bot will use them on its next check.')}
  catch(err){$('settings-error').textContent=err.message;$('settings-status').textContent='Not saved · your edits are still here'}
  finally{controls.forEach(el=>el.disabled=false);updateQuietInputs();button.disabled=!settingsDirty;$('settings-reload').disabled=false}
};
window.addEventListener('beforeunload',e=>{if(settingsDirty){e.preventDefault();e.returnValue=''}});
