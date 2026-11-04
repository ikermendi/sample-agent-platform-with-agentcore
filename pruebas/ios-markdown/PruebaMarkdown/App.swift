// Prueba del Markdown en streaming en SwiftUI: repite un chat real (historial más una respuesta larga que llega
// a trozos) con cada forma de pintarlo y mide los frames perdidos. Se configura con variables de entorno.
import Foundation
import MarkdownUI
import QuartzCore
import SwiftUI
import Textual

enum Modo: String { case plano, markdownui, markdownuiBloques, textual, textualBloques }

struct Ajustes {
  let modo = Modo(rawValue: ProcessInfo.processInfo.environment["MODO"] ?? "") ?? .textualBloques
  let intervaloMs = Int(ProcessInfo.processInfo.environment["INTERVALO_MS"] ?? "") ?? 250
  let historial = Int(ProcessInfo.processInfo.environment["HISTORIAL"] ?? "") ?? 20
  let repeticiones = Int(ProcessInfo.processInfo.environment["REPETICIONES"] ?? "") ?? 1
  let trozo = Int(ProcessInfo.processInfo.environment["TROZO"] ?? "") ?? 120
  let tickMs = Int(ProcessInfo.processInfo.environment["TICK_MS"] ?? "") ?? 33
  let salir = ProcessInfo.processInfo.environment["SALIR"] == "1"
}

// Cuenta los frames y los que se pasan del presupuesto, sin decidir nada de la interfaz.
@MainActor final class Medidor: NSObject {
  private var enlace: CADisplayLink?
  private var anterior: CFTimeInterval = 0
  private(set) var duraciones: [Double] = []

  func empezar() {
    duraciones.removeAll()
    anterior = 0
    enlace = CADisplayLink(target: self, selector: #selector(frame(_:)))
    enlace?.add(to: .main, forMode: .common)
  }

  @objc private func frame(_ enlace: CADisplayLink) {
    if anterior > 0 { duraciones.append((enlace.timestamp - anterior) * 1000) }
    anterior = enlace.timestamp
  }

  func parar() { enlace?.invalidate(); enlace = nil }
}

func memoriaMB() -> Double {
  var info = task_vm_info_data_t()
  var n = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
  let r = withUnsafeMutablePointer(to: &info) {
    $0.withMemoryRebound(to: integer_t.self, capacity: Int(n)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &n) }
  }
  return r == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576 : 0
}

@Observable @MainActor final class Chat {
  var mensajes: [Mensaje] = []
  var enCurso = ""
  var terminado = false
  @ObservationIgnored let ajustes = Ajustes()
  @ObservationIgnored let medidor = Medidor()
  @ObservationIgnored var altura: CGFloat = 0
  @ObservationIgnored var ultimoCambioAltura: CFTimeInterval = 0
  @ObservationIgnored var visible = false
  @ObservationIgnored var recibido: [Character] = []
  @ObservationIgnored var mostrado = 0

  init() {
    mensajes = (0..<ajustes.historial).map { Mensaje(id: $0, texto: Muestra.corta($0)) }
  }

  // Enseña lo recibido a ritmo constante: en cada tick, lo que falta repartido en un intervalo de llegada, para
  // ir siempre a la velocidad del modelo y no quedarse atrás. Termina cuando lo ha enseñado todo.
  func revelar() async {
    let total = Muestra.larga.count
    while mostrado < total {
      let falta = recibido.count - mostrado
      if falta > 0 {
        let paso = max(1, falta * ajustes.tickMs / ajustes.intervaloMs)
        enCurso += String(recibido[mostrado..<(mostrado + paso)])
        mostrado += paso
      }
      try? await Task.sleep(for: .milliseconds(ajustes.tickMs))
    }
  }

  func ejecutar() async {
    try? await Task.sleep(for: .seconds(2))
    var resultados: [[String: Any]] = []
    for _ in 0..<ajustes.repeticiones {
      enCurso = ""
      recibido = []
      mostrado = 0
      try? await Task.sleep(for: .milliseconds(500))
      medidor.empezar()
      let inicio = CACurrentMediaTime()
      var resto = Substring(Muestra.larga)
      var trozos = 0
      Cuerpos.n = 0
      let revelado = Task { await revelar() }
      while !resto.isEmpty {
        recibido += resto.prefix(ajustes.trozo)
        resto = resto.dropFirst(ajustes.trozo)
        trozos += 1
        try? await Task.sleep(for: .milliseconds(ajustes.intervaloMs))
      }
      await revelado.value
      let finTrozos = CACurrentMediaTime()
      try? await Task.sleep(for: .seconds(2))
      medidor.parar()
      let d = medidor.duraciones
      let largos = d.filter { $0 > 25 }
      let ordenadas = d.sorted()
      resultados.append([
        "trozos": trozos, "segundos": CACurrentMediaTime() - inicio, "frames": d.count,
        "frames_perdidos": largos.count, "porcentaje": 100 * Double(largos.count) / Double(max(d.count, 1)),
        "peor_ms": d.max() ?? 0, "p99_ms": ordenadas.isEmpty ? 0 : ordenadas[ordenadas.count * 99 / 100],
        "bloqueo_ms": largos.reduce(0, +), "memoria_mb": memoriaMB(),
        "altura": altura, "visible": visible, "cuerpos": Cuerpos.n,
        "retraso_ms": (ultimoCambioAltura - finTrozos) * 1000,
      ])
    }
    let salida: [String: Any] = [
      "modo": ajustes.modo.rawValue, "intervalo_ms": ajustes.intervaloMs, "tick_ms": ajustes.tickMs, "historial": ajustes.historial,
      "caracteres": Muestra.larga.count, "repeticiones": resultados,
    ]
    let datos = try! JSONSerialization.data(withJSONObject: salida, options: [.sortedKeys])
    let ruta = URL.documentsDirectory.appending(path: "resultado.json")
    try! datos.write(to: ruta)
    terminado = true
    if ajustes.salir {
      try? await Task.sleep(for: .seconds(3))
      exit(0)
    }
  }
}

// Cuántas veces se evalúa el cuerpo de un bloque: dice si un bloque terminado se vuelve a pintar.
@MainActor enum Cuerpos { static var n = 0 }

struct Mensaje: Identifiable { let id: Int; let texto: String }

// Parte el texto en bloques por líneas en blanco, sin cortar bloques de código. El último puede estar a medias.
func bloques(_ texto: String) -> [String] {
  var fuera: [String] = []
  var actual = ""
  var enCodigo = false
  for linea in texto.split(separator: "\n", omittingEmptySubsequences: false) {
    if linea.hasPrefix("```") { enCodigo.toggle() }
    if linea.isEmpty && !enCodigo {
      if !actual.isEmpty { fuera.append(actual); actual = "" }
    } else {
      actual += actual.isEmpty ? String(linea) : "\n" + linea
    }
  }
  if !actual.isEmpty { fuera.append(actual) }
  return fuera
}

// Un bloque terminado no cambia: `Equatable` evita volver a evaluar su cuerpo en cada trozo.
struct Bloque: View, Equatable {
  let texto: String
  let modo: Modo

  var body: some View {
    let _ = Cuerpos.n += 1
    switch modo {
    case .markdownui, .markdownuiBloques: Markdown(texto)
    case .textual, .textualBloques: StructuredText(markdown: texto)
    case .plano: Text(texto)
    }
  }
}

struct Respuesta: View {
  let texto: String
  let modo: Modo

  var body: some View {
    switch modo {
    case .markdownui, .textual, .plano:
      Bloque(texto: texto, modo: modo)
    case .markdownuiBloques, .textualBloques:
      let partes = bloques(texto)
      VStack(alignment: .leading, spacing: 12) {
        ForEach(Array(partes.enumerated()), id: \.offset) { _, parte in
          Bloque(texto: parte, modo: modo).equatable()
        }
      }
    }
  }
}

struct Pantalla: View {
  @State private var chat = Chat()

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 16) {
        ForEach(chat.mensajes) { m in
          Bloque(texto: m.texto, modo: chat.ajustes.modo).equatable().id(m.id)
        }
        Respuesta(texto: chat.enCurso, modo: chat.ajustes.modo)
          .id(-1)
          .onGeometryChange(for: CGFloat.self, of: \.size.height) { h in
            if h != chat.altura { chat.altura = h; chat.ultimoCambioAltura = CACurrentMediaTime() }
          }
          .onScrollVisibilityChange(threshold: 0.01) { chat.visible = $0 }
      }
      .padding()
    }
    .defaultScrollAnchor(.bottom)
    .defaultScrollAnchor(.bottom, for: .sizeChanges)
    .textual.textSelection(.enabled)
    .task { await chat.ejecutar() }
  }
}

@main struct PruebaApp: App {
  var body: some Scene { WindowGroup { Pantalla() } }
}
