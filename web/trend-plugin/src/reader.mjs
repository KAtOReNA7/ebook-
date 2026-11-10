import {catalog,normalize,text} from './model.mjs';
export async function readPages(table,filter,onProgress=()=>{},cancelled=()=>false){
 const records=[],ids=new Set(),tokens=new Set();let token;
 for(let page=0;page<500;page++){
  if(cancelled())throw Error('请求已取消');
  const result=await table.getRecordsByPage({pageSize:200,...(token===undefined?{}:{pageToken:token}),...(filter?{filter}:{}),stringValue:true});
  if(cancelled())throw Error('请求已取消');
  for(const r of result.records){if(ids.has(r.recordId))throw Error('分页返回重复记录，请刷新重试');ids.add(r.recordId);records.push(r);}
  onProgress(records.length);
  if(!result.hasMore){if(records.length!==result.total)throw Error('记录未完整读取，请刷新重试');return records;}
  if(result.pageToken===undefined||tokens.has(result.pageToken))throw Error('分页游标异常，未显示不完整数据');
  tokens.add(result.pageToken);token=result.pageToken;
 }
 throw Error('数据量超过当前读取上限，未显示部分结果');
}
export function bookCatalog(records,fields){
 const f=Object.fromEntries(fields.map(x=>[x.name,x.id]));
 for(const n of ['作品唯一键','书名','作者','平台'])if(!f[n])throw Error(`缺少目录字段：${n}`);
 return catalog(records.map(r=>({id:text(r.fields[f['作品唯一键']]),name:text(r.fields[f['书名']]),author:text(r.fields[f['作者']])||'作者待核实',platform:text(r.fields[f['平台']])})).filter(r=>r.id&&r.name&&['微信读书','番茄小说'].includes(r.platform)));
}
export function selectionFilter(fields,ids,range){
 const f=Object.fromEntries(fields.map(x=>[x.name,x.id]));
 if(!ids.length||!f['作品唯一键']||!f['数据日期'])throw Error('缺少作品或筛选字段');
 const start=Date.parse(range.start+'T00:00:00+08:00'),end=Date.parse(range.end+'T00:00:00+08:00')+86400000;
 if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end)throw Error('日期范围无效');
 return {conjunction:'and',conditions:[
  {conjunction:'or',conditions:[...new Set(ids)].map(id=>({fieldId:f['作品唯一键'],operator:'is',value:id}))},
  {conjunction:'and',conditions:[{fieldId:f['数据日期'],operator:'isGreater',value:start-1},{fieldId:f['数据日期'],operator:'isLess',value:end}]}
 ]};
}
export function checkedSelection(records,fields,ids,range){
 const rows=normalize(records,fields);
 if(rows.length!==records.length||rows.some(r=>!ids.includes(r.id)||r.date<range.start||r.date>range.end))throw Error('筛选结果不符，暂停显示，请刷新重试');
 return rows;
}
