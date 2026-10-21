/* Un unico agente SMED. Las escrituras se confirman mediante acciones visibles. */
window.SMED = (() => {
  let snapshot, plan, root, ready = false, conversation = crypto.randomUUID();
  let draft = {prepare_before_stop:true, stop_at:'14:00', production_ready_at:'14:30', target_stop_minutes:15, unavailable_resources:[], delays:[]};
  let health = {ai:false}, chatting = false, messages;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = value => Number(value).toLocaleString('en-GB',{maximumFractionDigits:2})+' USD';
  const toMin = v => Number(v.split(':')[0])*60+Number(v.split(':')[1]);
  const statusName = {pending:'Pending',in_progress:'In progress',completed:'Complete',planned:'Planned'};
  const taskName = id => snapshot?.tasks.find(t=>t.task_id===id)?.description || id;
  const resourceName = id => snapshot?.resources.find(r=>r.resource_id===id)?.name || id;
  const toolName = {consultar_estado:'Current status',consultar_procedimiento:'Cleaning procedure',simular_plan:'Plan simulation',comparar_planes:'Both preparation scenarios',calcular_impacto:'Potential value',proponer_avance:'Proposed update'};
  const actionName = {start_task:'Log start',complete_task:'Log finish',record_stop:'Log line stop',confirm_quality:'Confirm quality release',record_restart:'Log restart',set_clock:'Update time',settings:'Update availability',confirm_readiness:'Confirm readiness'};
  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await res.json().catch(()=>({error:'Invalid server response.'}));
    if (!res.ok || result.error) throw new Error(result.error || 'Error '+res.status);
    return result;
  }
  function error(text) {root.querySelector('#smed-error').textContent=text || '';}
  function actor() {return root.querySelector('#smed-actor')?.value.trim() || 'Operator';}
  function formatAnswer(container, text) {
    // Escape first. Only these small formatting rules can create HTML.
    const inline = value => esc(value).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
    const lines=String(text).replace(/\\([*_`])/g,'$1').split('\n');
    let list=null, paragraph=null;
    for(const line of lines){
      const trimmed=line.trim();
      if(!trimmed){list=null;paragraph=null;continue;}
      const item=trimmed.match(/^(?:[-*•]|\d+[.)])\s+(.+)$/);
      if(item){
        paragraph=null;
        if(!list){list=document.createElement(/^\d/.test(trimmed)?'ol':'ul');container.appendChild(list);}
        const li=document.createElement('li');li.innerHTML=inline(item[1]);list.appendChild(li);
      }else{
        list=null;
        if(!paragraph){paragraph=document.createElement('p');container.appendChild(paragraph);}
        else paragraph.appendChild(document.createElement('br'));
        const span=document.createElement('span');span.innerHTML=inline(trimmed.replace(/^#{1,6}\s+/,''));paragraph.appendChild(span);
      }
    }
  }
  function scrollChat(){messages.scrollTop=messages.scrollHeight;}
  function addMessage(who, text, traces=[]) {
    const el=document.createElement('div');el.className='smed-message '+who;
    const body=document.createElement('div');body.className='smed-message-body';
    if(who==='ai')formatAnswer(body,text);else body.textContent=text;el.appendChild(body);
    if(traces.length){const details=document.createElement('details');details.className='smed-trace';
      const summary=document.createElement('summary');summary.textContent='Tools used ('+traces.length+')';details.appendChild(summary);
      const pre=document.createElement('div');pre.textContent=traces.map(t=>toolName[t.name]||'Check').join('\n');details.appendChild(pre);el.appendChild(details);}
    messages.appendChild(el);scrollChat();return el;
  }
  function readDraft(){
    const val=id=>root.querySelector('#'+id).value;
    const delay=Number(val('smed-delay-min'));
    draft={prepare_before_stop:root.querySelector('#smed-prepare').checked,stop_at:val('smed-stop'),
      production_ready_at:val('smed-ready'),target_stop_minutes:Number(val('smed-target')),
      unavailable_resources:[...root.querySelectorAll('[data-resource]')].filter(e=>!e.checked).map(e=>e.dataset.resource),
      delays:delay>0?[{task_id:val('smed-delay-task'),minutes:delay}]:[]};
    return draft;
  }
  async function refresh(){snapshot=await api('/api/smed?session='+encodeURIComponent(snapshot.session.id));renderLeft();}
  async function event(action, task_id=null, value=null, note=''){
    error('');
    try{snapshot=await api('/api/smed/event',{session_id:snapshot.session.id,expected_revision:snapshot.session.revision,action,task_id,value,note,actor:actor()});
      draft.production_ready_at=snapshot.session.production_ready_at;draft.unavailable_resources=snapshot.session.unavailable_resources;
      if(snapshot.session.stopped_at)draft.stop_at=snapshot.session.stopped_at;
      plan=null;renderLeft();
    }catch(e){await refresh();error(e.message);}
  }
  async function simulate(){
    error('');readDraft();
    try{plan=await api('/api/smed/plan',{session_id:snapshot.session.id,options:draft});renderPlan();}
    catch(e){error(e.message);}
  }
  function renderLeft(){
    const s=snapshot.session,h=snapshot.historical_baseline;
    const oldActor=root.querySelector('#smed-actor')?.value || 'Operator';
    root.querySelector('#smed-left').innerHTML=`
      <div class="smed-panel"><span class="smed-badge">DEMO · SIMULATED TASKS</span>
        <h2 style="margin-top:10px">Cleaning plan</h2>
        <p class="smed-small">${h.available?`${h.sessions} historical stops · ${h.durations_minutes.join(', ')} min each`:'Historical stops unavailable'}. Task times and teams are simulated.</p>
        <div class="smed-form"><label>Scenario time · ${esc(s.date)}<input id="smed-clock" type="time" value="${esc(s.clock)}"></label>
          <label>Operator<input id="smed-actor" value="${esc(oldActor)}" maxlength="100"></label></div>
        <div class="smed-buttons"><button class="smed-btn" data-action="clock">Update time</button><button class="smed-btn" data-action="plus5">+5 min</button>
        <button class="smed-btn" data-action="record_stop" ${s.stopped_at?'disabled':''}>Log line stop</button><button class="smed-btn" data-action="new">New session</button></div>
        <p class="smed-small">Stop: ${esc(s.stopped_at||'pending')} · Restart: ${esc(s.restarted_at||'pending')}</p>
      </div>
      <div class="smed-panel"><h2>Compare plans</h2><div class="smed-form">
        <label>Stop time · 14:00–14:10<input id="smed-stop" type="time" value="${esc(draft.stop_at)}"></label>
        <label>Staff & materials ready at<input id="smed-ready" type="time" value="${esc(draft.production_ready_at)}"></label>
        <label>Target duration · min<input id="smed-target" type="number" min="1" max="180" value="${draft.target_stop_minutes}"></label>
        <label class="smed-check"><input id="smed-prepare" type="checkbox" ${draft.prepare_before_stop?'checked':''}>Prepare kits before the stop</label>
        <label>Delayed task<select id="smed-delay-task">${snapshot.tasks.map(t=>`<option value="${t.task_id}" ${draft.delays[0]?.task_id===t.task_id?'selected':''}>${esc(t.description)}</option>`).join('')}</select></label>
        <label>Added delay · min<input id="smed-delay-min" type="number" min="0" max="120" value="${draft.delays[0]?.minutes||0}"></label>
        </div><h3>Available teams</h3><div class="smed-form">${snapshot.resources.map(r=>`<label class="smed-check"><input data-resource="${r.resource_id}" type="checkbox" ${draft.unavailable_resources.includes(r.resource_id)?'':'checked'}>${esc(r.name)}</label>`).join('')}</div>
        <div class="smed-buttons"><button class="smed-btn primary" data-action="plan">Simulate plan</button><button class="smed-btn" data-action="settings">Save availability</button></div>
        <p class="smed-small">Simulation only. An earlier restart also needs staff and materials before 14:30.</p><div id="smed-plan"></div></div>
      <div class="smed-panel"><h2>Tasks</h2><p class="smed-small">Log actual progress using the scenario clock. Dependencies and minimum durations must be met.</p>
        <div class="smed-overflow"><table class="smed-table"><thead><tr><th>Task</th><th>Status / team</th><th>Progress</th></tr></thead><tbody>
        ${snapshot.tasks.map(t=>{const a=s.tasks[t.task_id]||{status:'pending'};return `<tr><td><b>${esc(t.description)}</b><br><span class="smed-small">${t.phase==='internal'?'During stop':'Before stop'} · ${t.duration_min} min</span>${t.predecessors.length?`<details class="smed-small"><summary>Prerequisites</summary>${esc(t.predecessors.map(taskName).join(', '))}</details>`:''}</td>
          <td>${statusName[a.status]}<br><span class="smed-small">${esc(a.resources?.map(resourceName).join(', ')||'—')} ${esc(a.started_at||'')} ${a.completed_at?'→ '+esc(a.completed_at):''}</span></td>
          <td><button class="smed-btn" data-start="${t.task_id}" ${a.status!=='pending'?'disabled':''}>Start</button> <button class="smed-btn" data-finish="${t.task_id}" ${a.status!=='in_progress'?'disabled':''}>Finish</button></td></tr>`;}).join('')}
        </tbody></table></div></div>
      <div class="smed-panel"><h2>Restart checks</h2>
        <label class="smed-check"><input id="smed-staff" type="checkbox" ${s.staff_ready?'checked':''}>Production staff ready</label>
        <label class="smed-check"><input id="smed-material" type="checkbox" ${s.material_ready?'checked':''}>Materials ready</label>
        <div class="smed-buttons"><button class="smed-btn" data-action="readiness">Save readiness</button>
        <button class="smed-btn" data-action="confirm_quality" ${s.quality_released?'disabled':''}>Confirm quality release</button>
        <button class="smed-btn primary" data-action="record_restart" ${!snapshot.readiness.recorded_conditions_met||s.restarted_at?'disabled':''}>Log restart</button></div>
        <p class="smed-small">Quality release: ${s.quality_released?'confirmed':'pending'}. ${esc(snapshot.readiness.reasons.filter(r=>r!=='Confirm quality release.').map(r=>r.startsWith('Required tasks pending:')?`${snapshot.tasks.filter(t=>t.mandatory&&s.tasks[t.task_id]?.status!=='completed').length} required tasks remaining.`:r).join(' '))}</p>
        <p class="smed-small">Human confirmation required. The app records progress; it does not operate equipment or certify hygiene.</p></div>
      <div class="smed-panel"><details><summary>Session log (${snapshot.events.length})</summary><div class="smed-history">${snapshot.events.map(e=>`${esc(e.clock)} · ${esc(actionName[e.action]||e.action)} ${esc(e.task_id?taskName(e.task_id):'')} · ${esc(e.actor)}`).join('<br>')||'No activity yet.'}</div></details></div>`;
    renderPlan();
  }
  function renderPlan(){
    const el=root.querySelector('#smed-plan');if(!el)return;
    if(!plan){el.innerHTML='';return;}
    const p=plan;
    const min=Math.min(...p.rows.map(r=>toMin(r.start))), max=Math.max(...p.rows.map(r=>toMin(r.end))), span=Math.max(1,max-min);
    el.innerHTML=`<div class="smed-kpis"><div class="smed-kpi"><span class="smed-small">Cleaning finish</span><strong>${esc(p.cleaning_finish||'Not feasible')}</strong></div>
      <div class="smed-kpi"><span class="smed-small">Earliest restart*</span><strong>${esc(p.earliest_restart||'—')}</strong></div>
      <div class="smed-kpi"><span class="smed-small">Production time recovered</span><strong>${p.recovered_production_minutes} min</strong></div></div>
      <p class="smed-small">${p.feasible?`Duration ${p.cleaning_minutes} min · target ${p.target_minutes} min: ${p.target_met?'met':'not met'}. Last task: ${esc(taskName(p.last_task))}.`:esc(p.reasons.join(' '))}</p>
      <p class="smed-small">*Requires quality release, staff and materials.</p>
      <div class="smed-overflow">${p.rows.map(r=>`<div class="smed-timeline"><b>${esc(r.description)}</b><div class="smed-track"><i style="left:${(toMin(r.start)-min)/span*100}%;width:${(toMin(r.end)-toMin(r.start))/span*100}%" title="${esc(r.start+'–'+r.end)}"></i></div><span class="smed-small">${esc(r.start+'–'+r.end)}</span></div>`).join('')}</div>
      <details><summary>Assignments & assumptions</summary><table class="smed-table">${p.rows.map(r=>`<tr><td>${esc(r.description)}</td><td>${esc(r.resources.map(resourceName).join(', '))}</td><td>${esc(r.start)}–${esc(r.end)}</td></tr>`).join('')}</table><p class="smed-small">${p.assumptions.map(esc).join('<br>')}<br>Resource-constrained schedule; a shorter feasible plan may exist.</p></details>
      ${p.impact?`<section class="smed-value" aria-label="Potential value"><span class="smed-value-label">Potential value</span><strong class="smed-value-amount">${money(p.impact.net_conditional_potential_usd)}</strong><span class="smed-value-period">over ${esc(p.impact.days)} production days</span><p>Conditional estimate · 100% utilization · 25,000 USD/h margin · no added costs. Potential value, not realized savings.</p></section>`:''}`;
  }
  async function ask(question){
    if(chatting||!health.ai)return;
    chatting=true;root.querySelector('#smed-send').disabled=true;
    const input=root.querySelector('#smed-question');input.value='';input.style.height='auto';
    messages.setAttribute('aria-busy','true');
    addMessage('me',question);const pending=addMessage('ai','Checking the plan…');
    try{const reply=await api('/api/chat',{session_id:snapshot.session.id,conversation,question});pending.remove();
      const message=addMessage('ai',reply.text,reply.tool_calls);
      for(const proposal of reply.proposals||[]){const button=document.createElement('button');button.className='smed-btn primary';
        button.textContent='Confirm: '+(actionName[proposal.action]||proposal.action)+' '+(proposal.task_id?taskName(proposal.task_id):'');
        button.onclick=async()=>{button.disabled=true;try{snapshot=await api('/api/smed/confirm',{session_id:snapshot.session.id,expected_revision:proposal.revision,proposal_id:proposal.id,confirmed:true,actor:actor()});
          button.textContent='Confirmed';plan=null;renderLeft();}catch(e){button.textContent='Not applied';error(e.message);await refresh();}};
        message.appendChild(button);}
      scrollChat();
    }catch(e){pending.textContent=e.message;}
    finally{chatting=false;messages.setAttribute('aria-busy','false');root.querySelector('#smed-send').disabled=!health.ai;input.focus({preventScroll:true});scrollChat();}
  }
  async function mount(element){
    if(ready)return;ready=true;root=element;
    root.innerHTML=`<div id="smed-error" class="smed-error" role="alert"></div><div class="smed-shell"><div id="smed-left">Loading plan…</div>
      <aside class="smed-panel smed-chat" aria-label="Cleaning assistant">
      <header class="smed-chat-header"><div><span class="smed-eyebrow">MELT ASSISTANT</span><h2>Cleaning, made clearer.</h2></div>
      <p id="smed-ai-status" class="smed-small">Checking connection…</p></header>
      <div id="smed-messages" class="smed-messages" role="log" aria-label="Conversation" aria-live="polite" aria-relevant="additions"></div>
      <form id="smed-composer" class="smed-compose"><textarea id="smed-question" rows="1" maxlength="3000" placeholder="Ask about your cleaning plan…" aria-label="Ask the cleaning assistant"></textarea><button id="smed-send" class="smed-send" type="submit" aria-label="Send message" title="Send message" disabled>↑</button></form>
      <p class="smed-chat-footnote">Enter to send · Shift+Enter for a new line</p></aside></div>`;
    messages=root.querySelector('#smed-messages');
    if(!location.protocol.startsWith('http')){root.querySelector('#smed-left').textContent='Start the demo with python -m melt_alert.server, then open http://127.0.0.1:8000.';return;}
    try{
      let id;try{id=localStorage.getItem('melt-smed-session');}catch(e){}
      snapshot=await api('/api/smed'+(id?'?session='+encodeURIComponent(id):''));
      try{localStorage.setItem('melt-smed-session',snapshot.session.id);}catch(e){}
      draft.production_ready_at=snapshot.session.production_ready_at;draft.unavailable_resources=snapshot.session.unavailable_resources;
      if(snapshot.session.stopped_at)draft.stop_at=snapshot.session.stopped_at;
      renderLeft();health=await api('/api/health');
      root.querySelector('#smed-ai-status').textContent=health.ai?health.provider+' · '+health.model+' · local assistant':health.why;
      root.querySelector('#smed-send').disabled=!health.ai;
      addMessage('ai','Your cleaning plan, in one conversation.\n\nI can check tasks, compare schedules and prepare updates for your confirmation. Task times and teams in this demo are simulated.');
    }catch(e){error(e.message);root.querySelector('#smed-left').textContent='Could not load the plan. Check the server.';}
    root.addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.dataset.start)return event('start_task',b.dataset.start);
        if(b.dataset.finish)return event('complete_task',b.dataset.finish);
        const action=b.dataset.action;if(!action)return;
        if(action==='plan')return simulate();
        if(action==='clock')return event('set_clock',null,root.querySelector('#smed-clock').value);
        if(action==='plus5'){const t=toMin(snapshot.session.clock)+5;if(t>=1440)throw new Error('The scenario must finish within the same day.');return event('set_clock',null,String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0'));}
        if(action==='settings'){readDraft();return event('settings',null,{production_ready_at:draft.production_ready_at,unavailable_resources:draft.unavailable_resources});}
        if(action==='readiness')return event('confirm_readiness',null,{staff_ready:root.querySelector('#smed-staff').checked,material_ready:root.querySelector('#smed-material').checked});
        if(action==='new'){snapshot=await api('/api/smed/session',{});try{localStorage.setItem('melt-smed-session',snapshot.session.id);}catch(e){}conversation=crypto.randomUUID();plan=null;draft={prepare_before_stop:true,stop_at:'14:00',production_ready_at:'14:30',target_stop_minutes:15,unavailable_resources:[],delays:[]};messages.textContent='';error('');return renderLeft();}
        return event(action);
      }catch(err){error(err.message);}
    });
    root.querySelector('#smed-composer').addEventListener('submit',e=>{e.preventDefault();const q=root.querySelector('#smed-question').value.trim();if(q)ask(q);});
    const input=root.querySelector('#smed-question');
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!chatting&&health.ai)root.querySelector('#smed-composer').requestSubmit();}});
    input.addEventListener('input',()=>{input.style.height='auto';input.style.height=Math.min(input.scrollHeight,112)+'px';});
  }
  return {mount};
})();
