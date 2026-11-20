import {h,url,icon,link,head,section,badge,bytes,age,date,label,alias,empty,table,pageLink} from './ui.js';

export function slotSummary(nodes){
  const all=nodes.flatMap(n=>n.slots || []);
  return {
    occupied:all.filter(s=>s.state==='OCCUPIED').length,
    eligible:all.filter(s=>['OCCUPIED','AVAILABLE'].includes(s.state)).length,
    admitting:nodes.filter(n=>n.status==='ONLINE').length,
  };
}

export function waitingJobs(jobs) {return jobs.filter(j=>['QUEUED','WAITING','PENDING'].includes(j.state));}
const reasons={
  SLOT_UNAVAILABLE:'Waiting for a free slot', USER_ALREADY_ACTIVE:'Owner holds another slot',
  'Waiting for a free half-capacity slot':'Waiting for a free slot',
  'User already has an active allocation':'Owner holds another slot',
  ENFORCEMENT_UNVERIFIED:'GPU enforcement unverified', DATASET_UNAVAILABLE:'Dataset unavailable',
  ENVIRONMENT_UNAVAILABLE:'Environment unavailable', NODE_UNREACHABLE:'Worker unreachable',
  NODE_DRAINING:'Worker is draining', COMPATIBLE_NODE_UNAVAILABLE:'Compatible worker unavailable',
};
export function reason(text) {
  if(!text) return 'Waiting for the scheduler';
  return reasons[text] || (String(text).includes('_')?label(text):String(text));
}
function kpi(title,value,caption,glyph){
  return `<article class="kpi"><div class="kpi-label">${h(title)}${icon(glyph,16)}</div><div class="kpi-number">${value}</div><div class="kpi-caption">${h(caption)}</div></article>`;
}

function slotCard(slot,node,viewer) {
  const occupied=slot.state==='OCCUPIED';
  const blocked=slot.state==='BLOCKED';
  const quarantined=slot.state==='QUARANTINED';
  const accessible=occupied && slot.job_id && (viewer.role==='admin' || slot.user_id===viewer.id);
  const title=occupied?(slot.job_name || 'Reserved job'):blocked?'Blocked':quarantined?'Quarantined':'Available';
  const blockReason={DRAINING:'Worker draining',UNREACHABLE:'Worker unreachable',RESOURCE_PRESSURE:'Resource pressure',QUARANTINED:'Cleanup unconfirmed'};
  const sub=occupied?(slot.alias || alias(slot.user_id)):blocked?(blockReason[node.status]||'Awaiting qualification'):quarantined?'Cleanup unconfirmed':'';
  return `<div class="slot ${occupied?'in-use':blocked||quarantined?'unavailable':'free'}">
      <div class="slot-label"><span>SLOT ${h(slot.id)}</span><span class="slot-state" title="${h(label(slot.state))}">${icon(occupied?'activity':blocked||quarantined?'locks':'partition',13)}</span></div>
      <strong>${accessible?link('job/'+url(slot.job_id),h(title),'slot-link'):h(title)}</strong>${sub?`<span class="slot-sub">${h(sub)}</span>`:''}</div>`;
}

export function nodeCard(node, viewer){
  const blocked=node.qualification_status==='BLOCKED_UNVERIFIED_ENFORCEMENT';
  const simulated=node.backend==='simulated';
  const unavailable=['UNREACHABLE','RESOURCE_PRESSURE','QUARANTINED'].includes(node.status) || node.draining || node.slots.some(s=>s.state==='QUARANTINED');
  const state=unavailable?(node.draining?'Draining':label(node.status)):blocked?'Unverified':simulated?'Simulated':label(node.status);
  const inventory = node.inventory || {};
  const fixture = inventory.execution_mode === 'fixture' || inventory.architecture === 'fixture';
  const backendName = fixture && inventory.logical_cpus && inventory.memory_bytes
    ? `${inventory.logical_cpus} logical CPUs · ${bytes(inventory.memory_bytes)} host memory`
    : ({cpu:'CPU container worker',simulated:'CPU container · Simulated GPU','spark-fixed-half':'NVIDIA GB10 · Unified memory','dell-mig':'Fixed MIG partitions'}[node.backend] || label(node.backend));
  const reserve=node.reserve_bytes!=null && node.envelope?bytes(node.reserve_bytes):'Unverified';
  const limit=node.envelope?.memory_bytes ? bytes(node.envelope.memory_bytes):'Unverified';
  const footer=blocked?'GPU execution blocked':unavailable?state:fixture?'Simulated host and GPU':simulated?'Simulated GPU allocation':'Fixed-capacity allocation';
  return `<article class="node-card ${blocked?'blocked-node':''}">
      <div class="node-header"><div class="node-symbol ${blocked?'muted-symbol':''}">${icon(simulated?'layers':'chip',23)}</div>
      <div class="node-name"><h3>${h(node.name)}</h3><p>${h(backendName)}</p></div>${badge(state,unavailable?'critical':blocked?'neutral':simulated?'pending':'ready')}</div>
      <div class="slot-grid">${node.slots.map(s=>slotCard(s,node,viewer)).join('')}</div>
      <div class="node-limits"><span>Host reserve <b>${h(reserve)}</b></span><span>Slot memory <b>${h(limit)}</b></span></div>
      <div class="node-foot"><i class="dot ${blocked?'neutral':unavailable?'critical':simulated?'simulated':'ready'}"></i><span>${h(footer)}</span><span class="node-id">${h(node.id)}</span></div></article>`;
}

export function queueRows(jobs) {
  return jobs.map(j=>[
    j.can_open?pageLink('job',j.id,j.name):`<strong>${h(j.name)}</strong>`,
    `<span class="person"><span class="person-avatar">${h(alias(j.alias||j.user_id).slice(0,1))}</span>${h(j.alias || alias(j.user_id))}</span>`,
    badge(j.state), h(j.state==='QUEUED'?reason(j.waiting_reason):'—'), `<span class="mono">${h(age(j.created_at))}</span>`,
  ]);
}
export function queueTable(jobs) {return table(['Job','Submitted by','State','Waiting reason','Submitted'],queueRows(jobs),'The queue is empty.');}

function telemetryTable(nodes){
  const rows=nodes.map(n=>{
    const m=n.metrics || {};
    const stamp=m.observed_at;
    const pressure=m.status==='MEASURED'?(n.pressure?'Above threshold':'Below threshold'):'Unavailable';
    const freshness=stamp?date(stamp)+(Date.now()/1000-stamp>60?' · stale':''):'Unavailable';
    return [h(n.name),h(bytes(m.memory_available_bytes)),h(pressure),h(bytes(m.disk_available_bytes)),h(label(m.status || 'Unavailable')),h(freshness)];
  });
  return table(['Worker','Available memory','Memory pressure','Available storage','Observation','Sample time'],rows,'No host samples.');
}

export function overview(nodes,jobs,viewer) {
  const slots=slotSummary(nodes);
  const waiting=waitingJobs(jobs);
  const quarantined=nodes.reduce((count,n)=>count+n.slots.filter(s=>s.state==='QUARANTINED').length,0);
  const free=slots.eligible-slots.occupied;
  const unverified=nodes.filter(n=>n.qualification_status==='BLOCKED_UNVERIFIED_ENFORCEMENT').length;
  const submitted=waiting.map(j=>j.created_at).filter(t=>Number.isFinite(t));
  const oldest=submitted.length?submitted.reduce((a,b)=>Math.min(a,b)):null;
  const submit=link('submit',icon('plus',16)+'Submit job','btn primary');
  return head('Cluster overview','',submit)+
    `<div class="kpi-grid">
      ${kpi('Slots in use',`${slots.occupied}<small> / ${slots.eligible}</small>`,quarantined?`${quarantined} in quarantine`:free===0?'No free slots':`${free} free ${free===1?'slot':'slots'}`,'partition')}
      ${kpi('Jobs waiting',waiting.length,oldest?`Oldest submission ${age(oldest).toLowerCase()}`:'','queue')}
      ${kpi('Workers accepting jobs',`${slots.admitting}<small> / ${nodes.length}</small>`,unverified?`${unverified} awaiting qualification`:'','admin')}
      ${kpi('GPU limit per student','50<small>%</small>','Even when a slot is idle','chip')}
    </div>
    <section>${section('Worker capacity','<span class="section-note">'+icon('partition',15)+'Two fixed slots per worker</span>')}
    <div class="node-grid">${nodes.length?nodes.map(n=>nodeCard(n,viewer)).join(''):empty('No workers have registered.')}</div></section>
    <section>${section('Waiting queue',link('queue','View queue '+icon('arrow',15),'text-link'),waiting.length)}
    <div class="panel">${queueTable(waiting.slice(0,4))}</div></section>
    <details class="telemetry"><summary>Host telemetry</summary>${telemetryTable(nodes)}</details>`;
}
