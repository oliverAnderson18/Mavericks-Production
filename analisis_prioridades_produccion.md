# Instrucciones del modelo y funcionamiento del pipeline

## Objetivo

El sistema analiza la línea completa de producción de barritas para estimar si el producto que está en proceso llegará a generar scrap y, cuando es posible, señalar qué estación presenta la señal más anómala. La decisión se toma por lote y ventana temporal, porque los datos disponibles no permiten seguir físicamente cada barrita de forma individual.

## Datos utilizados

- Telemetría de las máquinas: temperaturas, presiones, caudales, velocidades, humedad y estados.
- Producción: lote, producto, turno, velocidad y minuto de producción.
- Calidad: medidas del láser y eventos de visión.
- Scrap y OEE: cantidad rechazada, coste, paradas y motivo registrado.
- Incidencias: inicio, fin, tipo y máquina asociada cuando existe.

El modelo usa solo variables disponibles hasta el momento de la predicción. Las variables de una máquina futura se incorporan cuando la barrita llega a esa estación; no se utiliza información del resultado final que todavía no estaría disponible en una operación real.

## Pipeline paso a paso

1. **Carga y validación.** Se leen los ficheros de datos, se normalizan nombres y fechas, y se comprueba que no falten columnas esenciales.
2. **Construcción de ventanas.** Se agrupan los registros por minuto, lote y estación. Se calculan valores actuales, medias, cambios y tendencias recientes.
3. **Etiquetado.** Cada ventana recibe una etiqueta de scrap futuro si aparece rechazo en el horizonte definido después de esa ventana. También se conserva el coste económico del rechazo.
4. **Modelo A: riesgo de scrap.** Un clasificador binario estima P(scrap) para la siguiente ventana. Se calibra el umbral con validación temporal para priorizar el coste de los falsos negativos y evitar alertas excesivas.
5. **Modelo B: diagnóstico por estación.** Se entrena un clasificador binario independiente por estación. Cada uno estima si su máquina es la fuente más probable de la incidencia o del rechazo observado, usando sus señales y el contexto de la ruta.
6. **Reglas de decisión.**
   - **Continuar:** riesgo bajo; se mantiene la producción.
   - **Seguir con vigilancia:** riesgo intermedio; se aumenta la frecuencia de control y se revisan las variables principales.
   - **Retener o descartar:** riesgo alto y coste esperado superior al coste de inspección o reproceso.
7. **Cálculo económico.** Se comparan los rechazos que el modelo habría evitado con los rechazos reales del conjunto de validación. El ahorro se calcula como coste de scrap evitado menos el coste de las intervenciones; nunca se presenta como ahorro el simple número de alertas.
8. **Salida única para la demo.** El pipeline genera únicamente output/demo.json y output/evaluation.json. El primero contiene el estado de la ruta, riesgo, decisión y estación sospechosa; el segundo resume detecciones, falsos positivos, falsos negativos, coste y ahorro estimado.

## Interpretación para la demo

La simulación mueve una ventana de producción por las 15 estaciones de la ruta. En cada paso muestra el riesgo actualizado y, si el Modelo B supera el umbral de una estación, la marca en rojo como estación sospechosa. Esto indica dónde investigar primero; no demuestra por sí solo causalidad ni garantiza que una intervención evite el rechazo.

## Validación y límites

La evaluación debe hacerse separando los últimos lotes o días, sin mezclar futuro y pasado. Hay que informar de precisión, cobertura, falsas alarmas, coste evitado y coste de intervención. Las cifras históricas son una simulación basada en los datos disponibles; para prometer ahorro en planta se necesita una prueba piloto con intervención registrada y comparación contra un periodo de control.

## Ejecución

Desde la carpeta del proyecto:

    python -m scrap_ml.compact_pipeline run --data-dir .\data

Los modelos y resultados quedan en scrap_ml/artifacts_improved, scrap_ml/artifacts_machine_system y output.


## Instalar dependencias y ejecutar el pipeline

Desde la carpeta raíz del proyecto (`Mavericks`), instala todos los módulos necesarios con:

```powershell
pip install -r requirements.txt
```

El archivo `requirements.txt` incluye `numpy`, `pandas`, `scikit-learn`, `scipy`, `joblib`, `threadpoolctl`, `cloudpickle` y `narwhals`.

Después ejecuta el pipeline completo:

```powershell
python -m scrap_ml.compact_pipeline run --data-dir .\data
```

El comando entrena los modelos, evalúa el conjunto de prueba y genera únicamente `output/demo.json` y `output/evaluation.json`.
