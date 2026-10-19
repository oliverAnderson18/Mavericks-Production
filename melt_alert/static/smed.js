/* Un unico agente SMED. Las escrituras se confirman mediante acciones visibles. */
window.SMED = (() => {
  let snapshot, plan, root, ready = false, conversation = crypto.randomUUID();
  let draft = {prepare_before_stop:true, stop_at:'14:00', production_ready_at:'14:30', target_stop_minutes:15, unavailable_resources:[], delays:[]};
  let health = {ai:false}, chatting = false, messages;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = value => Number(value).toLocaleString('es-ES',{maximumFractionDigits:2})+' USD';
  const toMin = v => Number(v.split(':')[0])*60+Number(v.split(':')[1]);
  const statusName = {pending:'Pendiente',in_progress:'En curso',completed:'Completada',planned:'Prevista'};
  const actionName = {start_task:'Registrar inicio',complete_task:'Registrar finalización',record_stop:'Registrar paro',confirm_quality:'Registrar liberación de calidad',record_restart:'Registrar arranque'};
  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await res.json().catch(()=>({error:'Respuesta no válida del servidor.'}));
    if (!res.ok || result.error) throw new Error(result.error || 'Error '+res.status);
    return result;
  }
  function error(text) {root.querySelector('#smed-error').textContent=text || '';}
  function actor() {return root.querySelector('#smed-actor')?.value.trim() || 'Operario demo';}
  function addMessage(who, text, traces=[]) {
    const el=document.createElement('div');el.className='smed-message '+who;
    const body=document.createElement('div');body.textContent=text;el.appendChild(body);
    if(traces.length){const details=document.createElement('details');details.className='smed-trace';
      const summary=document.createElement('summary');summary.textContent='Herramientas consultadas ('+traces.length+')';details.appendChild(summary);
      const pre=document.createElement('div');pre.textContent=traces.map(t=>t.name+' · '+JSON.stringify(t.summary)).join('\n');details.appendChild(pre);el.appendChild(details);}
    messages.appendChild(el);messages.scrollTop=messages.scrollHeight;return el;
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
    const oldActor=root.querySelector('#smed-actor')?.value || 'Operario demo';
    root.querySelector('#smed-left').innerHTML=`
      <div class="smed-panel"><span class="smed-badge">TAREAS Y RECURSOS SIMULADOS · DEMO-1</span>
        <h2 style="margin-top:10px">Preparar y coordinar la limpieza</h2>
        <p class="smed-small">${h.available?`${h.sessions} paros originales · ${h.total_minutes} min · duraciones ${h.durations_minutes.join(', ')} min`:'Histórico original no disponible.'}. El desglose por tareas es un escenario de demostración.</p>
        <div class="smed-form"><label>Reloj del escenario · ${esc(s.date)}<input id="smed-clock" type="time" value="${esc(s.clock)}"></label>
          <label>Responsable del registro<input id="smed-actor" value="${esc(oldActor)}" maxlength="100"></label></div>
        <div class="smed-buttons"><button class="smed-btn" data-action="clock">Actualizar reloj</button><button class="smed-btn" data-action="plus5">+5 minutos</button>
        <button class="smed-btn" data-action="record_stop" ${s.stopped_at?'disabled':''}>Registrar línea detenida</button><button class="smed-btn" data-action="new">Nueva sesión demo</button></div>
        <p class="smed-small">Paro registrado: ${esc(s.stopped_at||'pendiente')} · Arranque registrado: ${esc(s.restarted_at||'pendiente')} · Revisión ${s.revision}</p>
      </div>
      <div class="smed-panel"><h2>Simular organización SMED</h2><div class="smed-form">
        <label>Inicio de paro · ventana 14:00–14:10<input id="smed-stop" type="time" value="${esc(draft.stop_at)}"></label>
        <label>Disponibilidad productiva<input id="smed-ready" type="time" value="${esc(draft.production_ready_at)}"></label>
        <label>Objetivo de duración · min<input id="smed-target" type="number" min="1" max="180" value="${draft.target_stop_minutes}"></label>
        <label class="smed-check"><input id="smed-prepare" type="checkbox" ${draft.prepare_before_stop?'checked':''}>Preparar kits antes del paro</label>
        <label>Tarea con retraso simulado<select id="smed-delay-task">${snapshot.tasks.map(t=>`<option value="${t.task_id}" ${draft.delays[0]?.task_id===t.task_id?'selected':''}>${esc(t.task_id)}</option>`).join('')}</select></label>
        <label>Retraso adicional · min<input id="smed-delay-min" type="number" min="0" max="120" value="${draft.delays[0]?.minutes||0}"></label>
        </div><h3>Recursos disponibles</h3><div class="smed-form">${snapshot.resources.map(r=>`<label class="smed-check"><input data-resource="${r.resource_id}" type="checkbox" ${draft.unavailable_resources.includes(r.resource_id)?'':'checked'}>${esc(r.name)}</label>`).join('')}</div>
        <div class="smed-buttons"><button class="smed-btn primary" data-action="plan">Simular plan</button><button class="smed-btn" data-action="settings">Guardar disponibilidad del escenario</button></div>
        <p class="smed-small">Simular no ejecuta tareas. Adelantar la disponibilidad productiva respecto a las 14:30 es una hipótesis adicional.</p><div id="smed-plan"></div></div>
      <div class="smed-panel"><h2>Seguimiento de tareas</h2><p class="smed-small">Los botones registran la confirmación humana. El reloj debe avanzar antes de finalizar una tarea; el servidor verifica dependencias, mínimos y recursos.</p>
        <div class="smed-overflow"><table class="smed-table"><thead><tr><th>Tarea</th><th>Estado / equipo</th><th>Registrar</th></tr></thead><tbody>
        ${snapshot.tasks.map(t=>{const a=s.tasks[t.task_id]||{status:'pending'};return `<tr><td><b>${esc(t.task_id)}</b><br>${esc(t.description)}<br><span class="smed-small">${t.phase==='internal'?'Interna':'Preparación externa'} · ${t.duration_min} min · Previas: ${esc(t.predecessors.join(', ')||'ninguna')}</span></td>
          <td>${statusName[a.status]}<br><span class="smed-small">${esc(a.resources?.join(', ')||'—')} ${esc(a.started_at||'')} ${a.completed_at?'→ '+esc(a.completed_at):''}</span></td>
          <td><button class="smed-btn" data-start="${t.task_id}" ${a.status!=='pending'?'disabled':''}>Iniciar</button> <button class="smed-btn" data-finish="${t.task_id}" ${a.status!=='in_progress'?'disabled':''}>Finalizar</button></td></tr>`;}).join('')}
        </tbody></table></div></div>
      <div class="smed-panel"><h2>Comprobaciones para el arranque</h2>
        <label class="smed-check"><input id="smed-staff" type="checkbox" ${s.staff_ready?'checked':''}>Personal de producción disponible</label>
        <label class="smed-check"><input id="smed-material" type="checkbox" ${s.material_ready?'checked':''}>Material disponible</label>
        <div class="smed-buttons"><button class="smed-btn" data-action="readiness">Registrar disponibilidad</button>
        <button class="smed-btn" data-action="confirm_quality" ${s.quality_released?'disabled':''}>Confirmar liberación de calidad</button>
        <button class="smed-btn primary" data-action="record_restart" ${!snapshot.readiness.recorded_conditions_met||s.restarted_at?'disabled':''}>Registrar arranque</button></div>
        <p class="smed-small">Liberación: ${s.quality_released?'registrada':'pendiente'}. ${esc(snapshot.readiness.reasons.join(' '))}</p>
        <p class="smed-small">Estas confirmaciones documentan el escenario. La aplicación no opera máquinas ni certifica higiene.</p></div>
      <div class="smed-panel"><details><summary>Registro de la sesión (${snapshot.events.length})</summary><div class="smed-history">${snapshot.events.map(e=>`${esc(e.clock)} · ${esc(actionName[e.action]||e.action)} ${esc(e.task_id||'')} · ${esc(e.actor)}`).join('<br>')||'Sin actuaciones registradas.'}</div></details></div>`;
    renderPlan();
  }
  function renderPlan(){
    const el=root.querySelector('#smed-plan');if(!el)return;
    if(!plan){el.innerHTML='';return;}
    const p=plan;
    const min=Math.min(...p.rows.map(r=>toMin(r.start))), max=Math.max(...p.rows.map(r=>toMin(r.end))), span=Math.max(1,max-min);
    el.innerHTML=`<div class="smed-kpis"><div class="smed-kpi"><span class="smed-small">Fin de tareas</span><strong>${esc(p.cleaning_finish||'Inviable')}</strong></div>
      <div class="smed-kpi"><span class="smed-small">Primer arranque posible*</span><strong>${esc(p.earliest_restart||'—')}</strong></div>
      <div class="smed-kpi"><span class="smed-small">Producción recuperable</span><strong>${p.recovered_production_minutes} min</strong></div></div>
      <p class="smed-small">${p.feasible?`Duración prevista ${p.cleaning_minutes} min. Objetivo ${p.target_minutes} min: ${p.target_met?'cumplido por este plan':'no alcanzado por este plan'}. Última tarea: ${esc(p.last_task)}.`:esc(p.reasons.join(' '))}</p>
      <p class="smed-small">*Condicionado a las liberaciones humanas y disponibilidad. Plan factible por heurística; no garantiza el óptimo global.</p>
      <div class="smed-overflow">${p.rows.map(r=>`<div class="smed-timeline"><b>${esc(r.task_id)}</b><div class="smed-track"><i style="left:${(toMin(r.start)-min)/span*100}%;width:${(toMin(r.end)-toMin(r.start))/span*100}%" title="${esc(r.start+'–'+r.end)}"></i></div><span class="smed-small">${esc(r.start+'–'+r.end)}</span></div>`).join('')}</div>
      <details><summary>Asignaciones y supuestos</summary><table class="smed-table">${p.rows.map(r=>`<tr><td>${esc(r.task_id)}</td><td>${esc(r.resources.join(', '))}</td><td>${esc(r.start)}–${esc(r.end)}</td></tr>`).join('')}</table><p class="smed-small">${p.assumptions.map(esc).join('<br>')}</p></details>
      ${p.impact?`<h3>Escenario económico</h3><p class="smed-small">${money(p.impact.net_conditional_potential_usd)} potenciales en ${p.impact.days} días, suponiendo el 100 % de aprovechamiento y cero coste adicional. Margen supuesto: 25.000 USD/h. No es ahorro realizado.</p>`:''}`;
  }
  async function ask(question){
    if(chatting||!health.ai)return;
    chatting=true;root.querySelector('#smed-send').disabled=true;root.querySelector('#smed-question').value='';
    addMessage('me',question);const pending=addMessage('ai','Consultando datos y herramientas…');
    try{const reply=await api('/api/chat',{session_id:snapshot.session.id,conversation,question});pending.remove();
      const message=addMessage('ai',reply.text,reply.tool_calls);
      for(const proposal of reply.proposals||[]){const button=document.createElement('button');button.className='smed-btn primary';
        button.textContent='Confirmar: '+(actionName[proposal.action]||proposal.action)+' '+(proposal.task_id||'');
        button.onclick=async()=>{button.disabled=true;try{snapshot=await api('/api/smed/confirm',{session_id:snapshot.session.id,expected_revision:proposal.revision,proposal_id:proposal.id,confirmed:true,actor:actor()});
          button.textContent='Registro confirmado';plan=null;renderLeft();}catch(e){button.textContent='Propuesta no aplicada';error(e.message);await refresh();}};
        message.appendChild(button);}
    }catch(e){pending.textContent=e.message;}
    finally{chatting=false;root.querySelector('#smed-send').disabled=!health.ai;}
  }
  async function mount(element){
    if(ready)return;ready=true;root=element;
    root.innerHTML=`<div id="smed-error" class="smed-error" role="alert"></div><div class="smed-shell"><div id="smed-left">Cargando escenario…</div>
      <aside class="smed-panel smed-chat"><span class="smed-badge">ÚNICO AGENTE · PAROS PROGRAMADOS / SMED</span><h2 style="margin-top:10px">Asistente de limpieza</h2>
      <p id="smed-ai-status" class="smed-small">Comprobando OpenAI…</p><div id="smed-messages" class="smed-messages" aria-live="polite"></div>
      <div class="smed-suggestions">${['¿Qué debemos preparar antes del paro?','Compara preparar los kits antes o durante el paro.','¿Qué pasa si solo queda el equipo de limpieza 1?','¿Por qué terminar antes no siempre recupera producción?'].map(q=>`<button class="smed-btn" data-question="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <form id="smed-composer" class="smed-compose"><textarea id="smed-question" maxlength="3000" placeholder="Pregunta por preparación, recursos o retrasos…" aria-label="Pregunta al asistente SMED"></textarea><button id="smed-send" class="smed-btn primary" disabled>Enviar</button></form>
      <p class="smed-small">El agente consulta herramientas. Cualquier propuesta de registro requiere tu confirmación.</p></aside></div>`;
    messages=root.querySelector('#smed-messages');
    if(!location.protocol.startsWith('http')){root.querySelector('#smed-left').textContent='Abre esta demo con python -m melt_alert.server y entra en http://127.0.0.1:8000.';return;}
    try{
      let id;try{id=localStorage.getItem('melt-smed-session');}catch(e){}
      snapshot=await api('/api/smed'+(id?'?session='+encodeURIComponent(id):''));
      try{localStorage.setItem('melt-smed-session',snapshot.session.id);}catch(e){}
      draft.production_ready_at=snapshot.session.production_ready_at;draft.unavailable_resources=snapshot.session.unavailable_resources;
      if(snapshot.session.stopped_at)draft.stop_at=snapshot.session.stopped_at;
      renderLeft();health=await api('/api/health');
      root.querySelector('#smed-ai-status').textContent=health.ai?'OpenAI · '+health.model+' · conexión API pendiente de la primera consulta.':health.why;
      root.querySelector('#smed-send').disabled=!health.ai;
      addMessage('ai','Puedo preparar el paro, comparar planes y explicar restricciones. Las tareas y recursos son simulados; el histórico original solo documenta el paro global de 30 minutos.');
    }catch(e){error(e.message);root.querySelector('#smed-left').textContent='No se pudo cargar el escenario. Comprueba el servidor.';}
    root.addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.dataset.question)return ask(b.dataset.question);
        if(b.dataset.start)return event('start_task',b.dataset.start);
        if(b.dataset.finish)return event('complete_task',b.dataset.finish);
        const action=b.dataset.action;if(!action)return;
        if(action==='plan')return simulate();
        if(action==='clock')return event('set_clock',null,root.querySelector('#smed-clock').value);
        if(action==='plus5'){const t=toMin(snapshot.session.clock)+5;if(t>=1440)throw new Error('El escenario termina en el mismo día.');return event('set_clock',null,String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0'));}
        if(action==='settings'){readDraft();return event('settings',null,{production_ready_at:draft.production_ready_at,unavailable_resources:draft.unavailable_resources});}
        if(action==='readiness')return event('confirm_readiness',null,{staff_ready:root.querySelector('#smed-staff').checked,material_ready:root.querySelector('#smed-material').checked});
        if(action==='new'){snapshot=await api('/api/smed/session',{});try{localStorage.setItem('melt-smed-session',snapshot.session.id);}catch(e){}conversation=crypto.randomUUID();plan=null;draft={prepare_before_stop:true,stop_at:'14:00',production_ready_at:'14:30',target_stop_minutes:15,unavailable_resources:[],delays:[]};messages.textContent='';error('');return renderLeft();}
        return event(action);
      }catch(err){error(err.message);}
    });
    root.querySelector('#smed-composer').addEventListener('submit',e=>{e.preventDefault();const q=root.querySelector('#smed-question').value.trim();if(q)ask(q);});
  }
  return {mount};
})();
