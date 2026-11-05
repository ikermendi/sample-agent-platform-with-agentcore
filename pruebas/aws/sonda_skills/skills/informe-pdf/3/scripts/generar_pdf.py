"""Markdown del espacio del chat → PDF en salida/. Solo biblioteca estándar (un PDF de texto, Helvetica).

Entrada en stdin: {"origen": "trabajo/informe.md", "titulo": "..."}. Salida en stdout: JSON con el fichero.
"""
from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from pathlib import Path

LINEAS_POR_PAGINA = 48


def lineas(md: str, titulo: str | None):
    if titulo:
        yield ("F2", 16, titulo)
        yield ("F1", 11, "")
    for l in md.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", l)
        if m:
            yield ("F2", {1: 15, 2: 13, 3: 12}[len(m.group(1))], m.group(2))
            continue
        l = re.sub(r"\*\*(.+?)\*\*", r"\1", l)
        sangria = "  " if l.lstrip().startswith(("-", "*")) else ""
        for trozo in textwrap.wrap(l.replace("\t", "  "), 90) or [""]:
            yield ("F1", 11, sangria + trozo if sangria and not trozo.lstrip().startswith(("-", "*")) else trozo)


def esc(t: str) -> bytes:
    return t.encode("cp1252", "replace").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def pdf(paginas: list[list[tuple]]) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    hijos = []
    for i, pag in enumerate(paginas):
        y, flujo = 800, [b"BT"]
        for fuente, tam, texto in pag:
            flujo.append(b"/%s %d Tf 1 0 0 1 50 %d Tm (%s) Tj" % (fuente.encode(), tam, y, esc(texto)))
            y -= tam + 5
        flujo.append(b"ET")
        cuerpo = b"\n".join(flujo)
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cuerpo), cuerpo))
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % len(objs))
        hijos.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % h for h in hijos), len(hijos))
    salida, pos = bytearray(b"%PDF-1.4\n"), []
    for n, o in enumerate(objs, 1):
        pos.append(len(salida))
        salida += b"%d 0 obj\n%s\nendobj\n" % (n, o)
    xref = len(salida)
    salida += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    salida += b"".join(b"%010d 00000 n \n" % p for p in pos)
    salida += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(salida)


def main():
    args = json.load(sys.stdin)
    origen = Path(args["origen"])
    if origen.is_absolute() or ".." in origen.parts or not origen.is_file():
        sys.exit(f"origen no válido: {origen}")
    todas = list(lineas(origen.read_text(), args.get("titulo")))
    paginas = [todas[i:i + LINEAS_POR_PAGINA] for i in range(0, len(todas), LINEAS_POR_PAGINA)] or [[]]
    Path("salida").mkdir(exist_ok=True)
    destino = Path("salida") / (origen.stem + ".pdf")
    destino.write_bytes(pdf(paginas))
    json.dump({"nombre_fichero": destino.name, "ruta": destino.as_posix(), "paginas": len(paginas),
               "bytes": destino.stat().st_size, "variables_aws": sum(k.startswith("AWS_") for k in os.environ)},
              sys.stdout)


if __name__ == "__main__":
    main()
