// Servidor de la prueba de trazas de la app. En el puerto 8799: /eco devuelve las cabeceras que recibe,
// /telemetry recibe el OTLP JSON y /resultado lo que la app concluye; cada petición queda en una línea JSON
// del fichero REGISTRO. CORS como lo tendría API Gateway: solo las cabeceras de CORS_CABECERAS. Con
// ESTATICO=<dir> sirve además ese directorio (la exportación web) en el puerto 8800, otro origen.
//
//   REGISTRO=/tmp/registro.jsonl ESTATICO=dist node servidor.mjs
import { createServer } from "node:http";
import { appendFileSync, readFileSync, existsSync, statSync } from "node:fs";
import { join, extname } from "node:path";

const REGISTRO = process.env.REGISTRO ?? "registro.jsonl";
const PERMITIDAS = process.env.CORS_CABECERAS ?? "content-type,traceparent,tracestate,x-amzn-trace-id";
const apunta = (x) => appendFileSync(REGISTRO, JSON.stringify(x) + "\n");

createServer((req, res) => {
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": PERMITIDAS };
  if (req.method === "OPTIONS") {
    apunta({ ruta: req.url, metodo: "OPTIONS", pide: req.headers["access-control-request-headers"] });
    res.writeHead(204, cors).end();
    return;
  }
  let cuerpo = "";
  req.on("data", (c) => (cuerpo += c));
  req.on("end", () => {
    let json = null;
    try { json = cuerpo ? JSON.parse(cuerpo) : null; } catch {}
    apunta({ ruta: req.url, metodo: req.method, cabeceras: req.headers, cuerpo: json });
    res.writeHead(200, { ...cors, "Content-Type": "application/json" });
    res.end(JSON.stringify(req.url === "/eco" ? req.headers : { ok: true }));
  });
}).listen(8799, () => console.log("servidor en 8799"));

if (process.env.ESTATICO) {
  const tipos = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".ico": "image/x-icon" };
  createServer((req, res) => {
    let f = join(process.env.ESTATICO, decodeURIComponent(req.url.split("?")[0]));
    if (!existsSync(f) || statSync(f).isDirectory()) f = join(process.env.ESTATICO, "index.html");
    res.writeHead(200, { "Content-Type": tipos[extname(f)] ?? "application/octet-stream" }).end(readFileSync(f));
  }).listen(8800, () => console.log("web en 8800"));
}
