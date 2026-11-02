// La web no guarda chats: todo vive en memoria (y la cola, en localStorage, ver `chats.ts`). La caché de iOS
// está en `cache.ios.ts`, que Metro elige en iOS; esta versión vacía evita que la web empaquete expo-sqlite.
export type ResumenChat = { id: string; titulo: string | null; actualizado: number; estado?: string };
export const hayCache = false;
export const rutaCache = (): string | null => null;
export const listaGuardada = (): ResumenChat[] => [];
export const sincronizar = (_lista: ResumenChat[]): { antes: number; despues: number; quitados: number; mensajesPurgados: number; ms: number } | null => null;
export const mensajesGuardados = (_chat: string): { mensajes: any[]; ms: number } => ({ mensajes: [], ms: 0 });
export const guardarMensajes = (_chat: string, _mensajes: { seq: number }[]) => 0;
export const quitarChat = (_chat: string) => {};
export const cola = { leer: (_chat: string): string | null => null, escribir: (_chat: string, _json: string) => {}, borrar: (_chat: string) => {} };
