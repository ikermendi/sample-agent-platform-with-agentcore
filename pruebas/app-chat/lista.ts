// La lista de chats para `useRemoteThreadListRuntime`, sobre `/chats` del backend.
import type { RemoteThreadListAdapter } from "@assistant-ui/react-native";
import { createAssistantStream } from "assistant-stream";
import { BASE, informa } from "./chats";
import { listaGuardada, rutaCache, sincronizar } from "./cache";

const pide = (ruta: string, init?: RequestInit): Promise<any> =>
  fetch(`${BASE}${ruta}`, { ...init, headers: { "Content-Type": "application/json" } }).then((r) => (r.status === 204 ? {} : r.json()));

export const listaDeChats: RemoteThreadListAdapter = {
  // Al arrancar: la lista entera del backend; en iOS se sincroniza con la caché (lo que no viene se borra).
  async list() {
    const t0 = performance.now();
    const guardada = listaGuardada();
    const msLeerCache = Math.round((performance.now() - t0) * 10) / 10;
    const t1 = performance.now();
    const r = await fetch(`${BASE}/chats`);
    const texto = await r.text();
    const msRed = Math.round(performance.now() - t1);
    const t2 = performance.now();
    const { chats } = JSON.parse(texto);
    const msParsear = Math.round((performance.now() - t2) * 10) / 10;
    const sinc = sincronizar(chats);
    informa({ tipo: "lista_sincronizada", chats: chats.length, enCache: guardada.length, msLeerCache, bytes: texto.length, msRed, msParsear, sinc, ruta: rutaCache() });
    return { threads: chats.map((c: any) => ({ status: c.estado, remoteId: c.id, title: c.titulo ?? undefined })) };
  },
  async initialize() {
    const { id } = await pide("/chats", { method: "POST" });
    return { remoteId: id, externalId: undefined };
  },
  async rename(remoteId, titulo) {
    await pide(`/chats/${remoteId}`, { method: "PATCH", body: JSON.stringify({ titulo }) });
  },
  async archive(remoteId) {
    await pide(`/chats/${remoteId}`, { method: "PATCH", body: JSON.stringify({ estado: "archived" }) });
  },
  async unarchive(remoteId) {
    await pide(`/chats/${remoteId}`, { method: "PATCH", body: JSON.stringify({ estado: "regular" }) });
  },
  async delete(remoteId) {
    await pide(`/chats/${remoteId}`, { method: "DELETE" });
  },
  async fetch(remoteId) {
    const c = await pide(`/chats/${remoteId}`);
    return { status: c.estado, remoteId: c.id, title: c.titulo ?? undefined };
  },
  // el título lo pone el backend al terminar la primera ejecución; aquí solo se espera a que exista
  async generateTitle(remoteId) {
    return createAssistantStream(async (c) => {
      for (let i = 0; i < 40; i++) {
        const { titulo } = await pide(`/chats/${remoteId}`);
        if (titulo) return c.appendText(titulo);
        await new Promise((r) => setTimeout(r, 500));
      }
    });
  },
};
