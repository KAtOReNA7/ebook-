export function text(v) {
  if(v == null) return '';
  if(Array.isArray(v)) return v.map(text).join('');
  if(typeof v === 'object') return text(v.text ?? v.name ?? v.value ?? '');
  return String(v);
}
export function numeric(v) {
  const s=text(v).trim().replaceAll(',','');
  if(!s || !/^-?\d+(\.\d+)?%?$/.test(s)) return null;
  const n=Number(s.replace('%',''));return Number.isFinite(n)?n:null;
}
export function dateKey(v) {
  if(typeof v==='number') return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(v));
  const m=text(v).match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  return m?`${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}`:'';
}
export function defaultRange(now=new Date()) {
  const end=dateKey(now.getTime());const d=new Date(`${end}T00:00:00Z`);d.setUTCDate(d.getUTCDate()-29);
  return {start:d.toISOString().slice(0,10),end};
}
export function axisBounds(values,percent=false) {
  const valid=values.filter(v=>typeof v==='number'&&Number.isFinite(v));
  if(!valid.length)return null;
  const lo=Math.min(...valid),hi=Math.max(...valid);
  const pad=hi===lo?(percent?0.5:Math.max(1,Math.abs(hi)*.05)):Math.max((hi-lo)*.12,percent?.05:1);
  let min=Math.max(0,lo-pad),max=percent?Math.min(100,hi+pad):hi+pad;
  if(max<=min)max=min+1;
  return {min:Number(min.toFixed(4)),max:Number(max.toFixed(4))};
}
export const METRICS=[
  {key:'推荐值',platform:'微信读书',title:'微信读书 · 推荐值',percent:true},
  {key:'今日阅读人数',platform:'微信读书',title:'微信读书 · 今日阅读人数'},
  {key:'点评人数',platform:'微信读书',title:'微信读书 · 点评人数'},
  {key:'累计阅读人数',platform:'番茄小说',title:'番茄小说 · 累计阅读人数'},
  {key:'点评人数',platform:'番茄小说',title:'番茄小说 · 点评人数'},
  {key:'书评数量',platform:'番茄小说',title:'番茄小说 · 书评数量'},
];
export function normalize(records,fields) {
  const f=Object.fromEntries(fields.map(x=>[x.name,x.id]));
  for(const required of ['作品唯一键','书名','平台','数据日期','对比作品',...new Set(METRICS.map(x=>x.key))]) if(!f[required])throw Error(`缺少字段：${required}`);
  return records.map(r=>{
    const read=n=>r.fields[f[n]];
    const id=text(read('作品唯一键')),name=text(read('书名')),platform=text(read('平台'));
    const old=text(read('对比作品')).split('｜');
    const author=old[0]===platform?old[2]:old[1];
    return {recordId:r.recordId,id,name,platform,author:author||'作者待核实',date:dateKey(read('数据日期')),original:text(read('书名来源原文')),values:Object.fromEntries(METRICS.map(m=>[m.key,numeric(read(m.key))]))};
  }).filter(r=>r.id&&r.name&&r.date&&['微信读书','番茄小说'].includes(r.platform));
}
export function catalog(rows) {
  const unique=new Map();for(const r of rows)unique.set(r.id,r);
  const books=[...unique.values()].sort((a,b)=>a.id.localeCompare(b.id));
  const groups=new Map();for(const b of books){const key=[b.platform,b.name,b.author].join('｜');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(b);}
  for(const [key,list]of groups)for(const [i,b]of list.entries())b.label=key+(list.length>1?`｜版本${i+1}`:'');
  return books.sort((a,b)=>a.label.localeCompare(b.label,'zh-CN'));
}
export function plot(rows,books,selected,range,metric) {
  const eligible=books.filter(b=>selected.includes(b.id)&&b.platform===metric.platform);
  const ids=new Set(eligible.map(b=>b.id));
  const subset=rows.filter(r=>ids.has(r.id)&&r.date>=range.start&&r.date<=range.end);
  const observed=[...new Set(subset.map(r=>r.date))].sort(),dates=[];
  if(observed.length){
    const day=new Date(observed[0]+'T00:00:00Z'),last=observed.at(-1);
    while(day.toISOString().slice(0,10)<=last){dates.push(day.toISOString().slice(0,10));day.setUTCDate(day.getUTCDate()+1);}
  }
  const values=new Map();for(const r of subset){const k=r.id+'|'+r.date,v=r.values[metric.key];if(values.has(k)&&values.get(k)!==v)throw Error(`同一作品同日存在冲突，暂停绘图：${r.name} ${r.date}`);values.set(k,v);}
  const series=eligible.map(b=>({id:b.id,name:b.label,data:dates.map(d=>values.get(b.id+'|'+d)??null)}));
  return {dates,series,bounds:axisBounds(series.flatMap(s=>s.data),metric.percent),hasValues:series.some(s=>s.data.some(x=>x!==null))};
}
