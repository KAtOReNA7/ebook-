import React,{useEffect,useMemo,useRef} from 'react';
import * as echarts from 'echarts/core';
import {LineChart} from 'echarts/charts';
import {GridComponent,TooltipComponent,LegendComponent} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';
import {plot} from './model.mjs';
import {rendered} from './source.mjs';
echarts.use([LineChart,GridComponent,TooltipComponent,LegendComponent,CanvasRenderer]);
export default function Chart({rows,books,selected,range,metric}){
 const ref=useRef(null),chart=useRef(null);let data,error;
 const computed=useMemo(()=>{try{return {data:plot(rows,books,selected,range,metric)};}catch(e){return {error:e.message};}},[rows,books,selected,range,metric]);
 data=computed.data;error=computed.error;
 useEffect(()=>{if(!ref.current||error||!data?.hasValues)return;const instance=echarts.init(ref.current);chart.current=instance;const resize=new ResizeObserver(()=>instance.resize());resize.observe(ref.current);return()=>{resize.disconnect();instance.dispose();};},[error,data?.hasValues]);
 useEffect(()=>{if(!chart.current||chart.current.isDisposed()||!data?.hasValues)return;chart.current.setOption({animation:false,color:['#3370ff','#14a67a','#e78e20','#a35fe4','#e05b78','#24a4b4','#8d9d25','#a76d42'],grid:{left:64,right:24,top:20,bottom:78},tooltip:{trigger:'axis',renderMode:'richText',valueFormatter:v=>v===null?'未采到':`${v}${metric.percent?'%':''}`},legend:{type:'scroll',bottom:0,textStyle:{fontSize:11},formatter:name=>name.split('｜').slice(1).join(' · ')},xAxis:{type:'category',data:data.dates,boundaryGap:false,axisLabel:{formatter:v=>v.slice(5)},axisLine:{lineStyle:{color:'#b8beca'}}},yAxis:{type:'value',min:data.bounds.min,max:data.bounds.max,scale:true,splitNumber:4,axisLabel:{formatter:v=>`${Number(v.toFixed(metric.percent?2:0))}${metric.percent?'%':''}`},splitLine:{lineStyle:{color:'#eef0f4'}}},series:data.series.map(s=>({...s,type:'line',connectNulls:false,showSymbol:true,symbolSize:6,lineStyle:{width:2},emphasis:{focus:'series'}}))},true);rendered();},[data,metric]);
 return <section className="chart-card"><h2>{metric.title}</h2>{error?<p role="alert">{error}</p>:data?.hasValues?<><div className="chart" ref={ref}/><p className="axis-note">纵轴 {data.bounds.min}–{data.bounds.max}{metric.percent?'%':''} · 随当前选书与日期调整</p></>:<div className="empty">{selected.length?'所选作品在此指标或日期内暂无数据':'请先选择作品'}</div>}</section>;
}
