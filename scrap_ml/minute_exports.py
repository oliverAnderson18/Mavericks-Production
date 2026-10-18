"""Three minute-level CSV exports from frozen models; no fitting of base models."""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from . import enrober, improved, machine_system
from .data import load_sources
from .full_line_data import build_full_line_dataset, predictive_stages
from .full_line_policy import candidate_decisions
from .train import split_masks

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / 'output'


def residual_bands(cal, test, group, prediction, target):
    """Signed 5/95% calibration residuals, clipped to [0,1]; empirical, not guaranteed."""
    residual = cal[target] - cal[prediction]
    limits = residual.groupby(cal[group]).quantile([.05, .95]).unstack()
    lower = (test[prediction] + test[group].map(limits[.05])).clip(0, 1)
    upper = (test[prediction] + test[group].map(limits[.95])).clip(0, 1)
    # Include the point prediction even for asymmetric residual distributions.
    lower = np.minimum(lower, test[prediction])
    upper = np.maximum(upper, test[prediction])
    valid = test[target].notna()
    return lower, upper, {
        'empirical_test_coverage': float(((test[target] >= lower) & (test[target] <= upper))[valid].mean()),
        'mean_test_band_width': float((upper - lower)[valid].mean()),
        'nominal_residual_quantiles': [.05, .95],
        'guaranteed_coverage': False,
        'calibration_rows': len(cal),
    }


def export(data_dir):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT / 'evaluation.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    q = joblib.load(ROOT / 'artifacts_improved/model.joblib')
    b = joblib.load(ROOT / 'artifacts_machine_system/diagnosis_model.joblib')
    c = joblib.load(enrober.OUT / 'model.joblib')
    cfg = q['config']; policy = q['policies']['materiales']
    sources = load_sources(data_dir)
    ix = sources['production'].index
    ix = ix[ix >= pd.Timestamp(cfg['splits']['test_start'])]
    print('CSV: riesgo por minuto y estacion...', flush=True)
    x, y, _, _ = build_full_line_dataset(sources, cfg)
    x = improved.augment(x, y, sources)
    masks = split_masks(y, cfg)
    frames = {}
    for split in ['calibration', 'test']:
        frame = y.loc[masks[split]].copy()
        features = x.loc[masks[split]]
        frame['p_reject'] = improved.predict_family(q['families'][policy['family']], features, frame.stage)
        if policy['persistence_blend']:
            blend = policy['persistence_blend']
            frame['p_reject'] = (1-blend)*frame.p_reject + blend*features.observed_final_reject_rate_5m.to_numpy()
        frames[split] = frame
    test = frames['test']
    test['prob_bajo'], test['prob_alto'], a_band = residual_bands(frames['calibration'], test, 'stage', 'p_reject', 'rate')
    test = candidate_decisions(test, cfg, 'materiales', policy['safety_margin'], disabled=policy['disabled'])
    test['decision'] = np.where(test.candidate_discard, 'retener', np.where(test.p_reject >= .05, 'vigilar', 'continuar'))
    risk = test.rename(columns={'observation_minute':'minute_ts', 'stage':'estacion', 'p_reject':'prob_scrap'})
    columns = ['prob_scrap', 'prob_bajo', 'prob_alto', 'decision']
    grid = pd.MultiIndex.from_product([ix, predictive_stages(cfg)], names=['minute_ts','estacion'])
    risk = risk.set_index(['minute_ts','estacion'])[columns].reindex(grid)
    risk['decision'] = risk.decision.fillna('vigilar')
    risk.reset_index().to_csv(OUTPUT / 'riesgo.csv', index=False)
    # Quality forecasting metrics supplement, rather than replace, economic replay.
    report['quality_model']['test']['forecast_rate_MAE'] = float(np.average(abs(test.rate-test.p_reject), weights=test.pieces))
    report['quality_model']['test']['intervals'] = a_band
    print('CSV: diagnostico por minuto...', flush=True)
    scores = machine_system.diagnosis_scores(b, machine_system.machine_features(sources).reindex(ix))
    flags = pd.DataFrame({m:scores[m] >= b['machines'][m]['threshold'] for m in machine_system.MACHINES})
    scores['maquina_senalada'] = scores.where(flags, -1).idxmax(axis=1).where(flags.any(axis=1), 'ninguna')
    scores.index.name = 'minute_ts'
    scores.reset_index().to_csv(OUTPUT / 'diagnostico.csv', index=False)
    del x, y, sources, frames
    print('CSV: prevencion por minuto y carril...', flush=True)
    tables = enrober.load_inputs(data_dir)
    frame, _ = enrober.build_features(tables)
    frame = enrober.add_targets(frame)
    split = enrober.masks(frame)
    cal = frame.loc[split['calibration']].copy()
    te = frame.loc[split['test']].copy()
    for part in [cal, te]:
        part['prediction'] = enrober.predict(c['model'], part)
    te['prob_bajo'], te['prob_alto'], c_band = residual_bands(cal, te, 'lane_no', 'prediction', 'target_rate')
    tickets = enrober.alerts(te, te.prediction.to_numpy(), c['threshold'])
    ticket_minutes = {e['observation_minute'] for e in tickets}
    aggregate = enrober.aggregate(te, te.prediction.to_numpy())
    te['accion_recomendada'] = np.where(te.observation_minute.isin(ticket_minutes), 'solicitar_revision_enrobadora',
        np.where(te.observation_minute.map(aggregate.predicted_rate).ge(c['threshold']), 'vigilar', 'continuar'))
    prevention = te.rename(columns={'observation_minute':'minute_ts','lane_no':'carril','prediction':'prob_degradacion'})
    grid = pd.MultiIndex.from_product([ix, enrober.EDGES], names=['minute_ts','carril'])
    prevention = prevention.set_index(['minute_ts','carril'])[['prob_degradacion','prob_bajo','prob_alto','accion_recomendada']].reindex(grid)
    prevention['accion_recomendada'] = prevention.accion_recomendada.fillna('vigilar')
    prevention['minutos_hasta_limite'] = np.nan
    prevention = prevention.reset_index()[['minute_ts','carril','prob_degradacion','prob_bajo','prob_alto','minutos_hasta_limite','accion_recomendada']]
    prevention.to_csv(OUTPUT / 'prevencion.csv', index=False)
    report['enrober_prevention']['test']['intervals'] = c_band
    report['minute_exports'] = {
        'period': 'test_only', 'minute_ts': 'observation minute; values become available at minute_ts + 1 minute',
        'files': {'prevencion.csv':len(prevention), 'riesgo.csv':len(risk), 'diagnostico.csv':len(scores)},
        'valid_prediction_rows': {'prevencion':int(prevention.prob_degradacion.notna().sum()),
                                  'riesgo':int(risk.prob_scrap.notna().sum()), 'diagnostico':len(scores)},
        'route':list(cfg['stages']), 'predictive_stations':predictive_stages(cfg),
        'prediction_semantics': {'prob_degradacion':'predicted future height reject fraction per edge lane, t+6..t+25; not probability of crossing 10%',
            'prob_scrap':'predicted final reject fraction of cohort, conditioned on station',
            'prob_bajo_prob_alto':'empirical rate bands using signed 5/95% calibration residuals per lane/station; clipped; no guaranteed coverage',
            'minutos_hasta_limite':'null: exact crossing time is not estimated by this single-horizon model'},
        'risk_station_column_required': True,
        'risk_missing_prediction': 'null rates and vigilar; offline common A evaluation domain, not an inference of zero risk',
        'risk_decisions': 'retener = existing economic discard candidate requiring operator review; vigilar = rate >=5% or missing; otherwise continuar',
        'retener_does_not_prove_savings': True,
        'prevention_repeated_ticket': 'same minute ticket repeats across six lane rows; count unique minute_ts only',
        'prevention_unavailable': 'null rates and vigilar; not enough eligible history or target support',
        'diagnosis_selection': 'highest score among machines exceeding their own frozen threshold; otherwise ninguna',
    }
    econ = report['enrober_prevention']['economics']
    report['savings_by_model'] = {
        'riesgo_A': {'net_historical_discard_replay_usd':report['decision_and_business_test']['net_scenario_usd'],
                     'realized_savings_usd':None, 'retention_only_savings_usd':None},
        'diagnostico_B': {'additional_savings_credited_usd':0., 'causal_savings_usd':None, 'reason':'diagnosis supports intervention; no independent repair outcomes'},
        'prevencion_C': {'additional_savings_credited_usd':0., 'causal_savings_usd':None,
                         'material_at_risk_upper_bound_usd':econ['edge_height_material_at_risk_upper_bound_usd'],
                         'conditional_scenarios':econ['scenarios']},
        'joint_conditional_no_double_count':econ['combined_sensitivity_no_double_count'],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    # This legacy generated output is replaced by the three minute CSVs.
    (OUTPUT / 'demo.json').unlink(missing_ok=True)
    print('Exportados prevencion.csv, riesgo.csv, diagnostico.csv y evaluation.json.', flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    export(parser.parse_args().data_dir)
