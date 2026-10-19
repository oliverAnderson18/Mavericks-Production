import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from melt_alert.agent import CleaningAgent, OllamaClient, AgentUnavailable
from melt_alert.planner import ValidationError, economic_impact, make_plan, minute
from melt_alert.server import make_handler
from melt_alert.service import ConflictError, SmedService


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.service = SmedService(self.db, Path(self.temp.name) / "absent_data")
        self.sid = self.service.session()["id"]

    def change(self, action, task_id=None, value=None):
        revision = self.service.session(self.sid)["revision"]
        return self.service.mutate(self.sid, revision, action, task_id=task_id, value=value, actor="Test operator")

    def test_plan_obeys_precedence_and_exclusive_resources(self):
        p = self.service.plan(self.sid)
        self.assertTrue(p["feasible"])
        rows = {r["task_id"]: r for r in p["rows"]}
        self.assertEqual(set(rows), set(self.service.by_id))
        for t in p["rows"]:
            self.assertGreaterEqual(minute(t["end"]) - minute(t["start"]), t["minimum_min"])
            for predecessor in t["predecessors"]:
                self.assertLessEqual(minute(rows[predecessor]["end"]), minute(t["start"]))
            if t["phase"] == "internal":
                self.assertGreaterEqual(minute(t["start"]), minute(p["stop_at"]))
        for resource in self.service.resources:
            intervals = sorted((minute(r["start"]), minute(r["end"])) for r in p["rows"] if resource["resource_id"] in r["resources"])
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])))

    def test_original_shift_prevents_fake_capacity_saving(self):
        p = self.service.plan(self.sid)
        self.assertLess(p["cleaning_minutes"], 30)
        self.assertEqual(p["earliest_restart"], "14:30")
        self.assertEqual(p["impact"]["net_conditional_potential_usd"], 0)
        self.assertIsNone(p["impact"]["realized_savings_usd"])

    def test_early_staffing_is_an_explicit_economic_assumption(self):
        p = self.service.plan(self.sid, {"production_ready_at": "14:15"})
        self.assertGreater(p["recovered_production_minutes"], 0)
        self.assertTrue(any("availability changed" in s for s in p["assumptions"]))
        impact = self.service.impact(self.sid, p["id"], days=10, utilization=.5, additional_cost_usd=100)
        expected = p["recovered_production_minutes"] / 60 * 25000 * 10 * .5 - 100
        self.assertAlmostEqual(impact["net_conditional_potential_usd"], round(expected, 2))

    def test_smed_preparation_shortens_internal_work_in_example(self):
        before = self.service.plan(self.sid)
        during = self.service.plan(self.sid, {"prepare_before_stop": False})
        self.assertLess(before["cleaning_minutes"], during["cleaning_minutes"])

    def test_comparison_is_calculated_even_if_llm_is_offline(self):
        client = SimpleNamespace(chat=lambda **kwargs: self.fail("Known comparison should use the planner directly"))
        result = CleaningAgent(self.service, client=client).ask(self.sid, "compare", "Compare preparing kits before vs during the stop.")
        self.assertEqual(result["tool_calls"][0]["name"], "comparar_planes")
        self.assertIn("14:16", result["text"])
        self.assertIn("14:22", result["text"])
        self.assertIn("6 minutes earlier", result["text"])
        self.assertIn("Neither option recovers production time", result["text"])
        self.assertNotIn("after 14:30", result["text"])
        self.assertEqual(self.service.session(self.sid)["revision"], 0)

    def test_comparison_respects_work_already_started(self):
        self.change("start_task", "KIT_ENR")
        result = CleaningAgent(self.service).ask(self.sid, "recorded", "Compare external preparation before vs during the stop.")
        comparison = self.service.tool(self.sid, "comparar_planes", {})
        saved = comparison["cleaning_minutes_saved"]
        self.assertIn(f"{saved} minutes earlier", result["text"])
        self.assertEqual(self.service.session(self.sid)["tasks"]["KIT_ENR"]["status"], "in_progress")

    def test_value_comparison_uses_planner_minutes_as_fractional_hours(self):
        self.change("start_task", "KIT_ENR")
        client = SimpleNamespace(chat=lambda **kwargs: self.fail("Economic comparison must not ask the LLM to calculate"))
        reply = CleaningAgent(self.service, client=client).ask(
            self.sid, "priced",
            "Compare preparation before vs during the stop if production is available at 14:15. "
            "Calculate savings for 65 days at 70% utilization and list required actions.")
        self.assertEqual([call["name"] for call in reply["tool_calls"]],
                         ["comparar_planes", "calcular_impacto", "calcular_impacto"])
        self.assertIn("14:16", reply["text"])
        self.assertIn("14:18", reply["text"])
        self.assertIn("4,083.33 USD/day", reply["text"])
        self.assertIn("3,500.00 USD/day", reply["text"])
        self.assertIn("265,416.67 USD over 65 days", reply["text"])
        self.assertIn("227,500.00 USD over 65 days", reply["text"])
        self.assertIn("37,916.67 USD over 65 days", reply["text"])
        self.assertIn("÷ 60", reply["text"])
        self.assertNotIn("24,500", reply["text"])
        self.assertEqual(reply["provider"], "Planner")

    def test_value_comparison_without_earlier_availability_has_zero_recovery(self):
        client = SimpleNamespace(chat=lambda **kwargs: self.fail("Known calculation should be deterministic"))
        reply = CleaningAgent(self.service, client=client).ask(
            self.sid, "zero", "Compare preparation before vs during the stop and show savings.")
        self.assertIn("0.00 USD/day", reply["text"])
        self.assertIn("0.00 USD over 65 days", reply["text"])
        self.assertIn("both restart at 14:30", reply["text"])

    def test_single_plan_value_uses_days_utilization_and_period_cost(self):
        client = SimpleNamespace(chat=lambda **kwargs: self.fail("Value must come from the calculator"))
        reply = CleaningAgent(self.service, client=client).ask(
            self.sid, "one",
            "How much money can cleaning preparation before the stop save if production is available at 14:15 "
            "for 65 days at 70% utilization with additional cost of $100?")
        self.assertEqual([call["name"] for call in reply["tool_calls"]],
                         ["simular_plan", "calcular_impacto"])
        self.assertIn("4,083.33 USD/day", reply["text"])
        self.assertIn("265,316.67 USD over 65 days", reply["text"])
        self.assertIn("100.00 USD additional costs for the period", reply["text"])
        self.assertIn("not realized savings", reply["text"])

    def test_llm_cannot_supply_money_without_a_verified_plan(self):
        client = SimpleNamespace(chat=lambda **kwargs: {"message": {
            "role": "assistant", "content": "The saving is $24,500 per day."}, "done": True})
        reply = CleaningAgent(self.service, client=client).ask(
            self.sid, "unsupported",
            "Compare preparation before vs during the stop with only team 1 and show savings.")
        self.assertNotIn("24,500", reply["text"])
        self.assertIn("verified simulated plan", reply["text"])

    def test_llm_plan_result_receives_computed_impact(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {
                "name": "simular_plan", "arguments": {"prepare_before_stop": True,
                    "production_ready_at": "14:15"}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "You saved $24,500 daily."}, "done": True},
        ])
        reply = CleaningAgent(self.service, client=SimpleNamespace(chat=lambda **kwargs: next(replies))).ask(
            self.sid, "complex",
            "How much money could cleaning preparation save with target 15 minutes and "
            "production available at 14:15 for 65 days at 70% utilization?")
        self.assertIn("4,083.33 USD/day", reply["text"])
        self.assertNotIn("24,500", reply["text"])

    def test_comparison_keeps_recorded_restart_constraint(self):
        client = SimpleNamespace(chat=lambda **kwargs: self.fail("Recorded availability is not a new stop time"))
        result = CleaningAgent(self.service, client=client).ask(self.sid, "same-time",
            "Compare external preparation before the stop and preparation during the stop, keeping production availability at 14:30.")
        self.assertIn("14:22", result["text"])
        self.assertIn("6 minutes earlier", result["text"])
        self.assertEqual(result["tool_calls"][0]["arguments"]["stop_at"], None)

    def test_tool_error_is_not_evidence_of_an_invalid_recorded_scenario(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {
                "name": "comparar_planes", "arguments": {"unavailable_resources": ["unknown_team"]}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "Your original stop at 14:30 is invalid. Change the recorded scenario."}, "done": True},
        ])
        result = CleaningAgent(self.service, client=SimpleNamespace(chat=lambda **kwargs: next(replies))).ask(self.sid, "wrong-args", "Compare plans without the unknown team")
        self.assertIn("could not complete", result["text"])
        self.assertNotIn("original stop at 14:30", result["text"])
        self.assertEqual(self.service.session(self.sid)["stopped_at"], None)

    def test_unrequested_llm_stop_and_target_changes_are_removed(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {
                "name": "comparar_planes", "arguments": {"stop_at": "14:05", "target_stop_minutes": 30,
                    "production_ready_at": "14:15", "unavailable_resources": [], "delays": []}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "The target is met."}, "done": True},
        ])
        result = CleaningAgent(self.service, client=SimpleNamespace(chat=lambda **kwargs: next(replies))).ask(
            self.sid, "silent-change", "Compare preparing kits before vs during the stop if production availability is 14:15.")
        self.assertIn("14:16", result["text"])
        self.assertIn("Target: 15 min — not met", result["text"])
        self.assertNotIn("14:21", result["text"])
        self.assertIsNone(result["tool_calls"][0]["arguments"]["stop_at"])

    def test_llm_cannot_override_calculated_times_or_value(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {
                "name": "simular_plan", "arguments": {"prepare_before_stop": True}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "Cleaning at 14:16 is after 14:30. You saved 999 USD. Press Confirm."}, "done": True},
        ])
        result = CleaningAgent(self.service, client=SimpleNamespace(chat=lambda **kwargs: next(replies))).ask(self.sid, "wrong", "Simulate preparation")
        self.assertIn("14:16", result["text"])
        self.assertIn("14:30", result["text"])
        self.assertNotIn("after 14:30", result["text"])
        self.assertNotIn("999", result["text"])
        self.assertNotIn("Confirm", result["text"])

    def test_infeasible_comparison_does_not_invent_difference(self):
        self.change("settings", value={"unavailable_resources": ["equipo_1", "equipo_2", "equipo_3"]})
        result = CleaningAgent(self.service).ask(self.sid, "impossible", "Compare preparation before vs during the stop.")
        self.assertIn("Not feasible", result["text"])
        self.assertIn("cannot be calculated", result["text"])
        self.assertNotIn("minutes earlier", result["text"])

    def test_custom_comparison_constraints_reach_the_tool(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {
                "name": "comparar_planes", "arguments": {"production_ready_at": "14:15"}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "Incorrect prose: zero recovery, no difference."}, "done": True},
        ])
        result = CleaningAgent(self.service, client=SimpleNamespace(chat=lambda **kwargs: next(replies))).ask(
            self.sid, "early", "Compare preparation before vs during the stop if production is available at 14:15.")
        self.assertIn("14:15", result["text"])
        self.assertIn("**14 min**", result["text"])
        self.assertIn("**8 min**", result["text"])
        self.assertNotIn("Neither option recovers", result["text"])

    def test_shortage_and_delay_change_schedule(self):
        p = self.service.plan(self.sid)
        short = self.service.plan(self.sid, {"unavailable_resources": ["equipo_2", "equipo_3"]})
        late = self.service.plan(self.sid, {"delays": [{"task_id": "ENR_CLEAN", "minutes": 6}]})
        self.assertGreater(short["cleaning_minutes"], p["cleaning_minutes"])
        self.assertGreater(late["cleaning_minutes"], p["cleaning_minutes"])

    def test_impossible_resources_have_no_economic_credit(self):
        p = self.service.plan(self.sid, {"unavailable_resources": ["equipo_1", "equipo_2", "equipo_3"]})
        self.assertFalse(p["feasible"])
        self.assertIsNone(p["impact"])
        with self.assertRaises(ValidationError):
            self.service.impact(self.sid, p["id"])

    def test_pending_work_is_not_backdated(self):
        self.change("set_clock", value="14:05")
        p = self.service.plan(self.sid)
        self.assertTrue(all(minute(r["start"]) >= minute("14:05") for r in p["rows"]))

    def test_finished_tasks_cannot_backdate_an_unrecorded_restart(self):
        state = self.service.session(self.sid)
        state["clock"] = "14:40"
        state["stopped_at"] = "14:00"
        state["tasks"] = {t["task_id"]:{"status":"completed","started_at":"14:00","completed_at":"14:16","resources":[]}
                          for t in self.service.tasks}
        p = make_plan(self.service.tasks,self.service.resources,self.service.scenario,state,{"production_ready_at":"14:15"})
        self.assertEqual(p["earliest_restart"],"14:40")
        self.assertEqual(p["recovered_production_minutes"],0)

    def test_tool_preserves_unavailability_when_not_overridden(self):
        self.change("settings",value={"unavailable_resources":["equipo_1","equipo_2","equipo_3"]})
        p = self.service.tool(self.sid,"simular_plan",{"unavailable_resources":None})
        self.assertFalse(p["feasible"])

    def test_invalid_plan_inputs_are_rejected(self):
        for options in [{"stop_at": "13:00"}, {"unavailable_resources": ["inventado"]}, {"prepare_before_stop": "false"},
                        {"delays": [{"task_id": "ENR_CLEAN", "minutes": -1}]}, {"target_stop_minutes": float("nan")}]:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.service.plan(self.sid, options)

    def test_runtime_checks_stop_predecessors_and_minimum_duration(self):
        with self.assertRaises(ValidationError):
            self.change("start_task", "ISOLATE")
        with self.assertRaises(ValidationError):
            self.change("start_task", "ENR_CLEAN")
        self.change("start_task", "KIT_ENR")
        with self.assertRaises(ValidationError):
            self.change("complete_task", "KIT_ENR")
        self.change("set_clock", value="13:48")
        self.change("complete_task", "KIT_ENR")
        with self.assertRaises(ValidationError):
            self.change("set_clock", value="13:47")

    def test_proposal_is_not_execution_and_cannot_be_reused(self):
        proposal = self.service.propose(self.sid, "start_task", "KIT_ENR")
        self.assertEqual(self.service.session(self.sid)["tasks"], {})
        self.service.mutate(self.sid, proposal["revision"], "", proposal_id=proposal["id"])
        self.assertEqual(self.service.session(self.sid)["tasks"]["KIT_ENR"]["status"], "in_progress")
        with self.assertRaises(ConflictError):
            self.service.mutate(self.sid, self.service.session(self.sid)["revision"], "", proposal_id=proposal["id"])

    def test_stale_proposal_is_rejected(self):
        proposal = self.service.propose(self.sid, "start_task", "KIT_ENR")
        self.change("set_clock", value="13:41")
        with self.assertRaises(ConflictError):
            self.service.mutate(self.sid, self.service.session(self.sid)["revision"], "", proposal_id=proposal["id"])
        self.assertEqual(self.service.session(self.sid)["tasks"], {})

    def test_state_survives_service_restart(self):
        self.change("start_task", "KIT_ENR")
        other = SmedService(self.db, self.temp.name)
        self.assertEqual(other.session(self.sid)["tasks"]["KIT_ENR"]["started_at"], "13:40")

    def test_full_execution_requires_quality_staff_and_material(self):
        p = self.service.plan(self.sid)
        at = {}
        for row in p["rows"]:
            at.setdefault(row["start"], {"start": [], "end": []})["start"].append(row["task_id"])
            at.setdefault(row["end"], {"start": [], "end": []})["end"].append(row["task_id"])
        at.setdefault("14:00", {"start": [], "end": []})
        for t in sorted(at):
            self.change("set_clock", value=t)
            for tid in at[t]["end"]:
                self.change("complete_task", tid)
            if t == "14:00":
                self.change("record_stop")
            for tid in at[t]["start"]:
                self.change("start_task", tid)
        with self.assertRaises(ValidationError):
            self.change("record_restart")
        self.change("confirm_quality")
        self.change("confirm_readiness", value={"staff_ready": True, "material_ready": True})
        with self.assertRaises(ValidationError):
            self.change("record_restart")
        self.change("set_clock", value="14:30")
        self.change("record_restart")
        self.assertEqual(self.service.session(self.sid)["restarted_at"], "14:30")

    def test_agent_calls_real_tool_and_does_not_apply_proposal(self):
        function = {"function": {"name": "proponer_avance", "arguments": {"action": "start_task", "task_id": "KIT_ENR", "note": "Preparacion"}}}
        class Responses:
            def __init__(self): self.requests = []
            def chat(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    return {"message": {"role": "assistant", "content": "", "tool_calls": [function]}, "done": True}
                self.output = kwargs["messages"][-1]
                return {"message": {"role": "assistant", "content": "Propuesta pendiente de confirmacion."}, "done": True}
        responses = Responses()
        agent = CleaningAgent(self.service, client=responses)
        reply = agent.ask(self.sid, "test", "Quiero iniciar el kit de ENR")
        self.assertEqual(len(reply["proposals"]), 1)
        self.assertEqual(self.service.session(self.sid)["tasks"], {})
        self.assertTrue(json.loads(responses.output["content"])["requires_human_confirmation"])
        self.assertFalse(responses.requests[0]["stream"])
        self.assertEqual(reply["provider"], "Ollama")

    def test_malformed_tool_arguments_are_reported_to_model(self):
        class Responses:
            def __init__(self): self.n = 0
            def chat(inner, **kwargs):
                inner.n += 1
                if inner.n == 1:
                    return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "simular_plan", "arguments": "{bad"}}]}, "done": True}
                self.assertIn("error", json.loads(kwargs["messages"][-1]["content"]))
                return {"message": {"role": "assistant", "content": "Necesito corregir los argumentos."}, "done": True}
        result = CleaningAgent(self.service, client=Responses()).ask(self.sid,"bad","Compara planes")
        self.assertIn("error", result["tool_calls"][0]["summary"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        ServiceCase.setUp(self)
        handler = make_handler(self.service, CleaningAgent(self.service))
        handler.log_message = lambda *args: None
        self.server = ThreadingHTTPServer(("127.0.0.1",0),handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:"+str(self.server.server_port)
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_http_ui_bootstrap_plan_and_private_files(self):
        with urlopen(self.base+"/") as response:
            self.assertIn(b"smed.js",response.read())
        body = json.dumps({"session_id":self.sid,"options":{}}).encode()
        with urlopen(Request(self.base+"/api/smed/plan",data=body,headers={"Content-Type":"application/json"})) as response:
            self.assertTrue(json.load(response)["feasible"])
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base+"/../fixtures/scenario.json")
        self.assertEqual(error.exception.code,404)

    def test_http_rejects_cross_origin_and_non_json(self):
        for headers,code in [({"Content-Type":"text/plain"},415),
                             ({"Content-Type":"application/json","Origin":"https://example.org"},403)]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base+"/api/smed/session",data=b"{}",headers=headers))
            self.assertEqual(error.exception.code,code)

    def test_http_requires_explicit_proposal_confirmation(self):
        proposal = self.service.propose(self.sid,"start_task","KIT_ENR")
        body = {"session_id":self.sid,"expected_revision":0,"proposal_id":proposal["id"],"confirmed":False}
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base+"/api/smed/confirm",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"}))
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.service.session(self.sid)["tasks"],{})


class OllamaHttpContract(unittest.TestCase):
    def setUp(self):
        ServiceCase.setUp(self)

    def test_http_serializes_tools_and_round_trip(self):
        from http.server import BaseHTTPRequestHandler
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(*args): pass
            def do_POST(inner):
                body = json.loads(inner.rfile.read(int(inner.headers["Content-Length"])))
                requests.append(body)
                self.assertEqual(inner.path, "/api/chat")
                self.assertNotIn("Authorization", inner.headers)
                self.assertFalse(body["stream"])
                if len(requests) == 1:
                    self.assertTrue(all("function" in t for t in body["tools"]))
                    message = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                        "name": "simular_plan", "arguments": {"prepare_before_stop": True,
                        "stop_at": None, "production_ready_at": None, "target_stop_minutes": None,
                        "unavailable_resources": None, "delays": []}}}]}
                else:
                    tool = body["messages"][-1]
                    self.assertEqual(tool["role"], "tool")
                    result = json.loads(tool["content"])
                    self.assertTrue(result["feasible"])
                    self.assertEqual(result["earliest_restart"], "14:30")
                    message = {"role": "assistant", "content": "Plan consultado: arranque a las 14:30."}
                data = json.dumps({"message": message, "done": True, "done_reason": "stop"}).encode()
                inner.send_response(200); inner.send_header("Content-Type", "application/json")
                inner.end_headers(); inner.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OllamaClient()
            client.host = "http://127.0.0.1:" + str(server.server_port)
            result = CleaningAgent(self.service, client=client).ask(self.sid, "http", "Prepara el paro")
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(len(requests),2)
        self.assertEqual(result["tool_calls"][0]["name"],"simular_plan")
        self.assertIn("14:30",result["text"])

    def test_truncated_answer_is_rejected(self):
        client = SimpleNamespace(chat=lambda **kwargs: {"message": {"role": "assistant", "content": "partial"},
                                                        "done": True, "done_reason": "length"})
        with self.assertRaises(AgentUnavailable):
            CleaningAgent(self.service, client=client).ask(self.sid, "short", "Prepara el paro")

    def test_health_reports_missing_model_and_server(self):
        with patch.object(OllamaClient, "request", return_value={"models": [{"name": "llama3:latest"}]}):
            health = CleaningAgent(self.service, model="qwen3:4b").health()
            self.assertFalse(health["ai"])
            self.assertIn("ollama pull qwen3:4b", health["why"])
        with patch.object(OllamaClient, "request", side_effect=AgentUnavailable("Ollama offline")):
            self.assertFalse(CleaningAgent(self.service).health()["ai"])

    def test_local_only_host_validation(self):
        for host in ["https://example.org", "http://localhost:11434@example.org", "http://127.0.0.1:11434/path"]:
            with self.subTest(host=host), patch.dict("os.environ", {"OLLAMA_HOST": host}):
                with self.assertRaises(AgentUnavailable):
                    OllamaClient()

    def test_unknown_tool_cannot_mutate_state(self):
        replies = iter([
            {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "operate_machine", "arguments": {}}}]}, "done": True},
            {"message": {"role": "assistant", "content": "This action is not allowed."}, "done": True},
        ])
        client = SimpleNamespace(chat=lambda **kwargs: next(replies))
        result = CleaningAgent(self.service, client=client).ask(self.sid, "unknown", "Start the line")
        self.assertIn("error", result["tool_calls"][0]["summary"])
        self.assertEqual(self.service.session(self.sid)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
