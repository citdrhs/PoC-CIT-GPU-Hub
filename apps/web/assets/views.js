import {h,url,link,icon,badge,label,head,section,stats,table,empty,bytes,date,age,alias,profiles,pageLink} from './ui.js';
import {queueTable,waitingJobs,reason} from './overview.js';

export function queuePage(jobs,state,query) {
  const q=query.trim().toLowerCase();
  const filtered=(state==='waiting'?waitingJobs(jobs):jobs.filter(j=>state==='all'||j.state===state)).filter(j=>[j.name,j.alias,j.user_id,j.waiting_reason,j.waiting_reason && reason(j.waiting_reason)].some(v=>String(v||'').toLowerCase().includes(q)));
  const options=['waiting','all',...new Set(jobs.map(j=>j.state))];
  return head('Job queue','',link('submit',icon('plus',16)+'Submit job','btn primary'))+
    `<div class="panel"><div class="toolbar"><label class="search-box">${icon('search',16)}<input type="search" id="queue-search" value="${h(query)}" placeholder="Find a job or student" aria-label="Find a job or student"></label>
    <select id="queue-state" aria-label="Filter by state">${options.map(v=>`<option value="${h(v)}" ${v===state?'selected':''}>${h(v==='waiting'?'Waiting jobs':v==='all'?'All states':label(v))}</option>`).join('')}</select></div>
    ${filtered.length?queueTable(filtered):empty(q?'No jobs match this search.':'No jobs match the selected state.')}</div>`;
}
export function projectsPage(projects,jobs) {
  return head('Projects','',link('submit',icon('plus',16)+'Submit job','btn primary'))+
    section('Project directory','',projects.length)+'<div class="panel">'+
    table(['Project','Owner','Created'],projects.map(p=>[pageLink('project',p.id,p.name),h(p.alias||alias(p.owner_id||p.user_id)),h(date(p.created_at))]),'Submit a job to create your first project.')+'</div>'+
    section('Run history','',jobs.length)+'<div class="panel">'+historyTable(jobs)+'</div>';
}
function historyTable(jobs){
  return table(['Job','Profile','State','Submitted'],jobs.map(j=>[pageLink('job',j.id,j.name),h(profiles[j.profile] || label(j.profile)),badge(j.state),h(date(j.created_at))]),'No runs recorded.');
}
export function projectPage(p,jobs){
  const revisions=p.revisions || [];
  const ids=new Set(revisions.map(r=>r.id));
  return head(p.name,'',link('submit?project='+url(p.id),icon('plus',16)+'Submit job','btn primary'))+
    section('Source revisions','',revisions.length)+'<div class="panel">'+table(['Revision','Entrypoint','Environment','Datasets','Digest','Created'],revisions.map(r=>[h(r.id),h(r.manifest?.entrypoint || 'Unavailable'),h(r.manifest?.environment || 'Unavailable'),h(r.manifest?.dataset_ids?.join(', ')||'None'),'<code title="'+h(r.source_hash||'')+'">'+h((r.source_hash || 'Unavailable').slice(0,16))+'</code>',h(date(r.created_at))]),'No source revisions uploaded.')+'</div>'+
    section('Run history')+'<div class="panel">'+historyTable(jobs.filter(j=>j.project_id===p.id||ids.has(j.revision_id)))+'</div>';
}

export function submitPage(projects,environments,datasets,selected,policy) {
  const runtimeLimit=policy.runtime_max_seconds;
  return head('Submit a job')+
    `<form id="submit-job" class="form-panel"><fieldset><legend>Source revision</legend>
    <div class="form-grid"><label>Project<select name="project_id"><option value="">Create a new project</option>${projects.map(p=>`<option value="${h(p.id)}" ${p.id===selected?'selected':''}>${h(p.name)}</option>`).join('')}</select></label>
    <label id="new-project-field" ${selected?'hidden':''}>New project name<input name="project_name" maxlength="80" ${selected?'':'required'} placeholder="e.g. Vision lab"></label>
    <label>Job name<input name="name" required maxlength="80" placeholder="e.g. Classifier baseline"></label>
    <label>Entrypoint<input name="entrypoint" required value="train.py"></label>
    <label class="wide">Source file<input type="file" name="source" required accept=".py,.zip"><small>Choose a .py file or a .zip with the entrypoint inside.</small></label>
    </div></fieldset>
    <fieldset><legend>Execution</legend><div class="form-grid">
    <label>Environment<select name="environment" required>${environments.map(e=>`<option value="${h(e.id)}">${h(e.name || e.id)}</option>`).join('')}</select></label>
    <label>Profile<select name="profile">${Object.entries(profiles).map(([k,v])=>`<option value="${k}">${h(v)}</option>`).join('')}</select></label>
    <label>Maximum runtime (seconds)<input type="number" min="1" max="${h(runtimeLimit)}" step="1" name="runtime_seconds" required value="${Math.min(600,runtimeLimit)}"><small>Your runtime limit is ${h(runtimeLimit)} seconds per attempt.</small></label>
    <label>Dataset<select name="dataset_id"><option value="">No dataset</option>${datasets.map(d=>`<option value="${h(d.id)}">${h(d.name || d.id)}</option>`).join('')}</select></label></div>
    <details><summary>Advanced options</summary><div class="form-grid"><label class="wide">Arguments (JSON array)<textarea name="args" rows="2">[]</textarea></label>
    <label>Output directory<input name="output_dir" value="outputs" required></label><label>Dependency lockfile (optional)<input name="dependencies_lock" placeholder="requirements.lock"></label>
    <label class="check wide"><input name="cooperative" type="checkbox"> Enable cooperative checkpoints</label></div></details></fieldset>
    <div id="form-status" role="status"></div><div class="form-actions">${link('overview','Cancel','btn secondary')}<button type="submit" class="btn primary">${icon('plus',16)}Submit job</button></div></form>`;
}

function metric(m,keys,format) {
  const value=keys.map(k=>m?.[k]).find(v=>v!=null);
  if(value==null) return 'Unavailable';
  const actual=typeof value==='object'?value.value:value;
  if(actual==null) return 'Unavailable';
  return format(actual)+(typeof value==='object'&&value.source?' · '+value.source:'');
}
function observationMetrics(a){
  const m=a?.metrics || {};
  const t=m.observed_at || m.timestamp || m.sampled_at;
  const stale=t && Date.now()/1000-Number(t)>60;
  const cpuUse=m.cgroup?.['cpu.stat']?.match(/^usage_usec (\d+)$/m)?.[1];
  return [
    ['Observation status',label(m.status || 'Unavailable')],
    ['GPU activity',metric(m,['gpu_activity_percent','gpu_utilization_percent','gpu_activity'],v=>v+'%')],
    ['GPU activity scope',m.gpu_activity_scope || m.scope || 'Unavailable'],
    ['Host memory',metric(m,['memory_current_bytes','memory_bytes','host_memory_bytes'],bytes)],
    ['Peak host memory',bytes(m.cgroup?.['memory.peak']??m.memory_peak_bytes)],
    ['CPU time',cpuUse==null?'Unavailable':(Number(cpuUse)/1e6).toFixed(2)+' s'],
    ['Sample interval',m.interval_seconds!=null?m.interval_seconds+' s':'Unavailable'],
    ['Observed',t?date(t)+(stale?' · stale':''):'Unavailable'],
  ];
}
export function jobPage(j,logs,events,checkpoints,artifacts,viewer) {
  const attempts=j.attempts || [];
  const a=attempts.at(-1);
  const e=a?.envelope || {};
  const terminal=['SUCCEEDED','CANCELLED','FAILED','COMPLETED','STOPPED','TIMED_OUT','PAUSED'].includes(j.state);
  const stop=j.desired_state==='CANCELLED'||j.desired_state==='STOP'||j.state==='STOPPING';
  const mayControl=viewer.id===j.user_id || viewer.role==='admin';
  const cancel=terminal||!mayControl?'':`<button class="btn secondary" data-action="cancel" ${stop?'disabled':''}>${stop?'Stop requested':'Cancel job'}</button>`;
  return head(j.name,'Job '+j.id,cancel)+
    `<div class="detail-banner">${badge(j.state)}<span>${h(j.waiting_reason?reason(j.waiting_reason):profiles[j.profile]||label(j.profile))}</span></div>
    <div class="detail-grid"><div class="panel padded"><h2>Allocation</h2>${stats([['Worker / slot',a?`${a.node_name} / ${a.slot}`:'Awaiting allocation'],['Profile',profiles[j.profile]||label(j.profile)],['Assigned GPU capacity',e.gpu_fraction==='1/2'?'50%'+(j.profile==='simulated'?' · simulated':''):e.gpu_fraction||'Unassigned'],['Host memory limit',bytes(e.memory_bytes)],['CPU-time quota',e.cpu_period_us?`${Number((e.cpu_quota_us/e.cpu_period_us).toFixed(2))} CPU equivalents`:'Unassigned'],['Memory warning / stop',e.warning_percent!=null?`${e.warning_percent}% / ${e.stop_percent}%`:'Unavailable'],['Time limit',j.manifest?.runtime_seconds?j.manifest.runtime_seconds+' s':'Unavailable'],['Deadline',date(a?.deadline)]])}</div>
    <div class="panel padded"><h2>Resource usage</h2>${stats(observationMetrics(a))}</div></div>
    <section>${section('Execution logs','<span class="section-note">'+icon('terminal',15)+'Cursor '+h(logs.next_cursor || 0)+'</span>')}<pre id="job-logs" class="logs" tabindex="0">${h(logs.items.map(l=>l.message).join('\n') || 'No log output yet.')}</pre></section>
    <section>${section('Checkpoints','',checkpoints.length)}<div class="panel">${table(['Checkpoint','Size','Completed',''],checkpoints.map(c=>[h(c.name || c.id),h(bytes(c.size_bytes)),h(date(c.completed_at || c.created_at)),terminal&&mayControl?`<button class="btn small secondary" data-action="resume" data-id="${h(c.id)}">Resume</button>`:'']), 'No completed checkpoints.')}</div></section>
    <section>${section('Artifacts','',artifacts.length)}<div class="panel">${table(['File','Size',''],artifacts.map(f=>[h(f.name || f.id),h(bytes(f.size_bytes)),`<button class="btn small secondary" data-action="download" data-id="${h(f.id)}" data-filename="${h(f.name||'artifact')}">${icon('download',13)}Download</button>`]),'No artifacts available.')}</div></section>
    <section>${section('Attempt history','',attempts.length)}<div class="panel">${table(['Attempt','Worker / slot','State','Started','Stop reason'],attempts.map(v=>[h(v.id),h(v.node_name+' / '+v.slot),badge(v.state),h(date(v.started_at)),h(v.reason?label(v.reason):'—')]),'No attempts have started.')}${a?'<details class="raw"><summary>Latest stop and cleanup evidence</summary><pre>'+h(JSON.stringify({reason:a.reason,cleanup_confirmed:a.cleanup_confirmed,...a.metadata},null,2))+'</pre></details>':''}</div></section>
    <section>${section('Lifecycle events')}<div class="panel">${table(['Time','Event','Details'],events.map(v=>[h(date(v.at || v.created_at || v.timestamp)),h(label(v.kind||v.type||v.state)),h(v.message || v.reason || JSON.stringify(v.metadata||{}))]),'No events available.')}</div></section>`;
}
function userPolicies(users,hubLimit) {
  const rows=users.map(user=>{
    const effective=user.effective_runtime_max_seconds ?? hubLimit;
    const override=user.runtime_override_seconds ?? '';
    const edit=user.role==='student'?`<details class="student-policy"><summary>Edit policy</summary>
      <form data-policy-user="${h(user.id)}" data-original-disabled="${Boolean(user.disabled)}" data-original-runtime="${h(override)}">
        <label>Access for ${h(user.alias)}<select name="disabled">
          <option value="false" ${user.disabled?'':'selected'}>Enabled</option>
          <option value="true" ${user.disabled?'selected':''}>Disabled</option>
        </select></label>
        <label>Runtime override (seconds)<input type="number" name="runtime" min="1" max="${h(hubLimit)}" step="1" value="${h(override)}" placeholder="Use hub limit">
          <small>Leave blank to use the hub limit of ${h(hubLimit)} seconds.</small></label>
        <button class="btn small secondary" type="submit">Save policy</button><span role="status" class="policy-status"></span>
      </form></details>`:'';
    return [h(user.alias),h(label(user.role)),h(user.disabled?'Disabled':'Enabled'),h(effective+' s'),edit];
  });
  return table(['Identity','Role','Access','Runtime limit',''],rows);
}
export function adminPage(nodes,selected,evidence,policy,system) {
  const choose=nodes.map(n=>link('admin/'+url(n.id),`${icon('chip',16)}${h(n.name)}`, 'node-tab '+(n.id===selected?.id?'active':''))).join('');
  let body=empty('No registered workers.');
  if(selected){
    const n=selected;
    const state=n.draining?'Draining':label(n.status);
    const controls=`<button class="btn secondary" data-action="${n.draining?'admit':'drain'}" data-id="${h(n.id)}">${n.draining?'Resume admissions':'Drain worker'}</button>`;
    const caps=evidence?.capabilities || evidence?.evidence?.capabilities || {};
    const entries=Array.isArray(caps)?caps.map(v=>[v.name||v.id,v]):Object.entries(caps);
    body=`<div class="panel padded"><div class="section-heading"><div><h2>${h(n.name)}</h2><p class="subtle">${h(n.id)}</p></div>${controls}</div>
      ${stats([['Administrative state',state],['Qualification',label(n.qualification_status)],['Last heartbeat',date(n.heartbeat_at)],['Resource model',profiles[n.backend]||label(n.backend)],['Host reserve',bytes(n.reserve_bytes)],['Pressure',n.metrics?.memory_pressure||'Unavailable'],['Storage available',bytes(n.metrics?.disk_available_bytes)]])}</div>
      <section>${section('Qualification evidence')}<div class="panel">${table(['Capability','Status'],entries.map(([k,v])=>[h(label(k)),badge(typeof v==='string'?v:v?.status||'Unknown')]),'No qualification results yet.')}
      <details class="raw"><summary>Inspect full evidence</summary><pre>${h(JSON.stringify(evidence,null,2))}</pre></details></div></section>`;
  }
  return head('Node administration')+
    '<div class="tabs">'+choose+'</div>'+body+'<section>'+section('Active reservations','',system.reservations.length)+'<div class="panel">'+
    table(['Worker','Slot','Owner','Attempt'],system.reservations.map(r=>[h(r.node_id),h(r.slot),h(alias(r.user_id)),h(r.attempt_id)]),'No active reservations.')+'</div></section>'+
    '<section>'+section('Student policies','',system.users.filter(u=>u.role==='student').length)+'<p class="policy-note">New runtime limits apply when a student submits or resumes a job. Disable access to hold their queued jobs.</p><div class="panel">'+userPolicies(system.users,policy.hub_runtime_max_seconds ?? policy.runtime_max_seconds)+'</div></section>'+
    '<section>'+section('Administrative events')+'<div class="panel">'+table(['Time','Action','Details'],system.audit.map(v=>[h(date(v.at)),h(label(v.kind)),h(v.message)]),'No administrative events recorded.')+'</div></section>'+
    '<section>'+section('Effective policy')+'<div class="panel"><details class="raw"><summary>View policy settings and quotas</summary><pre>'+h(JSON.stringify(policy,null,2))+'</pre></details></div></section>';
}
