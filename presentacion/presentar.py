"""Abre la presentación de Melt Alert en local, con la app real (y su asistente de IA) dentro de la demo.

Uso, desde la raíz del repositorio Mavericks-Production:

    python presentacion\\presentar.py

  --repo      carpeta del repositorio del equipo. Por defecto, la carpeta que contiene a esta
              (si tiene melt_alert/server.py). Arranca la app con `python -m melt_alert.server`
              (pestaña Cleaning y asistente con Ollama). Si ya hay una app en http://127.0.0.1:8000,
              usa esa; si no hay ninguna, la demo muestra la réplica.
  --data-dir  carpeta con los CSV del caso (por defecto, la carpeta data del repositorio).
  --port      puerto de la presentación (8080).

Antes de presentar: abrid Ollama y descargad el modelo una vez con `ollama pull qwen3:4b-instruct`.
Pulsad Ctrl+C en esta terminal para cerrar la presentación y la app.
"""
import argparse, os, subprocess, sys, time, urllib.request, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PITCH = HERE / "melt_alert_pitch.html"
APP_URL = "http://127.0.0.1:8000/"
HEAD = ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html", "/melt_alert_pitch.html"):
            body = (HEAD + PITCH.read_text(encoding="utf-8") + "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith(".py"):
            return self.send_error(404)
        return super().do_GET()

    def log_message(self, *args):
        pass


def app_running():
    try:
        with urllib.request.urlopen(APP_URL + "api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, help="carpeta del repositorio Mavericks-Production")
    ap.add_argument("--data-dir", type=Path, help="carpeta con los CSV del caso")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    args = ap.parse_args()
    if args.repo is None and (HERE.parent / "melt_alert" / "server.py").exists():
        args.repo = HERE.parent

    app = None
    if args.repo and not app_running():
        cmd = [sys.executable, "-m", "melt_alert.server"]
        if args.data_dir:
            cmd += ["--data-dir", str(args.data_dir)]
        app = subprocess.Popen(cmd, cwd=str(args.repo))
        for _ in range(40):
            if app_running():
                break
            time.sleep(0.5)

    if app_running():
        print("App completa en marcha en", APP_URL, "(la demo la mostrará con el asistente de IA)")
    else:
        print("No se encuentra la app en", APP_URL, "- la demo mostrará la réplica sin servidor")

    url = f"http://localhost:{args.port}/"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Presentación en", url, "· Ctrl+C para cerrar")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app:
            app.terminate()


if __name__ == "__main__":
    main()
