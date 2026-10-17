"""Decision economics; no PLC control or automatic physical rejection."""
import numpy as np


def threshold(avoidable_future_cost, false_reject_loss, disposal_cost=0.0, min_gain=0.0):
    """Break-even probability for p*S - (1-p)*L - D > min_gain."""
    values = [avoidable_future_cost, false_reject_loss, disposal_cost, min_gain]
    if any(v is None or not np.isfinite(v) or v < 0 for v in values):
        raise ValueError('Se requieren costes finitos no negativos, confirmados por negocio.')
    denom = avoidable_future_cost + false_reject_loss
    if denom <= 0:
        raise ValueError('El coste evitable mas la perdida por falso rechazo debe ser positivo.')
    return (false_reject_loss + disposal_cost + min_gain) / denom


def expected_gain(probability, avoidable_future_cost, false_reject_loss, disposal_cost=0.0):
    p = np.asarray(probability, dtype=float)
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('La probabilidad debe estar en [0,1].')
    threshold(avoidable_future_cost, false_reject_loss, disposal_cost)
    return p * avoidable_future_cost - (1 - p) * false_reject_loss - disposal_cost


def recommend(probabilities, stage, economics, model_qualified=True):
    """Return advisory actions. Missing economics cannot cause a discard decision."""
    p = np.asarray(probabilities, dtype=float)
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('Probabilidad no valida.')
    future = economics.get('avoidable_future_cost_usd', {}).get(stage)
    false_loss = economics.get('false_reject_loss_usd')
    if not economics.get('confirmed_by_business') or future is None or false_loss is None:
        return np.repeat('REVISAR_COSTES_Y_CALIDAD', len(p)), None, None
    if not model_qualified:
        return np.repeat('REVISAR_MODELO_NO_SUPERA_REFERENCIA', len(p)), None, None
    disposal = economics.get('extra_disposal_cost_per_piece_usd', 0)
    min_gain = economics.get('minimum_expected_gain_per_piece_usd', 0)
    cut = threshold(future, false_loss, disposal, min_gain)
    gains = expected_gain(p, future, false_loss, disposal)
    actions = np.where(gains > min_gain, 'DESCARTAR_COHORTE_SUGERIDO', 'CONTINUAR')
    return actions, gains, cut


def evaluate_policy(frame, probabilities, future_cost, false_loss, disposal=0.0):
    """Retrospective COUNTERFACTUAL proxy among final-laser survivors only."""
    p = np.asarray(probabilities)
    cut = threshold(future_cost, false_loss, disposal)
    chosen = p > cut
    n = frame.pieces.to_numpy(float)
    k = frame.rejects.to_numpy(float)
    good_lost = float((n[chosen] - k[chosen]).sum())
    bad_removed = float(k[chosen].sum())
    gain = k * future_cost - (n - k) * false_loss - n * disposal
    return {
        'threshold': cut, 'cohorts_discarded': int(chosen.sum()),
        'bad_pieces_removed': bad_removed, 'good_pieces_discarded': good_lost,
        'discard_purity': bad_removed / max(1, bad_removed + good_lost),
        'reject_recall': bad_removed / max(1, float(k.sum())),
        'avoidable_future_cost_on_bad_usd': bad_removed * future_cost,
        'false_reject_loss_usd': good_lost * false_loss,
        'extra_disposal_cost_usd': float(n[chosen].sum() * disposal),
        'net_scenario_usd': float(gain[chosen].sum()),
        'oracle_cohort_net_usd': float(np.maximum(gain, 0).sum()),
        'reference_continue_all_net_usd': 0.0,
        'not_realized_savings': True,
        'conditional_on_survival_to_final_laser': True
    }
