'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {pending:'等待',launching:'正在分发',running:'运行中',orphaned:'待核对',done:'已完成',failed:'失败',canceled:'已取消',interrupted:'已中断'};
let state, csrf, selectedGpu = null, editId = null, busy = false, terminalPane = null, noticeUntil = 0;

function notice(text, error = false) {
  $('#notice').hidden = false;
  $('#notice').textContent = text;
  $('#notice').classList.toggle('error',error);
  noticeUntil = Date.now()+9000;
}

async function api(request) {
  const response = await fetch('/api', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(request)});
  const result = await response.json();
  if(!response.ok) throw new Error(result.error || '请求失败');
  return result;
}

async function refresh() {
  if(busy) return;
  try { state = await api({action:'state'}); render(); }
  catch(error) { $('#connection').textContent = error.message; $('#dispatcher').textContent='连接待恢复'; $('#dispatcher').classList.add('offline'); }
}

async function mutate(request) {
  if(busy || !state) return false;
  busy = true;
  try {
    const result = await api({...request,revision:state.revision});
    state = result.state;
    render();
    if(request.action === 'new_shell') notice('已新建终端 '+result.result+'，可在命令编辑中选择。');
    else notice('已同步到服务器终端。');
    return true;
  } catch(error) { notice(error.message,true); return false; }
  finally { busy=false; await refresh(); }
}

function gpuName(uuid) {
  if(uuid === null) return 'CPU';
  const device = state.inventory.devices.find(d=>d.uuid===uuid);
  return device ? 'GPU '+device.index : '设备暂不可用';
}

function render() {
  if($('#command-dialog').open) commandHint();
  $('#version').textContent='v'+state.version;
  $('#connection').textContent='已连接 · '+new Date().toLocaleTimeString('zh-CN');
  $('#session').textContent=state.session;
  $('#dispatcher').textContent=state.monitor_running ? (state.paused?'分发已暂停':'终端分发器在线') : '终端分发器离线';
  $('#dispatcher').classList.toggle('offline',!state.monitor_running || state.paused);
  $('#manual').setAttribute('aria-pressed',state.mode==='manual');
  $('#auto').setAttribute('aria-pressed',state.mode==='auto');
  $('#pause').textContent=state.paused?'恢复分发':'暂停分发';
  if(Date.now()>noticeUntil) $('#notice').hidden=true;
  if(state.inventory.error) { notice(state.inventory.error,true); }
  const active = ['launching','running','orphaned'];
  $('#gpu-cards').innerHTML=state.inventory.devices.map(device=>{
    const jobs=state.commands.filter(item=>item.gpu===device.uuid);
    const used=device.used==null?'—':(device.used/1024).toFixed(1),total=device.total==null?'—':(device.total/1024).toFixed(1);
    const memory=device.total ? Math.min(100,Math.max(0,device.used/device.total*100)) : 0;
    return `<button class="gpu-card" data-gpu="${esc(device.uuid)}" aria-pressed="${selectedGpu===device.uuid}"><div class="gpu-heading">GPU ${device.index}<span>实时监控</span></div><div class="gpu-name">${esc(device.name)}</div><div class="metric"><div>显存<strong>${used} / ${total} GiB</strong></div><span class="meter"><i data-width="${memory}"></i></span></div><div class="metric"><div>计算利用率<strong>${device.util??'—'}%</strong></div><span class="meter compute"><i data-width="${device.util??0}"></i></span></div><div class="gpu-counts">运行 ${jobs.filter(item=>active.includes(item.state)).length} · 等待 ${jobs.filter(item=>item.state==='pending').length}</div></button>`;
  }).join('');
  document.querySelectorAll('[data-width]').forEach(el=>el.style.width=el.dataset.width+'%');
  const visible = state.commands.filter(item=>selectedGpu===null||item.gpu===selectedGpu);
  $('#command-count').textContent=visible.length;
  $('#command-hint').textContent=selectedGpu===null ? '与服务器终端同步，点击显卡查看对应命令。' : gpuName(selectedGpu)+' · 当前命令';
  $('#empty').hidden=visible.length>0;
  $('#commands').innerHTML=visible.map(item=>{
    const pending=item.state==='pending';
    const terminal=item.pane || (item.target!=='new' ? item.target : null);
    return `<tr><td><span class="command-text">${esc(item.command)}</span><div class="command-meta">${esc(item.id)} · ${esc(item.cwd)}</div>${item.reason?`<div class="command-meta">${esc(item.reason)}</div>`:''}</td><td><span class="state ${esc(item.state)}">${labels[item.state]||esc(item.state)}</span>${item.requested?'<div class="command-meta">已请求分发</div>':''}${item.exit_code!=null?`<div class="command-meta">退出码 ${item.exit_code}</div>`:''}</td><td>${gpuName(item.gpu)}<div class="command-meta">${item.peak_mib===-1?'峰值预算不限':item.peak_mib+' MiB'}</div></td><td>${terminal?`<button data-terminal="${esc(terminal)}">${esc(terminal)} ↗</button>`:'新建任务窗'}</td><td><div class="row-actions">${pending?`<button data-action="dispatch" data-id="${item.id}" ${state.paused?'disabled':''}>分发</button><button data-action="edit" data-id="${item.id}">编辑 / 切 GPU</button><button data-action="up" data-id="${item.id}">↑</button><button data-action="down" data-id="${item.id}">↓</button><button data-action="cancel" data-id="${item.id}">取消</button>`:'<span class="muted">'+(active.includes(item.state)?'在终端执行':'已结束')+'</span>'}</div></td></tr>`;
  }).join('');
  $('#processes').innerHTML=state.inventory.processes.length?state.inventory.processes.map(process=>`<tr><td>${gpuName(process.gpu)}</td><td class="mono">${process.pid}</td><td class="mono">${esc(process.name)}</td><td>${process.memory==null?'—':process.memory+' MiB'}</td><td><span class="tag">不可操作</span></td></tr>`).join(''):'<tr><td colspan="5" class="muted">当前未检测到计算进程</td></tr>';
  if(!($('#limits').contains(document.activeElement) && document.activeElement.tagName==='INPUT')) {
    Object.entries(state.limits).forEach(([key,value])=>$('#limits').elements[key].value=value);
  }
}

function commandHint() {
  const automatic=state.mode==='auto' && !state.paused;
  $('#save-hint').textContent=automatic?'当前为自动模式，保存后将在资源允许时执行。':
    (state.paused?'分发已暂停，保存后保留在当前清单。':'保存后加入当前清单，点击“分发”运行。');
  $('#save-command').textContent=automatic?'保存并允许自动分发':'保存命令';
}

function showCommand(id=null) {
  if(!state) return;
  editId=id;
  const item=id?state.commands.find(item=>item.id===id):null;
  const form=$('#command-form');
  form.reset();
  $('#dialog-title').textContent=item?'编辑命令':'预存命令';
  form.elements.command.value=item?.command||'';
  form.elements.gpu.innerHTML=state.inventory.devices.map(device=>`<option value="${esc(device.uuid)}">GPU ${device.index} · ${esc(device.name)}</option>`).join('')+'<option value="cpu">CPU</option>';
  form.elements.gpu.value=item?(item.gpu||'cpu'):(selectedGpu||state.inventory.devices[0]?.uuid||'cpu');
  const terminals=[...new Set(state.terminals)];
  form.elements.target.innerHTML=terminals.map(pane=>`<option value="${esc(pane)}">固定终端 ${esc(pane)}${pane===state.default_target?'（默认）':''}</option>`).join('')+'<option value="new">新建独立任务窗口</option>';
  form.elements.target.value=item?.target||state.default_target;
  form.elements.peak_mib.value=item?.peak_mib??8192;
  form.elements.cwd.disabled=!!item;
  form.elements.cwd.value=item?.cwd||'';
  $('#root-hint').textContent='默认根目录 '+state.root+'。GPU 在程序内映射为逻辑设备 0，请同步检查命令中的 GPU 参数。';
  $('#command-dialog').showModal();
  commandHint();
}

async function terminal(pane) {
  terminalPane=pane;
  $('#terminal-title').textContent='终端 '+pane;
  if(!$('#terminal-dialog').open) $('#terminal-dialog').showModal();
  try { const result=await api({action:'terminal',pane}); $('#terminal-output').textContent=result.text||'终端暂无输出'; }
  catch(error) { $('#terminal-output').textContent=error.message; }
}

$('#manual').onclick=()=>mutate({action:'control',mode:'manual'});
$('#auto').onclick=()=>mutate({action:'control',mode:'auto'});
$('#pause').onclick=()=>{if(state) mutate({action:'control',paused:!state.paused});};
$('#add-command').onclick=()=>showCommand();
$('#new-terminal').onclick=()=>mutate({action:'new_shell'});
$('#clear').onclick=()=>mutate({action:'clear'});
$('#all-gpus').onclick=()=>{selectedGpu=null;render();};
$('#gpu-cards').onclick=event=>{const card=event.target.closest('[data-gpu]');if(card){selectedGpu=selectedGpu===card.dataset.gpu?null:card.dataset.gpu;render();}};
$('#commands').onclick=event=>{
  const output=event.target.closest('[data-terminal]');
  if(output) return terminal(output.dataset.terminal);
  const button=event.target.closest('[data-action]');
  if(!button) return;
  const {action,id}=button.dataset;
  if(action==='edit') return showCommand(id);
  if(action==='up'||action==='down') return mutate({action:'move',id,offset:action==='up'?-1:1});
  mutate({action,id});
};
$('#command-form').onsubmit=async event=>{
  event.preventDefault();
  const data=new FormData(event.currentTarget);
  const values={command:data.get('command'),gpu:data.get('gpu'),peak_mib:Number(data.get('peak_mib')),target:data.get('target')};
  const request=editId?{action:'edit',id:editId,values}:{action:'add',...values,cwd:data.get('cwd')||null};
  if(await mutate(request)) $('#command-dialog').close();
};
$('#limits').onsubmit=event=>{
  event.preventDefault();
  const values=Object.fromEntries([...new FormData(event.currentTarget)].map(([key,value])=>[key,Number(value)]));
  mutate({action:'limits',values});
};
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$('#'+button.dataset.close).close());
$('#refresh-terminal').onclick=()=>terminal(terminalPane);

async function init() {
  try {
    const response=await fetch('/api/session');
    csrf=(await response.json()).csrf;
    await refresh();
    setInterval(refresh,2000);
    setInterval(()=>{if($('#terminal-dialog').open) terminal(terminalPane);},3000);
  } catch(error) {notice(error.message,true);}
}
init();
