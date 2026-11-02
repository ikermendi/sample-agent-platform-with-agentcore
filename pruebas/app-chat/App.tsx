// Prototipo de la app con assistant-ui: lista de chats (`useRemoteThreadListRuntime`) y chat
// (`useExternalStoreRuntime` por chat, con los mensajes en nuestro store de `chats.ts`), contra el backend
// simulado de `servidor.mjs`.
import { useEffect, useState, useSyncExternalStore } from "react";
import { Platform, Pressable, Text, View, useWindowDimensions } from "react-native";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  QueueItemPrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useExternalStoreRuntime,
  useRemoteThreadListRuntime,
  type ThreadMessageLike,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react-native";
import { informa, obtenerChat } from "./chats";
import { listaDeChats } from "./lista";

let montajes = 0;

// El runtime de un chat. assistant-ui puede volver a montar este hook (al cambiar de chat); el estado vive
// en el `Chat` del store, no aquí.
function useChatRuntime() {
  const aui = useAui();
  const id = useAuiState((s) => s.threadListItem.id);
  const remoteId = useAuiState((s) => s.threadListItem.remoteId);
  const chat = obtenerChat(id, remoteId);
  chat.inicializar = () => aui.threadListItem().initialize();
  useEffect(() => {
    const n = ++montajes;
    informa({ tipo: "hook_montado", id, remoteId, n });
    return () => void informa({ tipo: "hook_desmontado", id, remoteId, n });
  }, [id]);
  useEffect(() => {
    if (remoteId) chat.abrir(remoteId);
  }, [chat, remoteId]);
  const estado = useSyncExternalStore(chat.suscribir, chat.leer);
  return useExternalStoreRuntime<ThreadMessageLike>({
    messages: estado.mensajes,
    convertMessage: (m) => m,
    isRunning: estado.ejecutando,
    onNew: (m) => chat.enviar(m),
    onCancel: () => chat.cancelar(),
    queue: chat.cola.adapter,
    onRespondToToolApproval: (o) => chat.responder(o),
  });
}

const boton = { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 6, backgroundColor: "#e8e8ee", marginLeft: 6 };

function Herramienta(p: ToolCallMessagePartProps) {
  const a = p.approval;
  const pendiente = a && a.approved === undefined && !a.resolution;
  const estado = a?.resolution ?? (a?.approved === true ? "aprobada" : a?.approved === false ? "rechazada" : undefined);
  return (
    <View testID={`herramienta-${p.toolName}`} style={{ borderWidth: 1, borderColor: "#ccd", borderRadius: 8, padding: 8, marginVertical: 4 }}>
      <Text style={{ fontWeight: "600" }}>
        {(p.args as any)?.etiqueta ?? p.toolName} · {p.result === undefined ? "en curso" : p.isError ? "error" : "hecha"}
      </Text>
      <Text style={{ fontSize: 12, color: "#555" }}>{(p.args as any)?.resumen}</Text>
      {p.result !== undefined && <Text style={{ fontSize: 12 }}>{(p.result as any)?.resumen}</Text>}
      {a && (
        <View testID="aprobacion" style={{ marginTop: 6 }}>
          <Text testID="aprobacion-estado">{pendiente ? `¿Aprobar? ${a.prompt}` : `aprobación: ${estado}`}</Text>
          {pendiente && (
            <View style={{ flexDirection: "row", marginTop: 6 }}>
              <Pressable testID="aprobar" style={boton} onPress={() => p.respondToApproval({ approved: true })}><Text>Aprobar</Text></Pressable>
              <Pressable testID="rechazar" style={boton} onPress={() => p.respondToApproval({ approved: false })}><Text>Rechazar</Text></Pressable>
            </View>
          )}
        </View>
      )}
    </View>
  );
}

function Mensaje() {
  const rol = useAuiState((s) => s.message.role);
  const estado = useAuiState((s) => s.message.status?.type);
  return (
    <MessagePrimitive.Root style={{ alignSelf: rol === "user" ? "flex-end" : "stretch", maxWidth: "85%", marginVertical: 4 }}>
      <View testID={`mensaje-${rol}`} style={{ backgroundColor: rol === "user" ? "#dbe8ff" : "#f4f4f6", borderRadius: 10, padding: 10 }}>
        <MessagePrimitive.Parts components={{ Text: () => <MessagePartPrimitive.Text />, tools: { Fallback: Herramienta } }} />
        {rol === "assistant" && estado !== "complete" && <Text style={{ fontSize: 11, color: "#888" }}>{estado}</Text>}
      </View>
    </MessagePrimitive.Root>
  );
}

function Cola() {
  return (
    <ComposerPrimitive.Queue>
      {() => (
        <View testID="cola-item" style={{ flexDirection: "row", alignItems: "center", paddingVertical: 2 }}>
          <Text style={{ flex: 1, color: "#666" }}>⏳ <QueueItemPrimitive.Text /></Text>
          <QueueItemPrimitive.Remove testID="cola-quitar" style={boton}><Text>Quitar</Text></QueueItemPrimitive.Remove>
        </View>
      )}
    </ComposerPrimitive.Queue>
  );
}

function PantallaChat() {
  const remoteId = useAuiState((s) => s.threadListItem.remoteId);
  const titulo = useAuiState((s) => s.threadListItem.title);
  return (
    <ThreadPrimitive.Root style={{ flex: 1 }}>
      <Text testID="chat-titulo" style={{ fontWeight: "700", padding: 8 }}>{titulo ?? "Nuevo chat"} {remoteId ? `(${remoteId})` : ""}</Text>
      <ThreadPrimitive.MessagesFlatList style={{ flex: 1, paddingHorizontal: 8 }}>{() => <Mensaje />}</ThreadPrimitive.MessagesFlatList>
      <ComposerPrimitive.Root style={{ padding: 8, borderTopWidth: 1, borderColor: "#ddd" }}>
        <Cola />
        <View style={{ flexDirection: "row", alignItems: "center" }}>
          <ComposerPrimitive.Input testID="entrada" placeholder="Escribe un mensaje" autoCorrect={false} autoCapitalize="none" style={{ flex: 1, borderWidth: 1, borderColor: "#ccc", borderRadius: 6, padding: 8 }} />
          <ComposerPrimitive.Send testID="enviar" style={boton}><Text>Enviar</Text></ComposerPrimitive.Send>
          <ThreadPrimitive.If running>
            <ComposerPrimitive.Cancel testID="parar" style={boton}><Text>Parar</Text></ComposerPrimitive.Cancel>
          </ThreadPrimitive.If>
        </View>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}

function PantallaLista() {
  return (
    <ThreadListPrimitive.Root style={{ width: 240, borderRightWidth: 1, borderColor: "#ddd", padding: 8 }}>
      <ThreadListPrimitive.New testID="nuevo-chat" style={boton}><Text>+ Nuevo chat</Text></ThreadListPrimitive.New>
      <ThreadListPrimitive.Items
        renderItem={({ threadId }) => (
          <ThreadListItemPrimitive.Root>
            <ThreadListItemPrimitive.Trigger testID={`chat-${threadId}`} style={{ paddingVertical: 8 }}>
              <Text><ThreadListItemPrimitive.Title fallback="(sin título)" /></Text>
            </ThreadListItemPrimitive.Trigger>
          </ThreadListItemPrimitive.Root>
        )}
      />
    </ThreadListPrimitive.Root>
  );
}

// En pantallas estrechas (iPhone), la lista y el chat son dos pantallas; al elegir un chat se pasa al chat.
function Pantallas() {
  const ancho = useWindowDimensions().width >= 700;
  const [verLista, setVerLista] = useState(true);
  const principal = useAuiState((s) => s.threads.mainThreadId);
  useEffect(() => setVerLista(false), [principal]);
  return (
    <View style={{ flex: 1, flexDirection: "row", marginTop: Platform.OS === "ios" ? 50 : 0 }}>
      {(ancho || verLista) && <PantallaLista />}
      {(ancho || !verLista) && (
        <View style={{ flex: 1 }}>
          {!ancho && <Pressable onPress={() => setVerLista(true)} style={boton}><Text>‹ Chats</Text></Pressable>}
          <PantallaChat />
        </View>
      )}
    </View>
  );
}

export default function App() {
  const runtime = useRemoteThreadListRuntime({ runtimeHook: useChatRuntime, adapter: listaDeChats });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Pantallas />
    </AssistantRuntimeProvider>
  );
}
