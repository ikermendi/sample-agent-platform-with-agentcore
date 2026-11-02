// Recorre la app web (exportada en dist-web y servida por servidor.mjs) con Chrome y comprueba, con lo que la
// app manda a /resultado y lo que apunta el servidor, cada caso del punto "App con assistant-ui".
//
//   CORTE_MS=1500 APROBACION_MS=6000 REGISTRO=registro.jsonl ESTATICO=dist-web node servidor.mjs &
//   REGISTRO=registro.jsonl node prueba.mjs
import { chromium } from "playwright-core";
import { readFileSync, writeFileSync } from "node:fs";

const REGISTRO = process.env.REGISTRO ?? "registro.jsonl";
const WEB = "http://localhost:8800";
const API = "http://localhost:8799";
const espera = (ms) => new Promise((r) => setTimeout(r, ms));
const registro = () => readFileSync(REGISTRO, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const resultados = () => registro().filter((x) => x.resultado).map((x) => ({ t: x.t, ...x.resultado }));
const fines = (run) => (run ? resultados().filter((r) => r.tipo === "fin" && r.run === run) : []);
const casos = [];
const comprueba = (caso, ok, detalle) => {
  casos.push({ caso, ok: !!ok, detalle });
  console.log(`${ok ? "OK  " : "FALLA"} ${caso}${detalle ? " — " + JSON.stringify(detalle) : ""}`);
};
async function hasta(f, ms = 20000) {
  const t0 = Date.now();
  for (;;) {
    const v = await f();
    if (v) return v;
    if (Date.now() - t0 > ms) return v;
    await espera(200);
  }
}

writeFileSync(REGISTRO, "");
await fetch(`${API}/reiniciar`);
const navegador = await chromium.launch({ channel: "chrome", headless: true });
const pagina = await navegador.newPage({ viewport: { width: 1100, height: 800 } });
pagina.on("console", (m) => m.type() === "error" && console.log("  [consola]", m.text()));
const id = (x) => pagina.getByTestId(x);
const enviar = async (texto) => {
  await id("entrada").fill(texto);
  await id("enviar").click();
};
const nuevoChat = async () => {
  await id("nuevo-chat").click();
  await espera(300);
};
const ultimoRun = (texto) => hasta(() => resultados().find((r) => r.tipo === "post_run" && r.texto === texto)?.run);

await pagina.goto(WEB);
await id("nuevo-chat").waitFor();

// 1. Streaming y reconexión: con CORTE_MS el servidor corta cada SSE; la app reconecta con Last-Event-ID
await nuevoChat();
await enviar("hola largo");
const run1 = await ultimoRun("hola largo");
await hasta(() => fines(run1).length, 20000);
{
  const sse = await hasta(() => resultados().find((r) => r.tipo === "sse_fin" && r.run === run1));
  const servidor = registro().filter((x) => x.run === run1);
  const total = servidor.find((x) => x.fin)?.eventos;
  const seguidos = sse.recibidos.every((v, i) => Number(v) === i + 1);
  const conLastId = servidor.filter((x) => x.sse === "abre" && x.last_event_id).length;
  comprueba("1 reconexión: todos los eventos, en orden, sin duplicados", seguidos && sse.recibidos.length === total && sse.duplicados === 0, { eventos: total, conexiones: sse.conexiones, reconexiones_con_last_event_id: conLastId });
  const texto = await pagina.getByTestId("mensaje-assistant").last().innerText();
  comprueba("1 texto completo en pantalla", texto.includes("[30]") && texto.includes("Buscando en documentos · hecha"), texto.slice(0, 80));
  const titulo = await hasta(async () => (await id("chat-titulo").innerText()).includes("hola largo"), 10000);
  comprueba("1 título del backend en la lista y en el chat", titulo && (await pagina.getByText("hola largo", { exact: true }).count()) > 0);
}

// 2. Cola: dos mensajes con la ejecución en marcha se quedan en la cola y salen en orden al terminar
await enviar("uno largo");
await ultimoRun("uno largo");
await espera(500);
await enviar("dos");
await enviar("tres");
await espera(300);
{
  const enCola = await id("cola-item").count();
  const textos = await id("cola-item").allInnerTexts();
  comprueba("2 la cola muestra los dos mensajes", enCola === 2, textos);
  await hasta(() => fines(resultados().find((r) => r.tipo === "post_run" && r.texto === "tres")?.run).length, 40000);
  const posts = resultados().filter((r) => r.tipo === "post_run" && ["uno largo", "dos", "tres"].includes(r.texto));
  const finUno = fines(posts[0].run)[0];
  const ordenOk = posts.map((p) => p.texto).join(",") === "uno largo,dos,tres";
  comprueba("2 salen en orden, uno a uno, sin 409", ordenOk && posts.every((p) => p.estado === 202) && posts[1].t >= finUno.t, posts.map((p) => [p.texto, p.estado]));
}

// 3. Cambio de chat con una ejecución en marcha: el SSE sigue en el store y el mensaje llega completo
const chatA = await hasta(async () => (await id("chat-titulo").innerText()).match(/\((chat\d+)\)/)?.[1]);
await enviar("cambio largo");
const run3 = await ultimoRun("cambio largo");
await espera(1500);
const montajesAntes = resultados().filter((r) => r.tipo === "hook_montado").length;
await nuevoChat();
await enviar("en el otro chat");
await ultimoRun("en el otro chat");
await espera(1500);
// los chats creados en esta sesión tienen id local en la lista; se eligen por título
await pagina.getByText("hola largo", { exact: true }).click();
await espera(500);
{
  const enCurso = (await pagina.getByTestId("mensaje-assistant").last().innerText());
  comprueba("3 al volver, la respuesta sigue escribiéndose", !(await fines(run3)).length && enCurso.includes("cambio largo"), enCurso.slice(0, 60));
  await hasta(() => fines(run3).length, 20000);
  await espera(300);
  const texto = await pagina.getByTestId("mensaje-assistant").last().innerText();
  const sse = resultados().find((r) => r.tipo === "sse_fin" && r.run === run3);
  const montajes = resultados().filter((r) => r.tipo === "hook_montado").slice(montajesAntes);
  const desmontajes = resultados().filter((r) => r.tipo === "hook_desmontado");
  const idA = resultados().find((r) => r.tipo === "hook_montado")?.id;
  comprueba("3 mensaje completo tras cambiar de chat", texto.includes("[30]") && sse.recibidos.length === registro().find((x) => x.run === run3 && x.fin).eventos && sse.duplicados === 0, { conexiones: sse.conexiones });
  // informativo: si assistant-ui desmonta el hook del chat al cambiar (el store aguanta en los dos casos)
  comprueba("3 montajes del hook al ir y volver", true, { chat_A: idA, montajes_despues: montajes.map((m) => m.id), desmontajes: desmontajes.map((m) => m.id) });
}

// 4. Aprobaciones: aprobar, rechazar y dejar caducar
for (const [caso, accion] of [["aprobar", "aprobar"], ["rechazar", "rechazar"], ["caducar", null]]) {
  await enviar(`por favor aprobar (${caso})`);
  const run = await ultimoRun(`por favor aprobar (${caso})`);
  await id("aprobar").last().waitFor({ timeout: 10000 });
  if (accion) await id(accion).last().click();
  await hasta(() => fines(run).length, 15000);
  await espera(300);
  const estado = await id("aprobacion-estado").last().innerText();
  const botones = await id("aprobar").count();
  const fin = fines(run)[0]?.motivo;
  const esperado = { aprobar: ["aprobada", "ok"], rechazar: ["rechazada", "ok"], caducar: ["expired", "aprobacion_caducada"] }[caso];
  comprueba(`4 aprobación: ${caso}`, estado.includes(esperado[0]) && fin === esperado[1] && botones === 0, { estado, fin, botones });
}
{
  const caducadaLocal = resultados().find((r) => r.tipo === "aprobacion_caducada");
  comprueba("4 la caducidad la marca la app con `caduca`, antes de `fin`", caducadaLocal && caducadaLocal.t <= fines(caducadaLocal.run)[0].t, caducadaLocal && { antes_ms: fines(caducadaLocal.run)[0].t - caducadaLocal.t });
}

// 5. Cancelar con la cola llena: el runtime pausa la cola (notifyCancelled) y no sale el siguiente
await enviar("para largo");
const run5 = await ultimoRun("para largo");
await espera(1200);
await enviar("después de parar");
await espera(300);
await id("parar").click();
await hasta(() => fines(run5).length, 10000);
await espera(1500);
{
  const motivo = fines(run5)[0]?.motivo;
  const salio = resultados().some((r) => r.tipo === "post_run" && r.texto === "después de parar");
  const enCola = await id("cola-item").count();
  comprueba("5 cancelar: fin cancelado y la cola se queda parada", motivo === "cancelado" && !salio && enCola === 1, { motivo, salio, enCola });
  await enviar("reanuda");
  await hasta(() => resultados().some((r) => r.tipo === "post_run" && r.texto === "reanuda"), 15000);
  await hasta(() => fines(resultados().find((r) => r.tipo === "post_run" && r.texto === "reanuda")?.run).length, 15000);
  const orden = resultados().filter((r) => r.tipo === "post_run" && ["después de parar", "reanuda"].includes(r.texto)).map((r) => r.texto);
  comprueba("5 el siguiente envío reactiva la cola", orden.length === 2, orden);
}

// 6. La cola sobrevive a recargar la app con una ejecución en marcha (caché local + backend)
const chatB = await hasta(async () => (await id("chat-titulo").innerText()).match(/\((chat\d+)\)/)?.[1]);
await enviar("recarga largo");
const run6 = await ultimoRun("recarga largo");
await espera(800);
await enviar("guardado 1");
await enviar("guardado 2");
await espera(300);
await pagina.reload();
await id("nuevo-chat").waitFor();
await espera(800);
await id(`chat-${chatB}`).click();
{
  const restaurada = await hasta(() => resultados().find((r) => r.tipo === "cola_restaurada" && r.chat === chatB), 8000);
  comprueba("6 la cola se restaura al abrir el chat", restaurada?.textos?.join(",") === "guardado 1,guardado 2", restaurada?.textos);
  await hasta(() => fines(run6).length, 20000);
  const sse = await hasta(() => resultados().filter((r) => r.tipo === "sse_fin" && r.run === run6).at(-1));
  await hasta(() => fines(resultados().find((r) => r.tipo === "post_run" && r.texto === "guardado 2")?.run).length, 20000);
  const orden = resultados().filter((r) => r.tipo === "post_run" && r.texto?.startsWith("guardado")).map((r) => [r.texto, r.estado]);
  comprueba("6 tras recargar, la ejecución se retoma y la cola sale después", sse && sse.recibidos[0] === "1" && orden.length === 2 && orden.every((o) => o[1] === 202), { orden, eventos: sse?.recibidos.length });
}

await pagina.screenshot({ path: "captura-web.png" });
await navegador.close();
const fallos = casos.filter((c) => !c.ok);
console.log(`\n${casos.length - fallos.length}/${casos.length} bien`);
process.exit(fallos.length ? 1 : 0);
