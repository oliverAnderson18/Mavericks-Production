# Procedimiento de demostracion SMED — DEMO-1

Origen: SIMULATED_OPERATIONAL_SCENARIO. Acordado como ejemplo para el reto;
requiere sustitucion por el procedimiento aprobado de la planta antes de un uso real.

## Alcance
Una limpieza programada de la linea Xocoleta. El historico registra 30 minutos
de paro de 14:00 a 14:30. No contiene tiempos por tarea ni personal.

## Preparacion externa
KIT_ENR, KIT_CAR y KIT_SHARED preparan kits y materiales antes del paro. No
autorizan intervenir en maquinas en marcha. Si no se preparan antes, se ejecutan
durante el paro y ocupan los mismos recursos de limpieza.

## Trabajo interno
ISOLATE registra el aislamiento del escenario con mantenimiento. Las limpiezas
dependen de ISOLATE y de su kit. Los montajes siguen a su limpieza. INSPECT
requiere los montajes, BCM_CLEAN y GUI_CHECK completos. CHECKLIST sigue a INSPECT.
Las dependencias, competencias y duraciones minimas son obligatorias para este
ejemplo. Un equipo no puede ejecutar dos tareas simultaneas.

## Seguimiento
El operario registra inicio y fin, con el reloj de la sesion de demostracion.
Una tarea solo termina tras su duracion minima; un retraso puede simularse en el
plan, pero no modifica ni acredita la ejecucion real registrada.

## Finalizacion y arranque
Completar las tareas no equivale a liberar la linea. El responsable registra
por separado la liberacion de calidad y la disponibilidad de personal y material.
El horario productivo original es 14:30. Sin disponibilidad anterior no se
imputa capacidad recuperada aunque la limpieza acabe antes. Registrar el
arranque solo documenta una confirmacion humana, sin enviar ordenes a maquinas.

## Calidad posterior
Las alturas y el scrap permiten seguir calidad de producto; no certifican
higiene. El modelo C puede abstenerse durante unos 30 minutos despues del cambio
de segmento por falta de historia. Una ausencia de prediccion no acredita riesgo cero.

## Economia
25.000 USD/h es un supuesto de margen del caso. El resultado es margen potencial
de capacidad utilizable, sujeto a demanda, calidad y costes adicionales. No es
ahorro realizado. No sumar estos escenarios al scrap sin estudiar solapamientos.
