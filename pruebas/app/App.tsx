// Prueba de trazas de la app: un tramo "enviar mensaje" con una petición hija a /eco (que devuelve las
// cabeceras que recibe) y la exportación de los tramos a /telemetry. El resultado se ve en pantalla y se
// manda a /resultado para que la prueba lo lea sin mirar la pantalla.
import { useEffect, useState } from "react";
import { Platform, ScrollView, Text } from "react-native";
import { trace } from "@opentelemetry/api";
import { iniciar, pedir } from "./telemetria";

const BASE = process.env.EXPO_PUBLIC_SERVIDOR ?? "http://localhost:8799";

async function probar() {
  const proveedor = iniciar(BASE);
  const res: Record<string, unknown> = { plataforma: Platform.OS };
  await trace.getTracer("app").startActiveSpan("enviar mensaje", async (raiz) => {
    const { r, enviadas, traza, tramo } = await pedir(`${BASE}/eco`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
    const recibidas = await r.json();
    Object.assign(res, { traza, tramo_peticion: tramo, raiz: raiz.spanContext().spanId, enviadas, recibidas, estado: r.status });
    raiz.end();
  });
  await proveedor.forceFlush();
  await fetch(`${BASE}/resultado`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(res) });
  return res;
}

export default function App() {
  const [salida, setSalida] = useState("probando…");
  useEffect(() => {
    probar().then((r) => setSalida(JSON.stringify(r, null, 1))).catch((e) => setSalida("ERROR " + String(e?.stack ?? e)));
  }, []);
  return (
    <ScrollView style={{ padding: 24, marginTop: 40 }}>
      <Text testID="salida" style={{ fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace", fontSize: 11 }}>{salida}</Text>
    </ScrollView>
  );
}
