const paths = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.3"/><rect x="14" y="3" width="7" height="7" rx="1.3"/><rect x="3" y="14" width="7" height="7" rx="1.3"/><rect x="14" y="14" width="7" height="7" rx="1.3"/>',
  queue: '<path d="M8 6h13M8 12h13M8 18h9M3 6h.1M3 12h.1M3 18h.1"/>',
  projects: '<path d="M3 7h6l2-3h4l2 3h4v13H3z"/>',
  submit: '<path d="M12 21V9m-5 5 5-5 5 5M4 8V4h16v4"/>',
  admin: '<rect x="3" y="3" width="18" height="7" rx="1.5"/><rect x="3" y="14" width="18" height="7" rx="1.5"/><path d="M7 6.5h.1M7 17.5h.1m6-11h4m-4 11h4"/>',
  chip: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/><rect x="9" y="9" width="6" height="6" rx=".5"/>',
  arrow: '<path d="M4 12h15m-5-5 5 5-5 5"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  up: '<path d="M7 17 17 7M7 7h10v10"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  activity: '<path d="M2 12h5l3-7 4 15 3-8h5"/>',
  partition: '<rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="M12 5v14"/>',
  power: '<path d="M12 2v8M7 5a8 8 0 1 0 10 0"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  locks: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  terminal: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 3 3-3 3m6 1h5"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  signout: '<path d="M9 4H4v16h5m-1-8h13m-5-5 5 5-5 5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9v.1"/>',
  shield: '<path d="m12 2 8 3v6c0 5-3 8-8 11-5-3-8-6-8-11V5z"/><path d="m8 12 3 3 5-6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  lab: '<path d="M9 3h6m-5 0v6L3.5 19a1.3 1.3 0 0 0 1 2h15a1.3 1.3 0 0 0 1-2L14 9V3m-7 12h10"/>',
  history: '<path d="M3 10a9 9 0 1 1 0 5m0-11v6h6m4-4v6l4 2"/>',
  page: '<path d="M6 2h8l5 5v15H6zM14 2v6h5m-10 5h7m-7 4h5"/>',
};
export function icon(name,size=18) {
  return `<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ''}</svg>`;
}
export const mark = '<span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>';
export function h(value='') {
  return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
export function url(value) { return encodeURIComponent(value); }
export function label(value) { return String(value || 'unknown').toLowerCase().replaceAll('_',' ').replace(/^./,c=>c.toUpperCase()); }
export const profiles = {'simulated':'Simulated GPU','cpu':'CPU container','dell-mig':'Dell MIG','spark-fixed-half':'Spark fixed half'};
export function bytes(value) {
  if (value == null || !Number.isFinite(Number(value))) return 'Unavailable';
  const n=Number(value);
  for (const [unit,div] of [['GiB',2**30],['MiB',2**20],['KiB',1024]]) {
    if(n>=div) return `${Number((n/div).toFixed(1))} ${unit}`;
  }
  return `${n} B`;
}
export function date(value) {
  if (!value) return 'Unavailable';
  const d=new Date(typeof value==='number'?value*1000:value);
  return Number.isNaN(d.getTime())?'Unavailable':d.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}
export function age(value) {
  if (!value) return 'Unavailable';
  const seconds=Math.max(0, Math.floor((Date.now()-new Date(typeof value==='number'?value*1000:value).getTime())/1000));
  if(!Number.isFinite(seconds)) return 'Unavailable';
  if(seconds<60) return 'Just now';
  if(seconds<3600) return `${Math.floor(seconds/60)} min ago`;
  if(seconds<86400) return `${Math.floor(seconds/3600)} hr ago`;
  return `${Math.floor(seconds/86400)} d ago`;
}
export function badge(value,tone) {
  const code=String(value).toUpperCase();
  const inferred= /UNKNOWN|UNVERIFIED|BLOCKED|FAILED|QUARANTINED|ERROR|UNREACHABLE/.test(code)?'critical':/SIMULATED|WAIT|QUEUE|DRAIN|PREPAR/.test(code)?'pending':/SUCCE|COMPLETE|READY|ONLINE|RUNNING|AVAILABLE|VERIFIED/.test(code)?'ready':'neutral';
  return `<span class="badge ${tone || inferred}"><i></i>${h(label(value))}</span>`;
}
export function link(route,content, cls='') {return `<a class="${cls}" href="#${h(route)}">${content}</a>`;}
export function head(title,description='',action='') {
  return `<header class="page-heading"><div><h1>${h(title)}</h1>${description?`<p>${h(description)}</p>`:''}</div>${action}</header>`;
}
export function section(title,extra='',count='') {
  return `<div class="section-heading"><h2>${h(title)}${count!==''?`<span class="count">${h(count)}</span>`:''}</h2>${extra}</div>`;
}
export function empty(message) {return `<div class="empty">${icon('info',17)}<span>${h(message)}</span></div>`;}
export function stats(entries) {
  return `<dl class="facts">${entries.map(([k,v])=>`<div><dt>${h(k)}</dt><dd>${h(v??'Unavailable')}</dd></div>`).join('')}</dl>`;
}
export function table(headers, rows, emptyText='No records.') {
  if(!rows.length) return empty(emptyText);
  return `<div class="table-scroll"><table><thead><tr>${headers.map(x=>`<th scope="col">${h(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(x=>`<td>${x}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
export function messageOf(error) {return error?.message || String(error);}
export function alias(value){return String(value||'Private').replace(/^./,c=>c.toUpperCase());}
export function pageLink(kind,id,name){return link(kind+'/'+url(id),h(name)+' '+icon('up',13),'item-link');}
