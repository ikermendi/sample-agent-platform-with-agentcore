# Prueba de la app con assistant-ui (Expo)

Comprueba el punto "App con assistant-ui" de "Por verificar" del documento de arquitectura (`docs/arquitectura-agentes.md`): la lista de chats con `useRemoteThreadListRuntime` y un `useExternalStoreRuntime` por chat, con los mensajes, el SSE y la cola en un store propio, contra un backend simulado.

- `chats.ts`: el store de cada chat, fuera de React: historial (primero lo de la caché y después lo nuevo con `after=<seq>`), ejecución (`POST /chats/{id}/runs`), SSE con reconexión, eventos del kernel convertidos en partes de assistant-ui, aprobaciones y la cola (`createMessageQueue`, guardada en el dispositivo).
- `cache.ios.ts`: la caché de iOS, SQLite (`expo-sqlite`) en `Library/Caches`: la lista de chats, los mensajes de los chats abiertos en los últimos 30 días y la cola; `sincronizar` aplica la lista entera del backend y quita lo que ya no viene. `cache.ts` es la versión vacía de la web (todo en memoria; la cola, en `localStorage`).
- `sse.ts`: el cliente SSE con el `fetch` global, un parser propio y `Last-Event-ID`.
- `lista.ts`: el `RemoteThreadListAdapter` sobre `/chats`; `list()` pide la lista entera y la sincroniza con la caché.
- `App.tsx`: las dos pantallas (lista y chat) con las primitivas de `@assistant-ui/react-native`; en una pantalla estrecha son dos pantallas, en una ancha van lado a lado.
- `servidor.mjs`: el backend simulado (chats, ejecuciones con eventos guardados, SSE con cortes forzados, cancelar, aprobar); la ejecución depende del texto del mensaje ("aprobar", "largo"). Con `SEMBRAR_CHATS` y `SEMBRAR_MENSAJES` arranca con chats ya hechos.
- `prueba.mjs`: recorre la web con Chrome (Playwright) y comprueba cada caso con lo que la app manda a `/resultado` y lo que apunta el servidor.

Versiones fijadas: Expo SDK 58 (React Native 0.88), `@assistant-ui/react-native` 0.1.51 (`core` 0.3.32), `assistant-stream` 0.3.55, `expo-sqlite` 58.0.15, `expo-file-system` 58.0.13, `playwright-core` 1.64.1 (usa el Chrome instalado).

```bash
cd pruebas/app-chat
npm ci
npx tsc --noEmit
npx expo export --platform web --output-dir dist-web
npx expo export --platform ios --output-dir dist-ios      # empaqueta y compila a bytecode de Hermes
CORTE_MS=1500 APROBACION_MS=6000 REGISTRO=registro.jsonl ESTATICO=dist-web node servidor.mjs &
REGISTRO=registro.jsonl node prueba.mjs
npx expo run:ios     # en el simulador (necesita Xcode)

# caché de iOS: 500 chats, el primero con 500 mensajes; Release para medir sin Metro
SEMBRAR_CHATS=500 SEMBRAR_MENSAJES=500 REGISTRO=registro-cache-ios.jsonl node servidor.mjs &
LANG=en_US.UTF-8 npx expo run:ios --configuration Release --no-bundler
sqlite3 "$(xcrun simctl get_app_container booted com.anonymous.app data)/Library/Caches/SQLite/cache.db" 'select count(*) from chats'
```

Hecha en web (2-11-2026), 16 de 16:

- Reconexión: con el SSE cortado cada 1,5 s, 6 conexiones (5 con `Last-Event-ID`) y los 37 eventos de la ejecución en orden, sin huecos ni duplicados; el texto completo en pantalla.
- Lista de chats: crear, elegir y el título que pone el backend (`generateTitle` espera a que exista).
- Cola: dos mensajes escritos durante una ejecución se ven en la caja de texto y salen en orden al terminar, uno a uno y sin 409. Un envío con la ejecución en marcha va al carril `steer` del runtime; el driver no lleva `cancel`, así que no interrumpe.
- Cambio de chat con una ejecución en marcha: al volver, la respuesta sigue escribiéndose y llega completa. assistant-ui no desmontó el hook del primer chat; el store aguantaría igual si lo hiciera.
- Aprobaciones: `aprobacion_pendiente` como parte `approval`; aprobar y rechazar con `onRespondToToolApproval`; al caducar, la app pone `resolution: "expired"` con `caduca` (unos 150 ms antes de que llegue `fin` con `aprobacion_caducada`) y la tarjeta deja de ofrecer los botones.
- Parar: `fin` con `cancelado`; el runtime llama a `notifyCancelled` antes de `onCancel` y lo encolado no sale hasta el siguiente envío, que lo reactiva.
- Recargar la app con una ejecución en marcha y dos mensajes en cola: al abrir el chat, la ejecución se retoma desde el primer evento (el backend dice cuál está activa) y la cola guardada se vuelve a encolar y sale después.

En iOS (2-11-2026, simulador del iPhone 17 con iOS 27.2 y Xcode 27.2, `npx expo run:ios` con el backend en `localhost:8799`), a mano con el panel del simulador y el registro del servidor (`registro-ios.jsonl`):

- El `fetch` de `expo/fetch` entrega el SSE en streaming; con cortes cada 1,5 s, 3 conexiones (2 con `Last-Event-ID`) y los 15 eventos sin duplicados.
- Segundo plano: con la app en segundo plano a mitad de una ejecución no se abre ninguna conexión; al volver, 20 s después y con la ejecución ya terminada, reconecta una vez con `Last-Event-ID: 7` y recibe los 30 eventos que faltaban (37 en total, en orden).
- Aprobación: la tarjeta con aprobar y rechazar; aprobar termina en "Correo enviado."; si caduca, "expired" sin botones y `fin` con `aprobacion_caducada`.
- La cola se ve en la caja de texto ("⏳ tres" con Quitar) y sale al terminar; parar termina con `cancelado` y lo encolado se queda.
- Para escribir con el teclado del simulador, la caja de texto va sin autocorrección ni mayúsculas automáticas, y el backend no distingue mayúsculas en "aprobar" y "largo".

Caché de iOS (2-11-2026, mismo simulador, build Release, backend con 500 chats y `chat1` con 500 mensajes; registro en `registro-cache-ios.jsonl` y la base leída con `sqlite3` desde el contenedor de la app):

- Arranque con la caché vacía: `GET /chats` con los 500 chats, 53 KB, 29 ms de red; guardarlos en SQLite, 4,3 ms.
- Arranque con la caché llena: leer la lista guardada, 4,7 ms; sincronizar los 500, 3,7 ms.
- Abrir el chat de 500 mensajes sin caché: 99 ms de red y 4 ms en guardarlos. Con caché: 1,2 ms en leerlos de SQLite, en pantalla antes de pedir nada, y `after=<seq>` trae solo lo nuevo (0 o los 2 de una ejecución hecha entre medias).
- Borrados: con 50 chats borrados en el backend, el arranque siguiente quita esos 50 de la caché con sus mensajes (500 → 450, 4 ms).
- 30 días: los mensajes de un chat abierto hace 31 días se borran en la sincronización; el chat sigue en la lista.
- Cola: con `["uno","dos"]` guardado en la tabla `cola` de `chat1`, al abrir el chat la app los recupera y los envía en orden, uno tras otro; al vaciarse, la fila desaparece.
- La base va en `Library/Caches/SQLite/cache.db` (240 KB con 450 chats y 502 mensajes, más el WAL), que iOS no copia a iCloud.
- La web sigue pasando sus 16 casos con `cache.ts` vacío (la versión de SQLite para web necesita WebAssembly y cabeceras COOP/COEP; no se usa).

No cubre: un iPhone de verdad (segundo plano con el SSE abierto, redes móviles, `NSFileProtectionComplete`, que el simulador no aplica), el cambio de chat ni la recarga con una ejecución en marcha en iOS (el mismo código que en web), el dictado en iOS, que iOS vacíe `Library/Caches` con poco espacio (se perdería la cola pendiente) ni el CORS detrás de Akamai. La cola guardada se probó escribiéndola en la tabla, no tecleando en el simulador.
