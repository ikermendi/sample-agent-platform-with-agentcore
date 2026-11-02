// Cliente SSE como lo describe el documento: el `fetch` global (en nativo, el de `expo/fetch`, que entrega el
// cuerpo en streaming), un parser propio y `Last-Event-ID` enviado por la app al reconectar. Los comentarios
// de latido (": …") se ignoran.
export type EventoSSE = { id: string; evento: string; datos: any };

// Lee una conexión hasta que se cierra. Devuelve si la cerró un `fin` o se cortó antes.
export async function leerSSE(
  url: string,
  ultimoId: string | undefined,
  alEvento: (e: EventoSSE) => void,
  senal?: AbortSignal,
): Promise<"fin" | "cortado"> {
  const cabeceras: Record<string, string> = { Accept: "text/event-stream" };
  if (ultimoId) cabeceras["Last-Event-ID"] = ultimoId;
  const r = await fetch(url, { headers: cabeceras, signal: senal });
  if (!r.ok || !r.body) throw new Error(`SSE ${r.status}`);
  const lector = r.body.getReader();
  const dec = new TextDecoder();
  let resto = "";
  let fin = false;
  for (;;) {
    const { value, done } = await lector.read();
    if (done) break;
    resto += dec.decode(value, { stream: true });
    let corte: number;
    while ((corte = resto.indexOf("\n\n")) >= 0) {
      const bloque = resto.slice(0, corte);
      resto = resto.slice(corte + 2);
      const e: Partial<EventoSSE> = { evento: "message" };
      let datos = "";
      for (const linea of bloque.split("\n")) {
        if (linea.startsWith(":")) continue;
        const i = linea.indexOf(":");
        const campo = i < 0 ? linea : linea.slice(0, i);
        const valor = i < 0 ? "" : linea.slice(i + 1).replace(/^ /, "");
        if (campo === "id") e.id = valor;
        else if (campo === "event") e.evento = valor;
        else if (campo === "data") datos += (datos ? "\n" : "") + valor;
      }
      if (!datos) continue;
      e.datos = JSON.parse(datos);
      alEvento(e as EventoSSE);
      if (e.evento === "fin") fin = true;
    }
  }
  return fin ? "fin" : "cortado";
}
