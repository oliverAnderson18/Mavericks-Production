"""Quality risk (V3) plus independent machine diagnosis and guarded replay.

Diagnosis uses event annotations only as offline targets, never as features.
python -m scrap_ml.machine_system train --data-dir PATH
python -m scrap_ml.machine_system predict --data-dir PATH --stage CAM_POST --observation-minute TIME
python -m scrap_ml.machine_system replay --data-dir PATH --cohort-id YYYYMMDDTHHMM
"""
import argparse
import json
from pathlib import Path
import time
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import precision_recall_fscore_support, brier_score_loss
from threadpoolctl import threadpool_limits
from .data import load_sources, read_csv, fingerprint
from .full_line_data import build_full_line_dataset, build_station_features, predictive_stages
from .improved import transient_features, augment, score as quality_score, predict_family
from .full_line import dumps
from .full_line_policy import replay as economic_replay, daily_gain_interval
from .train import split_masks

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'artifacts_machine_system'
QUALITY_PATH=ROOT/'artifacts_improved/model.joblib'
MACHINES=['MIX','DST','MLD','OVN','BCL','BCM','BCR','CAR','GUI','ENR','CCL']


def machine_features(sources):
    t=sources['telemetry'];ix=t.index
    segment=(~ix.to_series().diff().eq(pd.Timedelta(minutes=1))).cumsum()
    delta=t-t.groupby(segment).shift(1)
    # Missing first-minute deltas do not import yesterday's state.
    return pd.concat([t.add_prefix('actual__'),(t-sources['setpoints']).add_prefix('error__'),
                      delta.add_prefix('change1__')],axis=1).astype('float32')


def event_targets(events,index):
    labels=pd.DataFrame(False,index=index,columns=MACHINES)
    use=events[events.machine_code.isin(MACHINES)&events.loss_type.ne('Setup')]
    for e in use.itertuples():
        labels.loc[(index>=e.started_at)&(index<e.ended_at),e.machine_code]=True
    return labels


def diagnosis_scores(bundle,x):
    result=pd.DataFrame(index=x.index)
    with threadpool_limits(limits=4):
        for machine,item in bundle['machines'].items():
            raw=item['model'].predict_proba(x[item['features']])[:,1]
            if item['calibrator'] is not None:raw=item['calibrator'].predict(raw)
            result[machine]=raw
    return result


def score_metrics(labels,predictions):
    a,b,c,_=precision_recall_fscore_support(labels,predictions,average='binary',zero_division=0)
    return {'precision':float(a),'recall':float(b),'f1':float(c),'positive_minutes':int(np.sum(labels)),
            'predicted_positive_minutes':int(np.sum(predictions))}


def group_columns(columns,machine):
    return [c for c in columns if '__' in c and c.split('__')[-1].startswith(machine+'_')]


def recompute_physical(x):
    pairs={'thermal_shock_z1_dough':('OVN_Z1_TEMP_C','MIX_DOUGH_TEMP_C'),
           'thermal_shock_z2_z1':('OVN_Z2_TEMP_C','OVN_Z1_TEMP_C'),
           'cooler_shock':('BCL_PRODUCT_IN_C','BCL_AIR_TEMP_C')}
    for dest,(a,b) in pairs.items():
        if 'now__'+a in x and 'now__'+b in x:x[dest]=x['now__'+a]-x['now__'+b]
    if 'now__GUI_PHASE_ERROR_MS' in x:x['guillotine_phase_abs']=x.now__GUI_PHASE_ERROR_MS.abs()
    if 'now__GUI_CUT_LENGTH_MM' in x:
        x['guillotine_rate_mismatch']=x.now__GUI_STROKE_PER_MIN-1000*x.now__GUI_LINE_SPEED_M_MIN/x.now__GUI_CUT_LENGTH_MM
    return x


def profile_sensitivity(bundle,quality,x,stage):
    """Observed training profiles; sensitivity is NOT a causal treatment effect."""
    reference=bundle['references'][stage];policy=quality['policies']['materiales']
    family=quality['families'][policy['family']]
    original=float(predict_family(family,x,[stage])[0])
    variants=[];machines=[]
    for machine in MACHINES:
        cols=group_columns(x.columns,machine)
        context=[c for c in x if c.startswith('now__') and not c.startswith('now__'+machine+'_')]
        scale=reference[context].std().replace(0,1).fillna(1)
        distance=(((reference[context]-x.iloc[0][context])/scale)**2).mean(axis=1)
        donors=reference.loc[distance.nsmallest(3).index]
        for _,donor in donors.iterrows():
            alt=x.copy();alt.loc[:,cols]=donor[cols].to_numpy(dtype='float32')
            variants.append(recompute_physical(alt));machines.append(machine)
    rows=pd.concat(variants,ignore_index=True)
    probabilities=predict_family(family,rows,[stage]*len(rows))
    blend=policy['persistence_blend']
    original=(1-blend)*original+blend*float(x.observed_final_reject_rate_5m.iloc[0])
    probabilities=(1-blend)*probabilities+blend*float(x.observed_final_reject_rate_5m.iloc[0])
    values=pd.DataFrame({'machine':machines,'p':probabilities}).groupby('machine').p.median()
    return {m:{'profile_risk':float(values[m]),'risk_drop_percentage_points':float(100*(original-values[m]))}
            for m in MACHINES}


def assess(sources,bundle,quality,stage,observation_minute,with_sensitivity=True):
    obs=pd.Timestamp(observation_minute)
    # Bound all sources before any feature construction or diagnosis.
    bounded={k:v.loc[v.index<=obs].copy() for k,v in sources.items()}
    try:
        result=quality_score(bounded,quality,stage,obs)
    except ValueError as exc:
        # A stoppage invalidates nominal transport for quality; diagnosis can
        # still be available from current machine sensors during that stoppage.
        spec=quality['config']['stages'][stage]
        result={'version':quality['version'],'stage':stage,'observation_minute':str(obs),
                'cohort_id':(obs+pd.Timedelta(minutes=spec['lead_minutes'])).strftime('%Y%m%dT%H%M'),
                'decision_available_at':str(obs+pd.Timedelta(minutes=1)),
                'predicted_reject_fraction':None,'economic_threshold':None,
                'simulation_action':'REVISAR_CALIDAD_FUERA_DE_DOMINIO','physical_actuation':False,
                'quality_status':'abstain','quality_reason':str(exc)}
    result['system_version']=bundle['version']
    is_predictive=stage in predictive_stages(quality['config'])
    if not is_predictive:
        result['quality_status']='observed_final_outcome' if stage=='LSR_BAR' else 'no_new_quality_label'
    mx=machine_features(bounded).loc[[obs]]
    scores=diagnosis_scores(bundle,mx).iloc[0]
    order=sorted(MACHINES,key=lambda m:float(scores[m]),reverse=True)
    active=[m for m in order if scores[m]>=bundle['machines'][m]['threshold']]
    sensitivity={}
    if with_sensitivity and is_predictive and result.get('predicted_reject_fraction') is not None:
        x,_,_=build_station_features(bounded,stage,quality['config'])
        x=pd.concat([x,transient_features(bounded)],axis=1).loc[[obs]].reset_index(drop=True)
        sensitivity=profile_sensitivity(bundle,quality,x,stage)
    ranking=[]
    for machine in order:
        value=bundle['machines'][machine];features=value['features']
        actual=mx.iloc[0][features]
        z=((actual-value['reference_median'])/value['reference_scale']).abs().dropna().sort_values(ascending=False)
        parameters=[]
        for name in z.index[:3]:
            parameters.append({'parameter':name,'observed':float(actual[name]),
                               'reference_median':float(value['reference_median'][name]),
                               'reference_deviation_units':float(z[name])})
        ranking.append({'machine':machine,'machine_label':quality['config']['stages'][machine]['label'],
                        'event_score':float(scores[machine]),'threshold':value['threshold'],
                        'event_flag':machine in active,'evidence':parameters,
                        'relation_to_cohort':'already_reached' if quality['config']['stages'][machine]['sequence']<=quality['config']['stages'][stage]['sequence'] else 'downstream_current_state',
                        **sensitivity.get(machine,{})})
    gate=bundle['economic_policies']['materiales']['gate']
    allow=gate=='none' or bool(active)
    if gate=='reached':allow=any(quality['config']['stages'][m]['sequence']<=quality['config']['stages'][stage]['sequence'] for m in active)
    result.update({'diagnosis_kind':'active_registered_event_estimate_not_scrap_causality',
                   'likely_fault_machine':active[0] if active else None,
                   'diagnostic_status':'event_detected' if active else 'no_supported_event',
                   'machine_ranking':ranking,'economic_gate':gate,
                   'paired_simulation_action':result['simulation_action'] if allow else 'CONTINUAR',
                   'preventive_savings_usd':None,'physical_actuation':False})
    return result


def train(data_dir):
    start=time.time();OUT.mkdir(exist_ok=True)
    quality=joblib.load(QUALITY_PATH);cfg=quality['config']
    print('Cargando telemetria y etiquetas de incidencia independientes...',flush=True)
    sources=load_sources(data_dir)
    events=pd.read_csv(Path(data_dir)/'fct_problem_event.csv',parse_dates=['started_at','ended_at'])
    x=machine_features(sources);labels=event_targets(events,x.index)
    tr=x.index<pd.Timestamp('2026-08-31');cal=(x.index>=pd.Timestamp('2026-08-31'))&(x.index<pd.Timestamp('2026-09-07'))
    val=(x.index>=pd.Timestamp('2026-09-07'))&(x.index<pd.Timestamp('2026-09-21'))
    test=x.index>=pd.Timestamp('2026-09-21')
    bundle={'version':'two_layer_v4','machines':{},'references':{},'economic_policies':{},
            'training_cutoff':'2026-08-31','quality_model_path':str(QUALITY_PATH),
            'target':'active machine event, excluding Setup and LINE','causal_claim':False}
    machine_metrics={}
    for machine in MACHINES:
        cols=group_columns(x.columns,machine);z=x[cols];target=labels[machine].astype(int)
        if target[tr].nunique()<2:raise ValueError('Insufficient training labels: '+machine)
        npos=int(target[tr].sum());nneg=int(tr.sum()-npos)
        weights=np.where(target[tr].to_numpy()==1,.5*tr.sum()/npos,.5*tr.sum()/nneg)
        model=HistGradientBoostingClassifier(max_iter=110,max_leaf_nodes=15,learning_rate=.08,
                    min_samples_leaf=10,l2_regularization=2,early_stopping=False,random_state=42)
        with threadpool_limits(limits=4):model.fit(z.loc[tr],target.loc[tr],sample_weight=weights)
        calibrator=None
        if target[cal].sum()>=10:
            calibrator=IsotonicRegression(out_of_bounds='clip')
            calibrator.fit(model.predict_proba(z.loc[cal])[:,1],target.loc[cal])
        healthy=z.loc[tr & ~labels[machine].to_numpy()]
        median=healthy.median();scale=(healthy.quantile(.75)-healthy.quantile(.25)).replace(0,1).fillna(1)
        item={'model':model,'calibrator':calibrator,'features':cols,'threshold':.5,
              'reference_median':median,'reference_scale':scale}
        bundle['machines'][machine]=item
        p=model.predict_proba(z.loc[val])[:,1]
        if calibrator is not None:p=calibrator.predict(p)
        trials=[{**score_metrics(target.loc[val],p>=t),'threshold':t} for t in [.1,.25,.5,.75,.9]]
        # Higher threshold breaks ties to reduce spurious flags.
        best=max(trials,key=lambda r:(r['f1'],r['precision'],r['threshold']))
        item['threshold']=best['threshold']
        machine_metrics[machine]={'validation':best,'threshold_trials':trials,
                                  'training_positive_minutes':npos,'calibrated':calibrator is not None}
        print('Diagnostico entrenado '+machine,flush=True)
    # Persist before evaluating labels on the diagnostic test.
    joblib.dump(bundle,OUT/'diagnosis_model.joblib',compress=3)
    scores=diagnosis_scores(bundle,x)
    for machine in MACHINES:
        flag=scores.loc[test,machine]>=bundle['machines'][machine]['threshold']
        machine_metrics[machine]['test']=score_metrics(labels.loc[test,machine],flag)
    test_scores=scores.loc[test];test_labels=labels.loc[test];fault=test_labels.any(axis=1)
    best=test_scores.idxmax(axis=1)
    correct=np.array([bool(test_labels.loc[t,m]) for t,m in best.items()])
    top1=float(correct[fault].mean()) if fault.any() else None
    print('Preparando referencias de calidad y contabilidad conjunta...',flush=True)
    qx,qy,_,coverage=build_full_line_dataset(sources,cfg);qx=augment(qx,qy,sources)
    qmasks=split_masks(qy,cfg)
    for stage in predictive_stages(cfg):
        mask=qmasks['train'] & qy.stage.eq(stage)&qy.rate.le(.01)
        donors=qx.loc[mask].sample(n=min(350,int(mask.sum())),random_state=42).reset_index(drop=True)
        bundle['references'][stage]=donors
    # Gating is tuned on validation; quality model and cost thresholds stay frozen.
    economics={};validation_frames={}
    for scenario,policy in quality['policies'].items():
        validation_frames[scenario]={}
        for split in ['selection','test']:
            mask=qmasks[split];frame=qy.loc[mask].reset_index(drop=True);xx=qx.loc[mask].reset_index(drop=True)
            p=predict_family(quality['families'][policy['family']],xx,frame.stage)
            p=(1-policy['persistence_blend'])*p+policy['persistence_blend']*xx.observed_final_reject_rate_5m.to_numpy()
            frame['p_reject']=p;frame['p_persistence']=xx.observed_final_reject_rate_5m.to_numpy();frame['p_constant']=.01
            validation_frames[scenario][split]=frame
        trials=[]
        for gate in ['none','any','reached']:
            f=apply_gate(validation_frames[scenario]['selection'],scores,bundle,cfg,gate)
            stats,records,_=economic_replay(f,cfg,scenario,policy['safety_margin'],'model',policy['disabled'])
            daily=records.groupby(records.target_minute.lt(pd.Timestamp('2026-09-14'))).net_scenario_usd.sum()
            weeks=[float(daily.get(True,0)),float(daily.get(False,0))]
            trials.append({'gate':gate,'validation_net_usd':stats['net_scenario_usd'],
                           'validation_good_discarded':stats['good_pieces_discarded'],'week1':weeks[0],'week2':weeks[1]})
        admissible=[t for t in trials if t['week1']>=0 and t['week2']>=0]
        chosen=max(admissible,key=lambda r:(r['validation_net_usd'],-r['validation_good_discarded'],r['gate']=='none'))
        bundle['economic_policies'][scenario]={**chosen,'trials':trials}
    joblib.dump(bundle,OUT/'diagnosis_model.joblib',compress=3)
    (OUT/'frozen_policies.json').write_text(dumps(bundle['economic_policies']),encoding='utf-8')
    for scenario,frames in validation_frames.items():
        economics[scenario]={}
        policy=quality['policies'][scenario];gate=bundle['economic_policies'][scenario]['gate']
        for split,frame in frames.items():
            gated=apply_gate(frame,scores,bundle,cfg,gate)
            stats,records,_=economic_replay(gated,cfg,scenario,policy['safety_margin'],'model',policy['disabled'])
            stats['day_bootstrap']=daily_gain_interval(records)
            records.to_csv(OUT/f'cohorts_{split}_{scenario}.csv',index=False)
            economics[scenario][split]=stats
        print(f'{scenario}: filtro {gate}, test {economics[scenario]["test"]["net_scenario_usd"]:.2f} USD',flush=True)
    scores.assign(split=np.where(test,'test','development')).to_csv(OUT/'machine_scores.csv',index_label='observation_minute')
    diag={'evaluated_minutes':int(test.sum()),'fault_minutes':int(fault.sum()),
          'top1_accuracy_on_registered_fault_minutes':top1,'per_machine':machine_metrics,
          'not_a_scrap_cause_accuracy':True}
    summary={'version':bundle['version'],'diagnosis':diag,'economics':economics,
             'coverage':coverage,'frozen_policies':bundle['economic_policies'],
             'preventive_additional_saving_usd':None,'test_previously_explored':True,
             'source_fingerprint':fingerprint(data_dir),'runtime_seconds':time.time()-start}
    (OUT/'metrics.json').write_text(dumps(summary),encoding='utf-8')
    # Choose an illustrative registered test fault after evaluation, not for tuning.
    example=events[(events.started_at>=pd.Timestamp('2026-09-21'))&events.machine_code.isin(MACHINES)&events.loss_type.ne('Setup')].iloc[0]
    candidates=qy[qy.stage.eq('CAM_POST') & (qy.observation_minute>=example.started_at)&(qy.observation_minute<example.ended_at)]
    if len(candidates):
        row=candidates.iloc[0]
        snapshot=assess(sources,bundle,quality,'CAM_POST',row.observation_minute)
        snapshot['illustration_selected_after_evaluation']=True
        (OUT/'example_snapshot.json').write_text(dumps(snapshot),encoding='utf-8')
        make_journey(sources,bundle,quality,row.cohort_id,OUT/'example_journey_15.json')
    print('Entrenamiento y evaluacion terminados.',flush=True)
    return summary


def apply_gate(frame,scores,bundle,cfg,gate):
    out=frame.copy()
    if gate=='none':return out
    aligned=scores.reindex(pd.DatetimeIndex(out.observation_minute)).fillna(0)
    flags=np.column_stack([aligned[m].to_numpy()>=bundle['machines'][m]['threshold'] for m in MACHINES])
    if gate=='reached':
        sequence=out.stage_sequence.to_numpy()
        for j,m in enumerate(MACHINES):flags[:,j]&=sequence>=cfg['stages'][m]['sequence']
    out.loc[~flags.any(axis=1),'p_reject']=0.
    return out


def make_journey(sources,bundle,quality,cohort_id,path):
    target=pd.to_datetime(cohort_id,format='%Y%m%dT%H%M');rows=[]
    for stage,spec in quality['config']['stages'].items():
        obs=target-pd.Timedelta(minutes=spec['lead_minutes'])
        try:row=assess(sources,bundle,quality,stage,obs,with_sensitivity=False)
        except ValueError as exc:row={'stage':stage,'cohort_id':cohort_id,'diagnostic_status':'abstain','reason':str(exc)}
        rows.append(row)
    # Stop decisions are stateful: later nodes must not process a removed cohort.
    discarded=False
    for row in rows:
        if discarded:row['journey_action']='NO_PROCESAR_YA_DESCARTADO'
        else:
            row['journey_action']=row.get('paired_simulation_action',row.get('simulation_action','CONTINUAR'))
            discarded=row['journey_action']=='DESCARTAR_COLECTIVO'
    artifact={'mode':'historical_replay','cohort_id':cohort_id,'stations':rows,
              'individual_tracking':False,'synthetic_machine_fault_injected':False}
    Path(path).write_text(dumps(artifact),encoding='utf-8');return artifact


def finalize(data_dir):
    """Additional reporting + demo illustration. Never changes any model/policy."""
    bundle=joblib.load(OUT/'diagnosis_model.joblib');quality=joblib.load(QUALITY_PATH)
    summary=json.loads((OUT/'metrics.json').read_text())
    scores=pd.read_csv(OUT/'machine_scores.csv',index_col='observation_minute',parse_dates=True)[MACHINES]
    scores=scores.loc[scores.index>=pd.Timestamp('2026-09-21')]
    events=pd.read_csv(Path(data_dir)/'fct_problem_event.csv',parse_dates=['started_at','ended_at'])
    truth=event_targets(events,scores.index)
    flags=pd.DataFrame({m:scores[m]>=bundle['machines'][m]['threshold'] for m in MACHINES})
    any_flag=flags.any(axis=1);any_fault=truth.any(axis=1)
    winner=scores.where(flags,-1).idxmax(axis=1)
    correct=np.array([bool(truth.loc[t,m]) for t,m in winner.items()]) & any_flag.to_numpy()
    summary['diagnosis']['thresholded']={
        'minutes_with_any_flag':int(any_flag.sum()),'correct_machine_flagged_minutes':int(correct.sum()),
        'precision_of_selected_machine':float(correct.sum()/max(1,any_flag.sum())),
        'recall_with_selected_machine':float(correct.sum()/max(1,any_fault.sum())),
        'abstentions_during_registered_fault_minutes':int((~any_flag & any_fault).sum()),
        'machine_flags_without_registered_fault':int((any_flag & ~any_fault).sum())}
    available=[v['test']['f1'] for v in summary['diagnosis']['per_machine'].values() if v['test']['positive_minutes']>0]
    summary['diagnosis']['macro_f1_on_machines_with_test_events']=float(np.mean(available))
    pred=pd.read_csv(ROOT/'artifacts_improved/predictions_test.csv',parse_dates=['observation_minute'])
    pred=pred[pred.stage.eq('CAM_POST')]
    sources=None
    test_events=events[(events.started_at>=pd.Timestamp('2026-09-21'))&events.machine_code.isin(MACHINES)&events.loss_type.ne('Setup')]
    for event in test_events.itertuples():
        candidates=pred[(pred.observation_minute>=event.started_at)&(pred.observation_minute<event.ended_at)]
        for row in candidates.itertuples():
            obs=row.observation_minute
            if not bool(flags.loc[obs,event.machine_code]):continue
            # Explicitly illustrative after evaluation, not independent evidence.
            sources=load_sources(data_dir)
            snapshot=assess(sources,bundle,quality,'CAM_POST',obs)
            snapshot.update({'illustration_selected_after_evaluation':True,
                             'registered_event_for_illustration':event.problem_code,
                             'registered_machine_for_illustration':event.machine_code})
            (OUT/'example_snapshot.json').write_text(dumps(snapshot),encoding='utf-8')
            make_journey(sources,bundle,quality,row.cohort_id,OUT/'example_journey_15.json')
            break
        if sources is not None:break
    # Second illustration: quality risk + annotated quality event + diagnosis.
    # Select by alert/event timing, not by looking for profitable future labels.
    for event in test_events[test_events.oee_factor.eq('Quality')].itertuples():
        candidates=pred[(pred.observation_minute>=event.started_at)&(pred.observation_minute<event.ended_at)&pred.p_reject.ge(.05)].sort_values('observation_minute')
        found=False
        for row in candidates.itertuples():
            obs=row.observation_minute
            if not bool(flags.loc[obs,event.machine_code]):continue
            if sources is None:sources=load_sources(data_dir)
            previous=OUT/'example_snapshot.json'
            if previous.exists():(OUT/'example_event_only_snapshot.json').write_text(previous.read_text(),encoding='utf-8')
            snapshot=assess(sources,bundle,quality,'CAM_POST',obs)
            snapshot.update({'illustration_selected_after_evaluation':True,
                             'registered_event_for_illustration':event.problem_code,
                             'registered_machine_for_illustration':event.machine_code,
                             'selection_rule':'first annotated quality event with matched diagnosis and predicted risk >= 5 percent'})
            (OUT/'example_snapshot.json').write_text(dumps(snapshot),encoding='utf-8')
            make_journey(sources,bundle,quality,row.cohort_id,OUT/'example_journey_15.json')
            found=True;break
        if found:break
    import hashlib
    label_path=Path(data_dir)/'fct_problem_event.csv'
    summary['source_fingerprint']['fct_problem_event.csv']={'bytes':label_path.stat().st_size,
                'sha256':hashlib.sha256(label_path.read_bytes()).hexdigest()}
    (OUT/'metrics.json').write_text(dumps(summary),encoding='utf-8')
    print(dumps(summary['diagnosis']['thresholded']),flush=True)
    print('Ejemplo de replay creado:',sources is not None,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['train','predict','replay','finalize'])
    p.add_argument('--data-dir',required=True);p.add_argument('--stage',default='CAM_POST')
    p.add_argument('--observation-minute');p.add_argument('--cohort-id')
    p.add_argument('--output',default=str(OUT/'journey_15.json'));args=p.parse_args()
    if args.command=='train':
        train(args.data_dir);finalize(args.data_dir)
    elif args.command=='finalize':finalize(args.data_dir)
    else:
        q=joblib.load(QUALITY_PATH);d=joblib.load(OUT/'diagnosis_model.joblib')
        if args.command=='predict':print(dumps(assess(load_sources(args.data_dir,cutoff=args.observation_minute),d,q,args.stage,args.observation_minute)))
        else:make_journey(load_sources(args.data_dir),d,q,args.cohort_id,args.output)
