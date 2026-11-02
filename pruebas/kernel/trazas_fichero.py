"""OpenTelemetry para los procesos de las pruebas de trazas: cada tramo terminado, una línea JSON en un fichero.

    configurar("hub", "/ruta/tramos.jsonl")
"""
import json

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


def tramo_a_dict(s, servicio):
    return {"servicio": servicio, "nombre": s.name, "tipo": s.kind.name,
            "traza": format(s.context.trace_id, "032x"), "tramo": format(s.context.span_id, "016x"),
            "padre": format(s.parent.span_id, "016x") if s.parent else None}


class AFichero(SpanExporter):
    def __init__(self, servicio, fichero):
        self.servicio, self.fichero = servicio, fichero

    def export(self, tramos):
        with open(self.fichero, "a") as f:
            for s in tramos:
                f.write(json.dumps(tramo_a_dict(s, self.servicio)) + "\n")
        return SpanExportResult.SUCCESS


def configurar(servicio, fichero):
    p = TracerProvider(resource=Resource.create({"service.name": servicio}))
    p.add_span_processor(SimpleSpanProcessor(AFichero(servicio, fichero)))
    trace.set_tracer_provider(p)
