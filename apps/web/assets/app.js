import {h,url,icon,mark,link,head,messageOf} from './ui.js';
import {overview} from './overview.js';
import {queuePage,projectsPage,projectPage,submitPage,jobPage,adminPage} from './views.js';

const app=document.querySelector('#app');
const state={token:sessionStorage.getItem('gpuhub.session')||'',config:{},me:null,revision:0,queue:[],queueState:'waiting',query:'',lastRoute:'',busy:false,logs:null};
const names={overview:'Overview',submit:'Submit job',queue:'Job queue',projects:'Projects',project:'Project history',job:'Job detail',admin:'Node administration'};
let draft=null;
const apiPath=id=>encodeURIComponent(id);
async function request(path,options={}) {
  const headers=new Headers(options.headers);
  if(state.token) headers.set('Authorization','Bearer '+state.token);
  if(options.body!==undefined) headers.set('Content-Type','application/json');
  const response=await fetch('/v1'+path,{...options,headers,cache:'no-store',body:options.body===undefined?undefined:JSON.stringify(options.body)});
  if(!response.ok){
    const value=await response.json().catch(()=>({}));
    const detail=value.error || value.detail || value;
    const error=new Error(typeof detail==='string'?detail:detail.message||detail.code||('Request failed ('+response.status+')'));
    error.status=response.status;
    throw error;
  }
  return options.blob?response.blob():response.json();
}
function activeRoute(){
  const hash=location.hash.slice(1) || 'overview';
  const [path,search='']=hash.split('?');
  const [kind,id]=path.split('/');
  return {kind,id:id?decodeURIComponent(id):null,query:new URLSearchParams(search),hash};
}
function notice(text){
  const el=document.querySelector('#notice');
  el.textContent=text;el.classList.add('visible');
  clearTimeout(notice.timer);notice.timer=setTimeout(()=>el.classList.remove('visible'),4200);
}
function brand(){return '<div class="brand">'+mark+'<b>CIT GPU Hub</b></div>';}
function navItem(route,title,active){
  return `<a href="#${route}" class="nav-item ${active===route?'current':''}" ${active===route?'aria-current="page"':''}>${icon(route,18)}<span>${h(title)}</span></a>`;
}
function shell(kind){
  const user=state.me.user;
  const active=kind==='project'?'projects':kind==='job'?'queue':kind;
  const dev=state.me.dev_auth || state.config.dev_auth;
  app.innerHTML=`<div class="shell"><aside id="sidebar"><div class="sidebar-inner">
      ${brand()}<nav aria-label="Workspace"><div class="nav-label">Workspace</div>${navItem('overview','Overview',active)}${navItem('queue','Job queue',active)}${navItem('projects','Projects',active)}${navItem('submit','Submit job',active)}
      ${user.role==='admin'?'<div class="nav-label second">Administration</div>'+navItem('admin','Node administration',active):''}</nav>
      <div class="sidebar-bottom"><div class="connection"><i class="dot ready"></i><span>API connected</span></div>
      <div class="identity"><span class="avatar">${h((user.alias||user.id).slice(0,1))}</span><div><b>${h(user.alias || user.id)}</b><small>${user.role==='admin'?'Administrator':'Student'}</small></div><button data-action="logout" title="Sign out" aria-label="Sign out">${icon('signout',15)}</button></div></div></div></aside>
      <div class="workspace"><header class="topbar"><div class="crumb"><button data-action="menu" class="menu-toggle" aria-controls="sidebar" aria-expanded="false" aria-label="Toggle navigation">${icon('menu',18)}</button><span class="crumb-home">Workspace</span>${icon('chevron',12)}<strong>${h(names[kind] || 'Page')}</strong></div>
      <div class="header-mode">${dev?'<span class="dev-mode">'+icon('lab',13)+'Development auth</span>':''}</div></header>
      <main id="main" tabindex="-1" class="main"><div id="view" aria-busy="true"><p class="loading">Loading…</p></div></main></div></div>`;
}
function login(message=''){
  state.me=null;
  const cfg=state.config;
  const dev=cfg.dev_auth;
  const identities=cfg.identities || [];
  app.innerHTML=`<main class="login-shell"><div class="login-box"><div class="login-brand">${brand()}</div><h1>Sign in to GPU Hub</h1>
    ${dev?`<form id="dev-login"><div class="login-mode">${icon('lab',15)} Local development auth</div><label>Development identity<select name="identity" required>${identities.map(u=>`<option value="${h(u.id)}">${h(u.alias || u.id)} · ${h(u.role)}</option>`).join('')}</select></label><button class="btn primary" type="submit">Open console ${icon('arrow',15)}</button></form>`:''}
    <details class="token-details" ${dev?'':'open'}><summary>Sign in with a scoped token</summary><form id="token-login"><label>Bearer token<input type="password" name="token" autocomplete="off" required></label><button class="btn secondary" type="submit">Sign in</button></form></details>
    <p role="alert" id="login-error">${h(message)}</p></div></main>`;
}
function logout(){
  sessionStorage.removeItem('gpuhub.session');state.token='';state.logs=null;draft=null;state.lastRoute='';state.revision++;login();
}
async function jobView(id){
  const p='/jobs/'+apiPath(id);
  const cursor=state.logs?.id===id?state.logs.next_cursor:0;
  const [j,newLogs,ev,ck,art]=await Promise.all([request(p),request(p+'/logs?cursor='+cursor),request(p+'/events'),request(p+'/checkpoints'),request(p+'/artifacts')]);
  let items=state.logs?.id===id?[...state.logs.items]:[];
  const known=new Set(items.map(item=>item.cursor || item.id));
  for(const l of newLogs.items) if(!known.has(l.cursor||l.id)) items.push(l);
  const logs={id,items:items.slice(-1200),next_cursor:newLogs.next_cursor??cursor};
  return {html:jobPage(j,logs,ev.items,ck.items,art.items,state.me.user),logs};
}
async function load(route) {
  const items=async path=>(await request(path)).items;
  const {kind,id}=route;
  switch(kind) {
    case 'overview':{const [nodes,queue]=await Promise.all([items('/nodes'),items('/queue')]);return overview(nodes,queue,state.me.user);}
    case 'queue':{
      const [waiting,visible]=await Promise.all([items('/queue'),items('/jobs')]);
      const queued=new Set(waiting.map(j=>j.id));
      const jobs=[...waiting,...visible.filter(j=>!queued.has(j.id)).map(j=>({...j,can_open:true}))];
      return {html:queuePage(jobs,state.queueState,state.query),queue:jobs};
    }
    case 'projects':{const [p,j]=await Promise.all([items('/projects'),items('/jobs')]);return projectsPage(p,j);}
    case 'project':{const [p,j]=await Promise.all([request('/projects/'+apiPath(id)),items('/jobs')]);return projectPage(p,j);}
    case 'submit':{
      const [p,e,d,me]=await Promise.all([items('/projects'),items('/environments'),items('/datasets'),request('/me')]);
      return submitPage(p,e,d,route.query.get('project'),me.policy);
    }
    case 'job':return jobView(id);
    case 'admin':{
      if(state.me.user.role!=='admin') throw new Error('Administrator access is required.');
      const nodes=await items('/nodes'),node=nodes.find(n=>n.id===id)||nodes[0];
      const [evidence,system]=await Promise.all([node?request('/admin/nodes/'+apiPath(node.id)+'/qualification'):null,request('/admin/overview')]);
      return adminPage(nodes,node,evidence,state.me.policy,system);
    }
    default: return head('Page unavailable')+link('overview','Return to overview','btn secondary');
  }
}
async function render(quiet=false){
  if(!state.me || (quiet && state.busy))return;
  const route=activeRoute();
  if(quiet && (route.kind==='submit'||document.hidden||document.querySelector('details[open]')||['INPUT','SELECT','TEXTAREA','BUTTON'].includes(document.activeElement?.tagName)))return;
  const version=++state.revision;
  if(state.lastRoute!==route.hash){
    shell(route.kind);state.lastRoute=route.hash;
    window.scrollTo(0,0);state.logs=null;draft=null;
    document.title=(names[route.kind]||'Page')+' · CIT GPU Hub';
  }
  try{
    const result=await load(route);
    if(version!==state.revision)return;
    if(result.logs)state.logs=result.logs;
    if(result.queue)state.queue=result.queue;
    const view=document.querySelector('#view');
    const before=view.querySelector('#job-logs');
    const offset=before?.scrollTop??null;
    const atEnd=before && before.scrollHeight-before.clientHeight-before.scrollTop<4;
    view.innerHTML=typeof result==='string'?result:result.html;
    view.setAttribute('aria-busy','false');
    const after=view.querySelector('#job-logs');
    if(after)after.scrollTop=atEnd||offset===null?after.scrollHeight:offset;
    const connection=document.querySelector('.connection');connection.innerHTML='<i class="dot ready"></i><span>API connected</span>';
  }catch(error){
    if(version!==state.revision)return;
    if(error.status===401){logout();login('Your session has expired. Sign in again.');return;}
    if(quiet){document.querySelector('.connection').innerHTML='<i class="dot critical"></i><span>Refresh failed</span>';return;}
    document.querySelector('#view').innerHTML='<div class="panel padded" role="alert"><h2>Couldn’t load this view</h2><p>'+h(messageOf(error))+'</p><button class="btn secondary" data-action="retry">Retry</button></div>';
  }
}
function bufferBase64(data){
  const bytes=new Uint8Array(data);const chunk=32768;let text='';
  for(let i=0;i<bytes.length;i+=chunk)text+=String.fromCharCode(...bytes.subarray(i,i+chunk));
  return btoa(text);
}
async function submitJob(form){
  const f=new FormData(form),file=f.get('source');
  if(!file||!file.size)throw new Error('Select a non-empty Python script or ZIP archive.');
  const limit=state.me.policy?.quotas?.upload_bytes;
  if(limit && file.size>limit)throw new Error('Source exceeds the configured upload limit.');
  const args=JSON.parse(f.get('args')||'[]');
  if(!Array.isArray(args)||args.some(v=>typeof v!=='string'))throw new Error('Arguments must be a JSON array of strings.');
  const manifest={schema_version:1,name:f.get('name').trim(),environment:f.get('environment'),entrypoint:f.get('entrypoint').trim(),args,runtime_seconds:Number(f.get('runtime_seconds')),dataset_ids:f.get('dataset_id')?[f.get('dataset_id')]:[],output_dir:f.get('output_dir').trim()};
  if(f.get('dependencies_lock').trim())manifest.dependencies_lock=f.get('dependencies_lock').trim();
  if(f.has('cooperative'))manifest.checkpoint={mode:'cooperative',directory:'checkpoints',interval_steps:200};
  const signature=JSON.stringify([manifest,f.get('project_id'),f.get('project_name'),f.get('profile'),file.name,file.size,file.lastModified]);
  if(!draft||draft.signature!==signature)draft={signature,project:f.get('project_id'),key:crypto.randomUUID()};
  if(!draft.project)draft.project=(await request('/projects',{method:'POST',body:{name:f.get('project_name').trim()}})).id;
  if(!draft.revision)draft.revision=(await request('/projects/'+apiPath(draft.project)+'/revisions',{method:'POST',body:{filename:file.name,content_base64:bufferBase64(await file.arrayBuffer()),manifest}})).id;
  const job=await request('/jobs',{method:'POST',body:{revision_id:draft.revision,profile:f.get('profile')},headers:{'Idempotency-Key':draft.key}});
  draft=null;notice('Job submitted');location.hash='job/'+url(job.id);
}
async function saveStudentPolicy(form){
  const payload={};
  const fields=new FormData(form);
  const access=fields.get('disabled');
  const runtime=fields.get('runtime').trim();
  if(access!==form.dataset.originalDisabled) payload.disabled=access==='true';
  if(runtime!==form.dataset.originalRuntime) payload.runtime_override_seconds=runtime===''?null:Number(runtime);
  if(!Object.keys(payload).length){
    form.querySelector('.policy-status').textContent='No changes to save.';
    return;
  }
  await request('/admin/users/'+apiPath(form.dataset.policyUser)+'/policy',{method:'POST',body:payload});
  await render();
  notice('Student policy saved');
}
app.addEventListener('submit',async event=>{
  const form=event.target;if(!(form instanceof HTMLFormElement))return;
  event.preventDefault();if(state.busy)return;
  const button=form.querySelector('button[type=submit]');
  button.disabled=true;state.busy=true;
  try{
    if(form.dataset.policyUser){
      await saveStudentPolicy(form);
    }else if(form.id==='submit-job'){
      const status=form.querySelector('#form-status');status.className='';status.textContent='Submitting job…';await submitJob(form);
    }else{
      const values=new FormData(form);
      state.token=form.id==='dev-login'?(await request('/auth/dev',{method:'POST',body:{identity:values.get('identity')}})).token:values.get('token').trim();
      state.me=await request('/me');sessionStorage.setItem('gpuhub.session',state.token);state.lastRoute='';await render();
    }
  }catch(error){
    const status=form.querySelector('.policy-status, #form-status') || document.querySelector('#login-error');
    if(status){status.className='form-error';status.textContent=messageOf(error);}
  }finally{button.disabled=false;state.busy=false;}
});
app.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  const action=button.dataset.action;const route=activeRoute();
  if(action==='menu'){const open=app.classList.toggle('menu-open');button.setAttribute('aria-expanded',String(open));return;}
  if(action==='logout'){logout();return;}
  if(action==='retry'){await render();return;}
  if(state.busy)return;
  state.busy=true;button.disabled=true;
  try{
    if(action==='cancel'){
      if(!confirm('Stop this job? Progress after the last completed checkpoint may be lost.'))return;
      await request('/jobs/'+apiPath(route.id)+'/cancel',{method:'POST',body:{}});notice('Stop request recorded');
    }else if(action==='resume'){
      const r=await request('/jobs/'+apiPath(route.id)+'/resume',{method:'POST',body:{checkpoint_id:button.dataset.id},headers:{'Idempotency-Key':crypto.randomUUID()}});
      location.hash='job/'+url(r.id);notice('Resume requested');
    }else if(action==='drain'||action==='admit'){
      const verb=action==='drain'?'drain':'resume';
      await request('/admin/nodes/'+apiPath(button.dataset.id)+'/'+verb,{method:'POST',body:{}});notice(action==='drain'?'Worker is draining':'Drain lifted. GPU jobs still require qualification.');
    }else if(action==='download'){
      const blob=await request('/artifacts/'+apiPath(button.dataset.id)+'/download',{blob:true});
      const local=URL.createObjectURL(blob);const a=document.createElement('a');a.href=local;a.download=button.dataset.filename;a.click();setTimeout(()=>URL.revokeObjectURL(local),30000);return;
    }
    await render();
  }catch(error){notice(messageOf(error));}finally{state.busy=false;button.disabled=false;}
});
function renderQueueFilter(focus){
  const position=focus?.selectionStart;
  document.querySelector('#view').innerHTML=queuePage(state.queue,state.queueState,state.query);
  if(focus){const replacement=document.getElementById(focus.id);replacement.focus();if(position!=null)replacement.setSelectionRange(position,position);}
}
app.addEventListener('input',event=>{
  if(event.target.id==='queue-search'){state.query=event.target.value;renderQueueFilter(event.target);}
});
app.addEventListener('change',event=>{
  if(event.target.id==='queue-state'){state.queueState=event.target.value;renderQueueFilter(event.target);}
  if(event.target.name==='project_id'){
    const group=document.getElementById('new-project-field');group.hidden=!!event.target.value;
    group.querySelector('input').required=!event.target.value;
  }
  if(event.target.name==='source'){
    const f=event.target.files[0];const entry=event.target.form.elements.entrypoint;
    if(f&&f.name.toLowerCase().endsWith('.py'))entry.value=f.name;
  }
});
window.addEventListener('hashchange',()=>{app.classList.remove('menu-open');render();});
document.querySelector('.skip-link').addEventListener('click',e=>{e.preventDefault();document.querySelector('#main')?.focus();});
async function start(){
  try{
    state.config=await request('/config');
    if(state.token){try{state.me=await request('/me');await render();return;}catch(e){if(e.status!==401)throw e;logout();}}
    login();
  }catch(e){app.innerHTML='<main class="login-shell"><div class="login-box"><h1>Console unavailable</h1><p role="alert">'+h(messageOf(e))+'</p><button class="btn secondary" id="reconnect">Reconnect</button></div></main>';document.querySelector('#reconnect').onclick=start;}
}
start();setInterval(()=>render(true),7000);
