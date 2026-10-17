"""V3: station specialists and causal transient features; validation-only selection.

Run: python -m scrap_ml.improved train --data-dir PATH
     python -m scrap_ml.improved predict --data-dir PATH --stage CAR --observation-minute TIME
"""
import argparse
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits
from .data import load_sources
from .full_line_data import build_full_line_dataset, build_station_features, predictive_stages
from .full_line import dumps
from .full_line_policy import replay, add_terminal_stations, daily_gain_interval, stage_costs
from .train import split_masks, metrics

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'artifacts_improved'


def transient_features(sources):
    """Use exact-clock backward lags; no forward fill across gaps or downtime."""
    final=sources['final'];ix=final.index
    nominal=sources['production'].line_speed_m_min.eq(6)
    segment=(~ix.to_series().diff().eq(pd.Timedelta(minutes=1)) | ~nominal | ~nominal.shift(fill_value=False)).cumsum()
    rate=final.rate
    features={}
    for lag in [0,1,2,3,5,10]:
        features[f'recent_reject_lag_{lag}']=rate.groupby(segment).shift(lag)
    for window in [2,3,10]:
        k=final.rejects.groupby(segment).rolling(window,min_periods=window).sum().reset_index(level=0,drop=True).reindex(ix)
        n=final.pieces.groupby(segment).rolling(window,min_periods=window).sum().reset_index(level=0,drop=True).reindex(ix)
        features[f'recent_reject_mean_{window}']=k/n.where(n>0)
    features['recent_reject_change']=rate-rate.groupby(segment).shift(2)
    for c in ['height_oos','width_oos','length_oos']:
        features['recent_'+c]=final[c]/final.pieces.where(final.pieces>0)
    for c in ['CAR_ROLLER_GAP_MM','ENR_VISCOSITY_CP','ENR_CURTAIN_KG_MIN','MLD_ROLLER_GAP_MM']:
        if c in sources['telemetry']:
            s=sources['telemetry'][c]
            features['change1__'+c]=s-s.groupby(segment).shift(1)
            features['change2__'+c]=s-s.groupby(segment).shift(2)
    return pd.DataFrame(features,index=ix).astype('float32')


def augment(x,y,sources):
    extra=transient_features(sources).reindex(pd.DatetimeIndex(y.observation_minute))
    extra.index=x.index
    return pd.concat([x,extra],axis=1)


def predict_family(family,x,stages):
    result=np.zeros(len(x));stages=np.asarray(stages)
    with threadpool_limits(limits=4):
        for stage,model in family['models'].items():
            mask=np.ones(len(x),dtype=bool) if stage=='shared' else stages==stage
            if mask.any():result[mask]=model.predict(x.loc[mask,list(model.feature_names_in_)])
    if family['residual']:result+=x.observed_final_reject_rate_5m.to_numpy()
    return np.clip(result,1e-6,1-1e-6)


def fast_gain(frame,p,config,scenario,margin):
    """Economic selector using chronological probabilities; labels only score it."""
    first=frame.drop_duplicates('cohort_id').set_index('cohort_id')
    n=first.pieces.to_numpy();k=first.rejects.to_numpy();gain=np.zeros(len(first));active=np.ones(len(first),bool)
    for stage,spec in config['stages'].items():
        if spec['role']!='prediction' or spec.get('allow_early_discard',True) is False:continue
        s,l,d=stage_costs(config,scenario,stage)
        if s<=0 or spec['lead_minutes']<=1:continue
        rows=frame.stage.eq(stage).to_numpy()
        stage_p=pd.Series(np.asarray(p)[rows],index=frame.loc[rows,'cohort_id']).reindex(first.index).to_numpy()
        select=active & (stage_p>(l+d)/(s+l)+margin)
        gain[select]=k[select]*s-(n[select]-k[select])*l-n[select]*d
        active[select]=False
    dates=pd.DatetimeIndex(first.target_minute)
    a=float(gain[dates<pd.Timestamp('2026-09-14')].sum());b=float(gain[dates>=pd.Timestamp('2026-09-14')].sum())
    return a,b,float(gain.sum()),int((~active).sum())


def train(data_dir,reuse_trained=False):
    start=time.time();OUT.mkdir(exist_ok=True)
    cfg=json.loads((ROOT/'config_full_line.json').read_text())
    print('Construyendo las mismas cohortes y nuevas variables causales...',flush=True)
    sources=load_sources(data_dir);x,y,_,coverage=build_full_line_dataset(sources,cfg)
    x=augment(x,y,sources);masks=split_masks(y,cfg)
    features=list(x.columns[~x.loc[masks['train']].isna().all()]);x=x[features]
    val=y.loc[masks['selection']].reset_index(drop=True)
    xv=x.loc[masks['selection']].reset_index(drop=True)
    previous=joblib.load(OUT/'model.joblib')['families'] if reuse_trained else {}
    if reuse_trained:
        (OUT/'initial_strict_selection.json').write_text((OUT/'metrics.json').read_text(),encoding='utf-8')
    families={};vp={};tr=masks['train']
    for specialist,residual in [(False,False),(False,True),(True,False),(True,True)]:
        name=('specialists' if specialist else 'shared')+('_residual' if residual else '_rate')
        if reuse_trained:
            families[name]=previous[name];vp[name]=predict_family(families[name],xv,val.stage)
            print('Pesos conservados: '+name,flush=True)
            continue
        family={'features':features,'residual':residual,'models':{}}
        stages=predictive_stages(cfg) if specialist else ['shared']
        for stage in stages:
            mask=tr & (y.stage.eq(stage) if stage!='shared' else True)
            target=y.loc[mask,'rate'].to_numpy()
            if residual:target=target-x.loc[mask,'observed_final_reject_rate_5m'].to_numpy()
            weights=y.loc[mask,'pieces'].to_numpy(float,copy=True);weights/=weights.mean()
            model=HistGradientBoostingRegressor(loss='squared_error',max_iter=150 if specialist else 200,
                max_leaf_nodes=15 if specialist else 31,learning_rate=.07,min_samples_leaf=30,
                l2_regularization=2,max_bins=127,early_stopping=False,random_state=42)
            # Unknown own exposures can be entirely missing at an early station.
            # Drop these using its training slice only, preserving no-future contract.
            local_features=list(x.columns[~x.loc[mask].isna().all()])
            with threadpool_limits(limits=4):model.fit(x.loc[mask,local_features],target,sample_weight=weights)
            family['models'][stage]=model
        families[name]=family;vp[name]=predict_family(family,xv,val.stage)
        print('Candidato entrenado: '+name,flush=True)
    # Select on two validation weeks, penalizing poor worst-week behavior.
    # No test predictions are computed until this policy is frozen to disk.
    policies={};trials=[]
    persistence=xv.observed_final_reject_rate_5m.to_numpy()
    for scenario in cfg['scenario_assumptions']['scenarios']:
        candidates=[]
        for name,raw in vp.items():
            for blend in [0.,.25,.5,.75]:
                p=(1-blend)*raw+blend*persistence
                for margin in [0.,.05,.1,.2]:
                    a,b,total,count=fast_gain(val,p,cfg,scenario,margin)
                    row={'family':name,'persistence_blend':blend,'safety_margin':margin,
                         'week1_net_usd':a,'week2_net_usd':b,'validation_net_usd':total,
                         'cohorts_discarded':count,'robust_score':min(a,b)+.25*total,'scenario':scenario}
                    trials.append(row)
                    if a>=0 and b>=0 and total>0:candidates.append(row)
        if candidates:
            best=max(candidates,key=lambda r:(r['robust_score'],r['validation_net_usd']))
            policies[scenario]={**best,'disabled':False}
        else:policies[scenario]={'family':'specialists_rate','persistence_blend':0.,'safety_margin':.2,'disabled':True}
        print('Politica congelada '+scenario+': '+str(policies[scenario]),flush=True)
    bundle={'version':'full_line_v3','config':cfg,'families':families,'policies':policies,
            'selection_rule':'both validation weeks nonnegative and total positive; maximize min(week1,week2)+0.25*total',
            'protocol_revision':'Initial strict positivity disabled every candidate because week 2 had no profitable actions. Changed using validation to allow zero-action weeks. No model weights changed.',
            'test_previously_explored':True}
    joblib.dump(bundle,OUT/'model.joblib',compress=3)
    (OUT/'frozen_policies.json').write_text(dumps(policies),encoding='utf-8')
    pd.DataFrame(trials).to_csv(OUT/'validation_trials.csv',index=False)
    print('Politicas guardadas. Comienza evaluacion del test ya explorado...',flush=True)
    report={'version':bundle['version'],'coverage':coverage,'features':len(features),
            'selection_rule':bundle['selection_rule'],'scenarios':{},'test_previously_explored':True,
            'no_guaranteed_or_realized_savings':True,'protocol_revision':bundle['protocol_revision']}
    for scenario,policy in policies.items():
        report['scenarios'][scenario]={}
        for split in ['selection','test']:
            mask=masks[split];frame=y.loc[mask].reset_index(drop=True);xx=x.loc[mask].reset_index(drop=True)
            p=predict_family(families[policy['family']],xx,frame.stage)
            blend=policy['persistence_blend'];p=(1-blend)*p+blend*xx.observed_final_reject_rate_5m.to_numpy()
            frame['p_reject']=p;frame['p_persistence']=xx.observed_final_reject_rate_5m.to_numpy();frame['p_constant']=.01
            stats,records,traj=replay(frame,cfg,scenario,policy['safety_margin'],'model',policy['disabled'])
            stats['day_bootstrap']=daily_gain_interval(records)
            stats['by_station']={s:metrics(g,g.p_reject) for s,g in frame.groupby('stage')}
            report['scenarios'][scenario][split]=stats
            records.to_csv(OUT/f'cohorts_{split}_{scenario}.csv',index=False)
            if scenario=='materiales':
                frame.to_csv(OUT/f'predictions_{split}.csv',index=False)
                journeys=add_terminal_stations(traj,cfg)
                journeys.to_csv(OUT/f'journeys_15_{split}.csv',index=False)
                ids=records.loc[records.bad_pieces_removed.gt(0),'cohort_id']
                chosen=ids.iloc[0] if len(ids) else records.cohort_id.iloc[0]
                journeys[journeys.cohort_id.eq(chosen)].to_csv(OUT/f'example_{split}.csv',index=False)
            print(f'{scenario} {split}: {stats["net_scenario_usd"]:.2f} USD, buenos descartados {stats["good_pieces_discarded"]}',flush=True)
    report['runtime_seconds']=time.time()-start
    (OUT/'metrics.json').write_text(dumps(report),encoding='utf-8')
    return report


def score(sources,bundle,stage,observation_minute,scenario='materiales'):
    obs=pd.Timestamp(observation_minute);cfg=bundle['config']
    if cfg['stages'][stage]['role']!='prediction':
        from .full_line import score_station
        return score_station(sources,{'config':cfg},stage,obs,scenario)
    sources={k:v.loc[v.index<=obs].copy() for k,v in sources.items()}
    x,valid,_=build_station_features(sources,stage,cfg)
    if obs not in x.index or not valid.loc[obs]:raise ValueError('Abstencion: historia insuficiente o transporte fuera de dominio.')
    x=pd.concat([x,transient_features(sources)],axis=1).loc[[obs]].reset_index(drop=True)
    policy=bundle['policies'][scenario]
    p=predict_family(bundle['families'][policy['family']],x,[stage])[0]
    p=(1-policy['persistence_blend'])*p+policy['persistence_blend']*x.observed_final_reject_rate_5m.iloc[0]
    s,l,d=stage_costs(cfg,scenario,stage);cut=(l+d)/(s+l)+policy['safety_margin'] if s else 1.
    eligible=cfg['stages'][stage].get('allow_early_discard',True) and cfg['stages'][stage]['lead_minutes']>1
    return {'version':bundle['version'],'stage':stage,'observation_minute':str(obs),
            'decision_available_at':str(obs+pd.Timedelta(minutes=1)),
            'cohort_id':(obs+pd.Timedelta(minutes=cfg['stages'][stage]['lead_minutes'])).strftime('%Y%m%dT%H%M'),
            'predicted_reject_fraction':float(p),'economic_threshold':float(cut),
            'simulation_action':'DESCARTAR_COLECTIVO' if eligible and not policy['disabled'] and p>cut else 'CONTINUAR',
            'physical_actuation':False,'deployment_action':'REVISAR_COSTES_Y_TRAZABILIDAD','policy':policy}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['train','predict','reselect'])
    parser.add_argument('--data-dir',required=True);parser.add_argument('--stage',default='CAR')
    parser.add_argument('--observation-minute');parser.add_argument('--scenario',default='materiales')
    args=parser.parse_args()
    if args.command in ['train','reselect']:train(args.data_dir,reuse_trained=args.command=='reselect')
    else:print(dumps(score(load_sources(args.data_dir,cutoff=args.observation_minute),joblib.load(OUT/'model.joblib'),args.stage,args.observation_minute,args.scenario)))
