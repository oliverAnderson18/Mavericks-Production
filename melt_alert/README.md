# Melt Alert · asistente SMED de limpieza

Un unico agente local de Ollama prepara y coordina la limpieza programada. La pantalla
incorpora la interfaz simplificada del equipo; su pestaña **Cleaning**
conserva el planificador, seguimiento y chat con Ollama, sin claves ni pago por tokens.

Toda la interfaz esta en ingles, incluidos avisos, errores, tareas y respuestas
del asistente. **Live** prioriza una recomendacion y oculta carriles y parametros
tras **Lanes & settings**. **Forecast** muestra hasta cinco estaciones A y tres
maquinas B; las metricas y escenarios economicos estan plegados en **Model
evaluation & assumptions**. **Troubleshooting** muestra comprobaciones por maquina.
Los datos historicos y predicciones integrados se conservan. La documentacion del
repositorio permanece en español. Para aplicar cambios del agente, mensajes o
catalogo, reinicia el servidor en la terminal donde configuraste el modelo;
despues recarga la pagina con Ctrl+F5.

## Ejecutar desde la raiz de Mavericks-Production

Requiere Python 3.10 o superior. El planificador y su interfaz funcionan con la
biblioteca estandar, tambien para el chat. No se necesita el paquete openai ni ollama
de pip. Instala la aplicacion desde https://ollama.com/download y descarga el modelo:

    ollama pull qwen3:4b-instruct

Abre Ollama. Si no esta ejecutandose, usa `ollama serve` en otra terminal (si el
puerto ya esta ocupado por Ollama, no arranques una segunda instancia).
En PowerShell, desde la raiz del proyecto:

    $env:OLLAMA_MODEL="qwen3:4b-instruct"
    python -m melt_alert.server

Abre http://127.0.0.1:8000 y selecciona **Cleaning**. OLLAMA_MODEL es opcional:
por defecto es qwen3:4b-instruct. Se elige la variante Instruct para evitar el
razonamiento largo de la etiqueta qwen3:4b actual. El agente usa /api/chat con llamadas a herramientas,
temperatura 0, sin streaming ni razonamiento visible. La primera respuesta puede
tardar mientras se carga el modelo. El timeout por llamada es 180 segundos.
Se verifica que el servidor responde y que el modelo esta descargado; la calidad
de sus respuestas se comprueba con las preguntas de demo, no con el indicador.
Sin Ollama, los planes, registros y escenarios siguen funcionando.

Todos los integrantes deben descargar el mismo modelo en su PC. La inferencia
usa los recursos locales y no envia conversaciones a OpenAI. OLLAMA_HOST permite
cambiar la direccion local (por defecto http://127.0.0.1:11434); no se admiten hosts
remotos. Para otro modelo: `ollama pull qwen3:8b` y configura OLLAMA_MODEL con ese
nombre. Requiere mas memoria; no garantiza la misma velocidad en todos los PCs.
El modelo debe admitir herramientas: llama3 no sustituye directamente a Qwen3 en
este agente. No hay descarga automatica ni alternativa de pago silenciosa.

Un entorno opcional independiente evita instalar el resto de dependencias ML:

    python -m venv .venv-smed
    .\.venv-smed\Scripts\python.exe -m melt_alert.server

Opciones: --port 8001, --data-dir RUTA y --db RUTA. Por defecto lee data/ del
repositorio y guarda el estado en melt_alert/runtime/smed.sqlite3 (ignorado por Git).
El servidor escucha solo en 127.0.0.1. No envia ordenes a maquinas.

## Procedencia y alcance

- ORIGINAL_CASE_DATA: se recalculan los paros LINE / PLANNED_CLEAN del CSV de OEE.
  En el caso hay 65 paros de 30 minutos. No se modifican los nueve CSV originales.
- SIMULATED_OPERATIONAL_SCENARIO: doce tareas, cinco equipos/recursos, sus
  competencias, duraciones, dependencias y ventanas. Estan en fixtures/.
- SIMULATED_OPERATOR_RECORD: actuaciones registradas en una sesion de demo.
- CONDITIONAL_SCENARIO: impacto economico sujeto a disponibilidad, demanda,
  produccion buena y costes. No es ahorro causal ni realizado.

procedure.md es un procedimiento ilustrativo del reto, NO un SOP industrial
validado. Las duraciones no representan tiempos tecnicos de higienizacion.
No se eliminan tareas obligatorias, se reducen minimos ni se convierten
intervenciones internas en trabajo con maquinaria en marcha.

## Como funciona

planner.py calcula una planificacion por listas con precedencias y recursos
exclusivos. Las preparaciones externas pueden anticiparse; el resto requiere
el paro. Es una heuristica factible, no garantiza el optimo global. Las tareas
ya registradas se respetan y el trabajo pendiente nunca se retrotrae al pasado.

service.py persiste sesiones, eventos, planes y propuestas en SQLite. Valida
dependencias, recursos, reloj, minimos, liberacion y disponibilidad. Usa revisiones
para rechazar confirmaciones obsoletas o repetidas.

agent.py ofrece cinco herramientas: consultar_estado, consultar_procedimiento,
simular_plan, calcular_impacto y proponer_avance. Las llamadas del agente aparecen
en la respuesta. proponer_avance solo crea una propuesta; el usuario debe pulsar
**Confirm**. Los botones del panel tambien registran la confirmacion humana.
No se ofrece una herramienta que opere maquinas o cambie duraciones minimas.

server.py obtiene el contexto del servidor, no de
predicciones o instrucciones arbitrarias enviadas por el navegador. Es un
prototipo local: el campo responsable documenta una identidad de demo, no
autenticacion industrial ni firma de calidad.

## Guion reproducible de demo

1. Nueva sesion: reloj 13:40. Simula con kits previos. Las tareas acaban a las
   14:16; la disponibilidad productiva original a las 14:30 deja cero minutos
   de produccion recuperada. El objetivo de 15 minutos no se alcanza.
2. Desmarca preparar antes: el mismo ejemplo acaba a las 14:22.
3. Deja solo equipo de limpieza 1 (manteniendo mantenimiento y calidad): acaba
   a las 14:24. Una tarea ENR_CLEAN con seis minutos de retraso lleva el plan
   normal a las 14:22. Retira todos los equipos de limpieza: plan inviable.
4. Simula disponibilidad productiva 14:15: el primer plan permitiria arrancar
   a las 14:16, con 14 minutos de capacidad potencial, condicionado a las
   liberaciones y al cambio de organizacion del turno. Es otro supuesto.
5. Pregunta: "Compara preparar los kits antes o durante el paro". El agente
   consulta procedimiento, estado y planes. Revisa la traza de herramientas.
6. Pide "Quiero registrar el inicio de KIT_ENR". La respuesta propone el
   registro. Confirma con el boton: aparece en la tabla y el historial.
7. Avanza el reloj hasta 13:48 y registra su finalizacion. Intenta iniciar
   ENR_CLEAN antes del paro/aislamiento: el servidor rechaza la operacion.

El reloj SMED es independiente de la reproduccion historica de scrap. Nueva
sesion conserva las sesiones anteriores y reinicia solo el escenario visible.
Una simulacion no modifica disponibilidad ni acredita actuaciones: usa los
botones de guardar o registrar cuando corresponda.

## Pruebas sin API ni datos privados

    python -m unittest discover -s melt_alert/tests -v

Incluyen restricciones del planificador, indisponibilidad, retrasos, ausencia de
capacidad recuperada con el turno original, guardas de ejecucion, persistencia,
flujo agente-herramienta con cliente simulado y contrato HTTP nativo de Ollama,
incluyendo propuesta sin aplicar, JSON invalido y respuestas truncadas. Las pruebas simuladas
del agente no acreditan latencia ni calidad linguistica de una llamada real.

## Medir valor despues del piloto

Registrar por tarea duracion, espera, responsable y cambios; comparar sesiones
equivalentes con una referencia y costes completos. Con el historico actual,
siempre de 30 minutos, no se aprende que cambio causa una reduccion.
La calidad de producto posterior no certifica higiene. El modelo C puede
abstenerse durante unos 30 minutos de historia tras el cambio de segmento.

Documentacion API utilizada:
https://docs.ollama.com/api/chat
https://docs.ollama.com/capabilities/tool-calling
