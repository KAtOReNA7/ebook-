import React,{useEffect,useState}from'react';
import{createRoot}from'react-dom/client';
import{defaultRange,METRICS}from'./model.mjs';
import{loadCatalog,loadSelection,invalidate,configuring,saveComponent,rendered}from'./source.mjs';
import'./style.css';
const Chart=React.lazy(()=>import('./Chart.jsx'));
function App(){
 const[data,setData]=useState(null),[error,setError]=useState(''),[progress,setProgress]=useState(0),[loading,setLoading]=useState(false);
 const[selected,setSelected]=useState([]),[search,setSearch]=useState(''),[platform,setPlatform]=useState('全部平台'),[range,setRange]=useState(defaultRange),[notice,setNotice]=useState(''),[open,setOpen]=useState(true);
 const [directory,setDirectory]=useState(null),[revision,setRevision]=useState(0),[metricsLoading,setMetricsLoading]=useState(false);
 const reload=async()=>{setLoading(true);setError('');setNotice('');setProgress(0);setData(null);invalidate();try{setDirectory(await loadCatalog(setProgress));}catch(e){setError(e.message||'读取失败');}finally{setLoading(false);setRevision(v=>v+1);}};
 useEffect(()=>{reload();rendered();},[]);
 const books=directory?.books||[];
 const queryKey=JSON.stringify([selected,range.start,range.end,revision]);
 useEffect(()=>{
  let cancelled=false;setData(null);
  if(loading||!selected.length||!range.start||!range.end||range.start>range.end){setMetricsLoading(false);return;}
  setMetricsLoading(true);setError('');
  const timer=setTimeout(async()=>{try{const d=await loadSelection(selected,range,()=>cancelled);if(!cancelled)setData({...d,queryKey});}catch(e){if(!cancelled)setError(e.message||'读取失败');}finally{if(!cancelled)setMetricsLoading(false);}},250);
  return()=>{cancelled=true;clearTimeout(timer);};
 },[queryKey,loading]);
 const current=data?.queryKey===queryKey?data:null;
 const choices=books.filter(b=>(platform==='全部平台'||b.platform===platform)&&b.label.toLowerCase().includes(search.toLowerCase()));
 const clear=()=>{setSelected([]);setSearch('');setPlatform('全部平台');setRange(defaultRange());setOpen(true);setNotice('已清空本页选择，日期已恢复过去30天。');};
 const choose=id=>{setNotice('');setSelected(old=>old.includes(id)?old.filter(x=>x!==id):[...old,id]);};
 const invalid=range.start>range.end||!range.start||!range.end;
 return <main><header><div><h1>作品趋势对比</h1><p>每个人独立选书 · 推荐值纵轴随选书自动调整</p></div><button onClick={clear} className="primary">一键清空</button></header>
 <div className="toolbar"><label>平台<select value={platform} onChange={e=>setPlatform(e.target.value)}><option>全部平台</option><option>微信读书</option><option>番茄小说</option></select></label><label className="search">搜索作品<input placeholder="输入书名或作者" value={search} onChange={e=>{setSearch(e.target.value);setOpen(true);}}/></label><label>开始日期<input type="date" value={range.start} onChange={e=>setRange({...range,start:e.target.value})}/></label><label>结束日期<input type="date" value={range.end} onChange={e=>setRange({...range,end:e.target.value})}/></label></div>
 <div className="selection-head"><button className="quiet" onClick={()=>setOpen(x=>!x)}>{open?'收起作品列表':'展开作品列表'}</button><span>已选 {selected.length} 本 · 建议同时比较2–5本</span><button className="quiet" disabled={loading} onClick={reload}>刷新数据</button></div>
 {open&&<div className="book-list" role="group" aria-label="选择作品">{choices.map(b=><label className={'book '+(selected.includes(b.id)?'selected':'')} key={b.id}><input type="checkbox" checked={selected.includes(b.id)} onChange={()=>choose(b.id)}/><span><span className={'platform '+(b.platform==='微信读书'?'wx':'fq')}>{b.platform}</span><strong>{b.name}</strong><small>{b.author}{b.label.includes('｜版本')?' · '+b.label.split('｜').at(-1):''}</small></span></label>)}{!choices.length&&!loading&&<p>没有匹配作品。可清空搜索或切换平台。</p>}</div>}
 {selected.length>0&&<div className="chips">{selected.map(id=>{const b=books.find(x=>x.id===id);return b&&<button key={id} onClick={()=>choose(id)} aria-label={'移除 '+b.label}>{b.label} ×</button>;})}</div>}
 <p className="status" role="status">{loading?`正在读取作品目录（${progress}条）…`:notice|| (directory?`目录 ${books.length} 本（${directory.seconds.toFixed(1)}秒）`:'')}{metricsLoading?' · 正在读取所选作品的数据…':current?` · 所选作品 ${current.count} 条记录（${current.cached?'本页缓存':current.seconds.toFixed(1)+'秒'}）`:''}{error&&<span role="alert">读取失败：{error}。请刷新重试。</span>}</p>
 {configuring()&&<div className="config"><span>此组件只读数据，个人选择不会写回表格。</span><button disabled={!directory||loading} onClick={async()=>{try{await saveComponent();setNotice('组件已保存');}catch(e){setError(e.message);}}}>保存到看板</button></div>}
 {invalid?<p role="alert">请填写有效日期，开始日期不能晚于结束日期。</p>:selected.length>0&&current?<React.Suspense fallback={<p role="status">正在加载图表…</p>}><div className="charts">{METRICS.map(m=><Chart key={m.platform+m.key} rows={current.rows} books={books} selected={selected} range={range} metric={m}/>)}</div></React.Suspense>:<div className="empty">{metricsLoading?'正在获取所选日期的真实数据…':error?'暂未显示图表，请重试。':'请先选择作品；仅加载所选作品的数据。'}</div>}
 <footer>推荐值保留原始百分比；空值不补零、缺日不连线。今日阅读不能累加为累计读者。点评人数是累计总量；番茄累计阅读可能为约数。选择只保存在本页，刷新会重置。</footer></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
