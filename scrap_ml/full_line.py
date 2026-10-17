"""Train, predict and replay Xocoleta's fifteen-station collective journey.

python -m scrap_ml.full_line train --data-dir PATH
python -m scrap_ml.full_line predict --data-dir PATH --stage LSR_CORE --observation-minute "2026-10-02 10:00:00"
"""
import argparse
from pathlib import Path
import json
import platform
import time
import numpy as np
import pandas as pd
import joblib
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits
from .data import load_sources,fingerprint
from .train import metrics,split_masks,calibration_table
from .full_line_data import build_full_line_dataset,build_station_features,predictive_stages
from .full_line_policy import replay,tune_policy,daily_gain_interval,add_terminal_stations,stage_costs

ROOT=Path(__file__).resolve().parent


def dumps(value):
    def convert(obj):
        if isinstance(obj,np.generic):return obj.item()
        if isinstance(obj,(pd.Timestamp,Path)):return str(obj)
        raise TypeError(type(obj).__name__)
    return json.dumps(value,indent=2,allow_nan=False,default=convert)


def raw_probability(bundle,x):
    return np.clip(bundle['model'].predict_proba(x.reindex(columns=bundle['features']))[:,1],1e-6,1-1e-6)


def predict_probability(bundle,x,stages):
    p=raw_probability(bundle,x);stages=np.asarray(stages)
    for stage,calibrator in bundle['calibrators'].items():
        mask=stages==stage
        if calibrator is not None and mask.any():p[mask]=calibrator.predict(p[mask])
    return np.clip(p,1e-6,1-1e-6)


def prediction_frame(bundle,x,y,mask):
    out=y.loc[mask].copy()
    out['p_reject']=predict_probability(bundle,x.loc[mask],out.stage)
    out['p_constant']=bundle['training_base_rate']
    out['p_persistence']=x.loc[mask,'observed_final_reject_rate_5m'].to_numpy()
    return out.reset_index(drop=True)


def group_importances(bundle,x,y):
    # Bounded validation-only diagnostic sample, retaining all stations of each sampled cohort.
    cohort_ids=y.cohort_id.drop_duplicates()
    rng=np.random.default_rng(42)
    if len(cohort_ids)>350:cohort_ids=pd.Series(rng.choice(cohort_ids,350,replace=False))
    mask=y.cohort_id.isin(cohort_ids);xx=x.loc[mask].copy();yy=y.loc[mask].copy()
    base=metrics(yy,predict_probability(bundle,xx,yy.stage))['binomial_log_loss']
    groups={}
    for c in xx:
        if '__' in c:
            last=c.split('__')[-1]
            group=last.split('_')[0] if last.split('_')[0] in ['MIX','DST','MLD','OVN','BCL','BCM','BCR','CAR','GUI','ENR','CCL'] else 'OTHER'
        elif c.startswith(('core_','own_core_')):group='CORE_GEOMETRY'
        elif c.startswith('observed_final'):group='PAST_FINAL_REJECTS'
        elif c.startswith('stage_'):group='STAGE'
        else:group='CONTEXT'
        groups.setdefault(group,[]).append(c)
    results=[]
    for group,cols in groups.items():
        altered=xx.copy()
        # Preserve stage distribution and conditional missingness when permuting sensors.
        if group=='STAGE':continue
        for stage in yy.stage.unique():
            sm=yy.stage.eq(stage).to_numpy();values=xx.loc[sm,cols].to_numpy()
            altered.loc[sm,cols]=values[rng.permutation(len(values))]
        score=metrics(yy,predict_probability(bundle,altered,yy.stage))['binomial_log_loss']
        results.append({'group':group,'columns':len(cols),'log_loss_increase':float(score-base)})
    return sorted(results,key=lambda r:r['log_loss_increase'],reverse=True)


def train(data_dir,output_dir=ROOT/'artifacts_full_line',config_path=ROOT/'config_full_line.json'):
    start=time.time();out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8'))
    print('Cargando CSV originales...',flush=True);sources=load_sources(data_dir)
    print('Construyendo colectivos comunes y observaciones en 13 estaciones predictivas + 2 estados terminales...',flush=True)
    x,y,manifests,coverage=build_full_line_dataset(sources,cfg)
    masks=split_masks(y,cfg)
    # Explicitly prove every replica of each cohort remains in a single split.
    split_name=pd.Series('',index=y.index)
    for name,mask in masks.items():split_name.loc[mask]=name
    if pd.DataFrame({'id':y.cohort_id,'split':split_name}).groupby('id').split.nunique().max()!=1:
        raise ValueError('Una cohorte aparece en varias particiones.')
    tr=masks['train'];cal=masks['calibration'];sel=masks['selection'];test=masks['test']
    # Some future own-cohort exposures (e.g. CCL) are unknown at EVERY
    # predictive point. They carry no trainable information; do not fill them
    # with invented future values. Selection is based on training data only.
    never_observed=list(x.columns[x.loc[tr].isna().all(axis=0)])
    if never_observed:
        x=x.drop(columns=never_observed)
    print(f'Entrenando modelo compartido: {tr.sum():,} filas de estacion, {y.loc[tr,"cohort_id"].nunique():,} colectivos, {len(x.columns)} variables...',flush=True)
    xx=x.loc[tr];n=y.loc[tr,'pieces'].to_numpy(float);k=y.loc[tr,'rejects'].to_numpy(float)
    expanded=pd.concat([xx,xx],ignore_index=True)
    classes=np.r_[np.ones(len(xx),dtype=np.int8),np.zeros(len(xx),dtype=np.int8)]
    # Every cohort's cumulative weight is the same regardless of station replicas.
    weights=np.r_[k,n-k]/(np.mean(n)*len(predictive_stages(cfg)))
    keep=weights>0
    model=HistGradientBoostingClassifier(**cfg['model'],early_stopping=False,random_state=cfg['random_seed'])
    with threadpool_limits(limits=4):
        model.fit(expanded.loc[keep],classes[keep],sample_weight=weights[keep])
    del expanded,xx,classes,weights
    bundle={'version':'full_line_v2','model':model,'features':list(x.columns),'config':cfg,
            'calibrators':{},'calibration_modes':{},'manifests':manifests,
            'training_base_rate':float(k.sum()/n.sum()),'policies':{},
            'omitted_all_missing_training_features':never_observed}
    station_results={}
    print('Calibrando probabilidades por estacion, con decisiones tomadas antes de la prueba...',flush=True)
    raw_cal=raw_probability(bundle,x.loc[cal]);raw_sel=raw_probability(bundle,x.loc[sel])
    for stage in predictive_stages(cfg):
        cm=y.loc[cal,'stage'].eq(stage).to_numpy();sm=y.loc[sel,'stage'].eq(stage).to_numpy()
        cal_y=y.loc[cal].loc[cm];sel_y=y.loc[sel].loc[sm]
        iso=IsotonicRegression(out_of_bounds='clip',y_min=1e-6,y_max=1-1e-6)
        iso.fit(raw_cal[cm],cal_y.rate,sample_weight=cal_y.pieces)
        before=metrics(sel_y,raw_sel[sm]);after=metrics(sel_y,iso.predict(raw_sel[sm]))
        use_iso=after['binomial_log_loss']<before['binomial_log_loss']
        bundle['calibrators'][stage]=iso if use_iso else None
        bundle['calibration_modes'][stage]='isotonic' if use_iso else 'raw'
        station_results[stage]={'role':'prediction','calibration_mode':bundle['calibration_modes'][stage],
                                'selection_log_loss_raw':before['binomial_log_loss'],
                                'selection_log_loss_isotonic':after['binomial_log_loss']}
    validation=prediction_frame(bundle,x,y,sel)
    print('Seleccionando politicas economicas secuenciales en validacion (sin consultar la prueba)...',flush=True)
    for scenario in cfg['scenario_assumptions']['scenarios']:
        bundle['policies'][scenario]={}
        for method in ['model','persistence','constant']:
            bundle['policies'][scenario][method]=tune_policy(validation,cfg,scenario,method)
    # Save the frozen bundle before test accounting.
    joblib.dump(bundle,out/'full_line_model.joblib',compress=3)
    (out/'feature_manifest.json').write_text(dumps(manifests),encoding='utf-8')
    (out/'frozen_policies.json').write_text(dumps(bundle['policies']),encoding='utf-8')
    test_pred=prediction_frame(bundle,x,y,test)
    for stage in predictive_stages(cfg):
        rows=test_pred[test_pred.stage.eq(stage)]
        sm=validation.stage.eq(stage)
        station_results[stage].update({
            'lead_minutes':cfg['stages'][stage]['lead_minutes'],
            'effective_lead_minutes':cfg['stages'][stage]['lead_minutes']-cfg['feature_availability_delay_minutes'],
            'timing_provenance':cfg['stages'][stage]['timing'],
            'test_model':metrics(rows,rows.p_reject),
            'test_persistence':metrics(rows,rows.p_persistence),
            'test_constant':metrics(rows,rows.p_constant),
            'test_calibration':calibration_table(rows,rows.p_reject)
        })
    for stage in ['LSR_BAR','CCL']:
        station_results[stage]={'role':cfg['stages'][stage]['role'],
                               'timing_provenance':cfg['stages'][stage]['timing'],
                               'ml_metrics':None}
    print('Comparando primera retirada propuesta con rechazo historico para cada colectivo...',flush=True)
    scenarios={};validation_trajectories=[];test_trajectories=[]
    for scenario,methods in bundle['policies'].items():
        scenarios[scenario]={}
        for method,policy in methods.items():
            vstats,vrecords,vtraj=replay(validation,cfg,scenario,policy['safety_margin'],method,policy['disabled'])
            stats,records,traj=replay(test_pred,cfg,scenario,policy['safety_margin'],method,policy['disabled'])
            stats['day_bootstrap']=daily_gain_interval(records)
            scenarios[scenario][method]={'frozen_policy':policy,'validation':vstats,'test':stats}
            if method=='model':
                records.to_csv(out/f'cohort_comparison_test_{scenario}.csv',index=False)
                vrecords.to_csv(out/f'cohort_comparison_validation_{scenario}.csv',index=False)
                test_trajectories.append(add_terminal_stations(traj,cfg))
                validation_trajectories.append(add_terminal_stations(vtraj,cfg))
        print(f'[{scenario}] validacion={scenarios[scenario]["model"]["validation"]["net_scenario_usd"]:.2f} USD; '
              f'prueba={scenarios[scenario]["model"]["test"]["net_scenario_usd"]:.2f} USD; '
              f'colectivos retirados={scenarios[scenario]["model"]["test"]["cohorts_discarded"]}',flush=True)
    test_pred.to_csv(out/'station_predictions_test.csv',index=False)
    validation.to_csv(out/'station_predictions_validation.csv',index=False)
    all_test=pd.concat(test_trajectories,ignore_index=True)
    all_test.to_csv(out/'journeys_15_stations_test.csv',index=False)
    all_validation=pd.concat(validation_trajectories,ignore_index=True)
    all_validation.to_csv(out/'journeys_15_stations_validation.csv',index=False)
    print('Calculando importancia predictiva en validacion...',flush=True)
    importances=group_importances(bundle,x.loc[sel],y.loc[sel])
    pd.DataFrame(importances).to_csv(out/'feature_group_importance.csv',index=False)
    split_summary={name:{'station_rows':int(mask.sum()),'cohorts':int(y.loc[mask,'cohort_id'].nunique()),
                         'first_final_minute':str(y.loc[mask,'target_minute'].min()),
                         'last_final_minute':str(y.loc[mask,'target_minute'].max())} for name,mask in masks.items()}
    summary={'version':'full_line_v2','python':platform.python_version(),'sklearn':sklearn.__version__,
             'config':cfg,'coverage':coverage,'splits':split_summary,'station_results':station_results,
             'scenarios':scenarios,'importances':importances,'data_fingerprint':fingerprint(data_dir),
             'single_shared_model':True,'features':len(x.columns),
             'omitted_all_missing_training_features':never_observed,
             'common_cohorts_no_duplicate_savings':True,'physical_actuation':False,
             'test_previously_explored_in_v1':True,
             'counterfactual_assumptions':['nominal fixed transport','final-laser survivors only',
                'scrap values approximate marginal costs','cohort-level diversion feasible',
                'removal does not alter other cohorts','earlier disposition cannot be individually tracked'],
             'runtime_seconds':round(time.time()-start,2)}
    (out/'metrics.json').write_text(dumps(summary),encoding='utf-8')
    example_journey(all_test,out,cfg)
    example_journey(all_validation,out,cfg,'example_validation_collective_15_stations.csv')
    write_report(summary,out)
    print(f'Terminado en {time.time()-start:.1f} s: {out.resolve()}',flush=True)
    return summary


def example_journey(journeys,out,config,filename='example_collective_15_stations.csv'):
    rows=journeys[journeys.scenario.eq('materiales')]
    candidates=rows[rows.replay_state.eq('DESCARTAR_COLECTIVO')]
    if len(candidates):chosen=candidates.sort_values('target_minute').iloc[0].cohort_id
    else:chosen=rows.cohort_id.iloc[0]
    # Illustration only: explicitly selected after evaluation, not another test.
    example=rows[rows.cohort_id.eq(chosen)].copy()
    example.to_csv(out/filename,index=False)


def score_station(sources,bundle,stage,observation_minute,scenario='materiales'):
    obs=pd.Timestamp(observation_minute);cfg=bundle['config'];spec=cfg['stages'][stage]
    sources={name:frame.loc[frame.index<=obs].copy() for name,frame in sources.items()}
    target=obs+pd.Timedelta(minutes=spec['lead_minutes'])
    result={'stage':stage,'station_sequence':spec['sequence'],'observation_minute':str(obs),
            'cohort_id':target.strftime('%Y%m%dT%H%M'),
            'decision_available_at':str(obs+pd.Timedelta(minutes=cfg['feature_availability_delay_minutes'])),
            'estimated_final_laser_minute':str(target),'timing_provenance':spec['timing'],
            'unit':'colectivo linea-minuto','physical_actuation':False,'scenario':scenario,
            'economics_confirmed':False}
    if spec['role']!='prediction':
        if target not in sources['final'].index:
            raise ValueError('No esta disponible el resultado final historico de este colectivo.')
        observed=sources['final'].loc[target]
        result.update({'predicted_reject_fraction':None,'observed_final_reject_fraction':float(observed.rate),
                       'observed_final_rejects':int(observed.rejects),
                       'simulation_action':'RECHAZO_FINAL_OBSERVADO' if stage=='LSR_BAR' else 'SIN_NUEVA_ETIQUETA_CALIDAD'})
        return result
    x,valid,_=build_station_features(sources,stage,cfg)
    if obs not in x.index or not bool(valid.loc[obs]):
        raise ValueError('Historia insuficiente o fuera del dominio de velocidad nominal; abstencion.')
    p=float(predict_probability(bundle,x.loc[[obs]],[stage])[0])
    policy=bundle['policies'][scenario]['model']
    # No future labels or final piece counts are needed to choose an action.
    one=pd.DataFrame({'stage':[stage],'p_reject':[p],'p_persistence':[float(x.loc[obs,'observed_final_reject_rate_5m'])],
                      'p_constant':[bundle['training_base_rate']]})
    from .full_line_policy import candidate_decisions
    candidate=candidate_decisions(one,cfg,scenario,policy['safety_margin'],'model',policy['disabled']).iloc[0]
    cut=candidate.economic_threshold
    result.update({'predicted_reject_fraction':p,'effective_lead_minutes':spec['lead_minutes']-cfg['feature_availability_delay_minutes'],
                   'simulation_action':'DESCARTAR_COLECTIVO' if candidate.candidate_discard else 'CONTINUAR',
                   'economic_threshold':None if pd.isna(cut) else float(cut),
                   'policy_disabled_by_validation':policy['disabled'],
                   'deployment_action':'REVISAR_COSTES_Y_TRAZABILIDAD',
                   'conditional_on_nominal_future_transport_and_final_survival':True})
    return result


def write_report(summary,out):
    cfg=summary['config'];coverage=summary['coverage']
    lines=['# Rechazo temprano por colectivos: recorrido de 15 estaciones','',
           'Un modelo compartido actualiza el riesgo en 13 estaciones. El laser final registra el resultado y el enfriador final no tiene una nueva etiqueta de calidad disponible.',
           'Los grupos pueden contener piezas buenas y defectuosas; el objetivo es la fraccion rechazada, no una etiqueta binaria inventada para todo el grupo.',
           '', '## Validacion y poblacion',
           '8 semanas de entrenamiento, 1 de calibracion, 2 para seleccionar politicas y 2 de prueba interna. Todas las replicas del mismo colectivo quedan en la misma particion.',
           'El periodo de prueba ya fue examinado en el prototipo anterior: esta es una evaluacion interna de desarrollo, no una prueba externa ciega.',
           f'Poblacion comun: {coverage["common_cohorts"]:,} colectivos; {100*coverage["reject_coverage_fraction"]:.2f}% de los rechazos FIN_BAR originales. Se excluyen ventanas de transporte no nominal y sin historia completa.',
           '', '| Particion | Colectivos | Primer minuto final | Ultimo minuto final |','|---|---:|---|---|']
    for name,v in summary['splits'].items():lines.append(f'| {name} | {v["cohorts"]:,} | {v["first_final_minute"]} | {v["last_final_minute"]} |')
    lines += ['', '## Las 15 estaciones',
              'El minuto de medida completo se conoce al minuto siguiente. Algunas estaciones comparten minuto y no proporcionan una nueva resolucion temporal.', '',
              '| Orden | Estacion | Minutos hasta laser final | Papel | Tiempo |','|---:|---|---:|---|---|']
    for stage,s in cfg['stages'].items():lines.append(f'| {s["sequence"]} | {stage}: {s["label"]} | {s["lead_minutes"]} | {s["role"]} | {s["timing"]} |')
    lines += ['', '## Calidad de las probabilidades en prueba',
              'MAE expresado en puntos porcentuales de tasa de rechazo. La referencia persistencia usa solo rechazo ya observado.', '',
              '| Estacion | Margen efectivo min | MAE modelo | MAE persistencia | Logloss modelo |','|---|---:|---:|---:|---:|']
    for stage,r in summary['station_results'].items():
        if r['role']=='prediction':
            m=r['test_model'];p=r['test_persistence']
            lines.append(f'| {stage} | {r["effective_lead_minutes"]} | {100*m["cohort_rate_mae"]:.3f} | {100*p["cohort_rate_mae"]:.3f} | {m["binomial_log_loss"]:.5f} |')
    lines += ['', '## Ahorro neto estimado en el replay',
              'La politica se congela con validacion antes de consultar los resultados de prueba. Se permite continuar todos los grupos si ninguna politica es rentable en validacion.',
              'Solo cuenta la primera retirada por colectivo. Baseline: rechazo de las piezas defectuosas en LSR_BAR.',
              'Ahorro neto = coste futuro evitado en defectuosas - perdida por buenas descartadas - coste adicional de descarte.',
              'Los importes usan proxies de materiales derivados del valor del scrap; no son costes marginales confirmados ni ahorro realizado.', '',
              '| Escenario / politica | Neto validacion USD | Neto prueba USD | Defectuosas retiradas prueba | Buenas descartadas prueba | Anticipacion mediana min |',
              '|---|---:|---:|---:|---:|---:|']
    for scenario,methods in summary['scenarios'].items():
        for method,r in methods.items():
            v=r['validation'];t=r['test'];anticip=t['median_anticipation_minutes']
            lines.append(f'| {scenario} / {method} | {v["net_scenario_usd"]:,.2f} | {t["net_scenario_usd"]:,.2f} | {t["bad_pieces_removed"]:,} | {t["good_pieces_discarded"]:,} | {anticip if anticip is not None else "n/a"} |')
    lines += ['', 'Escenarios: materiales = valor acumulado de materiales por buena descartada; margen_005 y margen_025 anaden 0,05 y 0,25 USD de perdida de oportunidad por buena descartada. Todos imputan 0,002 USD de descarte adicional por pieza.',
              '', '## Primera retirada propuesta frente al rechazo historico',
              '| Escenario ML | Estaciones de primera retirada | Coste futuro evitado USD | Coste falsas retiradas USD | Coste extra descarte USD |',
              '|---|---|---:|---:|---:|']
    for scenario,r in summary['scenarios'].items():
        t=r['model']['test']
        lines.append(f'| {scenario} | {json.dumps(t["station_counts"])} | {t["gross_future_cost_avoided_usd"]:,.2f} | {t["false_reject_loss_usd"]:,.2f} | {t["extra_disposal_cost_usd"]:,.2f} |')
    lines += ['', '## Limites que afectan al ahorro',
              '- Los resultados son retrospectivos sobre una simulacion. Faltan costes marginales de planta, capacidad del expulsor y validacion externa.',
              '- Los offsets internos de DST, OVN, BCM, BCR, CAR, ENR y CCL son aproximaciones documentadas en config_full_line.json, no seguimiento medido.',
              '- ENR tiene cero minutos de margen efectivo con agregados por minuto y no permite retirada anticipada en la politica. LSR_BAR no cuenta como prediccion. CCL carece de nueva etiqueta de defecto final.',
              '- El momento de rechazo historico es un intervalo de minuto; la anticipacion se mide de forma conservadora respecto al comienzo de ese intervalo.',
              '- La poblacion evaluada incluye supervivientes al laser final. Sin trazabilidad no se puede imputar a estos grupos la masa retirada antes en POST_CAM o GUI_CHUTE.',
              '- En la simulacion economica retirar un colectivo elimina tambien sus piezas buenas. Esas perdidas se contabilizan.',
              '- Los parametros actuales de maquinas posteriores son contexto conocido; no se usan sus lecturas futuras reales.',
              '- La incertidumbre por dias no captura todos los errores de transporte, costes ni seleccion de modelo.',
              '- No se han implementado una demo visual ni actuacion fisica ni modelos de limpiezas.','',
              '## Archivos para revisar',
              '- cohort_comparison_validation_*.csv y cohort_comparison_test_*.csv: una fila por colectivo con momento historico y primera retirada propuesta.',
              '- journeys_15_stations_validation.csv y journeys_15_stations_test.csv: recorrido completo con riesgo, decision y estado despues de la retirada.',
              '- station_predictions_*.csv: probabilidades y etiquetas separadas para auditar.',
              '- frozen_policies.json y full_line_model.joblib: modelo y politicas congelados antes del calculo de prueba.',
              '- example_collective_15_stations.csv: un recorrido ilustrativo elegido despues de evaluar, no una prueba adicional.','']
    sample_file=Path(out)/'example_validation_collective_15_stations.csv'
    if sample_file.is_file():
        sample=pd.read_csv(sample_file)
        row=sample.loc[sample.replay_state.eq('DESCARTAR_COLECTIVO')]
        if len(row):
            r=row.iloc[0]
            lines += ['## Ejemplo ilustrativo de validacion',
                      'Primer colectivo retirado cronologicamente en validacion; elegido despues de evaluar, no como prueba independiente.',
                      f'Colectivo {r["cohort_id"]}: {int(r["pieces"]):,} piezas, de las que {int(r["rejects"]):,} se rechazaron realmente en FIN_BAR.',
                      f'La politica propone retirada en {r["stage"]} a {r["decision_at"]}, frente al rechazo historico en el minuto {r["target_minute"]}.',
                      'Las piezas buenas incluidas en ese grupo se contabilizan como falsas retiradas. Ver example_validation_collective_15_stations.csv.','']
    (Path(out)/'evaluation.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    tr=sub.add_parser('train');tr.add_argument('--data-dir',required=True)
    tr.add_argument('--output-dir',default=str(ROOT/'artifacts_full_line'))
    tr.add_argument('--config',default=str(ROOT/'config_full_line.json'))
    pr=sub.add_parser('predict');pr.add_argument('--data-dir',required=True);pr.add_argument('--stage',required=True)
    pr.add_argument('--observation-minute',required=True);pr.add_argument('--scenario',default='materiales')
    pr.add_argument('--model-dir',default=str(ROOT/'artifacts_full_line'));pr.add_argument('--output')
    args=parser.parse_args()
    if args.command=='train':train(args.data_dir,args.output_dir,args.config)
    else:
        bundle=joblib.load(Path(args.model_dir)/'full_line_model.joblib')
        if args.stage not in bundle['config']['stages']:parser.error('Estacion desconocida.')
        sources=load_sources(args.data_dir,cutoff=args.observation_minute)
        result=score_station(sources,bundle,args.stage,args.observation_minute,args.scenario)
        text=dumps(result);print(text)
        if args.output:Path(args.output).write_text(text,encoding='utf-8')


if __name__=='__main__':main()
