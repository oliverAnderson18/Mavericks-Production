"""First-discard replay on aggregate cohorts, never using labels to act."""
import numpy as np
import pandas as pd
from .policy import threshold


def stage_costs(config,scenario_name,stage):
    assumptions=config['scenario_assumptions']
    accumulated=assumptions['accumulated_material_proxy_usd'][stage]
    full=assumptions['final_material_value_usd_per_piece']
    margin=assumptions['scenarios'][scenario_name]['extra_good_piece_opportunity_loss_usd']
    return max(0.,full-accumulated),accumulated+margin,assumptions['extra_disposal_cost_per_piece_usd']


def candidate_decisions(predictions,config,scenario_name,safety_margin=0.,method='model',disabled=False):
    result=predictions.copy()
    column={'model':'p_reject','persistence':'p_persistence','constant':'p_constant'}[method]
    result['policy_probability']=result[column]
    result['candidate_discard']=False
    result['economic_threshold']=np.nan
    result['future_avoidable_proxy_usd']=0.
    result['false_reject_loss_proxy_usd']=0.
    result['extra_disposal_proxy_usd']=0.
    for stage,spec in config['stages'].items():
        mask=result.stage.eq(stage)
        future,false_loss,disposal=stage_costs(config,scenario_name,stage)
        result.loc[mask,'future_avoidable_proxy_usd']=future
        result.loc[mask,'false_reject_loss_proxy_usd']=false_loss
        result.loc[mask,'extra_disposal_proxy_usd']=disposal
        eligible=(spec['role']=='prediction' and spec.get('allow_early_discard',True)
                  and spec['lead_minutes']>config['feature_availability_delay_minutes'] and future>0)
        if not eligible:continue
        cut=threshold(future,false_loss,disposal)+safety_margin
        result.loc[mask,'economic_threshold']=cut
        if not disabled:
            result.loc[mask,'candidate_discard']=result.loc[mask,column]>cut
    return result


def replay(predictions,config,scenario_name,safety_margin=0.,method='model',disabled=False):
    """Labels enter ACCOUNTING only after selecting actions from probabilities."""
    candidates=candidate_decisions(predictions,config,scenario_name,safety_margin,method,disabled)
    selected=(candidates.loc[candidates.candidate_discard]
              .sort_values(['cohort_id','stage_sequence'])
              .drop_duplicates('cohort_id',keep='first'))
    baseline=(predictions.sort_values('stage_sequence').drop_duplicates('cohort_id')
              [['cohort_id','target_minute','pieces','rejects','rate','reject_cost_usd']].copy())
    if baseline.cohort_id.duplicated().any():raise ValueError('Cohortes duplicadas.')
    baseline=baseline.set_index('cohort_id')
    chosen=selected.set_index('cohort_id')
    records=baseline.copy()
    records['historical_rejection_station']='LSR_BAR'
    records['historical_rejection_minute']=records.target_minute
    records['historical_result_available_at']=records.target_minute+pd.Timedelta(minutes=1)
    records['proposed_discard_station']=chosen.stage.reindex(records.index).fillna('NO_EARLY_DISCARD')
    records['proposed_decision_at']=chosen.decision_at.reindex(records.index)
    records['decision_probability']=chosen.policy_probability.reindex(records.index)
    records['economic_threshold']=chosen.economic_threshold.reindex(records.index)
    records['selected_sequence']=chosen.stage_sequence.reindex(records.index)
    is_selected=records.proposed_discard_station.ne('NO_EARLY_DISCARD')
    future=chosen.future_avoidable_proxy_usd.reindex(records.index).fillna(0.)
    false_loss=chosen.false_reject_loss_proxy_usd.reindex(records.index).fillna(0.)
    disposal=chosen.extra_disposal_proxy_usd.reindex(records.index).fillna(0.)
    records['bad_pieces_removed']=np.where(is_selected,records.rejects,0.)
    records['good_pieces_discarded']=np.where(is_selected,records.pieces-records.rejects,0.)
    records['gross_future_cost_avoided_usd']=records.bad_pieces_removed*future
    records['false_reject_loss_usd']=records.good_pieces_discarded*false_loss
    records['extra_disposal_cost_usd']=np.where(is_selected,records.pieces*disposal,0.)
    records['net_scenario_usd']=records.gross_future_cost_avoided_usd-records.false_reject_loss_usd-records.extra_disposal_cost_usd
    records['minutes_before_historical_rejection_start']=(records.target_minute-records.proposed_decision_at).dt.total_seconds()/60
    records['scenario']=scenario_name;records['method']=method
    selected_rows=records.loc[is_selected]
    bad=float(records.bad_pieces_removed.sum());good=float(records.good_pieces_discarded.sum())
    stats={
        'scenario':scenario_name,'method':method,'safety_margin':safety_margin,
        'disabled_by_validation':bool(disabled),'cohorts':len(records),
        'cohorts_discarded':int(is_selected.sum()),
        'pieces_in_evaluation':int(records.pieces.sum()),
        'historical_final_rejects':int(records.rejects.sum()),
        'historical_final_reject_cost_usd':float(records.reject_cost_usd.sum()),
        'bad_pieces_removed':int(bad),'good_pieces_discarded':int(good),
        'discard_purity':bad/(bad+good) if bad+good else None,
        'reject_recall':float(bad/records.rejects.sum()) if records.rejects.sum()>0 else None,
        'gross_future_cost_avoided_usd':float(records.gross_future_cost_avoided_usd.sum()),
        'false_reject_loss_usd':float(records.false_reject_loss_usd.sum()),
        'extra_disposal_cost_usd':float(records.extra_disposal_cost_usd.sum()),
        'net_scenario_usd':float(records.net_scenario_usd.sum()),
        'median_anticipation_minutes':float(selected_rows.minutes_before_historical_rejection_start.median()) if len(selected_rows) else None,
        'station_counts':selected_rows.proposed_discard_station.value_counts().to_dict(),
        'unique_first_decision_only':True,'not_realized_savings':True,
        'population':'historical survivors at final laser, common nominal-speed cohorts',
        'false_reject_extra_opportunity_loss_usd':config['scenario_assumptions']['scenarios'][scenario_name]['extra_good_piece_opportunity_loss_usd']
    }
    trajectory=candidates.copy()
    first_seq=records.selected_sequence.reindex(trajectory.cohort_id).to_numpy()
    trajectory['replay_state']=np.where(
        np.isnan(first_seq),'CONTINUAR',
        np.where(trajectory.stage_sequence.to_numpy()<first_seq,'CONTINUAR',
                 np.where(trajectory.stage_sequence.to_numpy()==first_seq,'DESCARTAR_COLECTIVO','NO_PROCESAR_YA_DESCARTADO')))
    trajectory['scenario']=scenario_name
    return stats,records.reset_index(),trajectory


def tune_policy(validation,config,scenario_name,method='model'):
    """Select on validation only; continue-all is a genuine candidate."""
    trials=[]
    for margin in config['policy_selection']['risk_safety_margins']:
        stats,_,_=replay(validation,config,scenario_name,margin,method)
        trials.append({'safety_margin':margin,'validation_net_usd':stats['net_scenario_usd'],
                       'validation_good_pieces_discarded':stats['good_pieces_discarded'],
                       'validation_cohorts_discarded':stats['cohorts_discarded']})
    best=max(trials,key=lambda r:(r['validation_net_usd'],-r['validation_good_pieces_discarded'],r['safety_margin']))
    enabled=best['validation_net_usd']>config['policy_selection']['minimum_validation_net_usd']
    return {'scenario':scenario_name,'method':method,'safety_margin':best['safety_margin'],
            'disabled':not enabled,'validation_net_usd':max(0.,best['validation_net_usd']),
            'trials':trials,'selected_on':'2026-09-07 through 2026-09-18, before test',
            'continue_all_was_candidate':True}


def daily_gain_interval(records,seed=42):
    daily=records.groupby(records.target_minute.dt.date).net_scenario_usd.sum().to_numpy()
    if not len(daily):return None
    rng=np.random.default_rng(seed)
    samples=daily[rng.integers(0,len(daily),size=(1000,len(daily)))].sum(axis=1)
    return {'unit':'day','days':len(daily),'bootstrap_resamples':1000,
            'total_net_scenario_95pct_interval_usd':np.quantile(samples,[.025,.975]).tolist(),
            'internal_simulated_sample_only':True}


def add_terminal_stations(trajectory,config):
    """All fifteen nodes, separating observations from forecasts."""
    base=trajectory.drop_duplicates('cohort_id').copy()
    terminals=[]
    for stage in ['LSR_BAR','CCL']:
        spec=config['stages'][stage];rows=base.copy()
        rows['stage']=stage;rows['stage_sequence']=spec['sequence']
        rows['observation_minute']=rows.target_minute-pd.Timedelta(minutes=spec['lead_minutes'])
        rows['decision_at']=rows.observation_minute+pd.Timedelta(minutes=config['feature_availability_delay_minutes'])
        rows['effective_lead_minutes']=spec['lead_minutes']-config['feature_availability_delay_minutes']
        rows['p_reject']=np.nan;rows['policy_probability']=np.nan
        rows['candidate_discard']=False;rows['economic_threshold']=np.nan
        rows['future_avoidable_proxy_usd']=0.
        rows['false_reject_loss_proxy_usd']=0.
        rows['extra_disposal_proxy_usd']=0.
        discarded_ids=set(trajectory.loc[trajectory.replay_state.eq('DESCARTAR_COLECTIVO'),'cohort_id'])
        rows['replay_state']=np.where(rows.cohort_id.isin(discarded_ids),'NO_PROCESAR_YA_DESCARTADO',
            'RECHAZO_FINAL_OBSERVADO' if stage=='LSR_BAR' else 'SIN_NUEVA_ETIQUETA_CALIDAD')
        rows['forecast_status']='observed_final_outcome' if stage=='LSR_BAR' else 'no_post_cooling_ground_truth'
        rows['timing_provenance']=spec['timing']
        terminals.append(rows)
    main=trajectory.copy();main['forecast_status']='prediction'
    main['timing_provenance']=main.stage.map({s:v['timing'] for s,v in config['stages'].items()})
    all_rows=pd.concat([main,*terminals],ignore_index=True).sort_values(['cohort_id','stage_sequence'])
    if not all_rows.groupby('cohort_id').size().eq(15).all():
        raise ValueError('El recorrido debe contener las 15 estaciones por colectivo.')
    return all_rows
