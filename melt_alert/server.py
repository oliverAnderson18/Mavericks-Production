"""Servidor local: python -m melt_alert.server; no necesita dependencias para el planificador."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import AgentUnavailable, CleaningAgent
from .planner import ValidationError
from .service import ConflictError, SmedService

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent


def make_handler(service, agent, static_dir=ROOT / "static"):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code, body):
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                return self.send_json(200, agent.health())
            if parsed.path == "/api/smed":
                try:
                    sid = parse_qs(parsed.query).get("session", [None])[0]
                    if not sid:
                        sid = service.session()["id"]
                    return self.send_json(200, service.snapshot(sid))
                except ValidationError as exc:
                    return self.send_json(400, {"error": str(exc)})
            names = {"/": "index.html", "/index.html": "index.html", "/smed.js": "smed.js", "/smed.css": "smed.css"}
            if parsed.path not in names:
                return self.send_error(404)
            path = Path(static_dir) / names[parsed.path]
            if not path.is_file():
                return self.send_error(404)
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(path.name)[0] or "application/octet-stream") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            try:
                # Solo JSON de la app local. Ninguna clave se recibe del navegador.
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.send_json(415, {"error": "Content type must be application/json."})
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    return self.send_json(403, {"error": "Origin not allowed."})
                size = int(self.headers.get("Content-Length", 0))
                if size < 0 or size > 32768:
                    return self.send_json(413, {"error": "Request too large."})
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValidationError("Request must be a JSON object.")
                path = urlparse(self.path).path
                if path == "/api/smed/session":
                    return self.send_json(201, service.snapshot(service.session()["id"]))
                sid = body.get("session_id")
                if not isinstance(sid, str) or not sid:
                    raise ValidationError("Session ID is required.")
                service.session(sid)
                if path == "/api/smed/plan":
                    return self.send_json(200, service.plan(sid, body.get("options")))
                if path == "/api/smed/event":
                    return self.send_json(200, service.mutate(sid, body.get("expected_revision"), body.get("action"),
                        task_id=body.get("task_id"), actor=body.get("actor", "Operator"), value=body.get("value"), note=body.get("note", "")))
                if path == "/api/smed/confirm":
                    if body.get("confirmed") is not True or not isinstance(body.get("proposal_id"), str):
                        raise ValidationError("Confirm the proposal explicitly before applying it.")
                    return self.send_json(200, service.mutate(sid, body.get("expected_revision"), "",
                        actor=body.get("actor", "Operator"), proposal_id=body["proposal_id"]))
                if path == "/api/chat":
                    return self.send_json(200, agent.ask(sid, body.get("conversation", "default"), body.get("question")))
                return self.send_error(404)
            except ConflictError as exc:
                self.send_json(409, {"error": str(exc)})
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                self.send_json(400, {"error": str(exc)})
            except AgentUnavailable as exc:
                self.send_json(503, {"error": str(exc)})
            except Exception as exc:
                # No devolver respuestas crudas del proveedor, cabeceras o credenciales.
                status = getattr(exc, "status_code", None)
                message = {401: "OpenAI rejected the API key. Check OPENAI_API_KEY on the server.",
                           403: "This account cannot access the selected OpenAI model.",
                           404: "Model unavailable. Check OPENAI_MODEL on the server.",
                           429: "OpenAI usage or credit limit reached. Check the account and try again."}.get(status,
                           "Request failed. Check the server connection, SDK and logs.")
                self.log_error("Fallo %s (status=%s)", type(exc).__name__, status)
                self.send_json(502 if status else 500, {"error": message})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--data-dir", type=Path, default=PROJECT / "data")
    parser.add_argument("--db", type=Path, default=ROOT / "runtime" / "smed.sqlite3")
    args = parser.parse_args()
    service = SmedService(args.db, args.data_dir)
    agent = CleaningAgent(service)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service, agent))
    server.daemon_threads = True
    print(f"Melt Alert SMED: http://127.0.0.1:{args.port}", flush=True)
    print(json.dumps(agent.health(), ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
