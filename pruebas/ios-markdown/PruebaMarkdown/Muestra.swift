import Foundation

// Textos de la prueba: lo que devuelve un agente, con títulos, listas, tablas, código y citas.
enum Muestra {
  static func corta(_ i: Int) -> String {
    """
    ### Respuesta \(i)

    He revisado el **informe de ventas** y hay tres puntos. El primero es `Q3`, con un *crecimiento* del \(i % 9 + 2) %.

    - Norte: por encima del objetivo
    - Sur: por debajo, sobre todo en [septiembre](https://example.com)

    | Región | Ventas |
    |---|---|
    | Norte | \(1000 + i) |
    | Sur | \(800 + i) |
    """
  }

  private static let seccion = """
    ## Análisis por región

    El trimestre cierra con **resultados desiguales**: la región norte crece gracias a dos cuentas grandes, mientras que el sur pierde peso por el retraso de un contrato. Conviene leer estas cifras con *cuidado*, porque `septiembre` incluye ajustes de meses anteriores que no se repiten.

    1. Revisar las cuentas con más de 90 días sin actividad
    2. Separar los ajustes puntuales del crecimiento real
       - Por cliente
       - Por producto
    3. Preparar la previsión del próximo trimestre

    | Región | Ventas | Objetivo | Diferencia |
    |---|---:|---:|---:|
    | Norte | 1.240 | 1.100 | +12,7 % |
    | Sur | 860 | 1.000 | -14,0 % |
    | Este | 1.030 | 1.000 | +3,0 % |
    | Oeste | 940 | 950 | -1,1 % |

    > Los datos de septiembre son provisionales hasta el cierre contable.

    Para repetir el cálculo:

    ```python
    import pandas as pd

    ventas = pd.read_csv("ventas.csv")
    resumen = ventas.groupby("region")["importe"].sum()
    for region, importe in resumen.items():
        print(f"{region}: {importe:,.0f}")
    ```

    Más detalle en [el informe](https://example.com/informe) y en el anexo de cuentas.


    """

  static let larga = String(repeating: seccion, count: Int(ProcessInfo.processInfo.environment["SECCIONES"] ?? "") ?? 6)
}
