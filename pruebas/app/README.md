# Prueba de trazas de la app (Expo)

Comprueba lo que la sección "End-to-end tracing" del documento de arquitectura (`docs/arquitectura-agentes.md`) dice de la app: OpenTelemetry sin instrumentación automática, ids en formato X-Ray, `traceparent` en cada petición y un exportador propio que manda OTLP JSON con `fetch` a `POST /telemetry`.

- `telemetria.ts`: el módulo de trazas tal como lo tendría la app.
- `App.tsx`: abre un tramo "enviar mensaje", hace una petición hija a `/eco` y exporta los tramos; el resultado sale en pantalla y en `POST /resultado`.
- `servidor.mjs`: `/eco` devuelve las cabeceras que recibe, `/telemetry` y `/resultado` las apuntan; CORS solo con las cabeceras de `CORS_CABECERAS`, como lo tendría API Gateway. Con `ESTATICO=dist-web` sirve la exportación web en otro origen (puerto 8800).

Versiones fijadas: Expo SDK 58 (React Native 0.88), `@opentelemetry/sdk-trace` 3.0.0 (sustituye a `sdk-trace-base`, deprecado), `core` y `resources` 3.0.0, `id-generator-aws-xray` 3.0.0, `api` 1.10.0.

```bash
cd pruebas/app
npm ci
npx tsc --noEmit
npx expo export --platform web --output-dir dist-web
npx expo export --platform ios --output-dir dist-ios      # empaqueta y compila a bytecode de Hermes
REGISTRO=registro.jsonl ESTATICO=dist-web node servidor.mjs   # y abrir http://localhost:8800
CORS_CABECERAS=content-type REGISTRO=registro-cors.jsonl ESTATICO=dist-web node servidor.mjs   # CORS sin las cabeceras de traza
npx expo run:ios     # en el simulador (necesita Xcode)
```

Hecha (2-11-2026):

- Metro resuelve `exports` e `imports` (`#platform`) de los paquetes 3.x sin configurar nada y elige la variante de navegador, en web y en iOS; el paquete de iOS compila a bytecode de Hermes.
- En web (Chromium, la página en `localhost:8800` y el servidor en `localhost:8799`): un id de traza en formato X-Ray (`6ae87189…`, la hora delante), `traceparent` con el tramo CLIENT de la petición como padre y muestreado (`-01`); el preflight pide solo `content-type,traceparent`; los dos tramos llegan a `/telemetry` en la misma traza, la petición colgando de "enviar mensaje"; la exportación sale sin cabeceras de contexto.
- Con un CORS que no permite `traceparent`, el navegador no envía la petición (`Failed to fetch` tras el preflight): API Gateway tiene que listarla en `Access-Control-Allow-Headers`.
- iOS en el simulador: sin ejecutar (no hay Xcode en la máquina de la prueba).
