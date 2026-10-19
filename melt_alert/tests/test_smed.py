import json
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from melt_alert.agent import CleaningAgent
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
        function = SimpleNamespace(type="function_call", name="proponer_avance", arguments=json.dumps({"action": "start_task", "task_id": "KIT_ENR", "note": "Preparacion"}), call_id="call_1")
        class Responses:
            def __init__(self): self.requests = []
            def create(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    return SimpleNamespace(output=[function], output_text="", status="completed")
                self.output = kwargs["input"][-1]
                return SimpleNamespace(output=[], output_text="Propuesta pendiente de confirmacion.", status="completed")
        responses = Responses()
        agent = CleaningAgent(self.service, client=SimpleNamespace(responses=responses))
        reply = agent.ask(self.sid, "test", "Quiero iniciar el kit de ENR")
        self.assertEqual(len(reply["proposals"]), 1)
        self.assertEqual(self.service.session(self.sid)["tasks"], {})
        self.assertTrue(json.loads(responses.output["output"])["requires_human_confirmation"])
        self.assertFalse(responses.requests[0]["store"])

    def test_malformed_tool_arguments_are_reported_to_model(self):
        class Responses:
            def __init__(self): self.n = 0
            def create(inner, **kwargs):
                inner.n += 1
                if inner.n == 1:
                    return SimpleNamespace(output=[SimpleNamespace(type="function_call",name="simular_plan",arguments="{bad",call_id="bad")],output_text="",status="completed")
                self.assertIn("error", json.loads(kwargs["input"][-1]["output"]))
                return SimpleNamespace(output=[],output_text="Necesito corregir los argumentos.",status="completed")
        result = CleaningAgent(self.service, client=SimpleNamespace(responses=Responses())).ask(self.sid,"bad","Compara planes")
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


@unittest.skipUnless(importlib.util.find_spec("openai"), "SDK OpenAI opcional no instalado")
class OpenAISdkContract(unittest.TestCase):
    def setUp(self):
        ServiceCase.setUp(self)

    def test_sdk_serializes_tools_and_round_trip_with_mock_transport(self):
        import httpx
        from openai import OpenAI
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(request.url.path,"/v1/responses")
            self.assertFalse(body["store"])
            if len(requests) == 1:
                self.assertTrue(all(t["strict"] for t in body["tools"]))
                output = [{"type":"function_call","id":"fc_demo","call_id":"call_demo","name":"simular_plan",
                           "arguments":json.dumps({"prepare_before_stop":True,"stop_at":None,"production_ready_at":None,
                               "target_stop_minutes":None,"unavailable_resources":None,"delays":[]}),"status":"completed"}]
            else:
                outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
                result = json.loads(outputs[-1]["output"])
                self.assertTrue(result["feasible"])
                self.assertEqual(result["earliest_restart"],"14:30")
                output = [{"type":"message","id":"msg_demo","role":"assistant","status":"completed",
                           "content":[{"type":"output_text","text":"Plan consultado: la disponibilidad mantiene el arranque a las 14:30.","annotations":[]}]}]
            return httpx.Response(200,json={"id":"resp_demo_"+str(len(requests)),"object":"response",
                "created_at":1,"status":"completed","model":"gpt-5.4-mini","output":output,
                "parallel_tool_calls":False,"error":None,"incomplete_details":None,"metadata":{},
                "tools":body["tools"],"tool_choice":"auto"})

        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            client = OpenAI(api_key="unit-test-placeholder",base_url="https://unit-test.invalid/v1",http_client=transport)
            result = CleaningAgent(self.service,client=client).ask(self.sid,"sdk","Prepara el paro")
        self.assertEqual(len(requests),2)
        self.assertEqual(result["tool_calls"][0]["name"],"simular_plan")
        self.assertIn("14:30",result["text"])


if __name__ == "__main__":
    unittest.main()
