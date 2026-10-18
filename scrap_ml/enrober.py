r"""C: causal lane-minute coating forecast, chronological validation and alerts.

No individual tracking, intervention effect, or automatic machine control is assumed.
python -m scrap_ml.enrober train --data-dir .\data
python -m scrap_ml.enrober predict --data-dir .\data --observation-minute "2026-09-21 10:00"
"""
import argparse
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, precision_recall_fscore_support
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'artifacts_enrober'
EDGES = [1, 2, 3, 30, 31, 32]
CENTRE = list(range(11, 23))
CONFIG = {
    'version': 'enrober_v1', 'edge_lanes': EDGES, 'centre_lanes': CENTRE,
    'availability_minutes': 1, 'response_minutes': 5, 'forecast_window_minutes': 20,
    'core_to_bar_lag_minutes': 2, 'history_minutes': 30,
    'high_reject_rate': .10, 'minimum_precision_validation': .8,
    'cooldown_minutes': 30, 'response_effect_is_measured': False,
    'calibration_start': '2026-08-31', 'validation_start': '2026-09-07',
    'test_start': '2026-09-21', 'seed': 42,
}


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str,
                               allow_nan=False), encoding='utf-8')


def load_inputs(data_dir, cutoff=None):
    """Bound every raw table before any feature engineering at inference."""
    data_dir = Path(data_dir)
    tables = {}
    specs = {
        'production': ('fct_production_minute.csv', None),
        'bar': ('fct_laser_bar_minute.csv', ['minute_ts', 'lane_no', 'pieces',
                'height_mm_avg', 'height_mm_sd', 'height_oos', 'width_oos', 'length_oos']),
        'core': ('fct_laser_core_minute.csv', ['minute_ts', 'lane_no', 'pieces',
                 'height_mm_avg', 'height_oos']),
        'oee': ('fct_oee_minute.csv', ['minute_ts', 'state_code']),
        'scrap': ('fct_scrap_minute.csv', None),
    }
    for name, (filename, cols) in specs.items():
        table = pd.read_csv(data_dir / filename, usecols=cols, parse_dates=['minute_ts'])
        if cutoff is not None:
            table = table.loc[table.minute_ts <= pd.Timestamp(cutoff)].copy()
        keys = ['minute_ts', 'lane_no'] if name in ['bar', 'core'] else ['minute_ts']
        if name == 'scrap':
            keys += ['collection_point_code']
        if table.duplicated(keys).any():
            raise ValueError('Duplicate input keys: ' + name)
        tables[name] = table
    chunks = []
    for chunk in pd.read_csv(data_dir / 'fct_telemetry_minute.csv', chunksize=250000):
        chunk = chunk.loc[chunk.machine_code.eq('ENR')].copy()
        chunk['minute_ts'] = pd.to_datetime(chunk.minute_ts)
        if cutoff is not None:
            chunk = chunk.loc[chunk.minute_ts <= pd.Timestamp(cutoff)]
        chunks.append(chunk)
    tables['telemetry'] = pd.concat(chunks, ignore_index=True)
    if tables['telemetry'].duplicated(['minute_ts', 'parameter_code']).any():
        raise ValueError('Duplicate ENR telemetry keys')
    for name in ['bar', 'core']:
        x = tables[name]
        if (x.pieces.lt(0) | x.height_oos.lt(0) | x.height_oos.gt(x.pieces)).any():
            raise ValueError('Invalid piece counts: ' + name)
    return tables


def _wide(raw, column, index):
    return raw.pivot(index='minute_ts', columns='lane_no', values=column).reindex(index)


def _weighted(height, pieces, lanes):
    n = pieces[lanes].sum(axis=1).where(pieces[lanes].notna().all(axis=1))
    valid = (height[lanes].notna() | pieces[lanes].eq(0)).all(axis=1)
    return (height[lanes] * pieces[lanes]).sum(axis=1).div(n.where((n > 0) & valid))


def build_features(tables):
    """Only current and past readings. All aggregates are available at t+1."""
    prod = tables['production'].set_index('minute_ts').sort_index()
    index = prod.index
    if not index.is_unique:
        raise ValueError('Nonunique production minute')
    shift = prod.shift_code.astype(str)
    # A segment never traverses a night, scheduled clean or shift boundary.
    segment = ((index.to_series().diff() != pd.Timedelta(minutes=1)) |
               shift.ne(shift.shift())).cumsum()
    bar = tables['bar']; core = tables['core']
    pieces = _wide(bar, 'pieces', index)
    height = _wide(bar, 'height_mm_avg', index)
    std = _wide(bar, 'height_mm_sd', index)
    oos = _wide(bar, 'height_oos', index)
    if not set(EDGES + CENTRE).issubset(pieces.columns):
        raise ValueError('Missing required lanes')
    core_h = _wide(core, 'height_mm_avg', index)
    core_n = _wide(core, 'pieces', index)
    core_oos = _wide(core, 'height_oos', index)
    # Index offset, not positional row shift: missing minutes remain missing.
    core_h.index += pd.Timedelta(minutes=2)
    core_n.index += pd.Timedelta(minutes=2)
    core_oos.index += pd.Timedelta(minutes=2)
    core_h, core_n, core_oos = [x.reindex(index) for x in [core_h, core_n, core_oos]]
    aligned = pieces.gt(0) & core_n.gt(0)
    speed = prod.line_speed_m_min
    stable = speed.eq(6)
    for lag in [1, 2]:
        past = speed.copy(); past.index += pd.Timedelta(minutes=lag)
        stable &= past.reindex(index).eq(6)
    increment = (height - core_h).where(aligned & stable.to_numpy()[:, None])
    context = pd.DataFrame(index=index)
    context['edge_height'] = _weighted(height, pieces, EDGES)
    context['centre_height'] = _weighted(height, pieces, CENTRE)
    context['edge_minus_centre_mm'] = context.edge_height - context.centre_height
    context['edge_height_rate'] = oos[EDGES].sum(axis=1) / pieces[EDGES].sum(axis=1).replace(0, np.nan)
    context['centre_height_rate'] = oos[CENTRE].sum(axis=1) / pieces[CENTRE].sum(axis=1).replace(0, np.nan)
    context['edge_increment_mm'] = _weighted(increment, pieces.where(aligned), EDGES)
    context['centre_increment_mm'] = _weighted(increment, pieces.where(aligned), CENTRE)
    context['increment_edge_minus_centre'] = context.edge_increment_mm - context.centre_increment_mm
    clock = index.hour * 60 + index.minute
    context['minutes_into_shift'] = np.where(shift.eq('A'), clock - 360, clock - 870)
    context['shift_B'] = shift.eq('B').astype(float)
    state = tables['oee'].set_index('minute_ts').state_code.reindex(index)
    last_clean = pd.Series(index.where(state.eq('PLANNED_CLEAN')), index=index)
    last_clean = last_clean.groupby(index.normalize()).ffill()
    context['minutes_since_observed_clean'] = (index.to_series() - last_clean).dt.total_seconds() / 60
    context['clean_observed_today'] = last_clean.notna().astype(float)
    # At day start no last clean timestamp is invented.
    context['line_speed'] = speed
    tele = tables['telemetry']
    actual = tele.pivot(index='minute_ts', columns='parameter_code', values='value_actual').reindex(index)
    setpoint = tele.pivot(index='minute_ts', columns='parameter_code', values='value_setpoint').reindex(index)
    context = pd.concat([context, actual, (actual - setpoint).add_prefix('setpoint_error__')], axis=1)
    for column in ['edge_minus_centre_mm', 'edge_height_rate', 'increment_edge_minus_centre', *actual.columns]:
        for window in [5, 15, 30]:
            g = context[column].groupby(segment)
            context[f'{column}__mean{window}'] = g.transform(lambda s: s.rolling(window, min_periods=max(2, window // 2)).mean())
            context[f'{column}__slope{window}'] = (context[column] - g.shift(window)) / window
    all_lanes = []
    for lane in EDGES:
        x = context.copy()
        x['lane_no'] = lane
        x['lane_height'] = height[lane].where(pieces[lane] > 0)
        x['lane_height_sd'] = std[lane].where(pieces[lane] > 0)
        x['lane_height_rate'] = oos[lane] / pieces[lane].replace(0, np.nan)
        x['aligned_core_height'] = core_h[lane].where(aligned[lane] & stable)
        x['aligned_core_height_rate'] = (core_oos[lane] / core_n[lane].replace(0, np.nan)).where(stable)
        x['height_increment_proxy'] = increment[lane]
        for window in [5, 15, 30]:
            for c in ['lane_height', 'lane_height_rate', 'height_increment_proxy']:
                g = x[c].groupby(segment)
                x[f'{c}__mean{window}'] = g.transform(lambda s: s.rolling(window, min_periods=max(2, window // 2)).mean())
                x[f'{c}__slope{window}'] = (x[c] - g.shift(window)) / window
        x['observation_minute'] = index
        x['shift_code'] = shift
        x['pieces'] = pieces[lane]
        x['height_oos'] = oos[lane]
        x['segment'] = segment
        x['eligible'] = (shift.isin(['A', 'B']) & speed.gt(0) & pieces[lane].gt(0) &
                         pieces[EDGES + CENTRE].notna().all(axis=1) &
                         x.lane_height_rate__mean30.notna() & segment.groupby(segment).cumcount().ge(30) &
                         # Known scheduled clean/shift end, also checked live.
                         (clock + 25 < np.where(shift.eq('A'), 14 * 60, 22 * 60 + 30)))
        all_lanes.append(x.reset_index(drop=True))
    frame = pd.concat(all_lanes, ignore_index=True).sort_values(['observation_minute', 'lane_no']).reset_index(drop=True)
    # A line-level alert is possible only with the same six-lane live support.
    frame['eligible'] = frame.groupby('observation_minute').eligible.transform('all')
    metadata = {'observation_minute', 'shift_code', 'pieces', 'height_oos', 'segment', 'eligible'}
    columns = [c for c in frame.columns if c not in metadata]
    return frame, columns


def add_targets(frame):
    """Future labels are created separately from causal features.

    Decision t+1; 5 minutes allowed for operator response; outcome t+6..t+25.
    Empty production minutes have zero exposure, never fabricated bars.
    """
    frame = frame.copy()
    future_n = pd.Series(0., index=frame.index)
    future_k = future_n.copy()
    complete = pd.Series(True, index=frame.index)
    for lane, g in frame.groupby('lane_no', sort=False):
        t = pd.DatetimeIndex(g.observation_minute)
        src = g.set_index('observation_minute')
        n = np.zeros(len(g)); k = n.copy(); valid = np.ones(len(g), dtype=bool)
        for delta in range(6, 26):
            future = src.reindex(t + pd.Timedelta(minutes=delta))
            valid &= future.segment.to_numpy() == g.segment.to_numpy()
            valid &= future.pieces.notna().to_numpy() & future.height_oos.notna().to_numpy()
            n += future.pieces.fillna(0).to_numpy()
            k += future.height_oos.fillna(0).to_numpy()
        future_n.loc[g.index] = n
        future_k.loc[g.index] = k
        complete.loc[g.index] = valid
    frame['future_pieces'] = future_n
    frame['future_height_oos'] = future_k
    frame['target_rate'] = future_k / future_n.replace(0, np.nan)
    frame['target_start'] = frame.observation_minute + pd.Timedelta(minutes=6)
    frame['target_end'] = frame.observation_minute + pd.Timedelta(minutes=25)
    frame['label_available'] = complete & future_n.gt(0)
    frame['label_available'] = frame.groupby('observation_minute').label_available.transform('all')
    return frame


def masks(frame):
    cal = pd.Timestamp(CONFIG['calibration_start']); val = pd.Timestamp(CONFIG['validation_start'])
    test = pd.Timestamp(CONFIG['test_start'])
    eligible = frame.eligible & frame.label_available
    result = {
        'train': eligible & frame.target_end.lt(cal),
        'calibration': eligible & frame.observation_minute.ge(cal) & frame.target_end.lt(val),
        'validation': eligible & frame.observation_minute.ge(val) & frame.target_end.lt(test),
        'test': eligible & frame.observation_minute.ge(test),
    }
    days = {k: set(frame.loc[m, 'observation_minute'].dt.date) for k, m in result.items()}
    for a in days:
        if not days[a]:
            raise ValueError('Empty split ' + a)
        for b in days:
            if a != b and days[a] & days[b]:
                raise AssertionError('Days shared across splits')
    return result


def fit_model(name, frame, features):
    if name in ['persistence', 'linear_trend', 'clock_profile']:
        item = {'kind': name}
        if name == 'clock_profile':
            key = frame.minutes_into_shift.floordiv(30).astype(int)
            table = frame.assign(bucket=key).groupby(['lane_no', 'shift_B', 'bucket']).target_rate.mean()
            item['profile'] = table.to_dict()
            item['fallback'] = float(frame.target_rate.mean())
        return item
    params = {'hgb_small': (120, 15), 'hgb_flexible': (180, 31)}
    iterations, leaves = params[name]
    model = HistGradientBoostingRegressor(max_iter=iterations, max_leaf_nodes=leaves,
                learning_rate=.07, min_samples_leaf=80, l2_regularization=5,
                max_bins=127, early_stopping=False, random_state=42)
    weights = frame.future_pieces.to_numpy(float)
    with threadpool_limits(limits=4):
        model.fit(frame[features].astype('float32'), frame.target_rate, sample_weight=weights / weights.mean())
    return {'kind': name, 'model': model, 'features': features}


def predict(item, frame, calibrated=True):
    kind = item['kind']
    if kind == 'persistence':
        # A resumption can have <7 producing minutes in its past 15 minutes.
        # Fall back to the available current observation, without future fill.
        p = frame.lane_height_rate__mean15.fillna(frame.lane_height_rate).to_numpy()
    elif kind == 'linear_trend':
        p = frame.lane_height_rate__mean5 + 15.5 * frame.lane_height_rate__slope15
        p = p.fillna(frame.lane_height_rate__mean15).fillna(frame.lane_height_rate)
    elif kind == 'clock_profile':
        keys = zip(frame.lane_no.astype(int), frame.shift_B, frame.minutes_into_shift.floordiv(30).astype(int))
        p = np.array([item['profile'].get(k, item['fallback']) for k in keys])
    else:
        with threadpool_limits(limits=4):
            p = item['model'].predict(frame[item['features']].astype('float32'))
    p = np.clip(np.asarray(p, dtype=float), 0, 1)
    if calibrated and item.get('calibrator') is not None:
        p = item['calibrator'].predict(p)
    if not np.isfinite(p).all():
        raise ValueError('Forecast must be finite; missing history needs a causal fallback')
    return np.clip(p, 0, 1)


def aggregate(frame, p):
    x = frame[['observation_minute', 'pieces', 'future_pieces', 'future_height_oos', 'edge_height_rate']].copy()
    # Prediction weighting must match live inference: current volume only.
    x['predicted_k'] = np.asarray(p) * x.pieces
    g = x.groupby('observation_minute').agg(n=('future_pieces', 'sum'), k=('future_height_oos', 'sum'),
              current_n=('pieces', 'sum'), predicted_k=('predicted_k', 'sum'),
              current_rate=('edge_height_rate', 'first'), lanes=('future_pieces', 'size'))
    g = g[g.lanes.eq(len(EDGES))].copy()
    g['observed_rate'] = g.k / g.n
    g['predicted_rate'] = g.predicted_k / g.current_n
    return g


def forecast_metrics(frame, p, threshold=None):
    if threshold is None:
        threshold = CONFIG['high_reject_rate']
    g = aggregate(frame, p)
    truth = g.observed_rate.ge(CONFIG['high_reject_rate'])
    flags = g.predicted_rate.ge(threshold)
    a, b, f, _ = precision_recall_fscore_support(truth, flags, average='binary', zero_division=0)
    w = frame.future_pieces.to_numpy()
    return {'lane_minutes': len(frame), 'minutes': len(g),
        'lane_rate_mae': float(np.average(abs(frame.target_rate - p), weights=w)),
        'edge_group_rate_mae': float(np.average(abs(g.observed_rate - g.predicted_rate), weights=g.n)),
        'precision': float(a), 'recall': float(b), 'f1': float(f),
        'average_precision': float(average_precision_score(truth, g.predicted_rate)) if truth.any() else None,
        'positive_minutes': int(truth.sum()), 'flagged_minutes': int(flags.sum()),
        'false_flag_minutes': int((flags & ~truth).sum()), 'threshold': float(threshold)}


def alerts(frame, p, threshold):
    """One ticket per 30 minutes per line; no six-lane multiplication."""
    g = aggregate(frame, p)
    episodes = []
    last = None
    for t, row in g.iterrows():
        if row.predicted_rate < threshold:
            continue
        if last is not None and (t - last).total_seconds() < CONFIG['cooldown_minutes'] * 60:
            continue
        last = t
        start = t + pd.Timedelta(minutes=6)
        end = t + pd.Timedelta(minutes=25)
        episodes.append({'observation_minute': t, 'decision_at': t + pd.Timedelta(minutes=1),
            'response_window_start': start, 'response_window_end': end,
            'predicted_edge_rate': float(row.predicted_rate), 'observed_future_rate': float(row.observed_rate),
            'correct_high_risk_alert': bool(row.observed_rate >= CONFIG['high_reject_rate']),
            'current_edge_rate': float(row.current_rate), 'already_high_at_alert': bool(row.current_rate >= CONFIG['high_reject_rate']),
            'action': 'SOLICITAR_REVISION_ENROBADORA', 'physical_actuation': False})
    return episodes


def onset_metrics(tables, frame, p, threshold):
    """Evaluate new sustained onsets separately from an already active defect."""
    raw = tables['bar']; raw = raw[raw.lane_no.isin(EDGES)]
    g = raw.groupby('minute_ts')[['pieces', 'height_oos']].sum()
    rate = g.height_oos.div(g.pieces.replace(0, np.nan))
    seg = (rate.index.to_series().diff().ne(pd.Timedelta(minutes=1)) | rate.isna()).cumsum()
    # Onset: previous five minutes below the operational rate, next five above it.
    high = rate.ge(CONFIG['high_reject_rate']) & rate.notna()
    prev_low = high.astype(float).groupby(seg).transform(lambda x: x.shift(1).rolling(5, min_periods=5).sum()).eq(0)
    next_high = sum(high.shift(-k, freq='min').reindex(rate.index).fillna(False).astype(int) for k in range(5)).eq(5)
    candidates = rate.index[high & prev_low & next_high]
    test_start = frame.observation_minute.min(); test_end = frame.target_end.max()
    candidates = candidates[(candidates >= test_start + pd.Timedelta(minutes=30)) & (candidates <= test_end)]
    ag = aggregate(frame, p)
    early = []; missed = 0
    for t in candidates:
        opportunity = ag.loc[(ag.index >= t - pd.Timedelta(minutes=25)) &
                             (ag.index <= t - pd.Timedelta(minutes=6))]
        opportunity = opportunity[opportunity.current_rate.lt(CONFIG['high_reject_rate']) & opportunity.predicted_rate.ge(threshold)]
        if len(opportunity):
            early.append(float((t - opportunity.index[0]).total_seconds() / 60 - 1))
        else:
            missed += 1
    return {'sustained_onsets': len(candidates), 'onsets_warned_with_at_least_5min_response': len(early),
        'onset_recall': len(early) / len(candidates) if len(candidates) else None,
        'median_warning_minutes_from_decision': float(np.median(early)) if early else None,
        'important': 'Overlapping forecast windows are not independent incidents.'}


def accounting(tables, frame, episodes, legacy_records=None):
    raw = tables['bar']; bar = raw[raw.lane_no.isin(EDGES)].groupby('minute_ts').height_oos.sum()
    scrap = tables['scrap']; fin = scrap[scrap.collection_point_code.eq('FIN_BAR')].set_index('minute_ts')
    # Height-only pieces cannot be isolated from width/length on the same bar.
    # This is an upper bound on material at risk, not attribution or saved money.
    unit = fin.cost_usd / fin.piece_count.replace(0, np.nan)
    risk = bar.reindex(fin.index, fill_value=0).clip(upper=fin.piece_count) * unit.fillna(0)
    days = pd.DatetimeIndex(frame.observation_minute.dt.normalize().unique())
    test = fin.index.normalize().isin(days)
    covered = pd.Series(False, index=fin.index)
    for e in episodes:
        covered |= (covered.index >= e['response_window_start']) & (covered.index <= e['response_window_end'])
    covered &= test
    cost_at_risk = float(risk.loc[covered].sum())
    all_fin = float(fin.loc[test, 'cost_usd'].sum())
    all_scrap = float(scrap.loc[scrap.minute_ts.dt.normalize().isin(days), 'cost_usd'].sum())
    effect_scenarios = []
    for efficacy in [0., .25, .5, .75, 1.]:
        gross = efficacy * cost_at_risk
        effect_scenarios.append({'assumed_effectiveness': efficacy, 'cost_not_supplied': True,
            'gross_conditional_material_usd': gross,
            'break_even_all_in_intervention_cost_per_ticket_usd': gross / len(episodes) if episodes else 0,
            'fraction_of_same_days_FIN_BAR': gross / all_fin if all_fin else 0,
            'fraction_of_same_days_total_scrap': gross / all_scrap if all_scrap else 0,
            'annual_260day_equivalent_before_intervention_cost_usd': gross / len(days) * 260})
    result = {'test_days': len(days), 'test_FIN_BAR_usd': all_fin, 'test_total_scrap_usd': all_scrap,
        'deduplicated_tickets': len(episodes), 'protected_minutes_once': int(covered.sum()),
        'edge_height_material_at_risk_upper_bound_usd': cost_at_risk,
        'realized_or_causally_validated_savings_usd': None,
        'conservative_additional_savings_credited_usd': 0.,
        'effectiveness_scenarios_are_assumptions_not_estimates': True,
        'scenario_formula': 'net = effectiveness * height_material_at_risk - all intervention costs',
        'scenarios': effect_scenarios,
        'limits': ['height_oos overlaps length/width; correcting height may not save the whole bar',
                   '5 minute operator response and 20 minute benefit window are operational assumptions',
                   'no intervention history; no causal efficacy or net correction saving is identifiable',
                   'existing defect is possible; use onset results for claims of advance warning']}
    if legacy_records is not None and len(legacy_records):
        r = legacy_records.copy(); r['target_minute'] = pd.to_datetime(r.target_minute)
        active = r.proposed_discard_station.ne('NO_EARLY_DISCARD')
        overlap = r.target_minute.map(covered).fillna(False).astype(bool)
        residual = float(r.loc[active & ~overlap, 'net_scenario_usd'].sum())
        standalone = float(r.net_scenario_usd.sum())
        result['model_A_net_replay_standalone_usd'] = standalone
        result['model_A_net_replay_outside_C_windows_usd'] = residual
        result['model_A_net_replay_inside_C_windows_usd'] = standalone - residual
        result['combined_sensitivity_no_double_count'] = [
            {'assumed_C_effectiveness': e['assumed_effectiveness'],
             'A_residual_plus_C_gross_before_correction_cost_usd': residual + e['gross_conditional_material_usd']}
            for e in effect_scenarios]
        result['combined_sensitivity_is_not_a_validated_joint_policy'] = True
    return result


def eda(tables):
    raw = tables['bar']; core = tables['core']; prod = tables['production']
    lane = raw.groupby('lane_no')[['pieces', 'height_oos']].sum()
    total_oos = float(lane.height_oos.sum())
    edge_oos = float(lane.loc[EDGES, 'height_oos'].sum())
    aligned = raw[['minute_ts', 'lane_no', 'pieces', 'height_mm_avg', 'height_oos']].merge(
        core[['minute_ts', 'lane_no', 'pieces', 'height_mm_avg', 'height_oos']].assign(
            minute_ts=lambda x: x.minute_ts + pd.Timedelta(minutes=2)),
        on=['minute_ts', 'lane_no'], how='left', suffixes=('_bar', '_core'), validate='one_to_one')
    valid = aligned.pieces_bar.gt(0) & aligned.pieces_core.gt(0)
    healthy = valid & aligned.height_oos_core.eq(0) & aligned.height_mm_avg_core.between(13, 15)
    o = aligned.loc[valid, 'height_oos_bar'].sum()
    def profile(g):
        return float(np.average(g.height_mm_avg, weights=g.pieces)) if g.pieces.sum() else None
    raw = raw.merge(prod[['minute_ts', 'shift_code']], on='minute_ts', validate='many_to_one')
    profile_rows = []
    for shift in ['A', 'B']:
        for zone, lanes in [('edges', EDGES), ('centre', CENTRE)]:
            g = raw[raw.shift_code.eq(shift) & raw.lane_no.isin(lanes) & raw.pieces.gt(0)]
            profile_rows.append({'shift': shift, 'zone': zone, 'height_mm': profile(g),
                'height_oos_rate': float(g.height_oos.sum() / g.pieces.sum())})
    s = tables['scrap']
    return {'days': prod.minute_ts.dt.date.nunique(), 'minutes': len(prod), 'lanes': len(lane),
        'total_scrap_usd': float(s.cost_usd.sum()),
        'FIN_BAR_usd': float(s.loc[s.collection_point_code.eq('FIN_BAR'), 'cost_usd'].sum()),
        'height_oos_count': int(total_oos), 'edge_height_oos_count': int(edge_oos),
        'edge_share_height_oos': edge_oos / total_oos,
        'edge_share_measured_volume': float(lane.loc[EDGES, 'pieces'].sum() / lane.pieces.sum()),
        'aligned_final_height_oos_with_healthy_core_fraction': float(aligned.loc[healthy, 'height_oos_bar'].sum() / o),
        'core_to_bar_alignment': '2 minute aggregate transport assumption; no individual match',
        'profile_by_shift': profile_rows,
        'causal_interpretation': 'consistent with coating or sensor bias; shift/clean coincidence does not isolate treatment'}


def bootstrap(frame, model_p, baseline_p):
    x = frame[['observation_minute', 'target_rate', 'future_pieces']].copy()
    x['a'] = abs(x.target_rate - model_p) * x.future_pieces
    x['b'] = abs(x.target_rate - baseline_p) * x.future_pieces
    daily = x.groupby(x.observation_minute.dt.date)[['a', 'b', 'future_pieces']].sum()
    rng = np.random.default_rng(42)
    ix = rng.integers(0, len(daily), size=(1000, len(daily)))
    a = daily.a.to_numpy()[ix].sum(axis=1); b = daily.b.to_numpy()[ix].sum(axis=1)
    n = daily.future_pieces.to_numpy()[ix].sum(axis=1)
    return {'unit': 'production_day_all_lanes_together', 'days': len(daily), 'resamples': 1000,
        'lane_MAE_improvement_vs_persistence_95pct': np.quantile((b - a) / n, [.025, .975]).tolist(),
        'dependent_windows_are_not_independent_samples': True}


def walk_forward(frame, features, kind):
    days = sorted(frame.observation_minute.dt.normalize().unique())
    reports = []
    for end in [20, 30, 40]:
        start = pd.Timestamp(days[end]); until = pd.Timestamp(days[end + 5])
        train = frame.eligible & frame.label_available & frame.target_end.lt(start)
        test = frame.eligible & frame.label_available & frame.observation_minute.ge(start) & frame.target_end.lt(until)
        a = frame.loc[train]; b = frame.loc[test]
        model = fit_model(kind, a, features)
        p = predict(model, b)
        persistence = predict({'kind': 'persistence'}, b)
        reports.append({'train_days': end, 'validation_start': str(start.date()), 'validation_days': 5,
            'model': forecast_metrics(b, p), 'persistence': forecast_metrics(b, persistence),
            'threshold_fixed_at_10pct_for_fold': True})
    return reports


def train(data_dir, legacy_records=None):
    OUT.mkdir(parents=True, exist_ok=True)
    save_json(OUT / 'protocol.json', CONFIG)
    print('C: leyendo carriles, geometria y telemetria ENR...', flush=True)
    tables = load_inputs(data_dir)
    frame, features = build_features(tables)
    frame = add_targets(frame)
    split = masks(frame)
    # This invariant directly checks the only feature function used by inference.
    cutoff = pd.Timestamp('2026-09-21 10:00')
    bounded = {k: v[v.minute_ts.le(cutoff)].copy() for k, v in tables.items()}
    truncated, truncated_features = build_features(bounded)
    a = frame.loc[frame.observation_minute.eq(cutoff), features].reset_index(drop=True)
    b = truncated.loc[truncated.observation_minute.eq(cutoff), truncated_features].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    tr = frame.loc[split['train']]; ca = frame.loc[split['calibration']]
    va = frame.loc[split['validation']]
    candidates = {}; val_predictions = {}; trials = []
    for kind in ['persistence', 'linear_trend', 'clock_profile', 'hgb_small', 'hgb_flexible']:
        print('C: candidato ' + kind, flush=True)
        item = fit_model(kind, tr, features)
        if kind.startswith('hgb'):
            calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip')
            calibrator.fit(predict(item, ca), ca.target_rate, sample_weight=ca.future_pieces)
            raw = predict(item, va)
            calibrated = calibrator.predict(raw)
            if forecast_metrics(va, calibrated)['lane_rate_mae'] < forecast_metrics(va, raw)['lane_rate_mae']:
                item['calibrator'] = calibrator
        p = predict(item, va); val_predictions[kind] = p
        metric = forecast_metrics(va, p)
        trials.append({'candidate': kind, **metric, 'calibrated': item.get('calibrator') is not None})
        candidates[kind] = item
    # Forecast quality selects the model on validation, never from test economics.
    chosen = min(trials, key=lambda r: (r['lane_rate_mae'], r['candidate']))['candidate']
    item = candidates[chosen]
    # Forecast cutoff is an action threshold, not a calibrated event probability.
    cuts = []
    for threshold in [.03, .04, .05, .06, .08, .1, .15]:
        m = forecast_metrics(va, val_predictions[chosen], threshold)
        cuts.append(m)
    # The operator-facing cutoff is specified before test by the business rule.
    threshold = CONFIG['high_reject_rate']
    bundle = {'version': CONFIG['version'], 'config': CONFIG, 'features': features,
        'selected_candidate': chosen, 'model': item, 'threshold': threshold,
        'trained_until_exclusive': CONFIG['calibration_start'],
        'selection_until_exclusive': CONFIG['test_start'], 'test_previously_explored_by_project': True}
    # Freeze to disk before final test predictions; no fit after this point.
    joblib.dump(bundle, OUT / 'model.joblib', compress=3)
    freeze_hash = hashlib.sha256((OUT / 'model.joblib').read_bytes()).hexdigest()
    te = frame.loc[split['test']].copy()
    p = predict(item, te)
    baseline = predict(candidates['persistence'], te)
    episodes = alerts(te, p, threshold)
    best_hgb = min([t for t in trials if t['candidate'].startswith('hgb')], key=lambda r: r['lane_rate_mae'])['candidate']
    folds = walk_forward(frame, features, best_hgb)
    metrics = {'version': CONFIG['version'], 'selected_candidate': chosen,
        'target': 'future edge-lane height_oos / measured pieces, t+6..t+25',
        'unit': 'lane x minute; pooled model over six physically fixed edge lanes',
        'features': len(features), 'forecast_rate_is_not_probability_of_machine_fault': True,
        'validation_trials': trials, 'validation_threshold_trials': cuts, 'threshold': threshold,
        'threshold_selection_rule': 'fixed_10pct_business_rule_not_selected_on_test',
        'test': forecast_metrics(te, p, threshold),
        'test_baselines': {k: forecast_metrics(te, predict(candidates[k], te), threshold)
                           for k in ['persistence', 'linear_trend', 'clock_profile']},
        'test_all_candidates_for_transparency_no_reselection': {k: forecast_metrics(te, predict(v, te), threshold)
                                                              for k, v in candidates.items()},
        'splits': {k: {'lane_minutes': int(m.sum()), 'days': frame.loc[m, 'observation_minute'].dt.date.nunique(),
                      'first_observation': str(frame.loc[m, 'observation_minute'].min()),
                      'last_target': str(frame.loc[m, 'target_end'].max())} for k, m in split.items()},
        'audit': {'features_equal_when_future_is_removed': True, 'day_and_lane_split_is_joint': True,
                  'future_target_windows_purged_at_split_boundaries': True,
                  'no_event_annotations_as_C_features': True, 'model_sha256_before_test': freeze_hash,
                  'test_not_used_for_model_or_threshold_selection': True,
                  'dataset_simulated_and_previously_explored': True,
                  'external_validation_available': False},
        'walk_forward_fixed_hgb': folds, 'uncertainty': bootstrap(te, p, baseline),
        'onset_test': onset_metrics(tables, te, p, threshold),
        'alerts_test': {'tickets': len(episodes), 'correct_future_high_risk_tickets': sum(e['correct_high_risk_alert'] for e in episodes),
            'tickets_with_already_high_current_rate': sum(e['already_high_at_alert'] for e in episodes),
            'tickets_per_day': len(episodes) / te.observation_minute.dt.date.nunique()},
        'economics': accounting(tables, te, episodes, legacy_records), 'eda': eda(tables),
        'limitations': ['Aggregate signals locate a candidate process, not proven root cause',
            'No measured response time or successful correction rate',
            'Historical actions would change later features; replay is observational',
            'Incoming-core alignment is nominal; stop/variable-speed pairs are masked',
            'Current downstream quality may already show defects; see separate onset metrics']}
    save_json(OUT / 'metrics.json', metrics)
    agg = aggregate(te, p)
    agg.to_csv(OUT / 'test_minutes.csv', index_label='observation_minute')
    # Full timeline is temporary; a concise stream is added to the one demo JSON.
    stream = []
    demo_day = te.observation_minute.dt.normalize().min()
    minute_subset = agg.loc[agg.index.normalize() == demo_day]
    for t, row in minute_subset.iterrows():
        if t.minute % 5:
            continue
        lanes = te.loc[te.observation_minute.eq(t)].copy()
        lane_p = p[te.observation_minute.eq(t).to_numpy()]
        stream.append({'observation_minute': str(t), 'decision_at': str(t + pd.Timedelta(minutes=1)),
            'machine': 'ENR', 'predicted_future_edge_height_rate': float(row.predicted_rate),
            'current_edge_height_rate': float(row.current_rate), 'threshold': threshold,
            'action': 'SOLICITAR_REVISION_ENROBADORA' if row.predicted_rate >= threshold else 'CONTINUAR_VIGILANCIA',
            'evidence': {'edge_minus_centre_mm': float(lanes.edge_minus_centre_mm.iloc[0])
                         if pd.notna(lanes.edge_minus_centre_mm.iloc[0]) else None},
            'lane_forecasts': [{'lane': int(l), 'predicted_height_reject_fraction': float(v)} for l, v in zip(lanes.lane_no, lane_p)],
            'physical_actuation': False})
    save_json(OUT / 'demo.json', {'selected_candidate': chosen, 'threshold': threshold,
        'selection_rule': 'first test day, every 5 minutes; not selected for flattering performance',
        'forecast_offset_minutes': [6, 25], 'timeline': stream,
        'tickets': [{**e, 'observation_minute': str(e['observation_minute'])} for e in episodes],
        'color': 'orange specific coating risk; red only suspected incident, not proven cause'})
    print('C: ' + chosen + '; test ' + json.dumps(metrics['test']) + '; ' + str(len(episodes)) + ' tickets.', flush=True)
    return metrics


def assess(data_dir, observation_minute):
    bundle = joblib.load(OUT / 'model.joblib')
    obs = pd.Timestamp(observation_minute)
    frame, _ = build_features(load_inputs(data_dir, cutoff=obs))
    x = frame.loc[frame.observation_minute.eq(obs) & frame.eligible]
    if len(x) != len(EDGES):
        return {'status': 'ABSTENERSE', 'reason': '30 minutos de historia y seis carriles completos requeridos',
                'physical_actuation': False}
    p = predict(bundle['model'], x)
    rate = float(np.average(p, weights=x.pieces))
    return {'status': 'OK', 'observation_minute': str(obs), 'decision_at': str(obs + pd.Timedelta(minutes=1)),
        'machine': 'ENR', 'predicted_future_edge_height_rate': rate, 'threshold': bundle['threshold'],
        'action': 'SOLICITAR_REVISION_ENROBADORA' if rate >= bundle['threshold'] else 'CONTINUAR_VIGILANCIA',
        'lane_forecasts': [{'lane': int(l), 'predicted_height_reject_fraction': float(v)} for l, v in zip(x.lane_no, p)],
        'physical_actuation': False}


def combine_decision(quality_decision, coating):
    """Combine independent cohort and following-stream advice, without actuation.

    In particular, C never claims to repair material that A already discards.
    The service/UI must acknowledge and deduplicate requests before action.
    """
    result = dict(quality_decision)
    result['enrober_prevention'] = coating
    result['preventive_review_request'] = None
    if coating.get('action') == 'SOLICITAR_REVISION_ENROBADORA':
        result['preventive_review_request'] = {
            'machine': 'ENR', 'operator_confirmation_required': True,
            'scope': 'following_production_stream', 'source': 'model_C',
            'reason': 'FORECAST_EDGE_HEIGHT_REJECTION',
            'forecast_rate': coating['predicted_future_edge_height_rate'],
            'physical_actuation': False, 'additional_savings_credited_usd': 0.}
        result['combined_action_with_prevention'] = result.get('cohort_action', result['action']) + '_Y_REVISAR_ENR'
    else:
        result['combined_action_with_prevention'] = result.get('combined_action', result['action'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['train', 'predict'])
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--observation-minute')
    args = parser.parse_args()
    if args.command == 'train':
        report = train(args.data_dir)
        output = ROOT.parent / 'output' / 'evaluation.json'
        existing = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {}
        existing['enrober_prevention'] = report
        save_json(output, existing)
        demo_path = ROOT.parent / 'output' / 'demo.json'
        demo = json.loads(demo_path.read_text(encoding='utf-8')) if demo_path.exists() else {}
        demo['enrober_prevention'] = json.loads((OUT / 'demo.json').read_text(encoding='utf-8'))
        save_json(demo_path, demo)
        for name in ['metrics.json', 'test_minutes.csv', 'demo.json', 'protocol.json']:
            (OUT / name).unlink(missing_ok=True)
    else:
        if not args.observation_minute:
            parser.error('--observation-minute required')
        print(json.dumps(assess(args.data_dir, args.observation_minute), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
