"""Estado persistente y validaciones de ejecucion de la demostracion SMED."""
from __future__ import annotations

import copy
import csv
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .planner import FIXTURES, ValidationError, clock, economic_impact, load_catalog, make_plan, minute, number


class ConflictError(ValidationError):
    pass


def historical_baseline(data_dir):
    path = Path(data_dir) / "fct_oee_minute.csv"
    if not path.is_file():
        return {"available": False, "source": str(path), "note": "Historico no encontrado; los 30 min del escenario son una referencia declarada."}
    runs, current = [], []
    previous = None
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("machine_code") != "LINE":
                continue
            stamp = datetime.fromisoformat(row["minute_ts"])
            is_clean = row["state_code"] == "PLANNED_CLEAN"
            if current and (not is_clean or previous is None or (stamp - previous).total_seconds() != 60):
                runs.append(current)
                current = []
            if is_clean:
                current.append(stamp)
            previous = stamp
    if current:
        runs.append(current)
    return {"available": True, "source": "fct_oee_minute.csv / LINE / PLANNED_CLEAN",
            "provenance": "ORIGINAL_CASE_DATA", "sessions": len(runs), "total_minutes": sum(map(len, runs)),
            "durations_minutes": sorted({len(r) for r in runs}),
            "first_date": str(runs[0][0].date()) if runs else None,
            "last_date": str(runs[-1][0].date()) if runs else None,
            "note": "Solo paro global: no contiene tareas, recursos ni eficacia de un cambio SMED."}


class SmedService:
    def __init__(self, db_path, data_dir, fixtures=FIXTURES):
        self.tasks, self.resources, self.scenario = load_catalog(fixtures)
        self.by_id = {t["task_id"]: t for t in self.tasks}
        self.resource_by_id = {r["resource_id"]: r for r in self.resources}
        self.procedure = (Path(fixtures) / "procedure.md").read_text(encoding="utf-8")
        self.baseline = historical_baseline(data_dir)
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, session_id=None):
        if session_id is not None and (not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_id)):
            raise ValidationError("Sesion no valida.")
        session_id = session_id or uuid.uuid4().hex
        with self.lock, self.connect() as db:
            row = db.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row:
                return json.loads(row["body"])
            body = {"id": session_id, "date": self.scenario["date"], "clock": self.scenario["initial_clock"],
                    "revision": 0, "stopped_at": None, "restarted_at": None, "quality_released": False,
                    "staff_ready": False, "material_ready": False,
                    "production_ready_at": self.scenario["production_ready_at"], "unavailable_resources": [],
                    "provenance": "SIMULATED_OPERATOR_SESSION", "tasks": {}}
            db.execute("INSERT INTO sessions VALUES (?,?)", (session_id, json.dumps(body)))
            return body

    def snapshot(self, session_id):
        state = self.session(session_id)
        with self.connect() as db:
            events = [json.loads(r[0]) for r in db.execute("SELECT body FROM events WHERE session_id=? ORDER BY id DESC LIMIT 60", (session_id,))]
        return {"session": state, "tasks": self.tasks, "resources": self.resources, "scenario": self.scenario,
                "historical_baseline": self.baseline, "readiness": self.readiness(state), "events": events}

    def readiness(self, state):
        missing = [t["task_id"] for t in self.tasks if t["mandatory"] and state["tasks"].get(t["task_id"], {}).get("status") != "completed"]
        reasons = []
        if not state["stopped_at"]:
            reasons.append("No se ha registrado el paro.")
        if missing:
            reasons.append("Tareas obligatorias pendientes: " + ", ".join(missing))
        if not state["quality_released"]:
            reasons.append("Falta confirmacion humana de liberacion de calidad.")
        if not state["staff_ready"] or not state["material_ready"]:
            reasons.append("Falta disponibilidad confirmada de personal o material.")
        if minute(state["clock"]) < minute(state["production_ready_at"]):
            reasons.append("El horario productivo indicado comienza a las " + state["production_ready_at"])
        return {"recorded_conditions_met": not reasons, "reasons": reasons, "physical_actuation": False,
                "note": "Comprobacion de registros del escenario; no orden de arranque ni certificado de higiene."}

    def _apply(self, state, action, task_id=None, actor="Operario demo", value=None, note=""):
        actor = actor.strip() if isinstance(actor, str) else ""
        if not actor or len(actor) > 100 or not isinstance(note, str) or len(note) > 600:
            raise ValidationError("Responsable o nota no validos.")
        now = minute(state["clock"])
        if state["restarted_at"] and action != "set_clock":
            raise ValidationError("La sesion ya registra un arranque. Crea otra sesion para repetir la demo.")
        if action == "set_clock":
            if minute(value) < now:
                raise ValidationError("El reloj no puede retroceder. Crea una sesion nueva para repetir.")
            state["clock"] = value
        elif action == "settings":
            if not isinstance(value, dict) or set(value) - {"production_ready_at", "unavailable_resources"}:
                raise ValidationError("Configuracion no valida.")
            if "production_ready_at" in value:
                minute(value["production_ready_at"])
                state["production_ready_at"] = value["production_ready_at"]
            if "unavailable_resources" in value:
                ids = value["unavailable_resources"]
                if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or set(ids) - self.resource_by_id.keys():
                    raise ValidationError("Recurso desconocido.")
                active = {r for t in state["tasks"].values() if t["status"] == "in_progress" for r in t["resources"]}
                if active & set(ids):
                    raise ValidationError("Un recurso ejecuta una tarea; registra su finalizacion antes de retirarlo.")
                state["unavailable_resources"] = sorted(set(ids))
        elif action == "record_stop":
            if state["stopped_at"]:
                raise ValidationError("El paro ya esta registrado.")
            if not minute(self.scenario["earliest_stop"]) <= now <= minute(self.scenario["latest_stop"]):
                raise ValidationError("El escenario solo permite registrar el paro entre 14:00 y 14:10.")
            state["stopped_at"] = state["clock"]
        elif action in {"start_task", "complete_task"}:
            if task_id not in self.by_id:
                raise ValidationError("Tarea desconocida.")
            task = self.by_id[task_id]
            actual = state["tasks"].get(task_id, {})
            if action == "start_task":
                if actual.get("status") in {"in_progress", "completed"}:
                    raise ValidationError("La tarea ya se inicio.")
                if any(state["tasks"].get(p, {}).get("status") != "completed" for p in task["predecessors"]):
                    raise ValidationError("Faltan tareas predecesoras por completar.")
                if task["phase"] == "internal" and not state["stopped_at"]:
                    raise ValidationError("Esta tarea requiere el paro registrado.")
                if now < minute(self.scenario["preparation_start"]):
                    raise ValidationError("Todavia no empieza la preparacion del escenario.")
                busy = {r for t in state["tasks"].values() if t["status"] == "in_progress" for r in t["resources"]}
                choices = [r["resource_id"] for r in self.resources if task["skill"] in r["skills"]
                           and r["resource_id"] not in busy and r["resource_id"] not in state["unavailable_resources"]
                           and minute(r["available_from"]) <= now and now + task["minimum_min"] <= minute(r["available_until"])]
                if len(choices) < task["workers"]:
                    raise ValidationError("No hay suficientes recursos compatibles y libres.")
                state["tasks"][task_id] = {"status": "in_progress", "started_at": state["clock"],
                    "completed_at": None, "resources": choices[:task["workers"]], "actor": actor}
            else:
                if actual.get("status") != "in_progress":
                    raise ValidationError("La tarea debe estar en curso.")
                if now - minute(actual["started_at"]) < task["minimum_min"]:
                    raise ValidationError("No ha transcurrido la duracion minima del escenario.")
                actual.update(status="completed", completed_at=state["clock"], completed_by=actor)
        elif action == "confirm_quality":
            if any(state["tasks"].get(t["task_id"], {}).get("status") != "completed" for t in self.tasks if t["mandatory"]):
                raise ValidationError("No se libera calidad con tareas obligatorias pendientes.")
            if state["quality_released"]:
                raise ValidationError("La liberacion ya esta registrada.")
            state["quality_released"] = True
        elif action == "confirm_readiness":
            if not isinstance(value, dict) or set(value) != {"staff_ready", "material_ready"} or any(type(v) is not bool for v in value.values()):
                raise ValidationError("Confirma personal y material con valores booleanos.")
            state.update(value)
        elif action == "record_restart":
            check = self.readiness(state)
            if not check["recorded_conditions_met"]:
                raise ValidationError("; ".join(check["reasons"]))
            state["restarted_at"] = state["clock"]
        else:
            raise ValidationError("Accion desconocida.")
        return {"action": action, "task_id": task_id, "actor": actor, "clock": state["clock"], "note": note,
                "value": value, "provenance": "SIMULATED_OPERATOR_RECORD", "physical_actuation": False}

    def mutate(self, session_id, expected_revision, action, task_id=None, actor="Operario demo", value=None, note="", proposal_id=None):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ValidationError("Sesion desconocida.")
            state = json.loads(row[0])
            if type(expected_revision) is not int or state["revision"] != expected_revision:
                raise ConflictError("El estado ha cambiado; actualiza antes de confirmar.")
            if proposal_id:
                proposal = db.execute("SELECT * FROM proposals WHERE id=? AND session_id=?", (proposal_id, session_id)).fetchone()
                if not proposal or proposal["status"] != "pending" or proposal["revision"] != state["revision"]:
                    raise ConflictError("Propuesta inexistente, ya aplicada o desactualizada.")
                supplied = json.loads(proposal["body"])
                action, task_id, note = supplied["action"], supplied.get("task_id"), supplied.get("note", "")
            event = self._apply(state, action, task_id, actor, value, note)
            state["revision"] += 1
            event["revision"] = state["revision"]
            db.execute("UPDATE sessions SET body=? WHERE id=?", (json.dumps(state), session_id))
            db.execute("INSERT INTO events(session_id,body) VALUES (?,?)", (session_id, json.dumps(event)))
            if proposal_id:
                db.execute("UPDATE proposals SET status='applied' WHERE id=?", (proposal_id,))
        return self.snapshot(session_id)

    def propose(self, session_id, action, task_id=None, note=""):
        if action not in {"start_task", "complete_task", "record_stop", "confirm_quality", "record_restart"}:
            raise ValidationError("El agente no puede proponer esa accion.")
        state = self.session(session_id)
        self._apply(copy.deepcopy(state), action, task_id, "Propuesta del agente", note=note)
        pid = uuid.uuid4().hex
        proposal = {"id": pid, "action": action, "task_id": task_id, "note": note,
                    "revision": state["revision"], "requires_human_confirmation": True}
        with self.connect() as db:
            db.execute("INSERT INTO proposals(id,session_id,revision,body) VALUES (?,?,?,?)",
                       (pid, session_id, state["revision"], json.dumps(proposal)))
        return proposal

    def plan(self, session_id, options=None):
        state = self.session(session_id)
        options = dict(options or {})
        if options.get("unavailable_resources") is None:
            options["unavailable_resources"] = state["unavailable_resources"]
        result = make_plan(self.tasks, self.resources, self.scenario, state, options)
        result.update(id=uuid.uuid4().hex, session_id=session_id, session_revision=state["revision"])
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?,?,?)", (result["id"], session_id, json.dumps(result)))
        result["impact"] = economic_impact(result, self.scenario) if result["feasible"] else None
        return result

    def impact(self, session_id, plan_id, days=65, utilization=1, additional_cost_usd=0):
        with self.connect() as db:
            row = db.execute("SELECT body FROM plans WHERE id=? AND session_id=?", (plan_id, session_id)).fetchone()
        if not row:
            raise ValidationError("Plan desconocido para esta sesion.")
        plan = json.loads(row[0])
        result = economic_impact(plan, self.scenario, days, utilization, additional_cost_usd)
        result.update(plan_id=plan_id, session_revision=plan["session_revision"],
                      stale=plan["session_revision"] != self.session(session_id)["revision"])
        return result

    def tool(self, session_id, name, args):
        if name == "consultar_estado":
            return self.snapshot(session_id)
        if name == "consultar_procedimiento":
            tid = args.get("task_id")
            if tid is not None and tid not in self.by_id:
                raise ValidationError("Tarea desconocida.")
            return {"version": self.scenario["procedure_version"], "provenance": "SIMULATED_PROCEDURE",
                    "text": self.procedure, "task": self.by_id.get(tid)}
        if name == "simular_plan":
            return self.plan(session_id, args)
        if name == "calcular_impacto":
            return self.impact(session_id, **args)
        if name == "proponer_avance":
            return self.propose(session_id, **args)
        raise ValidationError("Herramienta no permitida.")
