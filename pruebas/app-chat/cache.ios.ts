// Caché local de chats en iOS: SQLite (`expo-sqlite`) en Library/Caches, que no entra en la copia de iCloud.
// Guarda la lista de chats, los mensajes de los chats abiertos en los últimos 30 días y la cola de cada chat.
// Al arrancar, `sincronizar` pide la lista entera y quita lo que ya no viene. En web no hay caché: todo vive
// en memoria y la cola va a localStorage: `cache.ts` es la versión vacía que usa la web.
import { Paths } from "expo-file-system";
import { openDatabaseSync, type SQLiteDatabase } from "expo-sqlite";

const DIAS = 30;
const ahora = () => Date.now();
const ms = (t0: number) => Math.round((performance.now() - t0) * 10) / 10;

let db: SQLiteDatabase | null = null;
export const hayCache = true;

export function abrirCache(): SQLiteDatabase | null {
  if (!hayCache) return null;
  if (db) return db;
  const dir = decodeURI(Paths.cache.uri.replace(/^file:\/\//, "")) + "SQLite";
  db = openDatabaseSync("cache.db", undefined, dir);
  db.execSync(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, titulo TEXT, actualizado INTEGER, abierto_en INTEGER);
    CREATE TABLE IF NOT EXISTS mensajes (chat TEXT, seq INTEGER, json TEXT, PRIMARY KEY (chat, seq));
    CREATE TABLE IF NOT EXISTS cola (chat TEXT PRIMARY KEY, json TEXT);
  `);
  return db;
}

export const rutaCache = () => (hayCache ? decodeURI(Paths.cache.uri.replace(/^file:\/\//, "")) + "SQLite/cache.db" : null);

export type ResumenChat = { id: string; titulo: string | null; actualizado: number; estado?: string };

// La lista guardada, para enseñarla antes de que responda el backend.
export function listaGuardada(): ResumenChat[] {
  const d = abrirCache();
  if (!d) return [];
  return d.getAllSync<ResumenChat>("SELECT id, titulo, actualizado FROM chats ORDER BY actualizado DESC");
}

// Con la lista entera del backend: guarda la nueva, quita los chats que ya no vienen (con sus mensajes y su
// cola) y los mensajes de los chats que no se abren desde hace más de 30 días.
export function sincronizar(lista: ResumenChat[]) {
  const d = abrirCache();
  if (!d) return null;
  const t0 = performance.now();
  const antes = d.getFirstSync<{ n: number }>("SELECT count(*) AS n FROM chats")!.n;
  let quitados = 0;
  let purgados = 0;
  d.withTransactionSync(() => {
    d.execSync("CREATE TEMP TABLE IF NOT EXISTS vigentes (id TEXT PRIMARY KEY); DELETE FROM vigentes;");
    const ins = d.prepareSync("INSERT INTO vigentes (id) VALUES (?)");
    const up = d.prepareSync(
      "INSERT INTO chats (id, titulo, actualizado) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET titulo = excluded.titulo, actualizado = excluded.actualizado",
    );
    try {
      for (const c of lista) {
        ins.executeSync(c.id);
        up.executeSync(c.id, c.titulo, c.actualizado);
      }
    } finally {
      ins.finalizeSync();
      up.finalizeSync();
    }
    quitados = d.getFirstSync<{ n: number }>("SELECT count(*) AS n FROM chats WHERE id NOT IN (SELECT id FROM vigentes)")!.n;
    d.execSync(`
      DELETE FROM mensajes WHERE chat NOT IN (SELECT id FROM vigentes);
      DELETE FROM cola WHERE chat NOT IN (SELECT id FROM vigentes);
      DELETE FROM chats WHERE id NOT IN (SELECT id FROM vigentes);
    `);
    const limite = ahora() - DIAS * 86_400_000;
    purgados = d.runSync("DELETE FROM mensajes WHERE chat IN (SELECT id FROM chats WHERE abierto_en IS NULL OR abierto_en < ?)", limite).changes;
  });
  return { antes, despues: lista.length, quitados, mensajesPurgados: purgados, ms: ms(t0) };
}

export function mensajesGuardados(chat: string): { mensajes: any[]; ms: number } {
  const d = abrirCache();
  if (!d) return { mensajes: [], ms: 0 };
  const t0 = performance.now();
  const filas = d.getAllSync<{ json: string }>("SELECT json FROM mensajes WHERE chat = ? ORDER BY seq", chat);
  return { mensajes: filas.map((f) => JSON.parse(f.json)), ms: ms(t0) };
}

export function guardarMensajes(chat: string, mensajes: { seq: number }[]) {
  const d = abrirCache();
  if (!d) return 0;
  const t0 = performance.now();
  d.withTransactionSync(() => {
    const st = d.prepareSync("INSERT OR REPLACE INTO mensajes (chat, seq, json) VALUES (?, ?, ?)");
    try {
      for (const m of mensajes) st.executeSync(chat, m.seq, JSON.stringify(m));
    } finally {
      st.finalizeSync();
    }
    d.runSync("UPDATE chats SET abierto_en = ? WHERE id = ?", ahora(), chat);
  });
  return ms(t0);
}

export function quitarChat(chat: string) {
  const d = abrirCache();
  if (!d) return;
  d.withTransactionSync(() => {
    for (const t of ["mensajes", "cola"]) d.runSync(`DELETE FROM ${t} WHERE chat = ?`, chat);
    d.runSync("DELETE FROM chats WHERE id = ?", chat);
  });
}

export const cola = {
  leer: (chat: string) => abrirCache()?.getFirstSync<{ json: string }>("SELECT json FROM cola WHERE chat = ?", chat)?.json ?? null,
  escribir: (chat: string, json: string) => void abrirCache()?.runSync("INSERT OR REPLACE INTO cola (chat, json) VALUES (?, ?)", chat, json),
  borrar: (chat: string) => void abrirCache()?.runSync("DELETE FROM cola WHERE chat = ?", chat),
};
