import {bitable,dashboard,DashboardState} from '@lark-base-open/js-sdk';
import {normalize} from './model.mjs';
export const configuring=()=>[DashboardState.Create,DashboardState.Config].includes(dashboard.state);
export async function loadRows(onProgress) {
  const table=await bitable.base.getTableByName('每日数据');
  const fields=await table.getFieldMetaList();
  const records=[],ids=new Set(),tokens=new Set();let token;
  for(let page=0;page<500;page++){
    const result=await table.getRecordsByPage({pageSize:200,...(token===undefined?{}:{pageToken:token}),stringValue:true});
    for(const r of result.records){if(ids.has(r.recordId))throw Error('分页返回重复记录，请刷新重试');ids.add(r.recordId);records.push(r);}
    onProgress(records.length);
    if(!result.hasMore){if(records.length!==result.total)throw Error('记录未完整读取，请刷新重试');return {rows:normalize(records,fields),count:records.length,tableId:table.id};}
    if(result.pageToken===undefined||tokens.has(result.pageToken))throw Error('分页游标异常，未显示不完整数据');
    tokens.add(result.pageToken);token=result.pageToken;
  }
  throw Error('数据量超过当前读取上限，未显示部分结果');
}
export async function saveComponent(tableId){
  if(!configuring())throw Error('只允许组件配置状态保存');
  return dashboard.saveConfig({dataConditions:[],customConfig:{schema:1,tableId,source:'每日数据',readOnly:true}});
}
export function rendered(){dashboard.setRendered().catch(()=>{});}
