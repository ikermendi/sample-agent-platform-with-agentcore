// Backend simulado para la prueba de la app con assistant-ui. Hace lo que la app ve de `api`: los chats
// (`/chats`), las ejecuciones (`POST /chats/{id}/runs`, una a la vez por chat), el SSE de eventos con
// `Last-Event-ID` (`GET /runs/{id}/events`), cancelar y aprobar. Los eventos de cada ejecución se guardan en
// memoria como en DynamoDB, así que una reconexión recupera lo que falte. Cada petición queda en una línea
// JSON del fichero REGISTRO; la app manda a `/resultado` lo que observa.
//
//   CORTE_MS=1500 REGISTRO=registro.jsonl ESTATICO=dist-web node servidor.mjs
//
// CORTE_MS corta cada conexión SSE a los N ms (como el tope de 15 minutos o una réplica que se drena);
// APROBACION_MS es lo que se espera una aprobación antes de caducar. Qué hace una ejecución depende del texto
// del mensaje: "aprobar" pide aprobación de `enviar_correo`; "largo", unos 9 s de texto; si no, unos 3 s.
//
// SEMBRAR_CHATS=500 SEMBRAR_MENSAJES=500 arranca con 500 chats de 4 mensajes y el más reciente (`chat1`)
// con 500, para la caché de iOS; `GET /chats/{id}/messages?after=<seq>` da solo los posteriores.
import { createServer } from "node:http";
import { appendFileSync, readFileSync, existsSync, statSync } from "node:fs";
import { join, extname } from "node:path";

const REGISTRO = process.env.REGISTRO ?? "registro.jsonl";
const CORTE_MS = Number(process.env.CORTE_MS ?? 0);
const APROBACION_MS = Number(process.env.APROBACION_MS ?? 6000);
const apunta = (x) => appendFileSync(REGISTRO, JSON.stringify({ t: Date.now(), ...x }) + "\n");
const espera = (ms) => new Promise((r) => setTimeout(r, ms));

const chats = new Map(); // id -> { id, titulo, estado, actualizado, mensajes: [{ seq, rol, partes }], activa }
const runs = new Map(); // run_id -> { chat, eventos: [{ id, evento, datos }], cancelar, aprobaciones: Map }
let siguienteChat = 1;

const SEMBRAR_CHATS = Number(process.env.SEMBRAR_CHATS ?? 0);
const SEMBRAR_MENSAJES = Number(process.env.SEMBRAR_MENSAJES ?? 4);
const textoDeRelleno = (i) => `Respuesta ${i}: ` + "Las ventas del trimestre suben un 4 % en el norte y bajan un 2 % en el sur; el informe detalla cada región. ".repeat(3);
for (let n = 1; n <= SEMBRAR_CHATS; n++) {
  const id = `chat${siguienteChat++}`;
  const total = n === 1 ? SEMBRAR_MENSAJES : 4;
  const mensajes = Array.from({ length: total }, (_, i) => ({
    seq: i + 1,
    rol: i % 2 ? "assistant" : "user",
    run_id: `oid-prueba.semilla${n}x${Math.floor(i / 2)}`,
    partes: [{ tipo: "texto", texto: i % 2 ? textoDeRelleno(i) : `Pregunta ${i}: ¿cómo van las ventas por región?` }],
  }));
  chats.set(id, { id, titulo: `Chat sembrado ${n}`, estado: "regular", actualizado: Date.now() - n * 60_000, mensajes, activa: null });
}

function emite(run, evento, datos) {
  const id = String(run.eventos.length + 1);
  run.eventos.push({ id, evento, datos });
  return id;
}

async function ejecutar(runId, texto) {
  const run = runs.get(runId);
  const chat = chats.get(run.chat);
  const partes = [];
  const fin = (motivo) => {
    emite(run, "fin", { motivo, uso: { entrada: 1200, salida: 80 } });
    chat.mensajes.push({ seq: chat.mensajes.length + 1, rol: "assistant", run_id: runId, partes, motivo });
    chat.activa = null;
    chat.actualizado = Date.now();
    if (!chat.titulo) chat.titulo = texto.slice(0, 30);
    apunta({ run: runId, fin: motivo, eventos: run.eventos.length });
  };
  const texto_ = async (trozos) => {
    let t = "";
    for (const trozo of trozos) {
      if (run.cancelar) return false;
      emite(run, "texto", { texto: trozo });
      t += trozo;
      await espera(300);
    }
    partes.push({ tipo: "texto", texto: t });
    return true;
  };

  emite(run, "estado", { texto: "Esperando al modelo" });
  emite(run, "contexto", { total: 5400, porcentaje: 3, partes: { instrucciones: 1800, herramientas: 2100, conversacion: 1500, resultados: 0 } });
  await espera(200);
  if (run.cancelar) return fin("cancelado");

  if (texto.toLowerCase().includes("aprobar")) {
    const herramienta = { id: `tu_${runId.slice(-6)}`, nombre: "enviar_correo", etiqueta: "Enviando correo", resumen: "a: equipo@ejemplo.com" };
    emite(run, "herramienta_inicio", herramienta);
    const approvalId = `ap_${runId.slice(-6)}`;
    const caduca = Date.now() + APROBACION_MS;
    run.aprobaciones.set(approvalId, null);
    emite(run, "aprobacion_pendiente", { approval_id: approvalId, herramienta_id: herramienta.id, herramienta: herramienta.nombre, resumen: herramienta.resumen, caduca });
    while (run.aprobaciones.get(approvalId) === null && !run.cancelar && Date.now() < caduca) await espera(100);
    const decision = run.aprobaciones.get(approvalId);
    if (decision === null) run.aprobaciones.set(approvalId, "caducada");
    if (run.cancelar || decision === null) {
      // cancelada durante la espera o caducada: el kernel la deniega y el modelo sigue sin ella
      emite(run, "herramienta_fin", { id: herramienta.id, estado: "error", resumen: run.cancelar ? "cancelada" : "aprobación caducada" });
      partes.push({ tipo: "herramienta", ...herramienta, estado: "error", aprobacion: run.cancelar ? "cancelada" : "caducada" });
      return fin(run.cancelar ? "cancelado" : "aprobacion_caducada");
    }
    emite(run, "herramienta_fin", { id: herramienta.id, estado: decision ? "ok" : "error", resumen: decision ? "enviado" : "rechazada por el usuario" });
    partes.push({ tipo: "herramienta", ...herramienta, estado: decision ? "ok" : "error", aprobacion: decision ? "aprobada" : "rechazada" });
    if (!(await texto_(decision ? ["Correo ", "enviado."] : ["No ", "lo envío."]))) return fin("cancelado");
    return fin("ok");
  }

  const herramienta = { id: `tu_${runId.slice(-6)}`, nombre: "buscar_documentos", etiqueta: "Buscando en documentos", resumen: texto.slice(0, 40) };
  emite(run, "herramienta_inicio", herramienta);
  await espera(600);
  emite(run, "herramienta_fin", { id: herramienta.id, estado: "ok", resumen: "3 documentos" });
  partes.push({ tipo: "herramienta", ...herramienta, estado: "ok", resultado: "3 documentos" });
  const n = texto.toLowerCase().includes("largo") ? 30 : 8;
  const trozos = Array.from({ length: n }, (_, i) => `[${i + 1}] `);
  if (!(await texto_([`Respuesta a «${texto}»: `, ...trozos]))) return fin("cancelado");
  emite(run, "uso", { entrada: 1200, salida: 80 });
  fin("ok");
}

function sse(req, res, run, runId) {
  const ultimo = Number(req.headers["last-event-id"] ?? 0);
  const conexion = Math.random().toString(36).slice(2, 8);
  apunta({ run: runId, sse: "abre", conexion, last_event_id: req.headers["last-event-id"] ?? null });
  res.writeHead(200, { ...cors, "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  let enviado = ultimo;
  let cerrado = false;
  const cerrar = (motivo) => {
    if (cerrado) return;
    cerrado = true;
    clearInterval(sondeo);
    clearInterval(latido);
    clearTimeout(corte);
    apunta({ run: runId, sse: "cierra", conexion, motivo, hasta: enviado });
    res.end();
  };
  const sondeo = setInterval(() => {
    while (enviado < run.eventos.length) {
      const e = run.eventos[enviado];
      res.write(`id: ${e.id}\nevent: ${e.evento}\ndata: ${JSON.stringify(e.datos)}\n\n`);
      enviado++;
      if (e.evento === "fin") return cerrar("fin");
    }
  }, 100);
  const latido = setInterval(() => res.write(": latido\n\n"), 1000);
  const corte = CORTE_MS ? setTimeout(() => cerrar("corte"), CORTE_MS) : null;
  res.on("close", () => cerrar("cliente"));
}

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "authorization,content-type,last-event-id,traceparent",
};

createServer((req, res) => {
  if (req.method === "OPTIONS") return res.writeHead(204, cors).end();
  let cuerpo = "";
  req.on("data", (c) => (cuerpo += c));
  req.on("end", () => {
    let json = {};
    try { json = cuerpo ? JSON.parse(cuerpo) : {}; } catch {}
    const url = new URL(req.url, "http://x");
    const p = url.pathname.split("/").filter(Boolean);
    const responde = (estado, datos) => res.writeHead(estado, { ...cors, "Content-Type": "application/json" }).end(JSON.stringify(datos ?? {}));
    if (req.url !== "/resultado" && !(p[0] === "runs" && p[2] === "events")) apunta({ metodo: req.method, ruta: req.url, cuerpo: json });

    if (req.url === "/resultado") { apunta({ resultado: json }); return responde(200, { ok: true }); }
    if (req.url === "/reiniciar") { chats.clear(); runs.clear(); return responde(200, {}); }
    if (p[0] === "chats" && p.length === 1 && req.method === "GET")
      return responde(200, { chats: [...chats.values()].sort((a, b) => b.actualizado - a.actualizado).map(({ mensajes, ...c }) => c) });
    if (p[0] === "chats" && p.length === 1 && req.method === "POST") {
      const id = `chat${siguienteChat++}`;
      chats.set(id, { id, titulo: null, estado: "regular", actualizado: Date.now(), mensajes: [], activa: null });
      return responde(201, { id });
    }
    const chat = p[0] === "chats" ? chats.get(p[1]) : null;
    if (p[0] === "chats" && !chat) return responde(404, { error: "chat" });
    if (chat && p.length === 2 && req.method === "GET") { const { mensajes, ...c } = chat; return responde(200, c); }
    if (chat && p.length === 2 && req.method === "PATCH") { Object.assign(chat, json); return responde(200, {}); }
    if (chat && p.length === 2 && req.method === "DELETE") { chats.delete(chat.id); return responde(204); }
    if (chat && p[2] === "messages") {
      const despues = Number(url.searchParams.get("after") ?? 0);
      return responde(200, { mensajes: chat.mensajes.filter((m) => m.seq > despues), activa: chat.activa });
    }
    if (chat && p[2] === "runs" && req.method === "POST") {
      if (chat.activa) return responde(409, { error: "ya hay una ejecución en marcha en este chat", activa: chat.activa });
      const runId = json.run_id;
      chat.mensajes.push({ seq: chat.mensajes.length + 1, rol: "user", run_id: runId, partes: [{ tipo: "texto", texto: json.texto }] });
      chat.activa = runId;
      chat.actualizado = Date.now();
      runs.set(runId, { chat: chat.id, eventos: [], cancelar: false, aprobaciones: new Map() });
      ejecutar(runId, json.texto);
      return responde(202, { run_id: runId });
    }
    const run = p[0] === "runs" ? runs.get(p[1]) : null;
    if (p[0] === "runs" && !run) return responde(404, { error: "run" });
    if (run && p[2] === "events") return sse(req, res, run, p[1]);
    if (run && p[2] === "cancel") { run.cancelar = true; return responde(202, {}); }
    if (run && p[2] === "approvals") {
      if (run.aprobaciones.get(p[3]) !== null) return responde(409, { error: "ya decidida o caducada" });
      run.aprobaciones.set(p[3], !!json.aprobada);
      return responde(200, {});
    }
    responde(404, { error: "ruta" });
  });
}).listen(8799, () => console.log("servidor en 8799"));

if (process.env.ESTATICO) {
  const tipos = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".ico": "image/x-icon", ".ttf": "font/ttf" };
  createServer((req, res) => {
    let f = join(process.env.ESTATICO, decodeURIComponent(req.url.split("?")[0]));
    if (!existsSync(f) || statSync(f).isDirectory()) f = join(process.env.ESTATICO, "index.html");
    res.writeHead(200, { "Content-Type": tipos[extname(f)] ?? "application/octet-stream" }).end(readFileSync(f));
  }).listen(8800, () => console.log("web en 8800"));
}
