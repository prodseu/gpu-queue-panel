'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let csrf='',principal=null,authGeneration=0;
let state=null,dragging=null,busy=false,mutating=false,messageUntil=0;
let selectedGpu=sessionStorage.getItem('gpu-queue-view')||null;
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
function showLogin(){authGeneration++;principal=null;state=null;csrf='';$('#app').hidden=true;$('#login-panel').hidden=false;$('#tasks').innerHTML='';}
async function api(action,payload={}){
 const generation=authGeneration;
 const r=await fetch('/api/queue',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify({action,...payload})});
 const data=await r.json();if(generation!==authGeneration)throw new Error('登录已切换');if(!r.ok){if(r.status===401)showLogin();throw new Error(data.error||'连接失败');}
 if(data.csrf)csrf=data.csrf;if(data.user)principal=data.user;return data;
}

const gpuKey=g=>g.uuid||'index:'+g.index;
const onGPU=(t,g)=>t.gpu_uuid?Boolean(g.uuid&&t.gpu_uuid===g.uuid):t.gpu!=null&&Number(t.gpu)===g.index;
const processDetails=items=>(items||[]).map(p=>`<div class="process-meta">PID ${esc(p.pid)} · ${p.memory_used_mib==null?'显存暂不可用':number(p.memory_used_mib,0)+' MiB'}${p.python?`<div class="python-path" title="${esc(p.python)}">Python: ${esc(p.python)}</div>`:''}</div>`).join('');
function selectGPU(key){selectedGpu=key;if(key)sessionStorage.setItem('gpu-queue-view',key);else sessionStorage.removeItem('gpu-queue-view');render();}
function mergedOrder(ids){
 if(!selectedGpu)return ids;
 const visible=new Set(ids);let next=0;
 return state.tasks.filter(t=>t.state==='waiting').map(t=>visible.has(t.id)?ids[next++]:t.id);
}
function render(){
 principal=state.user||principal;
 $('#user-label').textContent=principal.username+' · '+(principal.role==='admin'?'管理员':'用户');
 renderScheduling();
 gpuHistory=state.gpu_history||{};
 const focused=document.activeElement?.dataset.gpuSelect;
 let device=state.gpus.find(g=>gpuKey(g)===selectedGpu);
 if(selectedGpu&&!device&&!state.gpu_error){selectedGpu=null;sessionStorage.removeItem('gpu-queue-view');notice('所选显卡不在当前快照中，已切回总队列。');}
 const records=[...(state.monitor_processes||[]),...state.tasks];
 const visible=selectedGpu?(device?records.filter(t=>onGPU(t,device)):[]):records;
 const allPending=state.tasks.filter(t=>t.state==='waiting'),pending=visible.filter(t=>t.state==='waiting');
 $('#connection').textContent='● 本机连接 · SSH';$('#connection').classList.remove('offline');$('#sampled').textContent='GPU 更新 '+clock(state.sampled);$('#count').textContent=visible.length;
 $('#queue-title').textContent=selectedGpu?(device?'GPU '+device.index+' · 我的队列':'显卡暂不可用'):'总队列';
 $('#queue-scope').textContent=selectedGpu?(device?device.name:'等待显卡快照恢复'):(principal.role==='admin'?'全部用户任务（管理员）':'本人全部显卡与CPU任务');
 $('#all-queues').setAttribute('aria-pressed',String(!selectedGpu));
 $('#queue-hint').textContent=selectedGpu?'显示这张卡的任务和进程。监控进程不可操作；卡内重排保留其他卡任务的位置。':'显示全部当前任务和GPU进程。监控行不可操作；等待任务可拖动、排序和取消。';
 if(Date.now()>messageUntil)$('#notice').textContent=state.gpu_error||state.process_error||((Date.now()/1000-state.sampled>15)?'服务器快照暂未更新，正在重连。':'');
 $('#gpus').innerHTML=state.gpus.length?state.gpus.map(g=>{
  const tasks=state.tasks.filter(t=>onGPU(t,g)),running=tasks.filter(t=>t.state==='running').length,waiting=tasks.filter(t=>t.state==='waiting').length,observed=(state.monitor_processes||[]).filter(t=>onGPU(t,g)).length;
  return `<button type="button" class="gpu" data-gpu-select="${esc(gpuKey(g))}" aria-pressed="${gpuKey(g)===selectedGpu}" aria-label="GPU ${g.index}，运行 ${running} 项，等待 ${waiting} 项，监控 ${observed} 项，查看该卡队列"><div class="gpu-title">GPU ${g.index}<small>最近 5 分钟</small></div><div class="gpu-name">${esc(g.name)}</div><div class="gpu-queue-summary"><span>运行 <b>${running}</b> · 等待 <b>${waiting}</b> · 监控 <b>${observed}</b></span><span class="gpu-open">查看队列 →</span></div><div class="metric"><span class="metric-label">显存用量</span><strong>${number(g.memory_used_mib==null?null:g.memory_used_mib/1024)}<small> / ${number(g.memory_total_mib==null?null:g.memory_total_mib/1024)} GiB</small></strong></div><div class="metric"><span class="metric-label">计算利用率</span><strong>${number(g.utilization,0)}<small> %</small></strong></div><div class="chart-legend"><span class="compute-key">计算 GPU-Util</span><span class="memory-key">显存占用率</span></div><canvas class="gpu-chart" data-gpu="${esc(g.uuid)}" aria-label="GPU ${g.index} 最近5分钟计算利用率和显存占用率，纵轴0至100%"></canvas></button>`;
 }).join(''):`<div class="empty gpu-discovery">${state.gpu_error?'显卡状态暂不可用，正在重连。':'暂未检测到NVIDIA显卡，仍可查看总队列。'}</div>`;
 $('#tasks').innerHTML=visible.length?`<div class="task-table"><div class="task-head"><span></span><span>任务 / 进程</span><span>状态</span><span>设备</span><span>进度 / 显存</span><span>调整等待项</span></div>${visible.map(t=>{
  const running=t.state==='running',p=t.progress||{},status=p.status||{},index=pending.findIndex(x=>x.id===t.id),current=status.updates;
  const gpu=state.gpus.find(g=>onGPU(t,g)),label=gpu?'GPU '+gpu.index:t.gpu_uuid?'GPU 暂不可用':t.gpu!=null?'GPU '+t.gpu:'CPU';
  if(t.read_only)return `<div class="task read-only" data-id="${esc(t.id)}" draggable="false"><span class="process-lock" aria-label="只读">🔒</span><div><div class="task-name">${esc(t.name)}</div><div class="task-id">未纳管进程 · NVIDIA元信息</div>${t.python?`<div class="python-path" title="${esc(t.python)}">Python: ${esc(t.python)}</div>`:''}</div><div><span class="badge read-only-badge">不可操作</span></div><div class="gpu-cell">${label}</div><div class="process-meta">PID ${esc(t.pid)}<div class="process-memory">${t.memory_used_mib==null?'显存暂不可用':number(t.memory_used_mib,0)+' MiB'}</div></div><div class="read-only-note">仅监控</div></div>`;
  return `<div class="task ${running?'running':''}" data-id="${esc(t.id)}" data-managed="true" draggable="${!running}"><span class="drag">${running?'●':'⠿'}</span><div><div class="task-name">${esc(t.name)}</div><div class="task-id">${esc(t.id)}</div><div class="task-owner">所属用户：${esc(t.owner_id||'未分配')}</div>${processDetails(t.processes)}</div><div><span class="badge">${running?'运行中':'等待 '+(index+1)}</span>${selectedGpu&&!running?`<div class="global-order">总序 ${allPending.findIndex(x=>x.id===t.id)+1}</div>`:''}</div><div class="gpu-cell">${!running&&(t.gpu_uuid||t.gpu!=null)?`<select data-gpu-target="${esc(t.id)}" aria-label="切换 ${esc(t.name)} 的GPU">${state.gpus.some(g=>onGPU(t,g))?'':`<option value="" selected disabled>${label}</option>`}${state.gpus.map(g=>`<option value="${esc(g.uuid)}" ${onGPU(t,g)?'selected':''}>GPU ${g.index}</option>`).join('')}</select>`:label}</div><div><div class="progress-text"><span>${p.percent==null?'尚未开始':p.percent.toFixed(1)+'%'}</span><span>${current==null?'':current+' / '+t.updates}</span></div>${bar(p.percent)}<div class="task-id">${status.epoch?'第 '+status.epoch+' / '+t.epochs+' 轮':''}</div></div><div class="actions">${running?'':`<button data-move="-1" data-id="${esc(t.id)}" ${index===0?'disabled':''} title="上移">↑</button><button data-move="1" data-id="${esc(t.id)}" ${index===pending.length-1?'disabled':''} title="下移">↓</button><button data-cancel="${esc(t.id)}">取消</button>`}</div></div>`;
 }).join('')}</div>`:`<div class="empty">${selectedGpu?'这张显卡没有你的运行或等待任务。':'当前没有等待或运行中的任务。'}</div>`;
 $('#controller').textContent=state.controller||'';$$('.fill').forEach(e=>e.style.width=e.dataset.width+'%');drawGPUCharts();
 $$('[data-gpu-select]').forEach(e=>e.addEventListener('click',()=>selectGPU(e.dataset.gpuSelect)));
 if(focused)$$('[data-gpu-select]').find(e=>e.dataset.gpuSelect===focused)?.focus({preventScroll:true});
 $$('[data-gpu-target]').forEach(e=>{e.addEventListener('change',()=>change('move_gpu',{id:e.dataset.gpuTarget,gpu_uuid:e.value},'已切换等待任务GPU，队列位置保持。'));e.addEventListener('pointerdown',v=>v.stopPropagation());e.addEventListener('dragstart',v=>{v.preventDefault();v.stopPropagation();});});
 $$('[data-cancel]').forEach(e=>e.addEventListener('click',()=>change('cancel',{id:e.dataset.cancel},'已取消该等待项，其他任务不变。')));
 $$('[data-move]').forEach(e=>e.addEventListener('click',()=>{const ids=pending.map(t=>t.id),i=ids.indexOf(e.dataset.id),to=i+Number(e.dataset.move);if(to<0||to>=ids.length)return;[ids[i],ids[to]]=[ids[to],ids[i]];change('reorder',{ids:mergedOrder(ids)},'等待顺序已保存。');}));
 $$('.task[draggable=true]').forEach(e=>{e.addEventListener('dragstart',v=>{dragging=e.dataset.id;v.dataTransfer.setData('text/plain',dragging);e.classList.add('dragging');});e.addEventListener('dragend',()=>{dragging=null;e.classList.remove('dragging');$$('.drop').forEach(x=>x.classList.remove('drop'));});e.addEventListener('dragover',v=>{v.preventDefault();e.classList.add('drop');});e.addEventListener('dragleave',()=>e.classList.remove('drop'));e.addEventListener('drop',v=>{v.preventDefault();const ids=pending.map(t=>t.id),from=ids.indexOf(v.dataTransfer.getData('text/plain')),to=ids.indexOf(e.dataset.id);dragging=null;if(from<0||to<0||from===to)return;const[id]=ids.splice(from,1);ids.splice(to,0,id);change('reorder',{ids:mergedOrder(ids)},'等待顺序已保存。');});});
}
async function change(action,payload,text){if(mutating)return;mutating=true;try{state=await api(action,{...payload,revision:state.revision});notice(text);render();}catch(e){notice(e.message);}finally{mutating=false;await refresh();}}
async function refresh(){if(!principal||busy||dragging||mutating||document.activeElement?.matches('[data-gpu-target]'))return;busy=true;try{state=await api('state');render();}catch(e){$('#connection').textContent='● 正在重连';$('#connection').classList.add('offline');notice(e.message);}finally{busy=false;}}
$('#all-queues').addEventListener('click',()=>selectGPU(null));

let settingsDirty=false;
function renderScheduling(){
 const settings=state.scheduling;
 if(!settings)return;
 const admin=principal?.role==='admin';
 for(const input of $('#scheduling-form').querySelectorAll('input,button'))input.disabled=!admin;
 $('#settings-permission').textContent=admin?'管理员可修改全局设置':'仅管理员可修改；当前为只读';
 $('#scheduler-usage').textContent=`运行 ${state.tasks.filter(t=>t.state==='running').length} / 窗口 ${settings.window===-1?'不限制':settings.window}`;
 const status=state.scheduler||{},age=Date.now()/1000-(status.time||0);
 $('#admission-reason').textContent=status.reason?(age>15?'调度状态待更新 · ':'')+status.reason:'等待调度器加载新版本';
 if(!settingsDirty)for(const [key,value] of Object.entries(settings)){
  const input=$('#scheduling-form').elements.namedItem(key);
  if(input)input.value=key.endsWith('_mib')&&value!==-1?value/1024:value;
 }
}
$('#scheduling-form').addEventListener('input',()=>settingsDirty=true);
$('#settings-reset').addEventListener('click',()=>{settingsDirty=false;renderScheduling();});
$('#scheduling-form').addEventListener('submit',async e=>{
 e.preventDefault();if(!state||mutating||principal?.role!=='admin')return;
 const settings={};for(const [key,value] of new FormData(e.currentTarget))settings[key]=Number(value)===-1?-1:Number(value)*(key.endsWith('_mib')?1024:1);
 if(Object.values(settings).some(v=>!Number.isInteger(v))){notice('设置必须是有效整数，显存单位为GiB。');return;}
 mutating=true;
 try{state=await api('configure',{settings,revision:state.revision});settingsDirty=false;notice('设置已保存；运行中的任务继续执行。');render();}
 catch(e){notice(e.message);}finally{mutating=false;await refresh();}
});

async function enter(session){
 authGeneration++;csrf=session.csrf;principal=session.user;settingsDirty=false;
 $('#login-panel').hidden=true;$('#app').hidden=false;$('#login-password').value='';await refresh();
}
$('#login-form').addEventListener('submit',async e=>{
 e.preventDefault();$('#login-error').textContent='';$('#login-submit').disabled=true;
 try{
  const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('#login-username').value,password:$('#login-password').value})});
  const result=await r.json();if(!r.ok)throw new Error(result.error||'登录失败');await enter(result);
 }catch(e){$('#login-error').textContent=e.message;}finally{$('#login-submit').disabled=false;}
});
$('#logout').addEventListener('click',async()=>{
 try{
  const r=await fetch('/api/logout',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:'{}'});
  const result=await r.json();if(!r.ok&&r.status!==401)throw new Error(result.error||'退出失败');showLogin();
 }catch(e){notice(e.message);}
});
(async()=>{
 try{const r=await fetch('/api/me',{credentials:'same-origin'});if(r.ok)await enter(await r.json());else showLogin();}
 catch{showLogin();$('#login-error').textContent='连接暂不可用，请检查SSH设置。';}
})();
setInterval(refresh,3000);
window.addEventListener('resize',drawGPUCharts);
