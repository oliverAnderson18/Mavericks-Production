# Melt Alert — Calidad predictiva y optimización de producción

Melt Alert es una solución desarrollada para el hackathon Mavericks de Accenture que ayuda a reducir el desperdicio de chocolatinas y optimizar los paros de limpieza.

Combina modelos de machine learning, un panel operativo y un asistente de IA para convertir los datos de producción en recomendaciones que los operarios puedan revisar y aplicar.

## El problema

Una línea de producción puede perder valor por dos vías: fabricar producto defectuoso y permanecer parada más tiempo del necesario.

Detectar el rechazo al final de la línea permite medir el problema, pero deja poco margen para evitarlo. Melt Alert utiliza la información disponible durante el proceso para anticipar riesgos, orientar la revisión de máquinas y preparar mejor las tareas de limpieza.

## Nuestra solución

### Modelo A · Riesgo de rechazo

Estima la proporción de barritas que podrían terminar rechazadas para los colectivos que atraviesan cada estación.

Utiliza telemetría de la línea, desviaciones respecto a las consignas, medidas de calidad y tendencias recientes. Trabaja con 13 regresores HistGradientBoostingRegressor especializados por estación.

Sus recomendaciones son:

- **Continuar:** mantener la producción y la monitorización.
- **Vigilar:** revisar la evolución del riesgo.
- **Retener para revisión:** valorar una retención cuando el riesgo y el balance económico lo justifican.

### Modelo B · Diagnóstico de máquinas

Estima si una máquina presenta una incidencia activa compatible con los eventos históricos.

Utiliza 11 clasificadores HistGradientBoostingClassifier, uno por equipo. Cada uno analiza sus sensores, la diferencia respecto a las consignas y los cambios frente al minuto anterior.

Señala qué máquina conviene revisar primero. La alerta orienta el diagnóstico, pero no demuestra por sí sola la causa del rechazo.

### Modelo C · Prevención en la enrobadora

Anticipa el rechazo por altura en los seis carriles exteriores: 1–3 y 30–32.

Compara su comportamiento con los carriles centrales y utiliza medidas de los láseres, telemetría de la enrobadora y tendencias de 5, 15 y 30 minutos.

Un HistGradientBoostingRegressor compartido estima la tasa de rechazo futura entre los minutos t+6 y t+25. Cuando la tasa conjunta prevista alcanza el 10 %, puede solicitar una revisión de la enrobadora. Las solicitudes se espacian para evitar alertas repetitivas.

### Asistente de limpieza · SMED

El asistente permite consultar tareas, comparar planes y entender qué impide reiniciar antes la producción.

Aplica el enfoque SMED: preparar antes de la parada aquellas tareas que pueden realizarse con la línea en marcha, respetando dependencias, recursos y requisitos de ejecución.

El chatbot utiliza un LLM local mediante Ollama y consulta herramientas de planificación y cálculo. Las tareas, duraciones y equipos de esta demo son simulados.

## Cómo funciona

1. Integra datos de producción, sensores, calidad, incidencias y paradas.
2. Construye variables por minuto, respetando la información disponible en cada instante.
3. Ejecuta los tres modelos de forma complementaria.
4. Presenta riesgos, máquinas a revisar y recomendaciones.
5. Permite comparar escenarios de limpieza y estimar su impacto potencial.

No se sigue cada chocolatina individualmente: se trabaja con agregados por minuto, estación y carril, utilizando una alineación nominal del recorrido.

## Demo

La aplicación organiza la información en cinco vistas:

- **Live:** estado de la línea y alertas del minuto seleccionado.
- **Forecast:** predicciones de rechazo y puntuaciones de diagnóstico.
- **Troubleshooting:** comprobaciones recomendadas para revisar posibles incidencias.
- **History:** patrones históricos de rechazo y producción.
- **Cleaning:** planificación, seguimiento de tareas y asistente de limpieza.

La demo reproduce un histórico de producción y permite explorar escenarios de limpieza.

## Validación

Los datos se separan cronológicamente en entrenamiento, calibración, selección y test. Los modelos y umbrales se eligen antes de evaluar el bloque final.

Se utilizan métricas de error de tasa, precisión y cobertura según el objetivo de cada modelo. También se controla que las variables no incorporen información futura.

Los resultados corresponden a una evaluación temporal interna sobre el dataset del reto. Su aplicación en fábrica requiere validar sensores, tiempos de transporte y respuesta operativa.

## Evaluación económica

El proyecto distingue entre:

- **Retirada temprana:** balance simulado entre material posterior evitable, producto bueno retirado y costes de retirada.
- **Corrección de proceso:** escenarios condicionados a cuánto rechazo consigue evitar una intervención real.
- **Optimización de limpieza:** capacidad productiva potencial recuperada si las tareas, el personal, los materiales y la liberación de calidad permiten reiniciar antes.

Los ahorros combinados evitan contabilizar dos veces el mismo producto. El modelo B no recibe un ahorro independiente: ayuda a orientar la intervención.

Las estimaciones son escenarios de evaluación, no ahorros realizados ni garantizados.

## Salidas del pipeline

- `riesgo.csv`: tasa de rechazo prevista por minuto y estación, bandas y decisión.
- `diagnostico.csv`: puntuación por máquina y equipo señalado.
- `prevencion.csv`: tasa futura prevista por carril exterior y acción recomendada.
- `evaluation.json`: métricas de test y evaluación económica.

## Tecnologías

Python · pandas · NumPy · scikit-learn · HistGradientBoosting · Ollama · HTML · CSS · JavaScript

## Alcance

Melt Alert es una herramienta de apoyo a la decisión. Genera recomendaciones y simulaciones; la revisión del producto, los ajustes de máquina y el reinicio de producción requieren confirmación humana.

Desarrollado para el hackathon Mavericks de Accenture.
