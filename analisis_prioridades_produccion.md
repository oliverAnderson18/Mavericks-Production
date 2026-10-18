# Calidad predictiva: instrucciones y pipeline

Desde la raíz de `Mavericks-Production`, instala las dependencias y ejecuta:

```powershell
python -m pip install -r requirements.txt
python -m scrap_ml.compact_pipeline run --data-dir .\data
python -m scrap_ml.report
```

La carpeta `data` debe contener los nueve CSV originales del caso. No se modifican. El último comando genera el PDF a partir de la evaluación, sin reentrenar.

## Qué hace cada modelo

- **A, calidad de la ruta:** la familia seleccionada usa regresores HistGradientBoostingRegressor por estación. Estima la fracción de rechazo de un colectivo de producción, no el resultado individual de cada barrita. La simulación compara el coste posterior evitado con el material bueno retirado y el coste de desechar.
- **B, diagnóstico:** once clasificadores HistGradientBoostingClassifier independientes estiman incidencias registradas activas por máquina. Cada equipo tiene su umbral; cuando hay suficientes datos se calibra su score. Detectar una incidencia no prueba que cause cada rechazo ni anticipa necesariamente una avería.
- **C, enrobadora:** un modelo compartido entre los carriles 1, 2, 3, 30, 31 y 32 prevé su tasa de rechazo por altura. Se comparan dos regresores de boosting con persistencia, tendencia y perfil horario. Se conserva el mejor candidato según la validación, aunque sea una referencia sencilla.

## Flujo operativo

1. Cargar producción, telemetría y medidas disponibles hasta el minuto observado. Los agregados del minuto t están disponibles en t+1.
2. Ejecutar A, B y C en paralelo cuando haya historia suficiente. No es una cadena en la que C deba fallar para activar A y después B.
3. Combinar recomendaciones: A informa sobre el colectivo actual; B señala máquinas a investigar; C solicita revisar ENR para la producción que viene detrás.
4. El operador decide, registra el ajuste y comprueba la calidad posterior. El código no cambia consignas ni opera máquinas.
5. Medir las consecuencias sin duplicar productos ni sumar como ahorro las solicitudes de revisión.

La ruta tiene 15 estaciones: MIX, DST, MLD, CAM_PRE, OVN, BCL, CAM_POST, BCM, BCR, CAR, GUI, LSR_CORE, ENR, LSR_BAR y CCL. A tiene 13 puntos predictivos; el láser final observa el resultado y CCL no tiene una nueva etiqueta de calidad. B cubre 11 equipos con etiquetas de incidencia.

## Modelo C y validación

La unidad es carril por minuto. Usa altura, dispersión, tasa fuera de altura, diferencias entre bordes y centro, medias y cambios de 5/15/30 minutos, turno y sensores ENR. El centro se fija en los carriles 11 a 22. El incremento barra(t) menos núcleo(t-2) es una aproximación agregada de transporte: se enmascaran pares con falta de piezas o velocidad incompatible. No es una medición individual del espesor de chocolate.

La previsión se emite en t+1. Se asumen cinco minutos para responder; se evalúa el rechazo de t+6 a t+25. La respuesta de cinco minutos y el beneficio durante veinte minutos son hipótesis operativas, no eficacia observada.

La regla operativa solicita revisar ENR cuando la tasa prevista de rechazo por altura en el conjunto de los seis carriles exteriores alcanza el 10 % en esa ventana. El evento positivo de la evaluación usa también el 10 %. El corte lo fija esta regla de negocio antes del test; el modelo de regresión sigue estimando una tasa continua. Las alertas consecutivas se agrupan para no generar más de una solicitud cada 30 minutos.

Se separan 40 días de entrenamiento, 5 de calibración, 10 de selección y 10 de prueba. Todos los carriles de un día permanecen juntos. Ninguna ventana objetivo cruza una frontera entre bloques o un cambio de turno. El modelo y el umbral se congelan antes de puntuar la prueba. Hay tres evaluaciones adicionales de avance temporal y un intervalo de incertidumbre por días completos.

También se comprueba que eliminar los datos futuros no cambie las variables de inferencia. Se miden por separado el rechazo futuro alto y los inicios nuevos de episodios, para no presentar la detección de un defecto ya activo como anticipación.

Los datos son simulados y ya habían sido explorados: es validación temporal interna, no una prueba externa intacta. A conserva el dominio parcial de transporte nominal y supervivencia al láser final del modelo original.

## Cómo ver resultados

Las salidas para la demo son tres CSV y un JSON:

- `output/evaluation.json`: métricas A/B, apartado `enrober_prevention` con la evaluación C y escenarios, y `comparable_business` con porcentajes y anualizaciones comparables.
- `output/prevencion.csv`: `minute_ts, carril, prob_degradacion, prob_bajo, prob_alto, minutos_hasta_limite, accion_recomendada`.
- `output/riesgo.csv`: `minute_ts, estacion, prob_scrap, prob_bajo, prob_alto, decision`. La estación es necesaria para distinguir los 13 puntos predictivos en un mismo minuto.
- `output/diagnostico.csv`: `minute_ts`, un score por cada una de las 11 máquinas y `maquina_senalada` (o `ninguna`).

Los CSV cubren cada minuto del bloque de test. `minute_ts` es el minuto observado: sus agregados se conocen en el minuto siguiente. Los campos `prob_scrap` y `prob_degradacion` contienen tasas de rechazo previstas entre 0 y 1, no una probabilidad de avería o de superar el 10 %. Los límites bajo/alto son bandas empíricas de tasas, construidas con cuantiles 5/95 de los errores de calibración por estación o carril; el JSON mide su cobertura real en test. No se garantiza un 90 % de cobertura en datos futuros.

`minutos_hasta_limite` queda vacío porque el modelo C no estima el instante exacto de cruce, sino el rechazo de t+6 a t+25. Una predicción vacía indica falta de soporte y se acompaña de `vigilar`. A conserva su dominio común de evaluación offline. `retener` significa revisar el candidato económico del replay; retener por sí solo no acredita el ahorro de descartar. `vigilar` se usa con tasa A de al menos 5 % o sin predicción; en el resto, `continuar`.

La solicitud C se emite como máximo cada 30 minutos y se repite en las seis filas de carril de ese minuto: hay que contar minutos únicos, no filas. En los minutos de riesgo entre solicitudes, la acción es `vigilar`. El JSON incluye `savings_by_model`: A conserva su ahorro simulado de descarte, B no recibe ahorro independiente y C muestra valor expuesto y escenarios condicionados. No se genera `demo.json`.

Para regenerar las salidas con modelos ya entrenados, sin reentrenar:

```powershell
python -m scrap_ml.compact_pipeline export --data-dir .\data
python -m scrap_ml.report
```

Los modelos se guardan en `scrap_ml/artifacts_improved/model.joblib`, `scrap_ml/artifacts_machine_system/diagnosis_model.joblib` y `scrap_ml/artifacts_enrober/model.joblib`. El PDF se guarda en `output/pdf/sistema_calidad_predictiva.pdf`. Los PDF existentes se preservan al reentrenar.

Para una predicción conjunta en un instante histórico:

```powershell
python -m scrap_ml.compact_pipeline predict --data-dir .\data --stage CAM_POST --observation-minute "2026-09-21 10:00"
```

Para actualizar de forma consistente los tres CSV, las métricas y la sensibilidad conjunta con A utiliza el pipeline completo. `export` reutiliza los modelos guardados y exige la evaluación ya generada. Las consultas puntuales se muestran por consola. `--keep-intermediates` es solo para depuración.

## Interpretación económica

El balance A es un replay de descarte temprano con costes aproximados. Se informan tres denominadores: FIN_BAR cubierto por A, todo FIN_BAR de los mismos días y todo scrap de esos días.

La anualización diaria es balance del test / días del test x 260. La antigua extrapolación a todo FIN_BAR por mezcla de costes es otra hipótesis y se mantiene identificada por separado; no equivale a multiplicar por días.

C informa del material asociado a altura expuesto después de las alertas, limitado por FIN_BAR. No es ahorro ni causalidad. Las eficacias del 0/25/50/75/100 % son escenarios explícitos: beneficio neto = eficacia x material expuesto - todos los costes de intervención. La precisión del modelo no estima esa eficacia. No se aplica una eficacia por defecto ni se promete un ahorro de corrección.

La sensibilidad conjunta excluye de A los minutos cubiertos por C para evitar solapes, pero sigue necesitando un piloto: una corrección cambiaría las lecturas y las decisiones posteriores. Antes de escalar se deben registrar intervención, tiempos, costes y calidad y comparar contra un control.
