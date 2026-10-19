"""Planificador determinista de listas; ninguna duracion la decide el LLM."""
from __future__ import annotations

import csv
import itertools
import json
import math
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ValidationError(ValueError):
    pass


def minute(value: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValidationError("La hora debe tener formato HH:MM.")
    try:
        h, m = map(int, value.split(":"))
    except ValueError as exc:
        raise ValidationError("Hora no valida.") from exc
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValidationError("Hora fuera de rango.")
    return h * 60 + m


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{name} debe ser un numero finito.")
    if not low <= value <= high:
        raise ValidationError(f"{name} debe estar entre {low} y {high}.")
    return value


def load_catalog(root=FIXTURES):
    root = Path(root)
    with (root / "tasks.csv").open(encoding="utf-8", newline="") as stream:
        tasks = list(csv.DictReader(stream))
    with (root / "resources.csv").open(encoding="utf-8", newline="") as stream:
        resources = list(csv.DictReader(stream))
    for t in tasks:
        t["duration_min"] = int(t["duration_min"])
        t["minimum_min"] = int(t["minimum_min"])
        t["workers"] = int(t["workers"])
        t["mandatory"] = t["mandatory"] == "true"
        t["predecessors"] = [v for v in t["predecessors"].split(";") if v]
    for r in resources:
        r["skills"] = r["skills"].split(";")
    scenario = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    ids = {t["task_id"] for t in tasks}
    if len(ids) != len(tasks) or len({r["resource_id"] for r in resources}) != len(resources):
        raise ValidationError("Identificadores duplicados en el catalogo.")
    for t in tasks:
        if not set(t["predecessors"]) <= ids or t["duration_min"] < t["minimum_min"] or t["minimum_min"] <= 0:
            raise ValidationError("Dependencia o duracion no valida en " + t["task_id"])
    return tasks, resources, scenario


def make_plan(tasks, resources, scenario, session, options=None):
    options = options or {}
    allowed = {"prepare_before_stop", "stop_at", "production_ready_at", "target_stop_minutes", "unavailable_resources", "delays"}
    if set(options) - allowed:
        raise ValidationError("Opciones de plan desconocidas.")
    prepare = options.get("prepare_before_stop", True)
    if not isinstance(prepare, bool):
        raise ValidationError("prepare_before_stop debe ser booleano.")
    now = minute(session["clock"])
    stop = minute(options.get("stop_at") or session.get("stopped_at") or scenario["default_stop"])
    if session.get("stopped_at") and stop != minute(session["stopped_at"]):
        raise ValidationError("No se puede cambiar la hora de un paro ya registrado.")
    if not minute(scenario["earliest_stop"]) <= stop <= minute(scenario["latest_stop"]):
        raise ValidationError("El inicio debe estar dentro de la ventana del escenario 14:00-14:10.")
    if not session.get("stopped_at"):
        stop = max(stop, now)
    ready = minute(options.get("production_ready_at") or session.get("production_ready_at") or scenario["production_ready_at"])
    target = options.get("target_stop_minutes")
    target = scenario["target_minutes"] if target is None else number(target, "Objetivo", 1, 180)
    unavailable = options.get("unavailable_resources", [])
    if not isinstance(unavailable, list) or any(not isinstance(x, str) for x in unavailable):
        raise ValidationError("unavailable_resources debe ser una lista de identificadores.")
    known = {r["resource_id"] for r in resources}
    if set(unavailable) - known:
        raise ValidationError("Recurso desconocido.")
    delays = options.get("delays", [])
    if not isinstance(delays, list):
        raise ValidationError("delays debe ser una lista.")
    duration_extra = {}
    by_id = {t["task_id"]: t for t in tasks}
    for delay in delays:
        if not isinstance(delay, dict) or set(delay) != {"task_id", "minutes"} or delay["task_id"] not in by_id:
            raise ValidationError("Retraso no valido.")
        duration_extra[delay["task_id"]] = int(number(delay["minutes"], "Retraso", 0, 120))
    available = [r for r in resources if r["resource_id"] not in unavailable]
    calendar = {r["resource_id"]: [] for r in resources}
    finished = {}
    rows = []
    state = session.get("tasks", {})
    reasons = []

    def downstream(tid, seen=None):
        seen = set() if seen is None else seen
        if tid in seen:
            raise ValidationError("El catalogo contiene un ciclo.")
        tails = [downstream(t["task_id"], seen | {tid}) for t in tasks if tid in t["predecessors"]]
        return by_id[tid]["duration_min"] + max(tails, default=0)

    priorities = {tid: downstream(tid) for tid in by_id}
    for task in tasks:
        tid = task["task_id"]
        actual = state.get(tid, {})
        if actual.get("status") not in {"completed", "in_progress"}:
            continue
        start = minute(actual["started_at"])
        end = minute(actual["completed_at"]) if actual.get("completed_at") else max(now + 1, start + task["duration_min"] + duration_extra.get(tid, 0))
        assigned = actual["resources"]
        for rid in assigned:
            calendar[rid].append((start, end))
        finished[tid] = end
        rows.append({**task, "start": clock(start), "end": clock(end), "resources": assigned, "status": actual["status"], "basis": "operator_record"})
    pending = set(by_id) - set(finished)
    while pending:
        candidates = []
        for tid in pending:
            t = by_id[tid]
            if not set(t["predecessors"]) <= finished.keys():
                continue
            release = minute(scenario["preparation_start"]) if t["phase"] == "external_before" and prepare else stop
            start_min = max(now, release, max((finished[p] for p in t["predecessors"]), default=0))
            duration = t["duration_min"] + duration_extra.get(tid, 0)
            qualified = [r for r in available if t["skill"] in r["skills"]]
            slots = []
            for group in itertools.combinations(qualified, t["workers"]):
                start = max(start_min, *(minute(r["available_from"]) for r in group))
                limit = min(minute(r["available_until"]) for r in group)
                while start + duration <= limit:
                    overlaps = [end for r in group for begin, end in calendar[r["resource_id"]] if start < end and start + duration > begin]
                    if not overlaps:
                        slots.append((start, tuple(r["resource_id"] for r in group)))
                        break
                    start = max(overlaps)
            if slots:
                start, group = min(slots)
                candidates.append((start, -priorities[tid], tid, duration, group))
        if not candidates:
            reasons.append("No hay recursos/horarios compatibles para: " + ", ".join(sorted(pending)))
            break
        start, _, tid, duration, group = min(candidates)
        end = start + duration
        finished[tid] = end
        for rid in group:
            calendar[rid].append((start, end))
        rows.append({**by_id[tid], "start": clock(start), "end": clock(end), "resources": list(group), "status": "planned", "basis": "simulated_duration"})
        pending.remove(tid)
    if stop > minute(scenario["latest_stop"]):
        reasons.append("La hora actual ya supera la ventana de paro; hace falta revisar el escenario.")
    feasible = not reasons
    completion = max((finished[t["task_id"]] for t in tasks if t["mandatory"] and t["task_id"] in finished), default=stop)
    restart = minute(session["restarted_at"]) if session.get("restarted_at") else max(completion, ready, now)
    baseline_restart = minute(scenario["default_stop"]) + scenario["historical_baseline_minutes"]
    recovered = max(0, baseline_restart - restart) if feasible else 0
    blocker = max(rows, key=lambda row: minute(row["end"]))["task_id"] if rows else None
    assumptions = ["Duraciones, tareas y recursos SIMULADOS; no son tiempos medidos de la planta."]
    if ready != minute(scenario["production_ready_at"]):
        assumptions.append("Disponibilidad productiva modificada respecto al horario original 14:30; requiere organizacion adicional.")
    if now > minute(scenario["preparation_start"]):
        assumptions.append("Solo se planifica trabajo pendiente desde el reloj actual; no se inventa preparacion pasada.")
    return {
        "feasible": feasible, "reasons": reasons, "rows": sorted(rows, key=lambda row: (row["start"], row["task_id"])),
        "stop_at": clock(stop), "cleaning_finish": clock(completion) if feasible else None,
        "production_ready_at": clock(ready), "earliest_restart": clock(restart) if feasible else None,
        "cleaning_minutes": max(0, completion - stop) if feasible else None,
        "target_minutes": target, "target_met": feasible and completion - stop <= target,
        "recovered_production_minutes": recovered, "last_task": blocker,
        "method": "resource_constrained_list_heuristic_not_global_optimum",
        "assumptions": assumptions, "provenance": "SIMULATED_OPERATIONAL_SCENARIO",
        "release_is_not_granted_by_plan": True, "options": options,
    }


def economic_impact(plan, scenario, days=65, utilization=1.0, additional_cost_usd=0.0):
    days = number(days, "Dias", 1, 366)
    utilization = number(utilization, "Aprovechamiento", 0, 1)
    costs = number(additional_cost_usd, "Coste adicional total del periodo", 0, 100000000)
    if not plan["feasible"]:
        raise ValidationError("No se atribuye valor a un plan inviable.")
    minutes = plan["recovered_production_minutes"]
    gross = minutes / 60 * scenario["hourly_margin_usd_assumption"] * days * utilization
    return {"recovered_minutes_per_day": minutes, "days": days, "capacity_utilization_assumption": utilization,
            "hourly_margin_usd_assumption": scenario["hourly_margin_usd_assumption"],
            "gross_potential_usd": round(gross, 2), "additional_cost_usd_for_period": costs,
            "net_conditional_potential_usd": round(gross - costs, 2), "realized_savings_usd": None,
            "provenance": "CONDITIONAL_SCENARIO", "note": "Sujeto a demanda, produccion buena, disponibilidad y costes. No sumar automaticamente al ahorro de scrap."}
