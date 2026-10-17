"""Route-free alert-to-diagnosis study for the line-correction action.

This measures observed abnormal FIN_BAR cost after an alert. It is a value-at-
risk measure: it intentionally does not claim that an unobserved repair saved it.
"""
import argparse
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from .machine_system import MACHINES,OUT as DIAG_OUT,event_targets
from .prevention import evaluate as prevention_evaluate
from .full_line import dumps

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'artifacts_correction_study'


def diagnosis_at_alert(alerts,scores,bundle,events):
    """Use only scores that existed one minute before each decision alert."""
    truth=event_targets(events,scores.index)
    rows=[]
    for alert in alerts.itertuples():
        observation=pd.Timestamp(alert.alert_time)-pd.Timedelta(minutes=1)
        if observation not in scores.index:continue
        score=scores.loc[observation,MACHINES]
        flagged=[m for m in MACHINES if score[m]>=bundle['machines'][m]['threshold']]
        machine=max(flagged,key=lambda m:score[m]) if flagged else None
        rows.append({'alert_time':alert.alert_time,'observation_minute':observation,
            'likely_machine':machine,'diagnostic_score':float(score[machine]) if machine else None,
            'machine_registered_active':bool(truth.loc[observation,machine]) if machine else False,
            'any_registered_machine_event':bool(truth.loc[observation].any()),
            'gross_fin_bar_cost_at_risk_usd':float(alert.gross_fin_bar_cost_at_risk_usd),
            'excess_fin_bar_cost_at_risk_usd':float(alert.excess_fin_bar_cost_at_risk_usd),
            'predicted_reject_fraction':float(alert.predicted_reject_fraction)})
    return pd.DataFrame(rows)


def consolidate_operational_actions(attached,horizon_minutes):
    """One correction request per machine while its response window is open."""
    rows=[];open_until={}
    for row in attached.sort_values('alert_time').itertuples():
        if row.likely_machine is None or pd.isna(row.likely_machine):continue
        until=open_until.get(row.likely_machine)
        if until is not None and row.alert_time<=until:continue
        end=row.alert_time+pd.Timedelta(minutes=horizon_minutes)
        open_until[row.likely_machine]=end
        rows.append({'correction_id':f'{row.likely_machine}-{row.alert_time:%Y%m%dT%H%M}',
            'machine':row.likely_machine,'alert_time':row.alert_time,'response_window_end':end,
            'diagnostic_score':row.diagnostic_score,'machine_registered_active':row.machine_registered_active,
            'gross_FIN_BAR_cost_at_risk_usd':row.gross_fin_bar_cost_at_risk_usd,
            'excess_FIN_BAR_cost_at_risk_usd':row.excess_fin_bar_cost_at_risk_usd,
            'predicted_reject_fraction':row.predicted_reject_fraction})
    return pd.DataFrame(rows)


def study(data_dir):
    OUT.mkdir(exist_ok=True)
    prevention=prevention_evaluate(data_dir)
    bundle=joblib.load(DIAG_OUT/'diagnosis_model.joblib')
    scores=pd.read_csv(DIAG_OUT/'machine_scores.csv',index_col='observation_minute',parse_dates=True)
    events=pd.read_csv(Path(data_dir)/'fct_problem_event.csv',parse_dates=['started_at','ended_at'])
    report={'method':'alert selected on validation, diagnosis score available at alert observation; FIN_BAR response windows are disjoint',
            'is_intervention_effect':False,'correction_saving_usd':None,'splits':{}}
    for split in ['selection','test']:
        alerts=pd.read_csv(ROOT/f'artifacts_improved/prevention_alerts_{split}.csv',parse_dates=['alert_time'])
        attached=diagnosis_at_alert(alerts,scores,bundle,events)
        attached.to_csv(OUT/f'alerts_with_diagnosis_{split}.csv',index=False)
        located=attached[attached.likely_machine.notna()].copy()
        matched=located[located.machine_registered_active].copy()
        episodes=consolidate_operational_actions(located,10)
        episodes.to_csv(OUT/f'operational_corrections_{split}.csv',index=False)
        report['splits'][split]={
            'alerts':len(attached),'alerts_with_machine_diagnosis':len(located),
            'machine_diagnoses_matching_registered_event':len(matched),
            'gross_FIN_BAR_cost_at_risk_all_alerts_usd':float(attached.gross_fin_bar_cost_at_risk_usd.sum()),
            'excess_FIN_BAR_cost_at_risk_all_alerts_usd':float(attached.excess_fin_bar_cost_at_risk_usd.sum()),
            'excess_FIN_BAR_cost_at_risk_when_machine_identified_usd':float(located.excess_fin_bar_cost_at_risk_usd.sum()),
            'excess_FIN_BAR_cost_at_risk_when_diagnosis_matches_event_usd':float(matched.excess_fin_bar_cost_at_risk_usd.sum()),
            'operational_line_correction_requests':len(episodes),
            'excess_FIN_BAR_cost_at_risk_for_deduplicated_corrections_usd':float(episodes.excess_FIN_BAR_cost_at_risk_usd.sum()),
            'machine_counts':located.likely_machine.value_counts().to_dict(),
            'correction_cost_not_available':True,
            'not_a_saved_amount':True}
    report['prevention_threshold_selected_on_validation']=prevention['threshold_selected_on_validation']
    (OUT/'metrics.json').write_text(dumps(report),encoding='utf-8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);args=p.parse_args()
    print(dumps(study(args.data_dir)))
