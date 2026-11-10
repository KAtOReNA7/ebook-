import test from 'node:test';
import assert from 'node:assert/strict';
import {readPages,selectionFilter,bookCatalog,checkedSelection} from '../src/reader.mjs';
const fields=['作品唯一键','书名','作者','平台','数据日期','对比作品','推荐值','今日阅读人数','点评人数','累计阅读人数','书评数量'].map(name=>({id:name,name}));
const range={start:'2026-11-01',end:'2026-11-10'};
test('selection query sends OR ids and inclusive Shanghai date range to SDK',()=>{
 const q=selectionFilter(fields,['a','b','a'],range);
 assert.equal(q.conjunction,'and');assert.deepEqual(q.conditions[0].conditions.map(c=>c.value),['a','b']);
 assert.equal(q.conditions[1].conditions[0].value,Date.parse('2026-11-01T00:00:00+08:00')-1);
 assert.equal(q.conditions[1].conditions[1].value,Date.parse('2026-11-11T00:00:00+08:00'));
 assert.throws(()=>selectionFilter(fields,[],range));
});
test('every page keeps server-side filter; incomplete results fail closed',async()=>{
 const calls=[],filter=selectionFilter(fields,['a'],range);
 const records=await readPages({getRecordsByPage:async p=>{calls.push(p);return p.pageToken?{records:[{recordId:'r2'}],hasMore:false,total:2}:{records:[{recordId:'r1'}],hasMore:true,pageToken:'next',total:2};}},filter);
 assert.equal(records.length,2);assert.ok(calls.every(p=>p.filter===filter&&p.pageSize===200));
 await assert.rejects(readPages({getRecordsByPage:async()=>({records:[],total:1,hasMore:false})}),/未完整/);
});
test('cancellation stops further pages and duplicate pagination is rejected',async()=>{
 let cancelled=false,calls=0;
 await assert.rejects(readPages({getRecordsByPage:async()=>{calls++;cancelled=true;return {records:[],hasMore:true,pageToken:'p'};}},null,undefined,()=>cancelled),/取消/);
 assert.equal(calls,1);
 await assert.rejects(readPages({getRecordsByPage:async()=>({records:[{recordId:'same'}],hasMore:true,pageToken:'p'})}),/重复/);
});
test('catalog reads project identities; unexpected unfiltered samples cannot render',()=>{
 const record={recordId:'r',fields:{'作品唯一键':'a','书名':'作品甲','作者':'作者甲','平台':'微信读书','数据日期':'2026-11-10','对比作品':'作品甲｜作者甲'}};
 const books=bookCatalog([record],fields);assert.equal(books[0].label,'微信读书｜作品甲｜作者甲');
 assert.equal(checkedSelection([record],fields,['a'],range).length,1);
 assert.throws(()=>checkedSelection([record],fields,['b'],range),/不符/);
 assert.throws(()=>checkedSelection([record],fields,['a'],{start:'2026-11-01',end:'2026-11-09'}),/不符/);
});
