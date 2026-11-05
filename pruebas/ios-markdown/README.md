# Prueba del Markdown en streaming en SwiftUI

Mide cómo se pinta una respuesta larga que llega a trozos, con un historial de 20 mensajes detrás, en `ScrollView` con `LazyVStack`. Llega como en el diseño, 120 caracteres cada 250 ms, y se revela a ritmo constante en el cliente, cada `TICK_MS`. Compara MarkdownUI 2.4.1 y Textual 0.5.0, con el mensaje entero o partido en bloques (`.equatable()` en los terminados), y `Text` plano como control.

Es un simulador sobre un Mac, en Release: sirve para comparar variantes, no como medida de un iPhone real.

```bash
xcodebuild -project PruebaMarkdown.xcodeproj -scheme PruebaMarkdown -configuration Release -sdk iphonesimulator \
  -destination 'platform=iOS Simulator,id=<udid>' -derivedDataPath build build
./medir.sh <udid>
```

`resultados.jsonl` tiene una línea por caso y `capturas/` el final de cada uno. Las variables de entorno (`MODO`, `TICK_MS`, `SECCIONES`, `INTERVALO_MS`, `TROZO`, `HISTORIAL`) están en `App.swift`.

Frames de más de 25 ms sobre el total (la respuesta visible en todos los casos):

| Variante | Revelado | 6.500 caracteres | 19.500 caracteres |
|---|---|---|---|
| Texto plano | 33 ms | 0 de 1.030 | |
| MarkdownUI, entero | 33 ms | 1-2 de 1.010 | |
| MarkdownUI, bloques | 33 ms | 0 de 1.015 (peor frame, 17 ms) | 196-263 de 2.450 (peor, 45 ms) |
| MarkdownUI, bloques | 66 ms | 13-16 de 1.010 | |
| Textual, bloques | 33 ms | 65-73 de 940 (peor, 68 ms) | |
| Textual, bloques | 66 ms | 127-128 de 860 (peor, 78 ms) | |

`resultados-saltos.jsonl` es la medición anterior, sin revelado: el texto aparece a golpes cada 250 ms.
