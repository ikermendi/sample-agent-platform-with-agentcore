"""Kernel de prueba para AgentCore Runtime V2: /ping y /invocations; cada invocación corre una prueba de
pruebas_kernel.py (Strands 1.63.0 con modelo falso) contra el hub, por el nombre fijo de Route 53."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pruebas_kernel  # Strands y mcp se cargan antes de la instantánea


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def responde(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.responde({"status": "Healthy", "time_of_last_update": int(time.time())})

    def do_POST(self):
        carga = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.responde(pruebas_kernel.ejecuta(carga))


ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
