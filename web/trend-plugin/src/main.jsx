import React,{useEffect,useMemo,useRef,useState}from'react';
import{createRoot}from'react-dom/client';
import*as echarts from'echarts';
import{catalog,defaultRange,METRICS,plot}from'./model.mjs';
import{loadRows,configuring,saveComponent,rendered}from'./source.mjs';
import'./style.css';
function Chart({rows,books,selected,range,metric}){
 const ref=useRef(null),chart=useRef(null);let data,error;
 try{data=plot(rows,books,selected,range,metric);}catch(e){error=e.message;}
 useEffect(()=>{if(!ref.current||error||!data?.hasValues)return;const instance=echarts.init(ref.current);chart.current=instance;const resize=new ResizeObserver(()=>instance.resize());resize.observe(ref.current);return()=>{resize.disconnect();instance.dispose();};},[error,data?.hasValues]);
 useEffect(()=>{if(!chart.current||chart.current.isDisposed()||!data?.hasValues)return;chart.current.setOption({animation:false,color:['#3370ff','#14a67a','#e78e20','#a35fe4','#e05b78','#24a4b4','#8d9d25','#a76d42'],grid:{left:64,right:24,top:20,bottom:78},tooltip:{trigger:'axis',renderMode:'richText',valueFormatter:v=>v===null?'未采到':`${v}${metric.percent?'%':''}`},legend:{type:'scroll',bottom:0,textStyle:{fontSize:11},formatter:name=>name.split('｜').slice(1).join(' · ')},xAxis:{type:'category',data:data.dates,boundaryGap:false,axisLabel:{formatter:v=>v.slice(5)},axisLine:{lineStyle:{color:'#b8beca'}}},yAxis:{type:'value',min:data.bounds.min,max:data.bounds.max,scale:true,splitNumber:4,axisLabel:{formatter:v=>`${Number(v.toFixed(metric.percent?2:0))}${metric.percent?'%':''}`},splitLine:{lineStyle:{color:'#eef0f4'}}},series:data.series.map(s=>({...s,type:'line',connectNulls:false,showSymbol:true,symbolSize:6,lineStyle:{width:2},emphasis:{focus:'series'}}))},true);rendered();},[data,metric]);
 return <section className="chart-card"><h2>{metric.title}</h2>{error?<p role="alert">{error}</p>:data?.hasValues?<><div className="chart" ref={ref}/><p className="axis-note">纵轴 {data.bounds.min}–{data.bounds.max}{metric.percent?'%':''} · 随当前选书与日期调整</p></>:<div className="empty">{selected.length?'所选作品在此指标或日期内暂无数据':'请先选择作品'}</div>}</section>;
}
function App(){
 const[data,setData]=useState(null),[error,setError]=useState(''),[progress,setProgress]=useState(0),[loading,setLoading]=useState(false);
 const[selected,setSelected]=useState([]),[search,setSearch]=useState(''),[platform,setPlatform]=useState('全部平台'),[range,setRange]=useState(defaultRange),[notice,setNotice]=useState(''),[open,setOpen]=useState(true);
 const reload=async()=>{setLoading(true);setError('');try{const d=await loadRows(setProgress);setData(d);}catch(e){setError(e.message||'读取失败');}finally{setLoading(false);}};
 useEffect(()=>{reload();},[]);
 const books=useMemo(()=>catalog(data?.rows||[]),[data]);
 const choices=books.filter(b=>(platform==='全部平台'||b.platform===platform)&&b.label.toLowerCase().includes(search.toLowerCase()));
 const clear=()=>{setSelected([]);setSearch('');setPlatform('全部平台');setRange(defaultRange());setOpen(true);setNotice('已清空本页选择，日期已恢复过去30天。');};
 const choose=id=>{setNotice('');setSelected(old=>old.includes(id)?old.filter(x=>x!==id):[...old,id]);};
 const invalid=range.start>range.end||!range.start||!range.end;
 return <main><header><div><h1>作品趋势对比</h1><p>每个人独立选书 · 推荐值纵轴随选书自动调整</p></div><button onClick={clear} className="primary">一键清空</button></header>
 <div className="toolbar"><label>平台<select value={platform} onChange={e=>setPlatform(e.target.value)}><option>全部平台</option><option>微信读书</option><option>番茄小说</option></select></label><label className="search">搜索作品<input placeholder="输入书名或作者" value={search} onChange={e=>{setSearch(e.target.value);setOpen(true);}}/></label><label>开始日期<input type="date" value={range.start} onChange={e=>setRange({...range,start:e.target.value})}/></label><label>结束日期<input type="date" value={range.end} onChange={e=>setRange({...range,end:e.target.value})}/></label></div>
 <div className="selection-head"><button className="quiet" onClick={()=>setOpen(x=>!x)}>{open?'收起作品列表':'展开作品列表'}</button><span>已选 {selected.length} 本 · 建议同时比较2–5本</span><button className="quiet" disabled={loading} onClick={reload}>刷新数据</button></div>
 {open&&<div className="book-list" role="group" aria-label="选择作品">{choices.map(b=><label className={'book '+(selected.includes(b.id)?'selected':'')} key={b.id}><input type="checkbox" checked={selected.includes(b.id)} onChange={()=>choose(b.id)}/><span><span className={'platform '+(b.platform==='微信读书'?'wx':'fq')}>{b.platform}</span><strong>{b.name}</strong><small>{b.author}{b.label.includes('｜版本')?' · '+b.label.split('｜').at(-1):''}</small></span></label>)}{!choices.length&&!loading&&<p>没有匹配作品。可清空搜索或切换平台。</p>}</div>}
 {selected.length>0&&<div className="chips">{selected.map(id=>{const b=books.find(x=>x.id===id);return b&&<button key={id} onClick={()=>choose(id)} aria-label={'移除 '+b.label}>{b.label} ×</button>;})}</div>}
 <p className="status" role="status">{loading?`正在读取每日数据（${progress}条）…`:notice|| (data?`已完整读取 ${data.count} 条每日记录 · ${books.length} 个平台作品`:'')}{error&&<span role="alert">读取失败：{error}。{data?'下图保留上次成功读取的数据。':''}</span>}</p>
 {configuring()&&<div className="config"><span>此组件只读每日数据，个人选择不会写回表格。</span><button disabled={!data||loading} onClick={async()=>{try{await saveComponent(data.tableId);setNotice('组件已保存');}catch(e){setError(e.message);}}}>保存到看板</button></div>}
 {invalid?<p role="alert">请填写有效日期，开始日期不能晚于结束日期。</p>:<div className="charts">{METRICS.map(m=><Chart key={m.platform+m.key} rows={data?.rows||[]} books={books} selected={selected} range={range} metric={m}/>)}</div>}
 <footer>推荐值保留原始百分比；空值不补零、缺日不连线。今日阅读不能累加为累计读者。点评人数是累计总量；番茄累计阅读可能为约数。选择只保存在本页，刷新会重置。</footer></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
