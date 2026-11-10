import {readPages,bookCatalog,selectionFilter,checkedSelection} from './reader.mjs';
let sdkValue,sdkPromise;
const sdk=()=>sdkPromise??=(import('@lark-base-open/js-sdk').then(value=>(sdkValue=value)).catch(error=>{sdkPromise=null;throw error;}));
export const configuring=()=>!!sdkValue&&[sdkValue.DashboardState.Create,sdkValue.DashboardState.Config].includes(sdkValue.dashboard.state);
const sources=new Map(),cache=new Map();
async function source(name){
 if(!sources.has(name))sources.set(name,(async()=>{const {bitable}=await sdk();const table=await bitable.base.getTableByName(name);return {table,fields:await table.getFieldMetaList()};})().catch(e=>{sources.delete(name);throw e;}));
 return sources.get(name);
}
export function invalidate(){cache.clear();sources.clear();}
export async function loadCatalog(onProgress){
 const start=performance.now(),{table,fields}=await source('项目总表');
 const records=await readPages(table,null,onProgress);
 return {books:bookCatalog(records,fields),seconds:(performance.now()-start)/1000};
}
export async function loadSelection(ids,range,cancelled=()=>false){
 const key=JSON.stringify([[...ids].sort(),range.start,range.end]),hit=cache.get(key);
 if(hit&&Date.now()-hit.time<60000)return {...hit.data,cached:true};
 const start=performance.now(),{table,fields}=await source('每日数据');
 const records=await readPages(table,selectionFilter(fields,ids,range),undefined,cancelled);
 const data={rows:checkedSelection(records,fields,ids,range),count:records.length,tableId:table.id,seconds:(performance.now()-start)/1000};
 if(!cancelled()){if(cache.size>=20)cache.delete(cache.keys().next().value);cache.set(key,{time:Date.now(),data});}
 return data;
}
export async function saveComponent(){
 if(!configuring())throw Error('只允许组件配置状态保存');
 const {table}=await source('每日数据');
 const {dashboard}=await sdk();
 return dashboard.saveConfig({dataConditions:[],customConfig:{schema:1,tableId:table.id,source:'每日数据',readOnly:true}});
}
export function rendered(){sdk().then(({dashboard})=>dashboard.setRendered()).catch(()=>{});}
