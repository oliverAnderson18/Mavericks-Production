"""Backtest a preventive quality alert, selected on validation before test.

The alert is an advisory: a human or PLC corrective action can reduce the
following abnormal FIN_BAR scrap.  It never assumes that an earlier action
rescues the already-travelling collective.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'artifacts_improved'


def fin_bar_cost(data_dir,start,end):
    scrap=pd.read_csv(Path(data_dir)/'fct_scrap_minute.csv',parse_dates=['minute_ts'])
    fin=scrap[scrap.collection_point_code.eq('FIN_BAR')].set_index('minute_ts').cost_usd
    index=pd.date_range(pd.Timestamp(start),pd.Timestamp(end),freq='min')
    return fin.reindex(index,fill_value=0.)


def alert_windows(predictions,fin_cost,threshold,cooldown_minutes=10,horizon_minutes=10):
    """One alert per cooldown; measures only unique future response windows."""
    rows=predictions[(predictions.stage.eq('CAM_POST')) &
                     (predictions.p_reject.ge(threshold))].sort_values('decision_at')
    chosen=[];next_allowed=pd.Timestamp.min
    for row in rows.itertuples():
        if row.decision_at>=next_allowed:
            chosen.append(row);next_allowed=row.decision_at+pd.Timedelta(minutes=cooldown_minutes)
    covered=set();records=[]
    for row in chosen:
        # Intervention starts after the minute-level decision; it cannot alter
        # the bar currently in the laser path, only following production.
        minutes=pd.date_range(row.decision_at+pd.Timedelta(minutes=1),
                              row.decision_at+pd.Timedelta(minutes=horizon_minutes),freq='min')
        unique=[t for t in minutes if t not in covered];covered.update(unique)
        gross=float(fin_cost.reindex(unique,fill_value=0.).sum())
        records.append({'alert_time':row.decision_at,'source_stage':'CAM_POST',
                        'predicted_reject_fraction':float(row.p_reject),'threshold':threshold,
                        'unique_response_minutes':len(unique),'gross_fin_bar_cost_at_risk_usd':gross})
    alerts=pd.DataFrame(records)
    baseline=float(fin_cost.mean())
    if len(alerts):
        alerts['normal_cost_expected_usd']=alerts.unique_response_minutes*baseline
        alerts['excess_fin_bar_cost_at_risk_usd']=(alerts.gross_fin_bar_cost_at_risk_usd-alerts.normal_cost_expected_usd).clip(lower=0.)
    else:
        alerts['normal_cost_expected_usd']=pd.Series(dtype=float)
        alerts['excess_fin_bar_cost_at_risk_usd']=pd.Series(dtype=float)
    return alerts,{'alerts':len(alerts),'baseline_fin_bar_cost_per_minute_usd':baseline,
                   'gross_fin_bar_cost_at_risk_usd':float(alerts.gross_fin_bar_cost_at_risk_usd.sum()),
                   'excess_fin_bar_cost_at_risk_usd':float(alerts.excess_fin_bar_cost_at_risk_usd.sum()),
                   'horizon_minutes':horizon_minutes,'cooldown_minutes':cooldown_minutes}


def evaluate(data_dir):
    settings={'selection':{'file':'selection','start':'2026-09-07','end':'2026-09-18 23:59'},
              'test':{'file':'test','start':'2026-09-21','end':'2026-10-02 23:59'}}
    candidates=[.05,.10,.15,.20,.30];runs={};chosen=None
    for split,s in settings.items():
        pred=pd.read_csv(OUT/f'predictions_{s["file"]}.csv',parse_dates=['decision_at'])
        cost=fin_bar_cost(data_dir,s['start'],s['end']);runs[split]={}
        for threshold in candidates:
            alerts,stats=alert_windows(pred,cost,threshold)
            runs[split][str(threshold)]={'stats':stats,'alerts':alerts}
        if split=='selection':
            # Chosen without test: maximum abnormal cost at risk.  The complete
            # grid is retained so an operational alert-cap can be selected later.
            chosen=max(candidates,key=lambda t:runs['selection'][str(t)]['stats']['excess_fin_bar_cost_at_risk_usd'])
    out={'alert_stage':'CAM_POST','threshold_selected_on_validation':chosen,
         'selection_rule':'maximize validation abnormal FIN_BAR cost at risk, with 10-minute cooldown and response horizon',
         'savings_not_estimable_without_intervention_log':True,
         'splits':{}}
    for split in settings:
        item=runs[split][str(chosen)];stats=item['stats'];alerts=item['alerts'].copy()
        alerts.to_csv(OUT/f'prevention_alerts_{split}.csv',index=False)
        out['splits'][split]=stats
    (OUT/'prevention_metrics.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    return out


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--data-dir',required=True);args=p.parse_args()
    print(json.dumps(evaluate(args.data_dir),indent=2))
