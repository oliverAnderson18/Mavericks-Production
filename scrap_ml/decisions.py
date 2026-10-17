"""Three-action policy and persistent, idempotent workflow. No PLC commands.

Prediction never consumes final outcomes. An unvalidated correction is a request,
not a pause or an assumed recovery. Calibrated causal effect must come from a pilot.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import joblib
import pandas as pd
from .full_line import dumps
from .full_line_policy import stage_costs, replay as economic_replay, daily_gain_interval
from .machine_system import OUT as MACHINE_OUT, QUALITY_PATH, assess

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'artifacts_decisions'
CONFIG = ROOT / 'decision_config.json'


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def number(value, minimum=0, maximum=math.inf):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and minimum <= value <= maximum)


def correction_option(snapshot, item, config, quality_config, size, best_alternative):
    """Separate a model-profile sensitivity from a validated treatment bound."""
    machine = item['machine']; stage = snapshot['stage']
    spec = quality_config['stages']; capability = config['machines'].get(machine, {})
    now = pd.Timestamp(snapshot['decision_available_at'])
    arrival = pd.Timestamp(snapshot['observation_minute']) + pd.Timedelta(
        minutes=spec[stage]['lead_minutes'] - spec[machine]['lead_minutes'])
    minutes_left = (arrival-now).total_seconds()/60
    option = {'machine': machine, 'event_score': item['event_score'],
              'minutes_until_exposure_at_decision': minutes_left,
              'correction_automatic_eligible': False, 'blockers': [],
              'additional_savings_booked_usd': 0.0}
    blockers = option['blockers']
    if spec[machine]['sequence'] <= spec[stage]['sequence'] or minutes_left <= 0:
        blockers.append('COLECTIVO_YA_EXPUESTO_O_SIN_TIEMPO')
        option['scope'] = 'future_cohorts_only'
        return option
    option['scope'] = 'current_cohort_candidate'
    p = snapshot['predicted_reject_fraction']
    profile = item.get('profile_risk')
    drop = max(0., p-profile) if number(profile, 0, 1) else None
    option['profile_drop_fraction_not_causal'] = drop
    # Sensitivity helps form an unvalidated hypothesis. It must not overrule a
    # subsequently measured causal effect or become a requirement for that effect.
    if drop is None: option['profile_note']='FALTA_SENSIBILIDAD_DEL_MODELO'
    elif drop <= 0: option['profile_note']='PERFIL_SALUDABLE_NO_REDUCE_RIESGO'
    fields = ['response_minutes', 'adjustment_minutes', 'fixed_adjustment_cost_usd',
              'downtime_cost_usd_per_minute', 'quality_check_cost_usd']
    complete = all(number(capability.get(k)) for k in fields)
    total_cost = None
    if complete:
        duration = capability['response_minutes'] + capability['adjustment_minutes']
        total_cost = (capability['fixed_adjustment_cost_usd'] + capability['quality_check_cost_usd']
                      + duration*capability['downtime_cost_usd_per_minute'])
        option.update({'response_and_adjustment_minutes': duration,
                       'full_intervention_cost_usd': total_cost})
        if duration > minutes_left: blockers.append('AJUSTE_NO_TERMINA_A_TIEMPO')
    else: blockers.append('FALTAN_TIEMPOS_O_COSTES_DE_INTERVENCION')
    if not capability.get('approved_by_operations'): blockers.append('CAPACIDAD_SIN_CONFIRMAR')
    if capability.get('can_hold_before_machine') is not True: blockers.append('RETENCION_NO_CONFIRMADA')
    if not config.get('transport_verified_by_operations'): blockers.append('TRANSPORTE_SIN_VERIFICAR')
    if not config.get('costs_confirmed_by_business'): blockers.append('COSTES_SIN_CONFIRMAR')
    final_value = quality_config['scenario_assumptions']['final_material_value_usd_per_piece']
    extra = quality_config['scenario_assumptions']['scenarios'][config['scenario']]['extra_good_piece_opportunity_loss_usd']
    value_per_saved_bar = final_value + extra
    margin = config.get('minimum_correction_advantage_usd', 0.)
    if total_cost is not None and drop is not None and drop > 0:
        # q is a CONDITIONAL scenario, not an estimated intervention success rate.
        q_min = (total_cost + best_alternative + margin)/(size*value_per_saved_bar*drop)
        option['conditional_break_even_success_fraction'] = q_min
        option['conditional_break_even_possible'] = q_min < 1
    effect = capability.get('effect', {})
    delta = effect.get('reject_fraction_reduction_lower_bound')
    valid_until = effect.get('valid_until')
    try: valid = valid_until is not None and now <= pd.Timestamp(valid_until)
    except (ValueError, TypeError): valid = False
    validated = (effect.get('validated') is True and bool(effect.get('study_reference'))
                 and number(delta, 0, 1) and delta > 0 and stage in effect.get('stages', []) and valid)
    if not validated: blockers.append('SIN_EFECTO_CAUSAL_VALIDADO_PARA_ESTA_ETAPA')
    if validated and total_cost is not None:
        # Direct lower bound in absolute rejection-rate points, not q * sensitivity.
        gain = size*min(p, delta)*value_per_saved_bar-total_cost
        option['validated_expected_gain_lower_bound_usd'] = gain
        if gain <= best_alternative+margin: blockers.append('NO_SUPERA_LA_MEJOR_ALTERNATIVA')
    option['correction_automatic_eligible'] = not blockers
    return option


def line_correction_option(snapshot,item,config):
    """Correct the machine for the following stream, not the current cohort."""
    policy=config['line_correction_policy']
    if not policy.get('enabled') or snapshot['stage']!=policy['selection_stage']:
        return None
    risk=snapshot.get('predicted_reject_fraction')
    if not item.get('event_flag') or not number(risk,0,1) or risk<policy['quality_alert_threshold']:
        return None
    return {'machine':item['machine'],'event_score':item['event_score'],
            'quality_risk':risk,'quality_alert_threshold':policy['quality_alert_threshold'],
            'response_horizon_minutes':policy['response_horizon_minutes'],
            'action_scope':'future_stream_after_operator_correction',
            'operator_confirmation_required':policy['operator_confirmation_required'],
            'automatic_machine_actuation':False,'live_savings_usd':None,
            'reason':'RIESGO_CALIDAD_Y_EVENTO_MAQUINA_COINCIDEN'}


def decide(snapshot, config, quality_config, cohort_size, now=None):
    """Only inference fields are read; rejects/rate/cost labels are ignored."""
    if not number(cohort_size, 1): raise ValueError('El tamano de entrada debe ser conocido y positivo.')
    if config.get('execution_mode') != 'simulation':
        raise ValueError('Esta implementacion emite simulaciones; no integra actuadores fisicos.')
    if config.get('scenario') != 'materiales':
        raise ValueError('El snapshot integrado y su politica estan congelados para materiales.')
    stage = snapshot['stage']; p = snapshot.get('predicted_reject_fraction')
    result = {'version': config['version'], 'cohort_id': snapshot['cohort_id'], 'stage': stage,
              'observation_minute': snapshot['observation_minute'],
              'decision_available_at': snapshot['decision_available_at'],
              'action': 'CONTINUAR', 'recommended_action': 'CONTINUAR',
              'cohort_action':'CONTINUAR','line_action':'MANTENER_LINEA',
              'combined_action':'CONTINUAR',
              'physical_actuation': False, 'cohort_size_at_input': cohort_size,
              'correction_options': [], 'additional_correction_savings_usd': None,
              'deployment_status': 'SIMULACION_REQUIERE_INTEGRACION_Y_VERIFICACION'}
    if now is not None:
        age = (pd.Timestamp(now)-pd.Timestamp(snapshot['decision_available_at'])).total_seconds()/60
        if age < 0 or age > config['maximum_snapshot_age_minutes']:
            result.update(action='REVISAR', recommended_action='REVISAR', reason='LECTURA_FUTURA_O_CADUCADA')
            return result
    role = quality_config['stages'][stage]['role']
    if role != 'prediction':
        result['reason'] = 'RESULTADO_OBSERVADO_NO_ES_DECISION_ANTICIPADA'
        return result
    if not number(p, 0, 1) or snapshot.get('quality_status') == 'abstain':
        result.update(action='REVISAR', recommended_action='REVISAR', reason='CALIDAD_FUERA_DE_DOMINIO')
        return result
    future, false_loss, disposal = stage_costs(quality_config, config['scenario'], stage)
    gain = cohort_size*(p*future-(1-p)*false_loss-disposal)
    # Recompute from risk and frozen configuration. Never trust an action string
    # received in an incoming message as authority to discard product.
    policy=config['quality_policy'];spec=quality_config['stages'][stage]
    cut=(false_loss+disposal)/(future+false_loss)+policy['safety_margin'] if future else 1.
    eligible=(spec.get('allow_early_discard',True) and
              spec['lead_minutes']>quality_config['feature_availability_delay_minutes'] and future>0)
    gate=policy['gate']
    allowed=(gate=='none' or any(v.get('event_flag') and (gate=='any' or
        quality_config['stages'][v['machine']]['sequence']<=spec['sequence']) for v in snapshot.get('machine_ranking',[])))
    discard=eligible and allowed and not policy['disabled'] and p>cut
    result.update(predicted_reject_fraction=p, expected_discard_gain_proxy_usd=gain,
                  economic_threshold=cut)
    if discard:
        result.update(action='DESCARTAR', recommended_action='DESCARTAR', reason='POLITICA_DE_DESCARTE_CONGELADA')
    best = max(0., gain if discard else 0.)
    for item in snapshot.get('machine_ranking', []):
        if item.get('event_flag'):
            result['correction_options'].append(correction_option(snapshot, item, config, quality_config, cohort_size, best))
    eligible = [o for o in result['correction_options'] if o['correction_automatic_eligible']]
    if eligible:
        option = max(eligible, key=lambda o: o['validated_expected_gain_lower_bound_usd'])
        result.update(action='CORREGIR', recommended_action='CORREGIR', machine=option['machine'],
                      reason='EFECTO_VALIDADO_SUPERA_CONTINUAR_Y_DESCARTAR',
                      expected_correction_gain_lower_bound_usd=option['validated_expected_gain_lower_bound_usd'])
    elif not discard:
        requests = [o for o in result['correction_options'] if o['scope'] == 'current_cohort_candidate'
                    and (o.get('profile_drop_fraction_not_causal') is None or o['profile_drop_fraction_not_causal'] > 0)]
        if requests:
            option = max(requests, key=lambda o: o['event_score'])
            result.update(current_cohort_correction_candidate=option,
                          current_cohort_candidate_reason='PENDIENTE_DE_CAPACIDAD_COSTES_Y_EFECTO')
    # Correcting a machine protects production that follows it. It therefore
    # coexists with continuing or discarding this current collective.
    line=[line_correction_option(snapshot,item,config) for item in snapshot.get('machine_ranking',[])]
    line=[x for x in line if x is not None]
    if line:
        option=max(line,key=lambda x:x['event_score'])
        result.update(line_action='CORREGIR_LINEA',line_machine=option['machine'],line_correction=option)
    result['cohort_action']=result['action']
    result['combined_action']=(result['cohort_action']+'_Y_CORREGIR_'+result['line_machine']
        if result['line_action']=='CORREGIR_LINEA' else result['cohort_action'])
    return result


class DecisionRuntime:
    """JSON-serializable closed-loop simulation; use one writer per state file.

    A pending suggestion never pauses a cohort. An approved correction must be
    acknowledged, then re-evaluated using fresh sensors and verified tracking.
    """
    def __init__(self, config, quality_config, state=None):
        self.config=config; self.quality_config=quality_config
        self.state=copy.deepcopy(state) if state else {'cohorts': {}, 'event_keys': {}, 'journal': [],
            'machine_locks': {}, 'line_corrections': {}}

    def step(self, snapshot, cohort_size, now=None):
        key='|'.join(str(snapshot[k]) for k in ['cohort_id', 'stage', 'observation_minute'])
        if key in self.state['event_keys']:
            return copy.deepcopy(self.state['event_keys'][key])
        cid=snapshot['cohort_id']
        cohort=self.state['cohorts'].setdefault(cid, {'status':'RUNNING', 'last_sequence':0,
                                                   'last_observation':None, 'size_at_input':cohort_size})
        if cohort.get('size_at_input') != cohort_size:
            raise ValueError('El tamano del colectivo debe fijarse en la entrada, no con la etiqueta final.')
        sequence=self.quality_config['stages'][snapshot['stage']]['sequence']
        decision=decide(snapshot,self.config,self.quality_config,cohort_size,now)
        decision['event_id']=hashlib.sha256(key.encode()).hexdigest()[:20]
        status=cohort['status']
        if status=='DISCARDED': decision.update(action='NO_PROCESAR', recommended_action='NO_PROCESAR', reason='COLECTIVO_YA_DESCARTADO')
        elif (sequence < cohort['last_sequence'] or (cohort['last_observation'] and
                pd.Timestamp(snapshot['observation_minute']) < pd.Timestamp(cohort['last_observation']))):
            decision.update(action='REVISAR',recommended_action='REVISAR',reason='LECTURA_FUERA_DE_ORDEN')
        elif status=='WAITING_CORRECTION':
            expired=pd.Timestamp(snapshot['decision_available_at'])>pd.Timestamp(cohort['adjustment_expected_by'])
            decision.update(action='REVISAR' if expired else 'ESPERAR',recommended_action='REVISAR' if expired else 'CORREGIR',
                            reason='AJUSTE_EXCEDE_TIEMPO_PREVISTO' if expired else 'AJUSTE_EN_CURSO')
        elif status=='AWAITING_REASSESSMENT':
            machine=cohort['machine']
            fresh=(pd.Timestamp(snapshot['observation_minute']) > pd.Timestamp(cohort['correction_completed_at']))
            resolved=any(v['machine']==machine and not v['event_flag'] for v in snapshot.get('machine_ranking',[]))
            if (not fresh or not snapshot.get('tracking_verified_after_hold') or not resolved or decision['action']=='REVISAR'):
                decision.update(action='REVISAR',recommended_action='REVISAR',reason='FALTA_REEVALUACION_FRESCA_O_TRAZABILIDAD')
            elif decision['action']=='CORREGIR':
                decision.update(action='REVISAR',recommended_action='REVISAR',reason='NUEVA_CORRECCION_REQUIERE_REVISION')
            else: cohort['status']='RUNNING'
        if cohort['status']=='RUNNING':
            if decision['action']=='DESCARTAR': cohort['status']='DISCARDED'
            elif decision['action']=='CORREGIR':
                machine=decision['machine']; lock=self.state['machine_locks'].get(machine)
                if lock:
                    decision.update(action='REVISAR',recommended_action='REVISAR',reason='MAQUINA_CON_AJUSTE_EN_CURSO',existing_adjustment_id=lock)
                else:
                    option=next(o for o in decision['correction_options'] if o['machine']==machine)
                    cohort.update(status='WAITING_CORRECTION', machine=machine,
                                  correction_started_at=snapshot['decision_available_at'], correction_id=decision['event_id'],
                                  adjustment_expected_by=str(pd.Timestamp(snapshot['decision_available_at'])+
                                      pd.Timedelta(minutes=option['response_and_adjustment_minutes'])))
                    self.state['machine_locks'][machine]=decision['event_id']
        # This lock prevents two nearby quality alerts from producing duplicate
        # repair tickets for the same machine. It never actuates equipment.
        if decision['line_action']=='CORREGIR_LINEA':
            machine=decision['line_machine'];active=self.state['line_corrections'].get(machine)
            horizon=decision['line_correction']['response_horizon_minutes']
            now_at=pd.Timestamp(snapshot['decision_available_at'])
            if active and now_at<=pd.Timestamp(active['expires_at']):
                decision.update(line_action='CORRECCION_LINEA_EN_CURSO',
                    line_correction_id=active['id'],line_action_status='YA_SOLICITADA')
            else:
                correction_id='line-'+decision['event_id']
                self.state['line_corrections'][machine]={'id':correction_id,'machine':machine,
                    'requested_at':snapshot['decision_available_at'],
                    'expires_at':str(now_at+pd.Timedelta(minutes=horizon)),
                    'status':'PENDIENTE_CONFIRMACION_OPERADOR'}
                decision.update(line_correction_id=correction_id,
                    line_action_status='PENDIENTE_CONFIRMACION_OPERADOR')
        cohort['last_sequence']=max(sequence,cohort['last_sequence'])
        if cohort['last_observation'] is None or pd.Timestamp(snapshot['observation_minute'])>=pd.Timestamp(cohort['last_observation']):
            cohort['last_observation']=snapshot['observation_minute']
        decision['cohort_state']=cohort['status']
        self.state['journal'].append(copy.deepcopy(decision))
        self.state['event_keys'][key]=copy.deepcopy(decision)
        return decision

    def correction_completed(self, cohort_id, correction_id, completed_at):
        cohort=self.state['cohorts'][cohort_id]
        if cohort.get('correction_id') != correction_id: raise ValueError('Identificador de ajuste incorrecto.')
        if cohort['status']=='AWAITING_REASSESSMENT': return
        if cohort['status']!='WAITING_CORRECTION': raise ValueError('No hay ajuste pendiente.')
        if pd.Timestamp(completed_at)<pd.Timestamp(cohort['correction_started_at']):
            raise ValueError('El ajuste no puede terminar antes de iniciarse.')
        cohort.update(status='AWAITING_REASSESSMENT', correction_completed_at=str(pd.Timestamp(completed_at)))
        self.state['machine_locks'].pop(cohort['machine'],None)
        self.state['journal'].append({'feedback':'adjustment_completed_not_quality_recovery',
            'cohort_id':cohort_id,'correction_id':correction_id,'completed_at':completed_at})


def write_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(dumps(value),encoding='utf-8'); temporary.replace(path)


def replay_journey(path, config):
    quality=joblib.load(QUALITY_PATH)
    journey=json.loads(Path(path).read_text(encoding='utf-8'))
    runtime=DecisionRuntime(config,quality['config']);rows=[]
    for snapshot in journey['stations']:
        if 'observation_minute' not in snapshot: continue
        # Refresh sensitivity only where needed: inputs stay bounded by assess.
        rows.append(runtime.step(snapshot,config['cohort_size_at_input']))
    return {'mode':'historical_replay','physical_actuation':False,'cohort_id':journey['cohort_id'],
            'individual_tracking':False,'stations':rows,'state':runtime.state,
            'correction_savings_not_estimated':True}


def evaluate(config, data_dir=None):
    """Replay current unvalidated policy. Final counts enter accounting only.

    Offline cohorts are those of the existing qualified population. To prevent
    fake counterfactuals, refuse to evaluate a configured correction as if the
    historical outcome were unchanged by it.
    """
    if any(v.get('effect',{}).get('validated') for v in config['machines'].values()):
        raise ValueError('Evaluar correcciones exige resultados de intervencion; no se reutiliza la etiqueta sin tratar.')
    quality=joblib.load(QUALITY_PATH); cfg=quality['config']; policy=quality['policies']['materiales']
    if any(config['quality_policy'][k]!=policy[k] for k in ['safety_margin','disabled']):
        raise ValueError('La configuracion no coincide con la politica congelada que se pretende evaluar.')
    OUT.mkdir(exist_ok=True);report={'version':config['version'],'splits':{},
        'correction_saving_usd':None,'realized_saving_usd':None,'test_previously_explored':True,
        'correction_effect_assumed':False,'physical_actuation':False}
    diagnostic=joblib.load(MACHINE_OUT/'diagnosis_model.joblib')
    if diagnostic['economic_policies']['materiales']['gate']!='none':
        raise ValueError('Este replay exige la politica conjunta congelada sin filtro diagnostico.')
    scores=pd.read_csv(MACHINE_OUT/'machine_scores.csv',index_col='observation_minute',parse_dates=True)
    for split in ['selection','test']:
        frame=pd.read_csv(ROOT/f'artifacts_improved/predictions_{split}.csv',parse_dates=['observation_minute','decision_at','target_minute'])
        stats,records,trajectory=economic_replay(frame,cfg,'materiales',policy['safety_margin'],'model',policy['disabled'])
        stats['day_bootstrap']=daily_gain_interval(records)
        # Exercise the NEW policy with a label-free input projection and a size
        # known at entry, then compare its FIRST choices to the accounting replay.
        chosen=[]
        cols=['cohort_id','stage','observation_minute','decision_at','p_reject',
              'economic_threshold','candidate_discard']
        for cid,stage,obs,available,p,cut,discard in trajectory[cols].itertuples(index=False,name=None):
            snapshot={'cohort_id':cid,'stage':stage,'observation_minute':str(obs),
                'decision_available_at':str(available),'predicted_reject_fraction':float(p),
                'economic_threshold':float(cut),'machine_ranking':[],
                'simulation_action':'DESCARTAR_COLECTIVO' if discard else 'CONTINUAR'}
            action=decide(snapshot,config,cfg,config['cohort_size_at_input'])['action']
            if action=='DESCARTAR':chosen.append((cid,stage,cfg['stages'][stage]['sequence']))
        actual=pd.DataFrame(chosen,columns=['cohort_id','stage','sequence'])
        first=actual.sort_values(['cohort_id','sequence']).drop_duplicates('cohort_id')
        expected=records[records.proposed_discard_station.ne('NO_EARLY_DISCARD')]
        if dict(zip(first.cohort_id,first.stage)) != dict(zip(expected.cohort_id,expected.proposed_discard_station)):
            raise AssertionError('La politica de tres decisiones cambio el descarte sin contabilizarlo.')
        stats['new_policy_first_discards_match_frozen_replay']=True
        # Avoid claim inflation: these are requests on untreated, non-discarded
        # historical cohorts, with no modeled effectiveness or paused trajectory.
        requests=[]
        for machine in diagnostic['machines']:
            flagged=scores[machine].ge(diagnostic['machines'][machine]['threshold'])
            current=trajectory.observation_minute.map(flagged).fillna(False).astype(bool)
            remaining=trajectory.effective_lead_minutes-cfg['stages'][machine]['lead_minutes']
            ahead=trajectory.stage_sequence.lt(cfg['stages'][machine]['sequence']) & remaining.gt(0)
            alive=trajectory.replay_state.eq('CONTINUAR')
            indices=trajectory.index[current & ahead & alive]
            requests.extend({'cohort_id':trajectory.loc[i,'cohort_id'],'machine':machine,
                             'observation_minute':trajectory.loc[i,'observation_minute']} for i in indices)
        req=pd.DataFrame(requests,columns=['cohort_id','machine','observation_minute']).drop_duplicates(['cohort_id','machine'])
        req.to_csv(OUT/f'correction_requests_{split}.csv',index=False)
        records.to_csv(OUT/f'cohorts_{split}.csv',index=False)
        report['splits'][split]={**stats,'automatic_current_cohort_corrections':0,
            'diagnostic_correction_requests_before_exposure':len(req),
            'cohorts_with_request':int(req.cohort_id.nunique()),
            'requests_are_not_quality_effects':True,
            'quality_sensitivity_not_screened_in_bulk_requests':True}
        print(f'{split}: {stats["net_scenario_usd"]:.2f} USD; {len(req)} solicitudes de revision sin ahorro imputado.',flush=True)
    # Denominators from raw accounting, not rounded 200k or overlapping OOS labels.
    report['sources']={'quality':'artifacts_improved/model.joblib','diagnosis':'artifacts_machine_system/diagnosis_model.joblib'}
    test=report['splits']['test']; ratio=test['net_scenario_usd']/test['historical_final_reject_cost_usd']
    # These exact source totals are audited again by prepare() before reporting.
    report['within_evaluated_FIN_BAR_net_fraction']=ratio
    if data_dir is not None:
        from .correction_study import study
        correction=study(data_dir)
        report['line_correction_evidence']=correction
        # Products in a correction response window are credited once, through
        # the correction value-at-risk, never again through early discard.
        for split,stats in report['splits'].items():
            episodes=pd.read_csv(ROOT/'artifacts_correction_study'/f'operational_corrections_{split}.csv',
                                 parse_dates=['alert_time','response_window_end'])
            records=pd.read_csv(OUT/f'cohorts_{split}.csv',parse_dates=['target_minute'])
            selected=records[records.proposed_discard_station.ne('NO_EARLY_DISCARD')].copy()
            covered=pd.Series(False,index=selected.index)
            for episode in episodes.itertuples():
                covered|=(selected.target_minute>episode.alert_time)&(selected.target_minute<=episode.response_window_end)
            residual=float(selected.loc[~covered,'net_scenario_usd'].sum())
            potential=float(episodes.excess_FIN_BAR_cost_at_risk_usd.sum())
            stats['line_correction_requests']=len(episodes)
            stats['line_correction_excess_FIN_BAR_cost_at_risk_usd']=potential
            stats['early_discard_net_outside_correction_windows_usd']=residual
            stats['combined_conditional_value_before_correction_cost_usd']=potential+residual
            stats['combined_is_not_realized_or_net_correction_saving']=True
        scrap=pd.read_csv(Path(data_dir)/'fct_scrap_minute.csv')
        total=float(scrap.cost_usd.sum()); finished=float(scrap.loc[scrap.collection_point_code.eq('FIN_BAR'),'cost_usd'].sum())
        share=finished/total
        combined=report['splits']['test']['combined_conditional_value_before_correction_cost_usd']
        combined_ratio=combined/report['splits']['test']['historical_final_reject_cost_usd']
        report['accounting']={'total_scrap_usd':total,'FIN_BAR_usd':finished,'FIN_BAR_fraction':share,
            'extrapolation_200k_total_scrap_usd':200000*share*ratio,
            'extrapolation_full_period_usd':finished*ratio,
            'conditional_combined_extrapolation_200k_total_scrap_usd':200000*share*combined_ratio,
            'conditional_combined_extrapolation_full_period_usd':finished*combined_ratio,
            'extrapolation_requires_same_risk_cost_mix_and_coverage':True,
            'extrapolation_is_not_a_saving_guarantee':True}
    write_json(OUT/'metrics.json',report)
    return report


def prepare(data_dir, config):
    from .data import load_sources
    quality=joblib.load(QUALITY_PATH); diagnostic=joblib.load(MACHINE_OUT/'diagnosis_model.joblib')
    source=json.loads((MACHINE_OUT/'example_snapshot.json').read_text(encoding='utf-8'))
    sources=load_sources(data_dir)
    snapshot=assess(sources,diagnostic,quality,source['stage'],source['observation_minute'])
    write_json(OUT/'example_snapshot.json',snapshot)
    write_json(OUT/'example_decision.json',decide(snapshot,config,quality['config'],config['cohort_size_at_input']))
    journey=json.loads((MACHINE_OUT/'example_journey_15.json').read_text(encoding='utf-8'))
    # Add actual model sensitivity to each predictive observation of the demo.
    for i, row in enumerate(journey['stations']):
        if any(m['event_flag'] and m['relation_to_cohort']=='downstream_current_state' for m in row.get('machine_ranking',[])):
            journey['stations'][i]=assess(sources,diagnostic,quality,row['stage'],row['observation_minute'])
    write_json(OUT/'example_input_journey_15.json',journey)
    write_json(OUT/'example_decisions_15.json',replay_journey(OUT/'example_input_journey_15.json',config))
    evaluate(config,data_dir)
    print('Ejemplo de 15 estaciones, decisiones y contabilidad guardados.',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','evaluate','predict','replay','step','feedback'])
    parser.add_argument('--config',default=str(CONFIG));parser.add_argument('--data-dir')
    parser.add_argument('--stage',default='CAM_POST');parser.add_argument('--observation-minute')
    parser.add_argument('--cohort-size',type=int);parser.add_argument('--input');parser.add_argument('--output')
    parser.add_argument('--state',default=str(OUT/'runtime_state.json'));parser.add_argument('--now')
    parser.add_argument('--cohort-id');parser.add_argument('--correction-id');parser.add_argument('--completed-at')
    args=parser.parse_args();config=load_config(args.config)
    if args.command=='prepare':
        if not args.data_dir:parser.error('--data-dir obligatorio')
        prepare(args.data_dir,config);return
    if args.command=='evaluate':evaluate(config,args.data_dir);return
    if args.command=='replay':
        if not args.input:parser.error('--input obligatorio')
        result=replay_journey(args.input,config)
    else:
        quality=joblib.load(QUALITY_PATH)
        if args.command=='feedback':
            if not all([args.cohort_id,args.correction_id,args.completed_at]):parser.error('Faltan argumentos del ajuste')
            state=json.loads(Path(args.state).read_text(encoding='utf-8'))
            runtime=DecisionRuntime(config,quality['config'],state)
            runtime.correction_completed(args.cohort_id,args.correction_id,args.completed_at)
            write_json(args.state,runtime.state);result={'status':'AWAITING_REASSESSMENT','physical_actuation':False}
        else:
            if args.command=='predict':
                from .data import load_sources
                if not args.data_dir or not args.observation_minute:parser.error('Falta --data-dir o --observation-minute')
                diagnostic=joblib.load(MACHINE_OUT/'diagnosis_model.joblib')
                snapshot=assess(load_sources(args.data_dir,cutoff=args.observation_minute),diagnostic,quality,args.stage,args.observation_minute)
            else:
                if not args.input or not args.now:parser.error('step exige --input y --now para comprobar frescura')
                snapshot=json.loads(Path(args.input).read_text(encoding='utf-8'))
            size=args.cohort_size if args.cohort_size is not None else config['cohort_size_at_input']
            if args.command=='predict':result=decide(snapshot,config,quality['config'],size,args.now)
            else:
                state=json.loads(Path(args.state).read_text(encoding='utf-8')) if Path(args.state).exists() else None
                runtime=DecisionRuntime(config,quality['config'],state)
                result=runtime.step(snapshot,size,args.now);write_json(args.state,runtime.state)
    if args.output:write_json(args.output,result)
    else:print(dumps(result))


if __name__=='__main__':main()
