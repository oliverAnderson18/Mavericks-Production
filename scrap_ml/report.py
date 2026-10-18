"""Create the client-facing PDF from the compact evaluation; never refit models.

python -m scrap_ml.report
"""
import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                               TableStyle, PageBreak, KeepTogether)
from reportlab.graphics.shapes import Drawing, Rect, String, Line

PROJECT = Path(__file__).resolve().parent.parent
INK = colors.HexColor('#192D40')
TEAL = colors.HexColor('#007F82')
GOLD = colors.HexColor('#C17A35')
MUTED = colors.HexColor('#51677B')
PALE = colors.HexColor('#EDF4F6')
WIDTH = 174 * mm


def num(x, decimals=0):
    if x is None:
        return 'No estimable'
    return f'{x:,.{decimals}f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def pct(x, decimals=1):
    return num(100 * x, decimals) + ' %' if x is not None else 'No estimable'


def money(x):
    return num(x, 2) + ' USD'


def make_report(evaluation, output):
    r = json.loads(Path(evaluation).read_text(encoding='utf-8'))
    c = r['enrober_prevention']; a = r['decision_and_business_test']; b = r['machine_diagnosis']
    eda = c['eda']; econ = c['economics']; comparison = r['comparable_business']
    high_rate = c['threshold']
    correct_flag_minutes = c['test']['flagged_minutes'] - c['test']['false_flag_minutes']
    test_total_scrap = comparison['test_total_scrap_all_minutes_usd']
    combined_scenarios = [
        {
            'label': 'A sin corrección C',
            'effectiveness': None,
            'test_usd': a['net_scenario_usd'],
            'annual_usd': comparison['A_annual_260day_daily_run_rate_usd'],
            'evidence': 'Replay de A',
        }
    ]
    for scenario in econ.get('combined_sensitivity_no_double_count', []):
        effectiveness = scenario['assumed_C_effectiveness']
        if effectiveness <= 0:
            continue
        test_usd = scenario['A_residual_plus_C_gross_before_correction_cost_usd']
        combined_scenarios.append({
            'label': 'A + B + C; C al ' + pct(effectiveness, 0),
            'effectiveness': effectiveness,
            'test_usd': test_usd,
            'annual_usd': test_usd / 10 * 260,
            'evidence': 'Escenario condicionado',
        })
    joint_mid = next(x for x in combined_scenarios if x['effectiveness'] == 0.5)
    joint_high = next(x for x in combined_scenarios if x['effectiveness'] == 0.75)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('TitleX', fontName='Helvetica-Bold', fontSize=29, leading=34, textColor=INK, spaceAfter=18))
    styles.add(ParagraphStyle('SubtitleX', fontName='Helvetica', fontSize=14, leading=20, textColor=TEAL, spaceAfter=15))
    styles.add(ParagraphStyle('HeadX', fontName='Helvetica-Bold', fontSize=19, leading=24, textColor=INK, spaceAfter=13))
    styles.add(ParagraphStyle('SectionX', fontName='Helvetica-Bold', fontSize=12, leading=16, textColor=TEAL, spaceBefore=13, spaceAfter=6))
    styles.add(ParagraphStyle('BodyX', fontName='Helvetica', fontSize=10, leading=14, textColor=INK, spaceAfter=8))
    styles.add(ParagraphStyle('SmallX', fontName='Helvetica', fontSize=8.5, leading=12, textColor=MUTED, spaceAfter=6))
    styles.add(ParagraphStyle('TableX', fontName='Helvetica', fontSize=8.7, leading=12, textColor=INK))
    styles.add(ParagraphStyle('TableHeadX', fontName='Helvetica-Bold', fontSize=8.7, leading=12, textColor=colors.white))
    story = []
    def p(text, style='BodyX'):
        return Paragraph(text, styles[style])
    def add(text, style='BodyX'):
        story.append(p(text, style))
    def title(k, text, sub=None):
        add(k.upper(), 'SmallX'); add(text, 'HeadX')
        if sub:
            add(sub, 'SubtitleX')
    def page():
        story.append(PageBreak())
    def table(headers, rows, widths=None):
        cells = [[p(escape(str(x)), 'TableHeadX') for x in headers]]
        cells += [[p(escape(str(x)), 'TableX') for x in row] for row in rows]
        obj = Table(cells, colWidths=widths or [WIDTH / len(headers)] * len(headers), repeatRows=1, hAlign='LEFT')
        obj.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), INK), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [PALE, colors.white]),
            ('LEFTPADDING', (0, 0), (-1, -1), 9), ('RIGHTPADDING', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LINEBELOW', (0, -1), (-1, -1), .4, colors.HexColor('#D3E0E8'))]))
        story.append(obj); story.append(Spacer(1, 9))
    def callout(text):
        obj = Table([[p(text)]], colWidths=[WIDTH])
        obj.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), PALE),
            ('BOX', (0, 0), (-1, -1), .5, colors.HexColor('#D5E3E8')),
            ('LEFTPADDING', (0, 0), (-1, -1), 12), ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        story.append(obj); story.append(Spacer(1, 10))

    add('XOCOLETA / SMART FACTORY', 'SmallX')
    story.append(Spacer(1, 18 * mm))
    add('Prevenir el defecto.<br/>Localizar la señal.<br/>Decidir con datos.', 'TitleX')
    add('Sistema de calidad predictiva para una línea de chocolatinas', 'SubtitleX')
    add('Tres modelos complementarios, evaluación temporal y una contabilidad que distingue resultados simulados de oportunidades de corrección.')
    story.append(Spacer(1, 6 * mm))
    table(['Problema observado', 'Resultado A', 'Eficacia C', 'A + B + C: 50 % / 75 %'], [
        [money(eda['FIN_BAR_usd']) + ' de scrap final en 65 días',
         money(a['net_scenario_usd']) + ' netos simulados en 10 días',
         pct(c['test']['precision']) + ' precisión / ' + pct(c['test']['recall']) + ' cobertura',
         money(joint_mid['annual_usd']) + ' / ' + money(joint_high['annual_usd']) + ' al año*']])
    add('Resumen ejecutivo', 'SectionX')
    add('El sistema combina una previsión de rechazo en cada etapa (A), un diagnóstico de incidencias activas por máquina (B) y una previsión específica del rechazo por altura en los bordes de la enrobadora (C). Los tres se ejecutan con las lecturas disponibles y alimentan una recomendación conjunta al operador.')
    add('C alcanza ' + pct(c['test']['precision']) + ' de precisión y ' + pct(c['test']['recall']) + ' de cobertura al reconocer minutos cuyo rechazo futuro de bordes alcanza el ' + pct(high_rate, 0) + '. Esa eficacia predictiva no es la eficacia física de corregir la enrobadora: el test contiene defectos que ya estaban activos y no acredita que el operador los elimine.')
    callout('<b>Dos escenarios para decisión.</b> Con una recuperación real del 50 %, el conjunto alcanza <b>' + money(joint_mid['annual_usd']) + ' al año</b>. Si la respuesta operativa permite recuperar el 75 %, alcanza <b>' + money(joint_high['annual_usd']) + ' al año</b>. Ambos cálculos evitan duplicar producto y son anteriores al coste de intervenir. B dirige la revisión, pero no añade ahorro independiente.')
    add('* El 50 % es el caso prudente. El 75 % es una meta operativa favorable para contrastar en el piloto si la alerta llega al responsable, el ajuste se ejecuta a tiempo y la calidad posterior confirma la recuperación; no es una eficacia demostrada por el modelo.', 'SmallX')
    add('Datos simulados del caso Mavericks. Evaluación interna, con histórico previamente explorado. Documento generado directamente desde output/evaluation.json.', 'SmallX')

    page(); title('01 / Evidencia de negocio', 'Por qué centrarse en la cobertura')
    table(['Indicador histórico', 'Valor', 'Qué significa'], [
        ['Scrap de todos los puntos', money(eda['total_scrap_usd']), 'Coste de material, 65 días'],
        ['Scrap FIN_BAR', money(eda['FIN_BAR_usd']), pct(eda['FIN_BAR_usd'] / eda['total_scrap_usd']) + ' del coste total'],
        ['Conteos de altura fuera de límite', num(eda['height_oos_count']), 'Una barra puede fallar también en otra dimensión'],
        ['Seis carriles exteriores', pct(eda['edge_share_height_oos']) + ' de los conteos de altura', pct(eda['edge_share_measured_volume']) + ' del volumen medido'],
        ['Altura final anómala con núcleo previo conforme', pct(eda['aligned_final_height_oos_with_healthy_core_fraction']), 'Agregados por carril, alineación nominal de 2 minutos']],
        [WIDTH * .36, WIDTH * .26, WIDTH * .38])
    add('La concentración transversal aporta una señal útil: los carriles 1, 2, 3, 30, 31 y 32 acumulan una parte desproporcionada del rechazo por altura. La geometría del núcleo permite comprobar si la desviación aparece entre ambos láseres. Esto orienta la investigación hacia la cobertura o el sistema de medida.')
    add('Perfil medido por turno, sin atribuir causalidad a la limpieza', 'SectionX')
    rows = []
    for shift in ['A', 'B']:
        e = next(z for z in eda['profile_by_shift'] if z['shift'] == shift and z['zone'] == 'edges')
        ce = next(z for z in eda['profile_by_shift'] if z['shift'] == shift and z['zone'] == 'centre')
        rows.append([shift, num(e['height_mm'], 3), num(ce['height_mm'], 3), num(ce['height_mm'] - e['height_mm'], 3), pct(e['height_oos_rate'])])
    table(['Turno', 'Bordes, mm', 'Centro, mm', 'Diferencia, mm', 'Altura OOS bordes'], rows)
    add('Los centros se fijan como carriles 11 a 22; los bordes se definen físicamente, sin elegirlos para maximizar el resultado del test. La diferencia mostrada es de altura final media, no una medición directa del espesor de chocolate.', 'SmallX')
    callout('La coincidencia de limpieza y cambio de turno no permite aislar el efecto de cada uno. El EDA respalda una hipótesis de mejora; no demuestra que limpiar menos o reajustar la cortina vaya a eliminar un porcentaje determinado de scrap.')

    page(); title('02 / Arquitectura', 'Un pipeline conjunto, con tres preguntas')
    table(['Modelo', 'Pregunta', 'Salida'], [
        ['C / Enrobadora', '¿Qué rechazo por altura cabe esperar en los bordes de la producción siguiente?', 'Tasa prevista por carril y solicitud de revisión ENR'],
        ['A / Calidad de la ruta', '¿Qué proporción del colectivo podría acabar rechazada?', 'Tasa de rechazo y simulación económica por etapa'],
        ['B / Diagnóstico', '¿Qué máquina se parece a sus incidencias históricas activas?', 'Score y umbral propios de cada máquina']], [WIDTH*.22, WIDTH*.43, WIDTH*.35])
    add('Orden operativo', 'SectionX')
    for n, text in enumerate([
        'Leer y comprobar producción, sensores, láseres y marcas de tiempo. Un agregado del minuto t está disponible en t+1.',
        'Ejecutar A, B y C en paralelo cuando disponen de historia suficiente. Ninguno tiene que esperar a que otro genere un fallo.',
        'Combinar las señales: A orienta la decisión sobre producto; B y C orientan la revisión de equipos y de la producción que viene detrás.',
        'El operador confirma la actuación, registra el ajuste y vuelve a comprobar la calidad con lecturas posteriores.',
        'Evaluar resultados por periodos y registrar una sola vez cada coste o pieza afectada.'], 1):
        add(f'<b>{n}.</b> {text}')
    add('La ruta completa', 'SectionX')
    add('MIX / DST / MLD / CAM_PRE / OVN / BCL / CAM_POST / BCM / BCR / CAR / GUI / LSR_CORE / ENR / LSR_BAR / CCL.')
    add('Hay 15 estaciones en la simulación de recorrido. A dispone de 13 puntos predictivos: el láser final observa el resultado y el enfriador final no dispone de una nueva etiqueta de calidad. B cubre 11 equipos con incidencias registradas; las cámaras y los láseres son observación. C se especializa en ENR y seis de los 32 carriles.')
    callout('<b>Ejemplo:</b> C detecta riesgo creciente en los bordes y solicita revisar ENR. A puede mantener el colectivo actual en producción o simular su retirada si supera su umbral económico. B puede corroborar una incidencia registrada o señalar otro equipo. Un score alto no acredita por sí solo una causa.')

    page(); title('03 / Modelos A y B', 'Cómo aprenden y qué pueden afirmar')
    add('A / Regresión por estación', 'SectionX')
    add('La familia seleccionada es specialists_rate: un HistGradientBoostingRegressor por estación, con 150 iteraciones, hasta 15 hojas por árbol y aprendizaje 0,07. Cada árbol corrige errores del conjunto anterior minimizando el error cuadrático de la tasa de rechazo. El modelo aprende interacciones no lineales y trata los valores desconocidos sin inventar futuras exposiciones.')
    add('El entrenamiento pondera los agregados según sus piezas. En cada punto se incorporan las exposiciones ya recorridas y el estado actual de las máquinas. Los estados futuros reales del mismo colectivo permanecen desconocidos. Las lecturas actuales de equipos posteriores describen su estado ahora, no el que tendrán cuando llegue ese producto.')
    add('La política compara ahorro posterior esperado con material bueno perdido y coste de retirada. El corte es (coste bueno perdido + retirada) / (coste posterior evitable + coste bueno perdido), más el margen de seguridad seleccionado. El CSV traduce el candidato a retirada como retener para revisión; el ahorro publicado supone que después se ejecuta el descarte del replay.')
    coverage = r['quality_model']['coverage']
    add('La población común evaluada de A representa ' + pct(coverage['cohort_coverage_fraction']) + ' de los minutos con piezas y ' + pct(coverage['reject_coverage_fraction']) + ' de los rechazos finales del histórico. Es cobertura del dominio, no tasa de detección.', 'SmallX')
    add('B / Clasificación independiente por equipo', 'SectionX')
    add('Cada una de las 11 máquinas tiene un HistGradientBoostingClassifier binario: 110 iteraciones, hasta 15 hojas y aprendizaje 0,08. Se equilibran los pesos de minutos con y sin incidencia para aprender eventos escasos. La etiqueta es una incidencia activa, excluyendo Setup y eventos genéricos. Entradas: sensores propios, error de consigna y cambio de un minuto; las anotaciones son solo etiquetas offline.')
    add('La calibración isotónica aprende un ajuste monótono del score en una semana posterior al entrenamiento, si hay positivos suficientes. Cada máquina maximiza F1 en validación sobre cortes 0,10/0,25/0,50/0,75/0,90. Se señala el mayor score entre quienes superan su propio corte; si ninguna lo supera se exporta ninguna.')
    table(['Métrica B en las últimas dos semanas', 'Resultado'], [
        ['Minutos evaluados / con incidencia registrada', num(b['evaluated_minutes']) + ' / ' + num(b['registered_fault_minutes'])],
        ['Precisión de la máquina seleccionada', pct(b['thresholded']['precision_of_selected_machine'])],
        ['Cobertura de minutos de incidencia con máquina correcta', pct(b['thresholded']['recall_with_selected_machine'])],
        ['Minutos correctos / minutos con alguna señal', num(b['thresholded']['correct_machine_flagged_minutes']) + ' / ' + num(b['thresholded']['minutes_with_any_flag'])],
        ['F1 medio entre máquinas con eventos en test', num(b['macro_f1_on_machines_with_test_events'], 3)]], [WIDTH*.7, WIDTH*.3])
    add('B diagnostica eventos activos. Estos porcentajes no significan que anticipe averías, que identifique la causa de cada rechazo o que una reparación tenga esa eficacia.', 'SmallX')
    add('El rendimiento no es uniforme: en MLD la cobertura por minuto es ' + pct(b['per_machine']['MLD']['test']['recall']) + ', y en ENR es ' + pct(b['per_machine']['ENR']['test']['recall']) + '. C aporta una señal específica de calidad de cobertura que B no estaba diseñado para etiquetar.', 'SmallX')
    ev = b.get('event_level')
    if ev:
        add('Por episodios completos, B selecciona la máquina correcta en ' + str(ev['events_with_correct_selected_machine']) + ' de ' + str(ev['registered_test_events']) + ' incidencias (' + pct(ev['recall']) + '), con latencia mediana de ' + num(ev['median_detection_latency_minutes'], 1) + ' minuto desde su inicio entre las detectadas.', 'SmallX')

    page(); title('04 / Modelo C', 'Previsión específica de la enrobadora')
    add('Unidad y objetivo', 'SectionX')
    add('Una observación es carril por minuto. El mismo modelo comparte información entre los seis carriles exteriores e incluye su posición física como variable. No necesita identificar barras individuales. El objetivo es la fracción de piezas fuera de altura en cada carril, ponderada por el volumen medido durante la ventana futura.')
    table(['Momento', 'Qué ocurre'], [
        ['Minuto t', 'Se cierra el agregado observado de láser y sensores'],
        ['t + 1', 'La previsión y la recomendación quedan disponibles'],
        ['t + 1 a t + 6', 'Supuesto operativo: cinco minutos para responder'],
        ['t + 6 a t + 25', 'Veinte minutos de resultado futuro evaluado']], [WIDTH*.25, WIDTH*.75])
    add('Entradas disponibles', 'SectionX')
    add('Altura media y dispersión por carril; tasa de altura fuera de límite; diferencia bordes-centro; medias y cambios de 5, 15 y 30 minutos; turno y tiempo transcurrido; caudal de cortina, cobertura inferior, soplador, temperatura y viscosidad de ENR, con sus desviaciones de consigna.')
    add('Se añade el incremento de altura barra(t) menos núcleo(t-2). Es una alineación agregada nominal del caso. Los pares incompatibles con velocidad nominal o falta de piezas se dejan como desconocidos. No se resta simplemente la fila anterior ni se rellena con datos del día siguiente.')
    add('Algoritmo y selección', 'SectionX')
    names = {'hgb_small': 'HistGradientBoostingRegressor, 120 iteraciones y hasta 15 hojas por árbol',
             'hgb_flexible': 'HistGradientBoostingRegressor, 180 iteraciones y hasta 31 hojas por árbol',
             'persistence': 'persistencia de los últimos 15 minutos', 'clock_profile': 'perfil de carril, turno y horario',
             'linear_trend': 'extrapolación de la tendencia reciente'}
    add('<b>Seleccionado por validación:</b> ' + names[c['selected_candidate']] + '. Se compara con una variante más flexible y tres referencias simples. Se elige por error medio de la tasa por carril. La calibración isotónica se conserva solo si mejora ese error en validación.')
    add('El boosting ajusta el error cuadrático de la tasa con peso proporcional a piezas futuras observadas solo al entrenar. Usa aprendizaje 0,07, mínimo 80 filas por hoja, penalización L2 de 5 y 127 intervalos por variable. Los intervalos agrupan valores para acelerar las divisiones de los árboles. Al emitir la alerta, la combinación de carriles usa únicamente las piezas actuales; no utiliza el volumen futuro.', 'SmallX')
    add('El umbral operativo fijado para los seis bordes es ' + pct(high_rate, 0) + ' de tasa prevista. El evento de evaluación también se define como tasa futura conjunta de al menos ' + pct(high_rate, 0) + '. Se compara el modelo con referencias simples en validación, pero este corte lo fija la regla de negocio antes del test. El resultado es una previsión de rechazo, no la probabilidad de que ENR esté averiada.')

    page(); title('05 / Validación', 'Separar el futuro de lo que el modelo puede ver')
    rows = []
    for key, label in [('train', 'Entrenamiento'), ('calibration', 'Calibración'), ('validation', 'Selección'), ('test', 'Prueba final interna')]:
        s = c['splits'][key]
        rows.append([label, str(s['days']), s['first_observation'][:10] + ' a ' + s['last_target'][:10], num(s['lane_minutes'])])
    table(['Bloque', 'Días', 'Fechas', 'Filas carril-minuto'], rows, [WIDTH*.25, WIDTH*.1, WIDTH*.4, WIDTH*.25])
    add('Los carriles de un mismo minuto y todos los minutos del día permanecen en el mismo bloque. Las ventanas cuyo objetivo atravesaría una frontera temporal o un cambio de turno se excluyen. El modelo y el umbral se congelan antes de puntuar el test. No se hace una división aleatoria de filas correlacionadas.')
    add('Comprobaciones adicionales', 'SectionX')
    add('Se compara la misma predicción construyendo variables con el histórico completo y eliminando todos los registros posteriores: las entradas coinciden. Se comprueban claves, cantidades, soporte de carriles y orden de fechas. Los pesos usados para combinar las previsiones son los volúmenes actuales; no se utiliza el volumen futuro para emitir una alerta.')
    add('Tres evaluaciones de avance temporal', 'SectionX')
    rows = []
    for fold in c['walk_forward_fixed_hgb']:
        rows.append([str(fold['train_days']) + ' días', fold['validation_start'],
            num(100*fold['model']['lane_rate_mae'], 2), num(100*fold['persistence']['lane_rate_mae'], 2),
            num(fold['model']['f1'], 3)])
    table(['Historia', 'Siguientes 5 días', 'MAE ML, pp', 'MAE persist., pp', 'F1 ML'], rows)
    add('Los cortes usan un algoritmo fijo y un umbral de ' + pct(high_rate, 0) + ' para contrastar estabilidad en periodos distintos. Las ventanas se solapan, por lo que la incertidumbre se estima remuestreando días completos y manteniendo unidos sus carriles.', 'SmallX')
    callout('<b>Alcance de la evidencia.</b> El dataset es simulado y sus últimas semanas ya se habían explorado en el proyecto. Esta evaluación es cronológica y reproducible, pero no es un test externo intacto. A conserva además su filtro retrospectivo de transporte nominal y supervivencia hasta el láser; sus cifras no describen toda la fábrica.')

    page(); title('06 / Resultados C', 'Prever rechazo y anticipar su inicio son cosas distintas')
    tests = {'Modelo C seleccionado': c['test'], **c['test_baselines']}
    rows = []
    for name, m in tests.items():
        rows.append([name, num(100*m['lane_rate_mae'], 2), pct(m['precision']), pct(m['recall']), num(m['f1'], 3)])
    table(['Método', 'MAE carril, pp', 'Precisión', 'Cobertura', 'F1'], rows, [WIDTH*.3, WIDTH*.19, WIDTH*.17, WIDTH*.17, WIDTH*.17])
    add('MAE expresa el error absoluto de la fracción prevista, en puntos porcentuales. Precisión y cobertura comparan alertas por minuto con un rechazo futuro conjunto de al menos ' + pct(high_rate, 0) + '. Se evalúan ' + num(c['test']['minutes']) + ' minutos con seis carriles completos; las filas por carril no son observaciones independientes.')
    boot = c['uncertainty']['lane_MAE_improvement_vs_persistence_95pct']
    add('Diferencia de MAE frente a persistencia', 'SectionX')
    add('El intervalo bootstrap del 95 % para la mejora es [' + num(100*boot[0], 3) + ', ' + num(100*boot[1], 3) + '] puntos porcentuales. Un valor positivo favorece al modelo; uno negativo favorece a persistencia. La tabla permite valorar sus ventajas y límites sin seleccionar de nuevo tras mirar test.')
    al = c['alerts_test']; on = c['onset_test']
    table(['Lectura operativa', 'Resultado test'], [
        ['Solicitudes de revisión tras limitar a una cada 30 min', str(al['tickets']) + ' (' + num(al['tickets_per_day'], 1) + '/día)'],
        ['Solicitudes seguidas de rechazo futuro alto', str(al['correct_future_high_risk_tickets']) + ' / ' + str(al['tickets']) + ' (' + pct(al['correct_future_high_risk_tickets'] / al['tickets']) + ')'],
        ['Solicitudes cuando el rechazo actual ya era alto', str(al['tickets_with_already_high_current_rate'])],
        ['Inicios sostenidos evaluables / avisados con 5 min para responder', str(on['sustained_onsets']) + ' / ' + str(on['onsets_warned_with_at_least_5min_response'])],
        ['Anticipación mediana antes del inicio, desde decisión', num(on['median_warning_minutes_from_decision'], 1) + ' min' if on['median_warning_minutes_from_decision'] is not None else 'Sin avisos anticipados acreditados']
    ], [WIDTH*.68, WIDTH*.32])
    add('Un episodio nuevo exige cinco minutos previos por debajo del ' + pct(high_rate, 0) + ' y cinco minutos posteriores por encima. Esa prueba es más estricta que reconocer un defecto persistente. No se debe anunciar que todos los tickets evitan el nacimiento de barras malas; algunos sirven para reducir la continuación de un problema activo.')
    callout('<b>Eficacia de C, leída correctamente.</b> De ' + num(c['test']['flagged_minutes']) + ' minutos señalados, ' + num(correct_flag_minutes) + ' preceden a una ventana futura de rechazo alto: ' + pct(c['test']['precision']) + ' de precisión. Detecta ' + num(correct_flag_minutes) + ' de ' + num(c['test']['positive_minutes']) + ' minutos futuros positivos: ' + pct(c['test']['recall']) + ' de cobertura. Hubo ' + str(on['sustained_onsets']) + ' inicios nuevos evaluables y ' + str(on['onsets_warned_with_at_least_5min_response']) + ' se avisaron con cinco minutos de respuesta.')
    add('La selección se mantiene congelada aunque una referencia simple obtenga una métrica mejor en test. Para un despliegue, se mantendría también esa referencia como control y se volvería a evaluar con datos nuevos.', 'SmallX')

    page(); title('07 / Economía A', 'Cuánto se simula y sobre qué coste')
    table(['Contabilidad del test A', 'USD'], [
        ['Coste posterior evitado, bruto', money(a['gross_future_cost_avoided_usd'])],
        ['Pérdida de material bueno retirado', '- ' + money(a['false_reject_loss_usd'])],
        ['Coste adicional de retirada', '- ' + money(a['extra_disposal_cost_usd'])],
        ['Balance neto simulado', money(a['net_scenario_usd'])]], [WIDTH*.7, WIDTH*.3])
    add('Se retiran ' + num(a['bad_pieces_removed']) + ' piezas históricamente defectuosas y también ' + num(a['good_pieces_discarded']) + ' buenas, en ' + str(a['cohorts_discarded']) + ' colectivos. El resultado compensa estas pérdidas con material posterior que ya no se incorporaría. No representa fabricar buenas todas las barras retiradas.')
    table(['Denominador de los mismos 10 días', 'Coste', 'Tasa simulada A'], [
        ['FIN_BAR incluido en el dominio de A', money(a['historical_final_reject_cost_usd']), pct(comparison['A_rate_on_A_covered_FIN_BAR'], 2)],
        ['Todo FIN_BAR del test', money(comparison['test_FIN_BAR_all_minutes_usd']), pct(comparison['A_rate_on_all_test_FIN_BAR'], 2)],
        ['Todo scrap del test', money(comparison['test_total_scrap_all_minutes_usd']), pct(comparison['A_rate_on_all_test_scrap'], 2)]], [WIDTH*.48, WIDTH*.28, WIDTH*.24])
    add('Extrapolaciones: dos preguntas diferentes', 'SectionX')
    add('<b>Por ritmo diario:</b> ' + money(a['net_scenario_usd']) + ' / 10 días x 260 días = <b>' + money(comparison['A_annual_260day_daily_run_rate_usd']) + '</b>. Para 65 días serían ' + money(comparison['A_historical_65day_daily_run_rate_usd']) + '. Este es el equivalente diario del test, sin ampliar la cobertura del modelo.')
    add('<b>Por mezcla de coste:</b> aplicar ' + pct(comparison['A_rate_on_A_covered_FIN_BAR'], 2) + ' a todo FIN_BAR histórico da ' + money(comparison['A_legacy_65day_same_risk_mix_extrapolation_usd']) + '. Requiere que la producción no cubierta tenga riesgos, costes y capacidad de intervención similares. Es la base del valor de 19.698 USD mostrado previamente; no es multiplicar el test por 6,5 días equivalentes.')
    ci = a['day_bootstrap']['total_net_scenario_95pct_interval_usd']
    callout('El intervalo del balance A remuestreando los 10 días es [' + money(ci[0]) + ', ' + money(ci[1]) + ']. La incertidumbre incluye un resultado próximo a cero o negativo. No se presenta un ahorro anual garantizado.')

    page(); title('08 / Economía C y combinación', 'El valor expuesto permite diseñar el piloto')
    add('Después de las solicitudes C y del supuesto de cinco minutos de respuesta, las ventanas protegibles contienen hasta <b>' + money(econ['edge_height_material_at_risk_upper_bound_usd']) + '</b> de material asociado a rechazo de altura en los bordes. Cada minuto se cuenta una sola vez, aunque coincidan solicitudes.')
    add('Es un techo de coste expuesto: una misma barra puede fallar por altura, anchura o longitud. Corregir la altura no garantiza recuperar esa pieza. Se valora usando el coste unitario FIN_BAR observado, limitado por el rechazo final contabilizado por minuto.')
    add('Se limita el cálculo a veinte minutos posteriores a la respuesta. No se imputa beneficio hasta final de turno porque aún no se ha medido cuánto dura el efecto de un reajuste.', 'SmallX')
    add('Frecuencia real del umbral en el test', 'SectionX')
    add('De ' + num(c['test']['minutes']) + ' ventanas de producción evaluables, ' + num(c['test']['positive_minutes']) + ' (' + pct(c['test']['positive_minutes'] / c['test']['minutes'], 1) + ') superaron realmente el ' + pct(high_rate, 0) + ' de rechazo futuro por altura en los seis carriles exteriores. C señaló ' + num(c['test']['flagged_minutes']) + ' (' + pct(c['test']['flagged_minutes'] / c['test']['minutes'], 1) + '). Cada ventana comienza un minuto después de la anterior y comparte casi todas sus barras; no son lotes independientes.')
    add('Tras agrupar alertas consecutivas con un máximo de una solicitud cada 30 minutos, quedaron <b>' + num(c['alerts_test']['tickets']) + ' solicitudes en ' + num(econ['test_days']) + ' días</b>: <b>' + num(c['alerts_test']['tickets_per_day'], 1) + ' al día</b>, aproximadamente una cada ' + num(c['test']['minutes'] / c['alerts_test']['tickets'], 1) + ' minutos de producción evaluable. Esta carga describe el replay; una intervención real cambiaría las lecturas y los tickets siguientes.')
    rows = []
    for s in econ['scenarios']:
        rows.append([pct(s['assumed_effectiveness'], 0), money(s['gross_conditional_material_usd']),
            pct(s['fraction_of_same_days_total_scrap'], 2), money(s['annual_260day_equivalent_before_intervention_cost_usd']),
            money(s['break_even_all_in_intervention_cost_per_ticket_usd'])])
    table(['Eficacia supuesta', 'Bruto test', '% scrap test', 'Equiv. 260 días', 'Coste máximo/ticket'], rows,
        [WIDTH*.16, WIDTH*.21, WIDTH*.17, WIDTH*.24, WIDTH*.22])
    add('Estos porcentajes son escenarios de sensibilidad, no eficacias aprendidas por el modelo. El ahorro neto sería eficacia x coste expuesto menos operarios, ajuste, calidad, parada y cualquier producción buena perdida. La última columna indica el coste medio máximo por ticket para no consumir ese bruto.', 'SmallX')
    add('Revisar una máquina no implica pararla. Si el ajuste exige detener toda la línea, su coste de capacidad debe incorporarse antes de decidir: una corrección técnicamente posible puede no ser rentable.', 'SmallX')
    page(); title('09 / Ahorro conjunto', 'Qué aporta el sistema completo, sin contar dos veces la misma barra')
    add('La cifra conjunta separa tres funciones: A decide sobre el producto, C identifica material de bordes que podría recuperarse mediante un ajuste y B dirige la revisión hacia la máquina más compatible con una incidencia activa. B mejora la acción y la explicación, pero no crea por sí solo un ahorro adicional medible en estos datos.')
    if 'model_A_net_replay_outside_C_windows_usd' in econ:
        add('A genera ' + money(a['net_scenario_usd']) + ' cuando actúa solo. Para combinarlo, se conserva su balance fuera de las ventanas C (' + money(econ['model_A_net_replay_outside_C_windows_usd']) + ') y, cuando C interviene, se sustituye el crédito de A en esas ventanas por el material recuperable de C. Así una misma barra nunca se cuenta dos veces. B prioriza la máquina a revisar y no recibe crédito económico propio.')
        rows = [[x['label'], money(x['test_usd']), pct(x['test_usd'] / test_total_scrap, 2), money(x['annual_usd']), x['evidence']]
                for x in combined_scenarios]
        table(['Política / supuesto', '10 días', '% scrap test', '260 días', 'Evidencia'], rows,
              [WIDTH*.25, WIDTH*.17, WIDTH*.15, WIDTH*.21, WIDTH*.22])
    add('La fila A es una simulación histórica. Las filas con C son escenarios: 25 %, 50 %, 75 % y 100 % representan la fracción del material expuesto que una actuación real conseguiría recuperar. Son importes brutos antes de operarios, ajustes, controles, paradas o pérdida de producción buena.')
    callout('<b>Rango para presentar:</b> el caso prudente del 50 % alcanza ' + money(joint_mid['test_usd']) + ' en 10 días y ' + money(joint_mid['annual_usd']) + ' en 260 días (' + pct(joint_mid['test_usd'] / test_total_scrap, 2) + ' del scrap del test). El caso operativo favorable del 75 % alcanza ' + money(joint_high['test_usd']) + ' y ' + money(joint_high['annual_usd']) + ' respectivamente (' + pct(joint_high['test_usd'] / test_total_scrap, 2) + '). El 75 % es una hipótesis de actuación humana que debe medirse en el piloto.')

    page(); title('10 / Operación y escala', 'De una línea a una solución de empresa')
    add('Una demo con significado industrial', 'SectionX')
    add('Los tres CSV entregan cada minuto del test: prevención por carril, riesgo por estación y diagnóstico de los once equipos. La demo puede pintar ENR en naranja ante una solicitud C y mostrar en rojo las máquinas señaladas por B. Las predicciones corresponden a minute_ts y se conocen al minuto siguiente. Cada solicitud C se repite en los seis carriles; se cuenta una sola vez por minuto.')
    add('La interfaz puede mostrar tasa prevista por carril, diferencia bordes-centro, solicitud de revisión, ventana temporal y estado de calidad. El sistema recomienda. No escribe consignas en PLC, no ejecuta una limpieza y no acredita un ajuste que el operador aún no ha realizado.')
    add('Piloto para medir el ahorro', 'SectionX')
    for n, text in enumerate([
        'Operación en sombra: comprobar reloj, calibración de láseres, alineación de dos minutos y tiempos reales de respuesta.',
        'Registrar cada solicitud y el ajuste real, quién lo hizo, cuánto tardó, tiempo de parada y controles de calidad posteriores.',
        'Comparar turnos o días asignados a tratamiento y control, con aprobación de calidad, manteniendo criterios de producción comparables. Evitar intervenir y controlar simultáneamente sobre la misma línea.',
        'Calcular diferencia de scrap por volumen producido, coste neto y calidad, con intervalos por día. Comparar la política conjunta contra A solo y contra una alerta simple.',
        'Escalar cuando se confirme beneficio neto, carga de alertas asumible y cumplimiento de calidad.'], 1):
        add(f'<b>{n}.</b> {text}')
    add('Escalabilidad técnica y económica', 'SectionX')
    add('Se puede compartir el contrato de datos, la lógica de ventanas, la validación y el panel. Cada línea necesita mapear sus estaciones y carriles, límites de especificación, transporte, sensores y costes. Se recalibra por producto y se comprueba si conviene un modelo común con contexto o modelos por línea.')
    add('La cifra corporativa debe sumar resultados netos validados de cada línea. Multiplicar el potencial de Xocoleta por el número de líneas presupone el mismo volumen, defecto, margen y eficacia. Se priorizan líneas con más coste expuesto, mejor cobertura de sensores y capacidad de actuar.')
    callout('<b>Decisión propuesta:</b> usar los tres modelos como apoyo operativo, conservar referencias simples y financiar un piloto de corrección de cobertura. La oportunidad está cuantificada; la eficacia de corregir debe medirse.')

    page(); title('11 / Reproducibilidad', 'Del dato al informe, con pocas salidas')
    table(['Elemento', 'Implementación'], [
        ['A', 'scrap_ml/improved.py + full_line_data.py + full_line_policy.py'],
        ['B', 'scrap_ml/machine_system.py'],
        ['C', 'scrap_ml/enrober.py'],
        ['Orquestación', 'scrap_ml/compact_pipeline.py + decisions.py'],
        ['Evaluación', 'output/evaluation.json: métricas, bandas y ahorro por modelo'],
        ['Demo por minuto', 'output/prevencion.csv, riesgo.csv y diagnostico.csv'],
        ['Modelos persistidos', 'Un archivo joblib de A, otro de B y otro de C'],
        ['Informe', 'scrap_ml/report.py, sin reentrenar ni cambiar selección']], [WIDTH*.3, WIDTH*.7])
    add('Comandos desde la raíz', 'SectionX')
    for cmd in ['python -m pip install -r requirements.txt',
                r'python -m scrap_ml.compact_pipeline run --data-dir .\data',
                'python -m scrap_ml.report']:
        add(escape(cmd), 'SmallX')
    add('El entrenamiento limpia intermediarios y conserva tres modelos, tres CSV y evaluation.json. Para regenerar los CSV sin reentrenar: python -m scrap_ml.compact_pipeline export --data-dir .\\data. El PDF se regenera con el último comando; los documentos existentes se preservan.')
    add('Contrato de los CSV', 'SectionX')
    add('prob_scrap y prob_degradacion son nombres de interfaz para tasas de rechazo previstas, no probabilidades de avería o de superar el 10 %. prob_bajo/prob_alto son bandas empíricas con cuantiles 5/95 del error en calibración, por estación o carril. Su cobertura real en test se publica en evaluation.json y no está garantizada. minutos_hasta_limite queda vacío: el modelo estima una ventana futura, sin localizar el minuto de cruce. Tasas vacías implican falta de soporte y decisión vigilar.', 'SmallX')
    add('Fuentes y lectura de resultados', 'SectionX')
    add('Fuentes: CSV originales de producción, telemetría, láseres, scrap, OEE, cámaras e incidencias. EDA se recalcula sin usar el notebook como etiqueta. evaluation.json reúne métricas, auditoría, bandas y savings_by_model; conserva el detalle económico y la sensibilidad conjunta sin solapes.', 'SmallX')
    add('La validación temporal usa datos simulados, transporte nominal y diagnósticos de eventos registrados. El efecto económico de corregir exige un piloto con actuaciones y costes observados. La precisión predictiva no determina por sí sola cuánto scrap se recupera.', 'SmallX')

    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    def footer(canvas, doc):
        canvas.setStrokeColor(colors.HexColor('#D5E2E8'))
        canvas.line(18*mm, 16*mm, 192*mm, 16*mm)
        canvas.setFillColor(MUTED); canvas.setFont('Helvetica', 8)
        canvas.drawString(18*mm, 11*mm, 'XOCOLETA | Calidad predictiva | Evaluación interna')
        canvas.drawRightString(192*mm, 11*mm, str(doc.page))
    doc = SimpleDocTemplate(str(output), pagesize=(210*mm, 297*mm),
        leftMargin=18*mm, rightMargin=18*mm, topMargin=19*mm, bottomMargin=23*mm,
        title='Xocoleta - Sistema de calidad predictiva', author='Mavericks Production')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(str(output.resolve()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation', default=str(PROJECT / 'output/evaluation.json'))
    parser.add_argument('--output', default=str(PROJECT / 'output/pdf/sistema_calidad_predictiva.pdf'))
    args = parser.parse_args()
    make_report(args.evaluation, args.output)


if __name__ == '__main__':
    main()
