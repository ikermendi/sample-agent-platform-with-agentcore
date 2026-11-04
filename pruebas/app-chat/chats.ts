// El store de los chats, fuera de React: los mensajes, la ejecución en marcha con su SSE y la cola de cada
// chat viven aquí, así que no se pierden si assistant-ui vuelve a montar el hook del chat (al cambiar de chat
// con una ejecución en marcha). assistant-ui solo lee lo que este store le da (`useExternalStoreRuntime`).
import { Platform } from "react-native";
import {
  createMessageQueue,
  type AppendMessage,
  type MessageQueueController,
  type RespondToToolApprovalOptions,
  type ThreadMessageLike,
} from "@assistant-ui/react-native";
import { leerSSE, type EventoSSE } from "./sse";
import { cola as colaGuardada, guardarMensajes, hayCache, mensajesGuardados, quitarChat } from "./cache";

export const BASE = process.env.EXPO_PUBLIC_SERVIDOR ?? "http://localhost:8799";

// lo que la app observa, para que la prueba lo lea sin mirar la pantalla
export const informa = (x: Record<string, unknown>) =>
  fetch(`${BASE}/resultado`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plataforma: Platform.OS, ...x }) }).catch(() => {});

// La cola guardada en el dispositivo: en iOS, en la caché SQLite (`cache.ts`); en web, en localStorage.
const almacen = {
  leer: (chat: string) => (hayCache ? colaGuardada.leer(chat) : globalThis.localStorage?.getItem(`cola:${chat}`) ?? null),
  escribir: (chat: string, v: string) => (hayCache ? colaGuardada.escribir(chat, v) : globalThis.localStorage?.setItem(`cola:${chat}`, v)),
  borrar: (chat: string) => (hayCache ? colaGuardada.borrar(chat) : globalThis.localStorage?.removeItem(`cola:${chat}`)),
};

// Un mensaje consolidado del backend, como lo pinta assistant-ui.
const convertir = (m: any): Mensaje => ({
  id: `${m.rol === "user" ? "u" : "a"}_${m.run_id}`,
  role: m.rol,
  status: m.rol === "assistant" ? { type: "complete", reason: "stop" } : undefined,
  content: m.partes.map((p: any): Parte =>
    p.tipo === "texto"
      ? { type: "text", text: p.texto }
      : {
          type: "tool-call", toolCallId: p.id, toolName: p.nombre, args: { etiqueta: p.etiqueta, resumen: p.resumen }, result: { resumen: p.resultado ?? p.aprobacion }, isError: p.estado === "error",
          ...(p.aprobacion && { approval: { id: `h_${p.id}`, ...(p.aprobacion === "caducada" || p.aprobacion === "cancelada" ? { resolution: p.aprobacion === "caducada" ? "expired" : "cancelled" } : { approved: p.aprobacion === "aprobada" }) } }),
        },
  ),
});

type Parte = Exclude<ThreadMessageLike["content"], string>[number];
type LlamadaHerramienta = Extract<Parte, { type: "tool-call" }>;
type Mensaje = Omit<ThreadMessageLike, "content" | "status"> & { id: string; content: Parte[]; status?: ThreadMessageLike["status"] };
type Estado = { mensajes: readonly ThreadMessageLike[]; ejecutando: boolean; contexto?: number; aviso?: string; continuar?: boolean; bloqueado?: boolean };

// Lo que la app hace según cómo terminó la última ejecución: seguir con Continuar o bloquear la caja de texto.
const trasFin = (motivo?: string) => ({ continuar: motivo === "limite_run" || motivo === "tiempo", bloqueado: motivo === "limite" });

const textoDe = (m: AppendMessage) => m.content.map((p) => (p.type === "text" ? p.text : "")).join("");
const mensajeDeCola = (texto: string): AppendMessage =>
  ({ role: "user", content: [{ type: "text", text: texto }], parentId: null, sourceId: null, runConfig: undefined, attachments: [], metadata: { custom: {} }, createdAt: new Date() }) as unknown as AppendMessage;

const nuevoRunId = () => `oid-prueba.${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;

export class Chat {
  remoteId?: string;
  inicializar?: () => Promise<{ remoteId: string }>;
  readonly cola: MessageQueueController;
  private mensajes: Mensaje[] = [];
  private estado: Estado = { mensajes: [], ejecutando: false };
  private subs = new Set<() => void>();
  private runId?: string;
  private abierto = false;
  private caducidades = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(readonly clave: string) {
    // Sin `cancel`: un mensaje enviado con la ejecución en marcha va al carril "steer" del runtime y, sin
    // `cancel`, no interrumpe la ejecución; se envía al terminar, como uno encolado.
    this.cola = createMessageQueue({
      run: (m) => {
        this.cola.notifyBusy();
        return this.lanzar(textoDe(m));
      },
    });
    this.cola.subscribe(() => this.guardarCola());
  }

  suscribir = (f: () => void) => {
    this.subs.add(f);
    return () => this.subs.delete(f);
  };
  leer = () => this.estado;

  private cambia(extra: Partial<Estado> = {}) {
    this.estado = { ...this.estado, ...extra, mensajes: this.mensajes.map((m) => ({ ...m, content: [...m.content] })) };
    this.subs.forEach((f) => f());
  }

  private guardarCola() {
    if (!this.remoteId) return;
    const textos = [...this.cola.adapter.steerItems, ...this.cola.adapter.items].map((i) => i.prompt);
    if (textos.length) almacen.escribir(this.remoteId, JSON.stringify(textos));
    else almacen.borrar(this.remoteId);
  }

  // Al abrir el chat: primero lo guardado en la caché (iOS), después lo nuevo del backend (`after=<seq>`), la
  // ejecución en marcha si la hay y la cola guardada.
  async abrir(remoteId: string) {
    this.remoteId = remoteId;
    if (this.abierto) return;
    this.abierto = true;
    const t0 = performance.now();
    const guardados = mensajesGuardados(remoteId);
    if (guardados.mensajes.length && !this.runId) {
      this.mensajes = guardados.mensajes.map(convertir);
      this.cambia();
    }
    const tPintado = performance.now() - t0;
    const despues = guardados.mensajes.at(-1)?.seq ?? 0;
    const t1 = performance.now();
    const resp = await fetch(`${BASE}/chats/${remoteId}/messages?after=${despues}`);
    if (resp.status === 404) {
      // se borró en otro dispositivo o caducó: fuera de la caché
      quitarChat(remoteId);
      return void informa({ tipo: "chat_404", chat: remoteId });
    }
    const r = await resp.json();
    const tRed = performance.now() - t1;
    const msGuardar = guardarMensajes(remoteId, r.mensajes);
    informa({
      tipo: "chat_abierto", chat: remoteId, cache: hayCache, deCache: guardados.mensajes.length, msLeerCache: guardados.ms,
      msPintarDesdeCache: Math.round(tPintado), nuevos: r.mensajes.length, msRed: Math.round(tRed), msGuardar,
    });
    if (!this.runId) {
      this.mensajes = [...this.mensajes, ...r.mensajes.map(convertir)];
      if (r.activa) {
        // ejecución en marcha (la app se cerró o se recargó): se retoma desde el principio de sus eventos
        this.cola.notifyBusy();
        this.runId = r.activa;
        this.mensajes.push({ id: `a_${r.activa}`, role: "assistant", status: { type: "running" }, content: [] });
        this.conectar(r.activa);
      }
      const ultimo = [...guardados.mensajes, ...r.mensajes].at(-1);
      this.cambia({ ejecutando: !!r.activa, aviso: ultimo?.motivo, ...trasFin(r.activa ? undefined : ultimo?.motivo) });
    }
    const guardada = almacen.leer(remoteId);
    if (guardada) {
      const textos: string[] = JSON.parse(guardada);
      informa({ tipo: "cola_restaurada", chat: remoteId, textos });
      for (const t of textos) this.cola.adapter.enqueue(mensajeDeCola(t));
    }
  }

  private async lanzar(texto: string) {
    const remoteId = this.remoteId ?? (await this.inicializar!()).remoteId;
    this.remoteId = remoteId;
    const runId = nuevoRunId();
    this.runId = runId;
    this.mensajes.push({ id: `u_${runId}`, role: "user", content: [{ type: "text", text: texto }] });
    this.mensajes.push({ id: `a_${runId}`, role: "assistant", status: { type: "running" }, content: [] });
    this.cambia({ ejecutando: true, aviso: undefined, ...trasFin() });
    const r = await fetch(`${BASE}/chats/${remoteId}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId, texto }) });
    informa({ tipo: "post_run", chat: remoteId, run: runId, texto, estado: r.status });
    if (r.status !== 202) return this.terminar(runId, { motivo: r.status === 402 ? "limite" : "error" });
    this.conectar(runId);
  }

  // Lee el SSE de la ejecución; si se corta antes de `fin`, reconecta con el último id recibido.
  private async conectar(runId: string) {
    let ultimo: string | undefined;
    const recibidos: string[] = [];
    let conexiones = 0;
    let duplicados = 0;
    for (;;) {
      conexiones++;
      try {
        const como = await leerSSE(`${BASE}/runs/${runId}/events`, ultimo, (e) => {
          if (ultimo && Number(e.id) <= Number(ultimo)) return void duplicados++;
          ultimo = e.id;
          recibidos.push(e.id);
          this.aplicar(runId, e);
        });
        if (como === "fin") break;
      } catch (error) {
        informa({ tipo: "sse_error", run: runId, error: String(error) });
      }
      await new Promise((r) => setTimeout(r, 300));
    }
    informa({ tipo: "sse_fin", chat: this.remoteId, run: runId, conexiones, recibidos, duplicados });
  }

  private herramienta(runId: string, id: string): LlamadaHerramienta | undefined {
    const m = this.mensajes.find((x) => x.id === `a_${runId}`);
    return m?.content.find((p): p is LlamadaHerramienta => p.type === "tool-call" && p.toolCallId === id);
  }

  private aplicar(runId: string, { evento, datos }: EventoSSE) {
    const m = this.mensajes.find((x) => x.id === `a_${runId}`);
    if (!m) return;
    const c = m.content;
    const cambiaParte = (id: string, f: (p: LlamadaHerramienta) => LlamadaHerramienta) => {
      const i = c.findIndex((p) => p.type === "tool-call" && p.toolCallId === id);
      if (i >= 0) c[i] = f(c[i] as LlamadaHerramienta);
    };
    switch (evento) {
      case "texto": {
        const ult = c.at(-1);
        if (ult?.type === "text") c[c.length - 1] = { type: "text", text: ult.text + datos.texto };
        else c.push({ type: "text", text: datos.texto });
        break;
      }
      case "herramienta_inicio":
        c.push({ type: "tool-call", toolCallId: datos.id, toolName: datos.nombre, args: { etiqueta: datos.etiqueta, resumen: datos.resumen } });
        break;
      case "herramienta_fin":
        cambiaParte(datos.id, (p) => ({ ...p, result: { resumen: datos.resumen }, isError: datos.estado === "error" }));
        break;
      case "aprobacion_pendiente": {
        // la parte `approval` de la llamada; al llegar `caduca` sin decisión, `resolution: "expired"`
        cambiaParte(datos.herramienta_id, (p) => ({ ...p, approval: { id: datos.approval_id, prompt: `${datos.herramienta}: ${datos.resumen}` } }));
        const t = setTimeout(() => this.caducar(runId, datos.herramienta_id), Math.max(0, datos.caduca - Date.now()));
        this.caducidades.set(datos.approval_id, t);
        break;
      }
      case "contexto":
        return this.cambia({ contexto: datos.porcentaje });
      case "fin":
        return this.terminar(runId, datos);
    }
    this.cambia();
  }

  private caducar(runId: string, herramientaId: string) {
    const p = this.herramienta(runId, herramientaId);
    if (!p?.approval || p.approval.approved !== undefined || p.approval.resolution) return;
    const m = this.mensajes.find((x) => x.id === `a_${runId}`)!;
    m.content = m.content.map((x) => (x === p ? { ...p, approval: { ...p.approval!, resolution: "expired" as const } } : x));
    informa({ tipo: "aprobacion_caducada", run: runId, herramienta: herramientaId });
    this.cambia();
  }

  private terminar(runId: string, datos: { motivo: string }) {
    const m = this.mensajes.find((x) => x.id === `a_${runId}`);
    if (m) {
      m.status =
        datos.motivo === "ok" ? { type: "complete", reason: "stop" }
        : datos.motivo === "cancelado" ? { type: "incomplete", reason: "cancelled" }
        : { type: "incomplete", reason: "other" };
      // lo que siga pendiente de aprobar ya no se puede decidir
      m.content = m.content.map((p) =>
        p.type === "tool-call" && p.approval && p.approval.approved === undefined && !p.approval.resolution
          ? { ...p, approval: { ...p.approval, resolution: datos.motivo === "aprobacion_caducada" ? ("expired" as const) : ("cancelled" as const) } }
          : p,
      );
    }
    for (const t of this.caducidades.values()) clearTimeout(t);
    this.caducidades.clear();
    if (this.runId === runId) this.runId = undefined;
    this.cambia({ ejecutando: false, aviso: datos.motivo === "ok" ? undefined : datos.motivo, ...trasFin(datos.motivo) });
    informa({ tipo: "fin", chat: this.remoteId, run: runId, motivo: datos.motivo });
    this.cola.notifyIdle();
  }

  async cancelar() {
    if (this.runId) await fetch(`${BASE}/runs/${this.runId}/cancel`, { method: "POST" });
  }

  async responder({ approvalId, approved }: RespondToToolApprovalOptions) {
    const runId = this.runId!;
    const r = await fetch(`${BASE}/runs/${runId}/approvals/${approvalId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ aprobada: approved }) });
    informa({ tipo: "aprobacion_respuesta", run: runId, approvalId, approved, estado: r.status });
    if (!r.ok) throw new Error(`aprobación ${r.status}`);
    clearTimeout(this.caducidades.get(approvalId));
    const m = this.mensajes.find((x) => x.id === `a_${runId}`)!;
    m.content = m.content.map((p) => (p.type === "tool-call" && p.approval?.id === approvalId ? { ...p, approval: { ...p.approval, approved } } : p));
    this.cambia();
  }

  // El botón Continuar: un mensaje normal, con el presupuesto comprobado de nuevo por `POST /runs`
  continuar() {
    if (this.estado.ejecutando) return;
    this.cola.notifyBusy();
    return this.lanzar("Continúa");
  }

  // `onNew` solo llega sin cola; con cola, los envíos pasan por `createMessageQueue`
  enviar(m: AppendMessage) {
    this.cola.notifyBusy();
    return this.lanzar(textoDe(m));
  }
}

// Un chat por hilo de assistant-ui: por el id local mientras no tiene remoteId y por los dos después, así
// que el mismo objeto sobrevive a que el hilo se inicialice o a que assistant-ui vuelva a montar el hook.
const porClave = new Map<string, Chat>();
export function obtenerChat(id: string, remoteId: string | undefined) {
  let chat = (remoteId && porClave.get(remoteId)) || porClave.get(id);
  if (!chat) {
    chat = new Chat(id);
    porClave.set(id, chat);
  }
  if (remoteId && !porClave.has(remoteId)) porClave.set(remoteId, chat);
  return chat;
}
