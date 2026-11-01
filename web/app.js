'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token=new URLSearchParams(location.hash.slice(1)).get('token')||sessionStorage.getItem('gpu-queue-local-token')||'';
if(token)sessionStorage.setItem('gpu-queue-local-token',token);
if(location.hash.startsWith('#token='))history.replaceState(null,'',location.pathname);
let state=null,dragging=null,busy=false,mutating=false,messageUntil=0;
const clock=t=>t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false}):'—';
const number=(n,d=1)=>n==null?'—':n.toFixed(d);
const bar=(n,extra='')=>`<div class="bar ${extra}"><div class="fill" data-width="${Math.max(0,Math.min(100,n||0))}"></div></div>`;
const HISTORY_SECONDS=300;
let gpuHistory={};
function drawGPUCharts(){
 const end=Date.now()/1000,start=end-HISTORY_SECONDS;
 $$('canvas.gpu-chart').forEach(canvas=>{
  const width=canvas.clientWidth,height=150,scale=window.devicePixelRatio||1;
  if(!width)return;
  canvas.width=Math.round(width*scale);canvas.height=Math.round(height*scale);
  canvas.dataset.windowStart=start;canvas.dataset.windowEnd=end;
  const c=canvas.getContext('2d');c.scale(scale,scale);
  const left=26,right=width-9,top=12,bottom=126;
  const x=t=>left+(t-start)/HISTORY_SECONDS*(right-left),y=v=>bottom-Math.max(0,Math.min(100,v))/100*(bottom-top);
  c.font='9px Segoe UI';c.fillStyle='#93a295';c.strokeStyle='#e9eee7';c.lineWidth=1;
  for(const v of [0,50,100]){c.beginPath();c.moveTo(left,y(v));c.lineTo(right,y(v));c.stroke();c.fillText(String(v),2,y(v)+3);}
  c.fillText('-5 min',left,145);c.textAlign='center';c.fillText('-2.5',x(end-150),145);c.textAlign='right';c.fillText('现在',right,145);
  const samples=(gpuHistory[canvas.dataset.gpu]||[]).filter(p=>p.t<=end);
  const first=samples.findIndex(p=>p.t>=start);
  const points=first<0?[]:samples.slice(Math.max(0,first-1));
  c.save();c.beginPath();c.rect(left,top,right-left,bottom-top);c.clip();
  for(const [key,color] of [['memory','#7b97b6'],['compute','#21865c']]){
   c.beginPath();c.strokeStyle=color;c.lineWidth=1.6;let previous=null;
   for(const p of points){
    if(!Number.isFinite(p[key])){previous=null;continue;}
    if(!previous||p.t-previous.t>10)c.moveTo(x(p.t),y(p[key]));else c.lineTo(x(p.t),y(p[key]));
    previous=p;
   }
   c.stroke();
   const last=points[points.length-1];
   if(last&&Number.isFinite(last[key])){c.beginPath();c.arc(x(last.t),y(last[key]),2,0,2*Math.PI);c.fillStyle=color;c.fill();}
  }
  c.restore();
 });
}
function notice(s){messageUntil=Date.now()+7000;$('#notice').textContent=s;}
async function api(action,payload={}){const r=await fetch('/api/queue',{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({action,...payload})});const data=await r.json();if(!r.ok){if(r.status===403&&!$('#auth-dialog').open)$('#auth-dialog').showModal();throw new Error(data.error||'连接失败');}return data;}
function render(){
 gpuHistory=state.gpu_history||{};
 const pending=state.tasks.filter(t=>t.state==='waiting');$('#connection').textContent='● 本机连接 · SSH';$('#connection').classList.remove('offline');$('#sampled').textContent='GPU 更新 '+clock(state.sampled);$('#count').textContent=state.tasks.length;
 if(Date.now()>messageUntil)$('#notice').textContent=state.gpu_error||((Date.now()/1000-state.sampled>15)?'服务器快照暂未更新，正在重连。':'');
 $('#gpus').innerHTML=state.gpus.map(g=>`<article class="gpu"><div class="gpu-title">GPU ${g.index}<small>最近 5 分钟</small></div><div class="gpu-name">${esc(g.name)}</div><div class="metric"><label>显存用量</label><strong>${number(g.memory_used_mib==null?null:g.memory_used_mib/1024)}<small> / ${number(g.memory_total_mib==null?null:g.memory_total_mib/1024)} GiB</small></strong></div><div class="metric"><label>计算利用率</label><strong>${number(g.utilization,0)}<small> %</small></strong></div><div class="chart-legend"><span class="compute-key">计算 GPU-Util</span><span class="memory-key">显存占用率</span></div><canvas class="gpu-chart" data-gpu="${esc(g.uuid)}" aria-label="GPU ${g.index} 最近5分钟计算利用率和显存占用率，纵轴0至100%"></canvas></article>`).join('');
 $('#tasks').innerHTML=state.tasks.length?`<div class="task-table"><div class="task-head"><span></span><span>我的任务</span><span>状态</span><span>设备</span><span>训练进度</span><span>调整等待项</span></div>${state.tasks.map(t=>{const running=t.state==='running',p=t.progress,index=pending.findIndex(x=>x.id===t.id),current=p.status.updates;return `<div class="task ${running?'running':''}" data-id="${esc(t.id)}" draggable="${!running}"><span class="drag">${running?'●':'⠿'}</span><div><div class="task-name">${esc(t.name)}</div><div class="task-id">${esc(t.id)}</div></div><div><span class="badge">${running?'运行中':'等待 '+(index+1)}</span></div><div class="gpu-cell">GPU ${t.gpu}</div><div><div class="progress-text"><span>${p.percent==null?'尚未开始':p.percent.toFixed(1)+'%'}</span><span>${current==null?'':current+' / '+t.updates}</span></div>${bar(p.percent)}<div class="task-id">${p.status.epoch?'第 '+p.status.epoch+' / '+t.epochs+' 轮':''}</div></div><div class="actions">${running?'':`<button data-move="-1" data-id="${esc(t.id)}" ${index===0?'disabled':''} title="上移">↑</button><button data-move="1" data-id="${esc(t.id)}" ${index===pending.length-1?'disabled':''} title="下移">↓</button><button data-cancel="${esc(t.id)}">取消</button>`}</div></div>`;}).join('')}</div>`:'<div class="empty">当前没有等待或运行中的任务。</div>';
 $('#controller').textContent=state.controller||'';$$('.fill').forEach(e=>e.style.width=e.dataset.width+'%');
 drawGPUCharts();
 $$('[data-cancel]').forEach(e=>e.addEventListener('click',()=>change('cancel',{id:e.dataset.cancel},'已取消该等待项，其他任务不变。')));
 $$('[data-move]').forEach(e=>e.addEventListener('click',()=>{const ids=pending.map(t=>t.id),i=ids.indexOf(e.dataset.id),to=i+Number(e.dataset.move);if(to<0||to>=ids.length)return;[ids[i],ids[to]]=[ids[to],ids[i]];change('reorder',{ids},'等待顺序已保存。');}));
 $$('.task[draggable=true]').forEach(e=>{e.addEventListener('dragstart',v=>{dragging=e.dataset.id;v.dataTransfer.setData('text/plain',dragging);e.classList.add('dragging');});e.addEventListener('dragend',()=>{dragging=null;e.classList.remove('dragging');$$('.drop').forEach(x=>x.classList.remove('drop'));});e.addEventListener('dragover',v=>{v.preventDefault();e.classList.add('drop');});e.addEventListener('dragleave',()=>e.classList.remove('drop'));e.addEventListener('drop',v=>{v.preventDefault();const ids=pending.map(t=>t.id),from=ids.indexOf(v.dataTransfer.getData('text/plain')),to=ids.indexOf(e.dataset.id);dragging=null;if(from<0||to<0||from===to)return;const[id]=ids.splice(from,1);ids.splice(to,0,id);change('reorder',{ids},'等待顺序已保存。');});});
}
async function change(action,payload,text){if(mutating)return;mutating=true;try{state=await api(action,{...payload,revision:state.revision});notice(text);render();}catch(e){notice(e.message);}finally{mutating=false;await refresh();}}
async function refresh(){if(busy||dragging||mutating)return;busy=true;try{state=await api('state');render();}catch(e){$('#connection').textContent='● 正在重连';$('#connection').classList.add('offline');notice(e.message);}finally{busy=false;}}
$('#auth-form').addEventListener('submit',e=>{e.preventDefault();token=$('#auth-token').value.trim();sessionStorage.setItem('gpu-queue-local-token',token);$('#auth-dialog').close();refresh();});refresh();setInterval(refresh,3000);
window.addEventListener('resize',drawGPUCharts);
