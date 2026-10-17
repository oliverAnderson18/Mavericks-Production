"""One cohort, fifteen lifecycle stations, strictly timestamp-bounded features."""
import numpy as np
import pandas as pd
from .data import build_features


def predictive_stages(config):
    return [s for s,v in config['stages'].items() if v['role']=='prediction']


def build_station_features(sources,stage,config):
    spec=config['stages'][stage]
    if spec['role']!='prediction':
        raise ValueError('Esta estacion observa el resultado final o carece de nueva etiqueta; no se entrena como prediccion anticipada.')
    base,valid,manifest=build_features(sources,stage,config)
    base=base.drop(columns=[c for c in base if c.startswith('cohort_past__')])
    tele=sources['telemetry'];index=tele.index
    # Same column contract across all stages. Future own-cohort exposures are
    # UNKNOWN (NaN), not replaced by real future readings or fictitious zeros.
    own=pd.DataFrame(np.nan,index=index,columns=['own_exposure__'+c for c in tele],dtype='float32')
    for machine,machine_spec in config['stages'].items():
        cols=[c for c in tele if c.startswith(machine+'_')]
        if not cols:continue
        reached=machine_spec['sequence']<=spec['sequence']
        lookback=machine_spec['lead_minutes']-spec['lead_minutes']
        base['exposure_known__'+machine]=float(reached)
        if reached:
            if lookback<0:raise ValueError('Los offsets de transporte deben ser monotonos.')
            values=tele[cols].copy()
            values.index=values.index+pd.Timedelta(minutes=lookback)
            own.loc[:,['own_exposure__'+c for c in cols]]=values.reindex(index).to_numpy(dtype='float32')
            valid &= values.reindex(index).notna().all(axis=1)
    core=sources['core'];own_core=core.copy()*np.nan
    if spec['sequence']>=config['stages']['LSR_CORE']['sequence']:
        shift=config['stages']['LSR_CORE']['lead_minutes']-spec['lead_minutes']
        values=core.copy();values.index=values.index+pd.Timedelta(minutes=shift)
        own_core=values.reindex(index)
        valid &= own_core.notna().all(axis=1)
    own_core=own_core.rename(columns=lambda c:'own_'+c)
    base=pd.concat([base,own,own_core],axis=1)
    base['stage_sequence']=spec['sequence']
    base['stage_lead_minutes']=spec['lead_minutes']
    base['stage_core_geometry_known']=float(spec['sequence']>=config['stages']['LSR_CORE']['sequence'])
    base=base.reindex(sorted(base.columns),axis=1).astype('float32')
    manifest.update({
        'features':list(base.columns),'station_sequence':spec['sequence'],
        'timing_provenance':spec['timing'],
        'unknown_own_future_exposures_are_nan':True,
        'stage_is_model_input':True,
        'current_downstream_snapshot_allowed':True
    })
    return base,valid,manifest


def build_full_line_dataset(sources,config):
    """Strict common cohort population, observed at each of 13 model stations.

    All station replicas of a cohort remain in the same temporal split.
    Final-laser and post-laser stations have lifecycle rows, not leaked labels
    disguised as ML predictions.
    """
    final=sources['final'];prod=sources['production'];ix=prod.index
    nominal=prod.line_speed_m_min.sub(config['nominal_speed_m_min']).abs().lt(1e-6)
    common=final.index[final.pieces.gt(0)&final.rate.notna()]
    matrices={};metadata={};manifests={}
    for stage in predictive_stages(config):
        x,valid,manifest=build_station_features(sources,stage,config)
        lead=config['stages'][stage]['lead_minutes']
        # Offline restriction: contiguous nominal transport from entry to label.
        stage_final=x.index+pd.Timedelta(minutes=lead)
        targets=final.reindex(stage_final).copy();targets.index=x.index
        valid &= targets.pieces.gt(0)&targets.rate.notna()
        meta=pd.DataFrame(index=x.index)
        meta['observation_minute']=x.index
        meta['decision_at']=x.index+pd.Timedelta(minutes=config['feature_availability_delay_minutes'])
        meta['target_minute']=stage_final
        meta['cohort_id']=stage_final.strftime('%Y%m%dT%H%M')
        meta['stage']=stage;meta['stage_sequence']=config['stages'][stage]['sequence']
        meta['effective_lead_minutes']=lead-config['feature_availability_delay_minutes']
        entry=prod.dough_batch_id.copy()
        entry.index=entry.index+pd.Timedelta(minutes=config['stages']['MIX']['lead_minutes']-lead)
        meta['batch_id_at_decision']=entry.reindex(x.index)
        for col in ['pieces','rejects','rate','reject_cost_usd']:
            meta[col]=targets[col]
        matrices[stage]=x.loc[valid].copy()
        metadata[stage]=meta.loc[valid].copy()
        common=common.intersection(pd.DatetimeIndex(meta.loc[valid,'target_minute']))
        manifests[stage]=manifest
    # A single common population for a fair first-decision replay.
    uninterrupted=pd.Series(True,index=common)
    for back in range(config['stages']['MIX']['lead_minutes']+1):
        query=common-pd.Timedelta(minutes=back)
        uninterrupted &= nominal.reindex(query,fill_value=False).to_numpy()
    common=common[uninterrupted.to_numpy()]
    allx=[];ally=[]
    for stage in predictive_stages(config):
        keep=metadata[stage].target_minute.isin(common)
        allx.append(matrices[stage].loc[keep]);ally.append(metadata[stage].loc[keep])
    x=pd.concat(allx,ignore_index=True);y=pd.concat(ally,ignore_index=True)
    if len(y)!=len(common)*len(predictive_stages(config)):
        raise ValueError('Faltan estados de alguna cohorte en el recorrido completo.')
    y['replicas_per_cohort']=len(predictive_stages(config))
    coverage={
        'common_cohorts':len(common),'station_rows':len(y),
        'predictive_stations':len(predictive_stages(config)),
        'lifecycle_stations':len(config['stages']),
        'cohort_coverage_fraction':len(common)/max(1,int(final.pieces.gt(0).sum())),
        'reject_coverage_fraction':float(final.reindex(common).rejects.sum()/final.rejects.sum()),
        'future_nominal_transport_is_offline_filter':True,
        'conditional_on_final_laser_survival':True
    }
    return x,y,manifests,coverage
