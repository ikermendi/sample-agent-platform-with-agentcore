#!/bin/zsh
# Repite el streaming con revelado suave con cada forma de pintar el Markdown y cada ritmo, siempre con la app recién
# arrancada. Deja una línea JSON por caso en resultados.jsonl y una captura en capturas/. Uso: ./medir.sh <udid>
UDID=$1
APP=build/Build/Products/Release-iphonesimulator/PruebaMarkdown.app
ID=com.prueba.markdown
xcrun simctl install $UDID $APP
: > resultados.jsonl
mkdir -p capturas
caso() {
  for VEZ in 1 2; do
    RUTA="$(xcrun simctl get_app_container $UDID $ID data)/Documents/resultado.json"
    rm -f "$RUTA"
    SIMCTL_CHILD_MODO=$1 SIMCTL_CHILD_TICK_MS=$2 SIMCTL_CHILD_SECCIONES=$3 SIMCTL_CHILD_SALIR=1 \
      xcrun simctl launch --terminate-running-process $UDID $ID >/dev/null
    for i in {1..150}; do [[ -f "$RUTA" ]] && break; sleep 1; done
    sleep 1
    xcrun simctl io $UDID screenshot capturas/$1-$2-$3.png >/dev/null 2>&1
    if [[ -f "$RUTA" ]]; then cat "$RUTA" >> resultados.jsonl; echo >> resultados.jsonl
    else echo "{\"modo\":\"$1\",\"tick_ms\":$2,\"secciones\":$3,\"fallo\":true}" >> resultados.jsonl; fi
    sleep 3
  done
}
caso plano 33 6
for TICK in 33 66; do
  for MODO in markdownuiBloques textualBloques; do caso $MODO $TICK 6; done
done
caso markdownui 33 6
caso markdownuiBloques 33 18
