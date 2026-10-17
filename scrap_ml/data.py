"""Read-only CSV inputs and explicitly causal, end-of-minute features.

The target is an exact aggregate binomial count: FIN_BAR rejects / bars
measured at the final laser, not a fabricated individual-bar label.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

SOURCE_FILES = [
    'fct_production_minute.csv', 'fct_telemetry_minute.csv',
    'fct_laser_core_minute.csv', 'fct_laser_bar_minute.csv',
    'fct_scrap_minute.csv', 'fct_vision_gap.csv', 'fct_vision_crack.csv'
]
CORE_NUMERIC = ['length_mm_avg', 'length_mm_sd', 'width_mm_avg', 'width_mm_sd',
                'height_mm_avg', 'height_mm_sd']
TREND_PARAMS = [
    'MIX_MOISTURE_PCT', 'MIX_DOUGH_TEMP_C', 'DST_FEED_PRESSURE_BAR',
    'MLD_ROLLER_GAP_MM', 'OVN_Z1_TEMP_C', 'OVN_Z2_TEMP_C',
    'BCM_FLOW_KG_MIN', 'BCM_PRESSURE_BAR', 'CAR_ROLLER_GAP_MM',
    'CAR_TEMP_C', 'ENR_CURTAIN_KG_MIN', 'ENR_BOTTOM_KG_MIN',
    'ENR_VISCOSITY_CP', 'ENR_BLOWER_PCT'
]


def read_csv(path, cutoff=None, time_col='minute_ts', **kwargs):
    df = pd.read_csv(path, **kwargs)
    df[time_col] = pd.to_datetime(df[time_col])
    if cutoff is not None:
        df = df.loc[df[time_col].dt.floor('min') <= pd.Timestamp(cutoff)].copy()
    return df


def load_sources(data_dir, cutoff=None):
    """Inference truncates ALL inputs before constructing any derived features."""
    root = Path(data_dir)
    prod = read_csv(root / SOURCE_FILES[0], cutoff).set_index('minute_ts').sort_index()
    if prod.index.duplicated().any():
        raise ValueError('Produccion contiene minutos duplicados.')
    idx = prod.index
    tele = read_csv(root / SOURCE_FILES[1], cutoff)
    actual = tele.pivot(index='minute_ts', columns='parameter_code', values='value_actual').reindex(idx)
    setpoint = tele.pivot(index='minute_ts', columns='parameter_code', values='value_setpoint').reindex(idx)
    core_raw = read_csv(root / SOURCE_FILES[2], cutoff)
    if core_raw.duplicated(['minute_ts', 'lane_no']).any():
        raise ValueError('Laser de nucleo contiene claves duplicadas.')
    # Geometry statistics are observable cohort summaries, never final labels.
    core = core_raw.groupby('minute_ts')[CORE_NUMERIC].agg(['mean', 'min', 'max'])
    core.columns = ['core_' + '_'.join(c) for c in core.columns]
    core = core.reindex(idx)
    bar = read_csv(root / SOURCE_FILES[3], cutoff,
                   usecols=['minute_ts','lane_no','pieces','length_oos','width_oos','height_oos'])
    if bar.duplicated(['minute_ts','lane_no']).any():
        raise ValueError('Laser final contiene claves duplicadas.')
    final = bar.groupby('minute_ts')[['pieces','length_oos','width_oos','height_oos']].sum().reindex(idx)
    scrap = read_csv(root / SOURCE_FILES[4], cutoff)
    fin = scrap[scrap.collection_point_code.eq('FIN_BAR')].set_index('minute_ts')
    if fin.index.duplicated().any():
        raise ValueError('Hay duplicados en FIN_BAR.')
    final['rejects'] = fin.piece_count.reindex(idx).fillna(0)
    final['reject_cost_usd'] = fin.cost_usd.reindex(idx).fillna(0)
    bad = (final.rejects < final[['length_oos','width_oos','height_oos']].max(axis=1)) | (final.rejects > final.pieces)
    if bad.any():
        raise ValueError(f'Los rechazos no concilian con los limites de las dimensiones: {bad.sum()} minutos.')
    final['rate'] = final.rejects.div(final.pieces.where(final.pieces > 0))
    cameras = pd.DataFrame(index=idx)
    gap = read_csv(root / SOURCE_FILES[5], cutoff, time_col='detected_at')
    gap['minute_ts'] = gap.detected_at.dt.floor('min')
    for camera in ['CAM_PRE', 'CAM_POST']:
        counts = gap.loc[gap.camera_code.eq(camera)].groupby('minute_ts').size()
        cameras[camera + '_gap_events'] = counts.reindex(idx).fillna(0)
    crack = read_csv(root / SOURCE_FILES[6], cutoff, time_col='detected_at')
    crack['minute_ts'] = crack.detected_at.dt.floor('min')
    cameras['CAM_POST_crack_events'] = crack.groupby('minute_ts').size().reindex(idx).fillna(0)
    return {'production':prod, 'telemetry':actual, 'setpoints':setpoint,
            'core':core, 'final':final, 'cameras':cameras}


def material_offsets():
    # Documented in the supplied HTML, const av: MIX->POST=18,
    # POST->CORE=3, CORE->BAR=2; PRE->POST=9; MLD->PRE=1.
    return {'MIX':23, 'MLD':15, 'CAM_PRE':14, 'CAM_POST':5, 'LSR_CORE':2}


def build_features(sources, stage, config):
    """All source timestamps <= observation_minute; available at +1 minute.

    Snapshot readings from downstream assets describe their CURRENT state;
    they are not the future conditions this cohort will necessarily encounter.
    """
    t = sources['telemetry']; prod = sources['production']; ix = t.index
    horizon = config['stages'][stage]['lead_minutes']
    frame = t.add_prefix('now__').copy()
    error = t - sources['setpoints']
    frame = pd.concat([frame, error.add_prefix('setpoint_error__'), sources['core'], sources['cameras']], axis=1)
    consecutive = ix.to_series().diff().eq(pd.Timedelta(minutes=1))
    nominal = prod.line_speed_m_min.sub(config['nominal_speed_m_min']).abs().lt(1e-6)
    segment = (~consecutive | ~nominal | ~nominal.shift(fill_value=False)).cumsum()
    group = t.groupby(segment)
    trends = t[TREND_PARAMS]
    rolling = trends.groupby(segment).rolling(5, min_periods=5)
    mean = rolling.mean().reset_index(level=0,drop=True).reindex(ix).add_prefix('past5_mean__')
    std = trends.groupby(segment).rolling(5,min_periods=5).std().reset_index(level=0,drop=True).reindex(ix).add_prefix('past5_sd__')
    delta = (trends - group[TREND_PARAMS].shift(5)).add_prefix('delta5__')
    frame = pd.concat([frame,mean,std,delta],axis=1)
    # Known earlier exposures of this nominally aligned cohort. No negative shift.
    for machine, offset in material_offsets().items():
        lookback = offset - horizon
        if lookback > 0:
            cols = [c for c in t if c.startswith(machine + '_')]
            if cols:
                exposure = t[cols].copy()
                exposure.index = exposure.index + pd.Timedelta(minutes=lookback)
                frame = pd.concat([frame, exposure.reindex(ix).add_prefix('cohort_past__')],axis=1)
    final = sources['final']
    # Past downstream results from OTHER cohorts; legitimate persistence signal.
    recent_k = final.rejects.groupby(segment).rolling(5,min_periods=5).sum().reset_index(level=0,drop=True).reindex(ix)
    recent_n = final.pieces.groupby(segment).rolling(5,min_periods=5).sum().reset_index(level=0,drop=True).reindex(ix)
    frame['observed_final_reject_rate_5m'] = recent_k.div(recent_n.where(recent_n > 0))
    frame['line_speed_m_min'] = prod.line_speed_m_min
    clock = ix.hour * 60 + ix.minute
    frame['clock_sin'] = np.sin(2*np.pi*clock/1440)
    frame['clock_cos'] = np.cos(2*np.pi*clock/1440)
    frame['minutes_into_shift'] = np.where(clock<14*60,clock-6*60,clock-14.5*60)
    frame['thermal_shock_z1_dough'] = t.OVN_Z1_TEMP_C - t.MIX_DOUGH_TEMP_C
    frame['thermal_shock_z2_z1'] = t.OVN_Z2_TEMP_C - t.OVN_Z1_TEMP_C
    frame['cooler_shock'] = t.BCL_PRODUCT_IN_C - t.BCL_AIR_TEMP_C
    frame['guillotine_phase_abs'] = t.GUI_PHASE_ERROR_MS.abs()
    frame['guillotine_rate_mismatch'] = t.GUI_STROKE_PER_MIN - 1000*t.GUI_LINE_SPEED_M_MIN/t.GUI_CUT_LENGTH_MM
    lookback = max(config['history_minutes'], max(material_offsets().values())-horizon)
    # Exact-clock coverage; no ffill over stops, nights, cleaning or weekends.
    full_history = nominal.astype(int).groupby(segment).rolling(lookback+1,min_periods=lookback+1).sum().reset_index(level=0,drop=True).reindex(ix)
    valid_history = nominal & full_history.eq(lookback+1) & frame.notna().all(axis=1)
    frame = frame.astype('float32')
    manifest = {
        'stage':stage, 'nominal_lead_minutes':horizon,
        'available_at_offset_minutes':config['feature_availability_delay_minutes'],
        'effective_lead_minutes':horizon-config['feature_availability_delay_minutes'],
        'features':list(frame.columns), 'max_source_time_offset_minutes':0,
        'minimum_contiguous_nominal_history_minutes':lookback+1,
        'cohort_alignment':'nominal fixed offsets from supplied HTML; no per-bar tracking',
        'forbidden_sources':['future actual readings','future rejection','problem_event','future OEE state','absolute batch ID'],
        'downstream_snapshot_is_not_future_exposure':True,
        'availability_contract':'Whole observation minute has completed before scoring.'
    }
    return frame, valid_history, manifest


def build_dataset(sources, stage, config):
    x, valid, manifest = build_features(sources,stage,config)
    lead = config['stages'][stage]['lead_minutes']
    final = sources['final'].copy()
    # This shift is ONLY for supervised outcomes, never for feature construction.
    final.index = final.index - pd.Timedelta(minutes=lead)
    target = final.reindex(x.index)
    target['observation_minute'] = x.index
    target['decision_at'] = x.index + pd.Timedelta(minutes=config['feature_availability_delay_minutes'])
    target['target_minute'] = x.index + pd.Timedelta(minutes=lead)
    prod = sources['production']; nominal=prod.line_speed_m_min.sub(config['nominal_speed_m_min']).abs().lt(1e-6)
    # FUTURE selection is explicit: labels are aligned only in nominal-speed
    # uninterrupted windows. This is an offline domain restriction, never a
    # runtime feature nor a claim that future speed is known at deployment.
    regular = nominal & prod.index.to_series().diff().eq(pd.Timedelta(minutes=1))
    for offset in range(1,lead+1):
        shifted=regular.copy(); shifted.index=shifted.index-pd.Timedelta(minutes=offset)
        valid &= shifted.reindex(x.index,fill_value=False)
    valid &= target.pieces.gt(0) & target.rate.notna()
    target['batch_id_at_decision'] = prod.dough_batch_id
    manifest['future_nominal_speed_is_offline_domain_filter'] = True
    return x.loc[valid], target.loc[valid], manifest


def fingerprint(data_dir):
    result={}
    for name in SOURCE_FILES:
        path=Path(data_dir)/name
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
        result[name]={'bytes':path.stat().st_size,'sha256':digest.hexdigest()}
    return result
