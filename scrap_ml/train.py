"""python -m scrap_ml.train --data-dir PATH [--output-dir PATH]."""
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
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits
from .data import load_sources, build_dataset, fingerprint
from .policy import recommend, evaluate_policy, expected_gain, threshold


def probability(bundle, x):
    raw = bundle['model'].predict_proba(x[bundle['features']])[:,1]
    if bundle['calibration_mode']=='isotonic':
        raw = bundle['calibrator'].predict(raw)
    return np.clip(raw,1e-6,1-1e-6)


def metrics(frame, pred):
    n=frame.pieces.to_numpy(float); k=frame.rejects.to_numpy(float)
    p=np.clip(np.asarray(pred),1e-6,1-1e-6); y=k/n
    total=n.sum()
    # Exact individual-outcome scores computed from aggregate sufficient statistics.
    auc=roc_auc_score(np.r_[np.ones(len(n)),np.zeros(len(n))],
                      np.r_[p,p],sample_weight=np.r_[k,n-k])
    return {
        'cohorts':len(n),'pieces':int(total),'rejects':int(k.sum()),
        'reject_rate':float(k.sum()/total),
        'predicted_reject_rate':float(np.average(p,weights=n)),
        'binomial_log_loss':float(-np.sum(k*np.log(p)+(n-k)*np.log1p(-p))/total),
        'individual_brier':float(np.sum(k*(1-p)**2+(n-k)*p**2)/total),
        'cohort_rate_mae':float(np.average(abs(y-p),weights=n)),
        'cohort_rate_rmse':float(np.sqrt(np.average((y-p)**2,weights=n))),
        'piece_weighted_roc_auc':float(auc)
    }


def split_masks(frame, config):
    s=config['splits'];obs=frame.observation_minute;tgt=frame.target_minute
    cal=pd.Timestamp(s['calibration_start']);sel=pd.Timestamp(s['selection_start']);test=pd.Timestamp(s['test_start'])
    masks={
        'train':tgt<cal,
        'calibration':(obs>=cal)&(tgt<sel),
        'selection':(obs>=sel)&(tgt<test),
        'test':obs>=test
    }
    for name, mask in masks.items():
        if not mask.any():raise ValueError(f'Particion vacia: {name}')
    groups={name:set(frame.loc[mask,'batch_id_at_decision']) for name,mask in masks.items()}
    names=list(groups)
    for i,a in enumerate(names):
        for b in names[i+1:]:
            if groups[a]&groups[b]:raise ValueError(f'Lotes compartidos entre {a} y {b}.')
    return masks


def calibration_table(frame,p):
    df=pd.DataFrame({'p':p,'n':frame.pieces.to_numpy(),'k':frame.rejects.to_numpy()})
    df['bin']=pd.cut(df.p,[-.001,.01,.02,.05,.1,.25,.5,.75,.9,1.001],include_lowest=True)
    rows=[]
    for bucket,g in df.groupby('bin',observed=True):
        rows.append({'bin':str(bucket),'cohorts':len(g),'pieces':int(g.n.sum()),
                     'mean_probability':float(np.average(g.p,weights=g.n)),
                     'observed_reject_rate':float(g.k.sum()/g.n.sum())})
    return rows


def grouped_importance(bundle,x,frame):
    """Joint permutation preserves within-machine feature dependency.

    An exploratory predictive importance, not a causal effect. This shuffles
    observations, so autocorrelation also limits its statistical interpretation.
    """
    base=metrics(frame,probability(bundle,x))['binomial_log_loss']
    groups={}
    for c in x:
        if c.startswith(('now__','setpoint_error__','past5_','delta5__','cohort_past__')):
            key=c.split('__')[-1].split('_')[0]
        elif c.startswith('core_'):key='LASER_CORE'
        elif c.startswith('observed_final'):key='HISTORICAL_FINAL_REJECTS'
        elif c.startswith('CAM_'):key='CAMERAS'
        else:key='CONTEXT_AND_PHYSICAL_RELATIONS'
        groups.setdefault(key,[]).append(c)
    rng=np.random.default_rng(42);rows=[]
    for group,cols in groups.items():
        perm=rng.permutation(len(x));xx=x.copy();xx.loc[:,cols]=x[cols].to_numpy()[perm]
        value=metrics(frame,probability(bundle,xx))['binomial_log_loss']-base
        rows.append({'feature_group':group,'log_loss_increase_when_shuffled':float(value),'columns':len(cols)})
    return sorted(rows,key=lambda r:r['log_loss_increase_when_shuffled'],reverse=True)


def daily_uncertainty(frame,probabilities,constant):
    n=frame.pieces.to_numpy(float);k=frame.rejects.to_numpy(float)
    p=np.clip(probabilities,1e-6,1-1e-6);q=np.clip(constant,1e-6,1-1e-6)
    loss=-k*np.log(p)-(n-k)*np.log1p(-p)
    ref=-k*np.log(q)-(n-k)*np.log1p(-q)
    df=pd.DataFrame({'date':frame.target_minute.dt.date.to_numpy(),'n':n,'delta':ref-loss})
    daily=df.groupby('date')[['n','delta']].sum()
    rng=np.random.default_rng(42);boots=[]
    for _ in range(500):
        z=daily.iloc[rng.integers(0,len(daily),len(daily))]
        boots.append(z.delta.sum()/z.n.sum())
    return {'unit':'day','days':len(daily),'resamples':500,
            'log_loss_improvement_vs_constant_95pct_interval':np.quantile(boots,[.025,.975]).tolist(),
            'small_simulated_sample':True}


def run(data_dir,output_dir,config_path):
    started=time.time();out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8'))
    print('Leyendo CSV originales y construyendo agregados...',flush=True)
    sources=load_sources(data_dir)
    global_summary={'data_fingerprint':fingerprint(data_dir),'config':cfg,
                    'python':platform.python_version(),'sklearn':sklearn.__version__,
                    'target':'FIN_BAR unique rejected pieces / final-laser pieces, per line-minute',
                    'individual_bar_identification':False,
                    'source_total_final_pieces':int(sources['final'].pieces.sum()),
                    'source_total_final_rejects':int(sources['final'].rejects.sum()),
                    'stages':{}}
    predictions=[];imports=[]
    for stage,stage_cfg in cfg['stages'].items():
        print(f'[{stage}] Features y separacion cronologica...',flush=True)
        x,y,manifest=build_dataset(sources,stage,cfg); masks=split_masks(y,cfg)
        train=masks['train'];cal=masks['calibration'];sel=masks['selection'];test=masks['test']
        xx=x.loc[train];yy=y.loc[train]
        n=yy.pieces.to_numpy(float);k=yy.rejects.to_numpy(float)
        # Two weighted class observations are a BINOMIAL likelihood; they do not
        # invent labels for individual bars. No oversampling or class balancing.
        expanded=pd.concat([xx,xx],ignore_index=True)
        target=np.r_[np.ones(len(xx),dtype=np.int8),np.zeros(len(xx),dtype=np.int8)]
        weights=np.r_[k,n-k]/np.mean(n)
        keep=weights>0
        model=HistGradientBoostingClassifier(**cfg['model'],early_stopping=False,
                                             random_state=cfg['random_seed'])
        with threadpool_limits(limits=4):
            model.fit(expanded.loc[keep],target[keep],sample_weight=weights[keep])
        bundle={'model':model,'features':list(x.columns),'calibration_mode':'raw',
                'calibrator':None,'stage':stage,'manifest':manifest,'config':cfg}
        raw_cal=model.predict_proba(x.loc[cal])[:,1]
        iso=IsotonicRegression(out_of_bounds='clip',y_min=1e-6,y_max=1-1e-6)
        iso.fit(raw_cal,y.loc[cal,'rate'],sample_weight=y.loc[cal,'pieces'])
        raw_sel=probability(bundle,x.loc[sel]);iso_sel=iso.predict(raw_sel)
        raw_loss=metrics(y.loc[sel],raw_sel)['binomial_log_loss']
        iso_loss=metrics(y.loc[sel],iso_sel)['binomial_log_loss']
        if iso_loss<raw_loss:
            bundle['calibration_mode']='isotonic';bundle['calibrator']=iso
        selected_sel=probability(bundle,x.loc[sel])
        base_rate=float(k.sum()/n.sum())
        persist_sel=x.loc[sel,'observed_final_reject_rate_5m'].to_numpy()
        best_ref_loss=min(metrics(y.loc[sel],np.repeat(base_rate,sel.sum()))['binomial_log_loss'],
                          metrics(y.loc[sel],persist_sel)['binomial_log_loss'])
        # Qualification is decided on selection week, NEVER on test.
        qualified=metrics(y.loc[sel],selected_sel)['binomial_log_loss']<best_ref_loss
        bundle['qualified_on_selection_week']=bool(qualified)
        joblib.dump(bundle,out/(stage+'.joblib'),compress=3)
        (out/(stage+'_feature_manifest.json')).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        test_y=y.loc[test];test_x=x.loc[test];pr=probability(bundle,test_x)
        baseline=np.repeat(base_rate,len(pr));persist=test_x.observed_final_reject_rate_5m.to_numpy()
        actions,gains,cut=recommend(pr,stage,cfg['economics'],qualified)
        pred=test_y[['observation_minute','decision_at','target_minute','pieces','rejects','rate','reject_cost_usd']].copy()
        pred['stage']=stage;pred['nominal_lead_min']=stage_cfg['lead_minutes']
        pred['effective_lead_min']=stage_cfg['lead_minutes']-cfg['feature_availability_delay_minutes']
        pred['p_reject']=pr;pred['p_constant']=base_rate;pred['p_persistence']=persist
        pred['action']=actions
        predictions.append(pred)
        stage_summary={
            'nominal_lead_minutes':stage_cfg['lead_minutes'],
            'effective_lead_minutes':stage_cfg['lead_minutes']-cfg['feature_availability_delay_minutes'],
            'features':len(x.columns),'cohorts_all_splits':len(y),
            'cohort_coverage_fraction':float(len(y)/sources['final'].pieces.gt(0).sum()),
            'reject_coverage_fraction':float(y.rejects.sum()/sources['final'].rejects.sum()),
            'splits':{name:{'cohorts':int(mask.sum()),'start':str(y.loc[mask,'target_minute'].min()),
                            'end':str(y.loc[mask,'target_minute'].max())} for name,mask in masks.items()},
            'calibration_mode':bundle['calibration_mode'],
            'selection_log_losses':{'raw':raw_loss,'isotonic':iso_loss,'best_reference':best_ref_loss},
            'qualified_on_selection_week':bool(qualified),
            'test':{'model':metrics(test_y,pr),'constant':metrics(test_y,baseline),
                    'persistence_5m':metrics(test_y,persist)},
            'calibration_test':calibration_table(test_y,pr),
            'day_bootstrap':daily_uncertainty(test_y,pr,base_rate),
            'default_policy':'NO_DISCARD_WITH_UNCONFIRMED_ECONOMICS'
        }
        scenario=cfg['illustrative_scenario_only'];future=scenario['future_cost_proxies_usd'].get(stage)
        if future is not None:
            stage_summary['illustrative_economics']={
                'assumed_future_avoidable_cost':future,
                'assumed_false_reject_loss':scenario['false_reject_loss_usd'],
                'assumed_extra_disposal_cost':scenario['extra_disposal_cost_per_piece_usd'],
                'model':evaluate_policy(test_y,pr,future,scenario['false_reject_loss_usd'],scenario['extra_disposal_cost_per_piece_usd']),
                'persistence':evaluate_policy(test_y,persist,future,scenario['false_reject_loss_usd'],scenario['extra_disposal_cost_per_piece_usd'])
            }
            for false_loss in [.1,.3,1.0]:
                stage_summary.setdefault('economic_sensitivity',[]).append({
                    'false_reject_loss':false_loss,
                    **evaluate_policy(test_y,pr,future,false_loss,scenario['extra_disposal_cost_per_piece_usd'])})
        # Explanations are taken on SELECTION data. Test is not used to select features.
        print(f'[{stage}] Explicaciones por grupos de sensores...',flush=True)
        imp=grouped_importance(bundle,x.loc[sel],y.loc[sel])
        for row in imp:row['stage']=stage
        imports.extend(imp);stage_summary['top_predictive_groups']=imp[:5]
        global_summary['stages'][stage]=stage_summary
        print(f'[{stage}] test MAE={stage_summary["test"]["model"]["cohort_rate_mae"]:.5f}; '
              f'logloss={stage_summary["test"]["model"]["binomial_log_loss"]:.5f}; '
              f'calibracion={bundle["calibration_mode"]}; apto={qualified}',flush=True)
    allpred=pd.concat(predictions,ignore_index=True)
    allpred.to_csv(out/'test_predictions.csv',index=False)
    pd.DataFrame(imports).to_csv(out/'feature_group_importance.csv',index=False)
    global_summary['sequential_illustrative_policy']=sequential_policy(allpred,cfg,global_summary)
    global_summary['runtime_seconds']=round(time.time()-started,2)
    (out/'metrics.json').write_text(json.dumps(global_summary,indent=2,allow_nan=False),encoding='utf-8')
    write_report(global_summary,out)
    print(f'Terminado: {out.resolve()} ({time.time()-started:.1f} s)',flush=True)
    return global_summary


def sequential_policy(pred,cfg,summary):
    scenario=cfg['illustrative_scenario_only'];decisions=[]
    for stage,future in scenario['future_cost_proxies_usd'].items():
        g=pred[pred.stage.eq(stage)].copy()
        if not summary['stages'][stage]['qualified_on_selection_week']:continue
        cut=threshold(future,scenario['false_reject_loss_usd'],scenario['extra_disposal_cost_per_piece_usd'])
        g=g[g.p_reject>cut].copy();g['future_cost_proxy']=future
        decisions.append(g)
    if not decisions:return {'cohorts':0,'net_scenario_usd':0.0}
    chosen=pd.concat(decisions).sort_values('decision_at').drop_duplicates('target_minute',keep='first')
    k=chosen.rejects.to_numpy();n=chosen.pieces.to_numpy();future=chosen.future_cost_proxy.to_numpy()
    return {'cohorts':len(chosen),'selected_stage_counts':chosen.stage.value_counts().to_dict(),
            'good_pieces_discarded':int((n-k).sum()),'bad_pieces_removed':int(k.sum()),
            'net_scenario_usd':float((k*future-(n-k)*scenario['false_reject_loss_usd']-n*scenario['extra_disposal_cost_per_piece_usd']).sum()),
            'double_counted_cohorts':0,'not_realized_savings':True,
            'conditional_on_final_survivors_and_nominal_transport':True}


def write_report(result,out):
    text=['# Evaluacion del modelo de rechazo temprano','',
          'Datos simulados; unidad: cohorte de linea por minuto, condicionada a llegar al laser final.',
          'Objetivo exacto: piezas unicas rechazadas en FIN_BAR / piezas medidas en laser final.',
          'Ventanas con velocidad nominal continua. Sin trazabilidad individual de barritas.',
          'Entrenamiento: primeras 9 semanas. Calibracion: semana 10. Seleccion: semana 11. Prueba: semanas 12-13.',
          'Los minutos contienen agregados completos: la decision se emite al minuto siguiente.', '',
          '| Punto | Margen efectivo (min) | MAE tasa (puntos %) | MAE persistencia (puntos %) | Logloss modelo | Logloss constante | Logloss persistencia | Apto en seleccion |',
          '|---|---:|---:|---:|---:|---:|---:|---|']
    for stage,r in result['stages'].items():
        m=r['test']['model'];p=r['test']['persistence_5m']
        c=r['test']['constant']
        text.append(f'| {stage} | {r["effective_lead_minutes"]} | {100*m["cohort_rate_mae"]:.3f} | {100*p["cohort_rate_mae"]:.3f} | {m["binomial_log_loss"]:.5f} | {c["binomial_log_loss"]:.5f} | {p["binomial_log_loss"]:.5f} | {r["qualified_on_selection_week"]} |')
    text += ['', 'Los modelos tempranos no superaron la mejor referencia en la semana de seleccion y no habilitan recomendaciones de descarte, aunque alguna metrica de prueba sea favorable.',
             '', '## Cobertura del dominio evaluado',
             'Porcentajes respecto al historico completo de FIN_BAR. La cobertura es distinta en cada punto; las metricas no describen toda la produccion.', '',
             '| Punto | Cohortes incluidas | % minutos con piezas | % rechazos finales incluidos |',
             '|---|---:|---:|---:|']
    for stage,r in result['stages'].items():
        text.append(f'| {stage} | {r["cohorts_all_splits"]} | {100*r["cohort_coverage_fraction"]:.2f}% | {100*r["reject_coverage_fraction"]:.2f}% |')
    text += ['','## Escenario economico ilustrativo',
             'Costes marginales reales no disponibles. El escenario usa diferencias de valor de scrap como aproximaciones, no ahorro validado.',
             'Asume perdida de 0,30 USD por barra buena descartada y 0,002 USD de coste adicional de descarte por pieza.',
             'No se habilitan descartes por defecto. Los costes deben confirmarse en config.json.',
             'La evaluacion supone que solo se actua sobre las piezas que historicamente llegaron al laser final: no acredita un actuador real ni resuelve rechazos anteriores.', '',
             '| Punto | Umbral | Defectuosas retiradas | Buenas descartadas | Balance hipotetico USD |',
             '|---|---:|---:|---:|---:|']
    for stage,r in result['stages'].items():
        if 'illustrative_economics' in r:
            m=r['illustrative_economics']['model']
            text.append(f'| {stage} | {100*m["threshold"]:.2f}% | {m["bad_pieces_removed"]:,.0f} | {m["good_pieces_discarded"]:,.0f} | {m["net_scenario_usd"]:,.2f} |')
    text += ['', 'Sensibilidad ilustrativa de pre_chocolate (no elegida para maximizar resultados de prueba):', '',
             '| Perdida por buena descartada USD | Umbral | Buenas descartadas | Defectuosas retiradas | Balance hipotetico USD |',
             '|---|---:|---:|---:|---:|']
    for r in result['stages']['pre_chocolate'].get('economic_sensitivity',[]):
        text.append(f'| {r["false_reject_loss"]:.2f} | {100*r["threshold"]:.2f}% | {r["good_pieces_discarded"]:,.0f} | {r["bad_pieces_removed"]:,.0f} | {r["net_scenario_usd"]:,.2f} |')
    text += ['','Politica secuencial: primera recomendacion por cohorte, sin sumar varias veces el mismo producto.',
             '```json',json.dumps(result['sequential_illustrative_policy'],indent=2),'```','',
             '## Restricciones',
             '- No anticipa fallos futuros independientes del estado actual.',
             '- FIN_BAR es rechazo dimensional; no cubre chocolate sin cuajar tras el enfriador final ni toda la calidad OEE.',
             '- Los offsets fijos (23/14/5/2 minutos) proceden del material del taller y requieren validacion fisica.',
             '- El filtrado retrospectivo de velocidad nominal limita la poblacion evaluada. En vivo solo se verifica la historia; la continuidad futura es una condicion, no un dato conocido.',
             '- Las importancias son predictivas; no demuestran causas ni efectos de cambiar un ajuste.',
             '- Una cohorte mezcla piezas buenas y malas. No identifica cual barrita individual descartar.',
             '- La lectura de datos reales futuros no esta permitida. El snapshot actual de una maquina posterior no equivale a su estado cuando llegue el producto.',
             '- Los cuatro modelos comparten objetivo; no se suman sus ahorros independientes.',
             '- La prueba cronologica es interna en un conjunto ya explorado; faltan validacion externa y prueba en sombra.','']
    (out/'evaluation.md').write_text('\n'.join(text),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--output-dir',default=str(Path(__file__).parent/'artifacts'))
    parser.add_argument('--config',default=str(Path(__file__).parent/'config.json'))
    args=parser.parse_args()
    run(args.data_dir,args.output_dir,args.config)


if __name__=='__main__':main()
