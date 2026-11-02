// Trazas de la app: OpenTelemetry sin instrumentación automática, ids en formato X-Ray, las dos cabeceras
// (`traceparent` y `X-Amzn-Trace-Id`) en cada petición y un exportador propio que envía OTLP JSON con fetch
// a `POST /telemetry`. Es lo que el documento de arquitectura describe para la app (Expo, iOS y web).
import { context, propagation, trace, SpanKind, type Span } from "@opentelemetry/api";
import { CompositePropagator, W3CTraceContextPropagator, ExportResultCode, type ExportResult } from "@opentelemetry/core";
import { AWSXRayIdGenerator } from "@opentelemetry/id-generator-aws-xray";
import { AWSXRayPropagator } from "@opentelemetry/propagator-aws-xray";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  SimpleSpanProcessor, StackContextManager, TracerProvider, type ReadableSpan, type SpanExporter,
} from "@opentelemetry/sdk-trace";

const nanos = (t: [number, number]) => `${t[0]}${String(t[1]).padStart(9, "0")}`;
const valor = (v: unknown) =>
  typeof v === "number" ? (Number.isInteger(v) ? { intValue: String(v) } : { doubleValue: v })
  : typeof v === "boolean" ? { boolValue: v } : { stringValue: String(v) };

class ExportadorOtlpFetch implements SpanExporter {
  constructor(private url: string) {}

  export(tramos: ReadableSpan[], fin: (r: ExportResult) => void) {
    const porRecurso = {
      resource: { attributes: Object.entries(tramos[0]?.resource.attributes ?? {}).map(([key, v]) => ({ key, value: valor(v) })) },
      scopeSpans: [{
        scope: { name: "app" },
        spans: tramos.map((s) => ({
          traceId: s.spanContext().traceId,
          spanId: s.spanContext().spanId,
          parentSpanId: s.parentSpanContext?.spanId,
          name: s.name,
          kind: s.kind + 1, // OTLP: 0 sin especificar, 1 INTERNAL… ; la API: 0 INTERNAL…
          startTimeUnixNano: nanos(s.startTime),
          endTimeUnixNano: nanos(s.endTime),
          attributes: Object.entries(s.attributes).map(([key, v]) => ({ key, value: valor(v) })),
          status: { code: s.status.code },
        })),
      }],
    };
    // la propia exportación no se traza: fetch directo, sin contexto
    fetch(this.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resourceSpans: [porRecurso] }) })
      .then((r) => fin({ code: r.ok ? ExportResultCode.SUCCESS : ExportResultCode.FAILED }))
      .catch((error) => fin({ code: ExportResultCode.FAILED, error }));
  }

  shutdown() {
    return Promise.resolve();
  }
}

export function iniciar(base: string) {
  const proveedor = new TracerProvider({
    resource: resourceFromAttributes({ "service.name": "app" }),
    idGenerator: new AWSXRayIdGenerator(),
    spanProcessors: [new SimpleSpanProcessor({ exporter: new ExportadorOtlpFetch(`${base}/telemetry`) })],
  });
  trace.setGlobalTracerProvider(proveedor);
  context.setGlobalContextManager(new StackContextManager().enable());
  // el orden importa poco aquí: la app solo inyecta, y las dos cabeceras salen del mismo tramo
  propagation.setGlobalPropagator(new CompositePropagator({
    propagators: [new W3CTraceContextPropagator(), new AWSXRayPropagator()],
  }));
  return proveedor;
}

// fetch con un tramo CLIENT y las dos cabeceras de contexto
export async function pedir(url: string, init: RequestInit = {}) {
  const tr = trace.getTracer("app");
  return tr.startActiveSpan(`HTTP ${init.method ?? "GET"}`, { kind: SpanKind.CLIENT }, async (s: Span) => {
    const cab: Record<string, string> = { ...(init.headers as Record<string, string>) };
    propagation.inject(context.active(), cab);
    try {
      const r = await fetch(url, { ...init, headers: cab });
      s.setAttribute("http.response.status_code", r.status);
      return { r, enviadas: cab, traza: s.spanContext().traceId, tramo: s.spanContext().spanId };
    } finally {
      s.end();
    }
  });
}
