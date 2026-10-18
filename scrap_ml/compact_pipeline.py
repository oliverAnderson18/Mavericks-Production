r"""Single entry point for training, demo export and compact evaluation output.

Usage:
    python -m scrap_ml.compact_pipeline run --data-dir .\data

Final generated files:
    scrap_ml/artifacts_improved/model.joblib
    scrap_ml/artifacts_machine_system/diagnosis_model.joblib
    scrap_ml/artifacts_enrober/model.joblib
    output/prevencion.csv
    output/riesgo.csv
    output/diagnostico.csv
    output/evaluation.json
"""
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from . import improved, machine_system, enrober
from .decisions import CONFIG, OUT as DECISION_OUT, prepare


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
OUTPUT = PROJECT / "output"
QUALITY_OUT = ROOT / "artifacts_improved"
MACHINE_OUT = ROOT / "artifacts_machine_system"
CORRECTION_OUT = ROOT / "artifacts_correction_study"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def compact_evaluation(data_dir):
    quality = read_json(QUALITY_OUT / "metrics.json")
    diagnosis = read_json(MACHINE_OUT / "metrics.json")
    decisions = read_json(DECISION_OUT / "metrics.json")
    correction = read_json(CORRECTION_OUT / "metrics.json")
    quality_bundle = joblib.load(QUALITY_OUT / "model.joblib")
    diagnosis_bundle = joblib.load(MACHINE_OUT / "diagnosis_model.joblib")

    quality_policy = quality_bundle["policies"]["materiales"]
    quality_test = quality["scenarios"]["materiales"]["test"]
    diagnosis_summary = diagnosis["diagnosis"]
    diagnosis_by_machine = {}
    for machine, item in diagnosis_summary["per_machine"].items():
        diagnosis_by_machine[machine] = {
            "threshold": diagnosis_bundle["machines"][machine]["threshold"],
            "calibrated": item["calibrated"],
            "training_positive_minutes": item["training_positive_minutes"],
            "test": item["test"],
        }

    report = {
        "schema_version": "compact_evaluation_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_directory": str(Path(data_dir).resolve()),
        "models": {
            "quality": "scrap_ml/artifacts_improved/model.joblib",
            "diagnosis": "scrap_ml/artifacts_machine_system/diagnosis_model.joblib",
            "enrober": "scrap_ml/artifacts_enrober/model.joblib",
        },
        "quality_model": {
            "version": quality["version"],
            "features": quality["features"],
            "selected_policy": quality_policy,
            "coverage": quality["coverage"],
            "test": {
                "cohorts": quality_test["cohorts"],
                "pieces": quality_test["pieces_in_evaluation"],
                "historical_rejects": quality_test["historical_final_rejects"],
                "reject_rate": quality_test["historical_final_rejects"] / quality_test["pieces_in_evaluation"],
            },
        },
        "machine_diagnosis": {
            "version": diagnosis["version"],
            "evaluated_minutes": diagnosis_summary["evaluated_minutes"],
            "registered_fault_minutes": diagnosis_summary["fault_minutes"],
            "thresholded": diagnosis_summary["thresholded"],
            "macro_f1_on_machines_with_test_events": diagnosis_summary["macro_f1_on_machines_with_test_events"],
            "per_machine": diagnosis_by_machine,
            "event_level": diagnosis_summary.get("event_level"),
            "interpretation": "Detects similarity to registered machine incidents; it is not proof of scrap causality.",
        },
        "decision_and_business_test": decisions["splits"]["test"],
        "correction_test": correction["splits"]["test"],
        "accounting": decisions.get("accounting"),
        "claims": {
            "discard_value_is_historical_replay": True,
            "correction_is_value_at_risk_not_realized_saving": True,
            "physical_actuation": False,
            "test_previously_explored": True,
        },
    }
    report["enrober_prevention"] = read_json(enrober.OUT / "metrics.json")
    economy = report['enrober_prevention']['economics']
    a = report['decision_and_business_test']
    net = a['net_scenario_usd']
    report['comparable_business'] = {
        'days': economy['test_days'], 'test_FIN_BAR_all_minutes_usd': economy['test_FIN_BAR_usd'],
        'test_total_scrap_all_minutes_usd': economy['test_total_scrap_usd'],
        'A_net_simulated_usd': net,
        'A_rate_on_A_covered_FIN_BAR': net / a['historical_final_reject_cost_usd'],
        'A_rate_on_all_test_FIN_BAR': net / economy['test_FIN_BAR_usd'],
        'A_rate_on_all_test_scrap': net / economy['test_total_scrap_usd'],
        'A_annual_260day_daily_run_rate_usd': net / economy['test_days'] * 260,
        'A_historical_65day_daily_run_rate_usd': net / economy['test_days'] * 65,
        'A_legacy_65day_same_risk_mix_extrapolation_usd': decisions['accounting']['extrapolation_full_period_usd'],
        'legacy_extrapolation_is_cost_mix_scaling_not_multiplication_by_days': True,
        'C_additional_saving_credited_without_causal_evidence_usd': 0.,
        'joint_policy_savings_not_identified': True,
    }
    write_json(OUTPUT / "evaluation.json", report)
    return report


def compact_demo():
    config = read_json(ROOT / "config_full_line.json")
    journey = read_json(DECISION_OUT / "example_decisions_15.json")
    example = read_json(DECISION_OUT / "example_decision.json")
    demo = {
        "schema_version": "compact_demo_v1",
        "route": list(config["stages"]),
        "diagnosed_machines": machine_system.MACHINES,
        "color_rule": {
            "red": "event_flag=true: machine incident detected",
            "yellow": "high quality risk without reliable machine diagnosis",
            "blue": "correction ticket already active",
            "grey": "no incident signal or observation-only station",
        },
        "example_decision": example,
        "journey": journey,
        "enrober_prevention": read_json(enrober.OUT / "demo.json"),
        "workflow": {
            "execution": "A, B and C run in parallel on available measurements; combine before recommending",
            "C": "Specific coating forecast for the next stream; request ENR inspection",
            "A": "Estimate aggregate reject fraction at each predictive station",
            "B": "Independently estimate active registered machine incidents",
            "priority": ["abstain for invalid inputs", "review/hold high-risk production using A",
                         "review ENR on C alert, or machines flagged by B", "continue with monitoring"],
            "action_authority": "operator; no automatic adjustment or live discard",
            "accounting": "A replay and C material at risk are separate; joint sensitivity removes overlap",
        },
    }
    write_json(OUTPUT / "demo.json", demo)
    return demo


def remove_directory(path):
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)


def remove_files_except(folder, names):
    folder = Path(folder)
    if not folder.exists():
        return
    allowed = set(names)
    for child in folder.iterdir():
        if child.name in allowed:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def clean_generated_outputs():
    """Keep three runtime models and compact JSON; preserve user PDFs/documents."""
    remove_files_except(QUALITY_OUT, {"model.joblib"})
    remove_files_except(MACHINE_OUT, {"diagnosis_model.joblib"})
    remove_files_except(enrober.OUT, {"model.joblib"})
    for folder in [
        ROOT / "artifacts",
        ROOT / "artifacts_full_line",
        ROOT / "artifacts_decisions",
        ROOT / "artifacts_correction_study",
    ]:
        remove_directory(folder)
    for cache in ROOT.rglob("__pycache__"):
        remove_directory(cache)


def package_existing(data_dir, keep_intermediates=False):
    compact_evaluation(data_dir)
    from .minute_exports import export
    export(data_dir)
    if not keep_intermediates:
        clean_generated_outputs()
    for name in ['prevencion.csv', 'riesgo.csv', 'diagnostico.csv']:
        print(str(OUTPUT / name))
    print(str(OUTPUT / "evaluation.json"))


def run(data_dir, keep_intermediates=False):
    improved.train(data_dir)
    machine_system.train(data_dir)
    machine_system.finalize(data_dir)
    config = read_json(CONFIG)
    prepare(data_dir, config)
    records = pd.read_csv(DECISION_OUT / "cohorts_test.csv")
    enrober.train(data_dir, legacy_records=records)
    package_existing(data_dir, keep_intermediates=keep_intermediates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "package", "predict", "export"])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--keep-intermediates", action="store_true")
    parser.add_argument("--observation-minute")
    parser.add_argument("--stage", default="CAM_POST")
    args = parser.parse_args()
    if args.command == "run":
        run(args.data_dir, keep_intermediates=args.keep_intermediates)
    elif args.command == 'package':
        package_existing(args.data_dir, keep_intermediates=args.keep_intermediates)
    elif args.command == 'export':
        from .minute_exports import export
        export(args.data_dir)
    else:
        if not args.observation_minute:
            parser.error('--observation-minute required')
        from .data import load_sources
        from .decisions import decide
        q = joblib.load(QUALITY_OUT / 'model.joblib')
        b = joblib.load(MACHINE_OUT / 'diagnosis_model.joblib')
        sources = load_sources(args.data_dir, cutoff=args.observation_minute)
        snapshot = machine_system.assess(sources, b, q, args.stage, args.observation_minute, with_sensitivity=False)
        decision = decide(snapshot, read_json(CONFIG), q['config'], 1920)
        coating = enrober.assess(args.data_dir, args.observation_minute)
        print(json.dumps(enrober.combine_decision(decision, coating), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
