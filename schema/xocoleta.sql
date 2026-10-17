-- Xocoleta line schema, brand Fandonat.
-- Generated from src/data. Do not edit by hand; run npm run export-sql.
-- Types are plain so this loads in DuckDB or PostgreSQL.
-- Laser tables are lane-by-minute summaries, not one row per bar.
-- PAM means Predictive Asset Maintenance, which the workshop builds.
-- dim_problem and fct_problem_event are the labeled history those models learn from.

DROP VIEW IF EXISTS v_line_oee_minute;
DROP TABLE IF EXISTS fct_scrap_minute, dim_scrap_point, fct_problem_event, fct_oee_minute, fct_laser_bar_minute, fct_laser_core_minute, fct_vision_crack, fct_vision_gap, fct_telemetry_minute, fct_production_minute, dim_problem, dim_defect, dim_spec, dim_lane, dim_parameter, dim_machine;

CREATE TABLE dim_machine (
  machine_code VARCHAR,
  machine_name VARCHAR,
  sequence_no INTEGER,
  asset_type VARCHAR,
  PRIMARY KEY (machine_code)
);

CREATE TABLE dim_parameter (
  parameter_code VARCHAR,
  machine_code VARCHAR,
  parameter_name VARCHAR,
  unit VARCHAR,
  setpoint DOUBLE,
  range_min DOUBLE,
  range_max DOUBLE,
  is_primary BOOLEAN,
  driver_note VARCHAR,
  PRIMARY KEY (parameter_code)
);

CREATE TABLE dim_lane (
  lane_no SMALLINT,
  lane_band VARCHAR,
  PRIMARY KEY (lane_no)
);

CREATE TABLE dim_spec (
  product_stage VARCHAR,
  measure_name VARCHAR,
  nominal_mm DOUBLE,
  lsl_mm DOUBLE,
  usl_mm DOUBLE,
  PRIMARY KEY (product_stage, measure_name)
);

CREATE TABLE dim_defect (
  defect_code VARCHAR,
  defect_name VARCHAR,
  family VARCHAR,
  detected_at VARCHAR,
  lane_pattern VARCHAR,
  mechanism VARCHAR,
  PRIMARY KEY (defect_code)
);

CREATE TABLE dim_problem (
  problem_code VARCHAR,
  problem_name VARCHAR,
  machine_code VARCHAR,
  loss_type VARCHAR,
  oee_factor VARCHAR,
  symptom VARCHAR,
  PRIMARY KEY (problem_code)
);

CREATE TABLE fct_production_minute (
  minute_ts TIMESTAMP,
  dough_batch_id VARCHAR,
  shift_code VARCHAR,
  sku_code VARCHAR,
  line_speed_m_min DOUBLE,
  PRIMARY KEY (minute_ts)
);

CREATE TABLE fct_telemetry_minute (
  minute_ts TIMESTAMP,
  machine_code VARCHAR,
  parameter_code VARCHAR,
  value_actual DOUBLE,
  value_setpoint DOUBLE,
  PRIMARY KEY (minute_ts, parameter_code)
);

CREATE TABLE fct_vision_gap (
  gap_event_id BIGINT,
  detected_at TIMESTAMP,
  camera_code VARCHAR,
  lane_no SMALLINT,
  gap_length_mm DOUBLE,
  gap_width_mm DOUBLE,
  coverage_pct DOUBLE,
  severity VARCHAR,
  PRIMARY KEY (gap_event_id)
);

CREATE TABLE fct_vision_crack (
  crack_event_id BIGINT,
  detected_at TIMESTAMP,
  camera_code VARCHAR,
  lane_no SMALLINT,
  crack_length_mm DOUBLE,
  orientation VARCHAR,
  severity VARCHAR,
  PRIMARY KEY (crack_event_id)
);

CREATE TABLE fct_laser_core_minute (
  minute_ts TIMESTAMP,
  lane_no SMALLINT,
  pieces INTEGER,
  length_mm_avg DOUBLE,
  length_mm_sd DOUBLE,
  width_mm_avg DOUBLE,
  width_mm_sd DOUBLE,
  height_mm_avg DOUBLE,
  height_mm_sd DOUBLE,
  length_oos INTEGER,
  width_oos INTEGER,
  height_oos INTEGER,
  PRIMARY KEY (minute_ts, lane_no)
);

CREATE TABLE fct_laser_bar_minute (
  minute_ts TIMESTAMP,
  lane_no SMALLINT,
  pieces INTEGER,
  length_mm_avg DOUBLE,
  length_mm_sd DOUBLE,
  width_mm_avg DOUBLE,
  width_mm_sd DOUBLE,
  height_mm_avg DOUBLE,
  height_mm_sd DOUBLE,
  length_oos INTEGER,
  width_oos INTEGER,
  height_oos INTEGER,
  PRIMARY KEY (minute_ts, lane_no)
);

CREATE TABLE fct_oee_minute (
  minute_ts TIMESTAMP,
  machine_code VARCHAR,
  shift_code VARCHAR,
  state_code VARCHAR,
  scheduled_s DOUBLE,
  planned_downtime_s DOUBLE,
  breakdown_s DOUBLE,
  minor_stop_s DOUBLE,
  runtime_s DOUBLE,
  ideal_bars_per_min DOUBLE,
  total_bars INTEGER,
  good_bars INTEGER,
  reject_bars INTEGER,
  problem_code VARCHAR,
  PRIMARY KEY (minute_ts, machine_code)
);

CREATE TABLE fct_problem_event (
  problem_event_id BIGINT,
  machine_code VARCHAR,
  problem_code VARCHAR,
  started_at TIMESTAMP,
  ended_at TIMESTAMP,
  duration_s INTEGER,
  loss_type VARCHAR,
  oee_factor VARCHAR,
  note VARCHAR,
  PRIMARY KEY (problem_event_id)
);

CREATE TABLE dim_scrap_point (
  collection_point_code VARCHAR,
  collection_point_name VARCHAR,
  after_machine_code VARCHAR,
  scrap_type VARCHAR,
  piece_kg DOUBLE,
  usd_per_kg DOUBLE,
  note VARCHAR,
  PRIMARY KEY (collection_point_code)
);

CREATE TABLE fct_scrap_minute (
  minute_ts TIMESTAMP,
  collection_point_code VARCHAR,
  scrap_type VARCHAR,
  piece_count INTEGER,
  quantity_kg DOUBLE,
  usd_per_kg DOUBLE,
  cost_usd DOUBLE,
  PRIMARY KEY (minute_ts, collection_point_code)
);

INSERT INTO dim_machine (machine_code, machine_name, sequence_no, asset_type) VALUES
  ('MIX', 'Dough mixer', 1, 'process'),
  ('DST', 'Dough distributor', 2, 'process'),
  ('MLD', 'Moulder', 3, 'process'),
  ('CAM_PRE', 'Camera, before the oven', 4, 'camera'),
  ('OVN', 'Oven', 5, 'process'),
  ('BCL', 'Biscuit cooler', 6, 'process'),
  ('CAM_POST', 'Camera, after the oven', 7, 'camera'),
  ('BCM', 'Barrier cream', 8, 'process'),
  ('BCR', 'Barrier cooler', 9, 'process'),
  ('CAR', 'Caramel roller', 10, 'process'),
  ('GUI', 'Guillotine', 11, 'process'),
  ('LSR_CORE', 'Laser, core size', 12, 'laser'),
  ('ENR', 'Enrober-bottomer', 13, 'process'),
  ('LSR_BAR', 'Laser, finished bar', 14, 'laser'),
  ('CCL', 'Chocolate cooler', 15, 'process');

INSERT INTO dim_parameter (parameter_code, machine_code, parameter_name, unit, setpoint, range_min, range_max, is_primary, driver_note) VALUES
  ('MIX_MOISTURE_PCT', 'MIX', 'Dough moisture', '%', 18.5, 16.4, 20.2, TRUE, 'Dry dough is the main path to cracks after the oven. Below about 17.6% the biscuit goes brittle.'),
  ('MIX_SPECIFIC_ENERGY_KJ_KG', 'MIX', 'Specific energy from torque', 'kJ/kg', 14, 8, 22, TRUE, 'Energy delivered into the batch through the agitator. High energy with low moisture means an overworked, tight dough.'),
  ('MIX_SPEED_RPM', 'MIX', 'Mixer speed', 'rpm', 42, 0, 55, TRUE, 'Zero while the agitator is stopped. A trip on torque drops this to zero mid-batch.'),
  ('MIX_TORQUE_NM', 'MIX', 'Agitator torque', 'N·m', 180, 40, 320, FALSE, 'Instantaneous torque. Specific energy is the integral of torque and speed over the batch, not this snapshot.'),
  ('MIX_DOUGH_TEMP_C', 'MIX', 'Dough temperature', '°C', 24, 18, 32, TRUE, 'Use with oven zone 1. A large gap between dough temperature and zone 1 is a thermal shock.'),
  ('DST_HOPPER_LEVEL_PCT', 'DST', 'Hopper level', '%', 62, 0, 100, TRUE, 'The buffer between the mixer and the rest of the line. Gaps follow a low level once the moulder is starved.'),
  ('DST_MASS_FLOW_KG_MIN', 'DST', 'Mass flow', 'kg/min', 28, 0, 34, FALSE, 'Dough delivered toward the moulder. Falls with hopper level and with a blocked auger.'),
  ('DST_FEED_PRESSURE_BAR', 'DST', 'Feed pressure', 'bar', 1.8, 0.4, 2.6, TRUE, 'Low pressure leaves the outer lanes short. The gap count rises across several lanes on each side.'),
  ('DST_AUGER_RPM', 'DST', 'Auger speed', 'rpm', 35, 0, 48, FALSE, 'Zero during a bridge or a trip.'),
  ('DST_DOUGH_TEMP_C', 'DST', 'Dough temperature', '°C', 24, 18, 32, FALSE, 'Should track the mixer batch that is currently in the hopper, about 8 minutes later.'),
  ('MLD_ROLLER_GAP_MM', 'MLD', 'Roller gap', 'mm', 11.5, 9.5, 13.5, TRUE, 'Sets dough height. A wider gap makes a taller, softer rope and can change bake and core height.'),
  ('MLD_ROLLER_RPM', 'MLD', 'Roller speed', 'rpm', 18, 0, 26, FALSE, 'Matched to line speed. A wrap of dough on the roller shows up as an unstable speed and then a stop.'),
  ('MLD_LINE_SPEED_M_MIN', 'MLD', 'Line speed', 'm/min', 6, 0, 6.4, TRUE, 'Nominal 6 m/min. With a 100 mm cut this is 60 strokes per minute on each lane.'),
  ('MLD_PRESSURE_BAR', 'MLD', 'Moulding pressure', 'bar', 2.2, 1.2, 3.2, FALSE, 'Too high squeezes dough out of the edge lanes. Too low leaves a narrow, tall rope.'),
  ('MLD_SIDE_BALANCE_PCT', 'MLD', 'Side balance', '%', 0, -25, 25, TRUE, 'Zero is even. Positive pushes dough toward lane 32 and starves lane 1. Negative does the opposite.'),
  ('OVN_Z1_TEMP_C', 'OVN', 'Zone 1 temperature', '°C', 150, 130, 185, TRUE, 'First heat the dough meets. Compare with dough temperature from the mixer.'),
  ('OVN_Z2_TEMP_C', 'OVN', 'Zone 2 temperature', '°C', 175, 155, 200, TRUE, 'Main bake. A jump of more than about 40°C from zone 1 is a shock.'),
  ('OVN_Z3_TEMP_C', 'OVN', 'Zone 3 temperature', '°C', 190, 165, 210, TRUE, 'Hottest zone. Drives color and dryness.'),
  ('OVN_Z4_TEMP_C', 'OVN', 'Zone 4 temperature', '°C', 165, 145, 190, TRUE, 'Finish and a slight let-down before the cooler.'),
  ('OVN_BELT_SPEED_M_MIN', 'OVN', 'Belt speed', 'm/min', 6, 0, 6.4, FALSE, 'Bake time is belt speed. Slower belt dries the biscuit and raises crack risk.'),
  ('OVN_EXHAUST_PCT', 'OVN', 'Exhaust damper', '%', 40, 15, 80, FALSE, 'High exhaust pulls moisture out of the oven and dries the surface.'),
  ('BCL_PRODUCT_IN_C', 'BCL', 'Product in', '°C', 96, 80, 115, TRUE, 'Biscuit temperature at the oven exit.'),
  ('BCL_AIR_TEMP_C', 'BCL', 'Cooling air', '°C', 18, 8, 30, TRUE, 'A large gap versus product-in is the cooler shock.'),
  ('BCL_PRODUCT_OUT_C', 'BCL', 'Product out', '°C', 32, 24, 48, TRUE, 'Must be cool enough for cream to stay where it is laid, and not so cold that the next shock is violent.'),
  ('BCL_FAN_PCT', 'BCL', 'Fan speed', '%', 70, 0, 100, FALSE, 'A fan trip lets product-out temperature climb.'),
  ('BCM_CREAM_TEMP_C', 'BCM', 'Cream temperature', '°C', 48, 36, 58, TRUE, 'Too cold and the cream will not flow. Too hot and it soaks in before the barrier cooler sets it.'),
  ('BCM_VISCOSITY_CP', 'BCM', 'Cream viscosity', 'cP', 2500, 1200, 5000, FALSE, 'Rises fast as temperature falls.'),
  ('BCM_PUMP_RPM', 'BCM', 'Pump speed', 'rpm', 40, 0, 55, FALSE, 'Zero when the pump trips or the pipe is blocked.'),
  ('BCM_FLOW_KG_MIN', 'BCM', 'Cream flow', 'kg/min', 4.5, 0, 6.5, TRUE, 'The deposit rate. A drop here is a skip.'),
  ('BCM_PRESSURE_BAR', 'BCM', 'Manifold pressure', 'bar', 1.4, 0.3, 2.4, FALSE, 'A rising pressure with falling flow means a blockage.'),
  ('BCR_AIR_TEMP_C', 'BCR', 'Cooling air', '°C', 12, 6, 24, TRUE, 'The set temperature for the cream.'),
  ('BCR_PRODUCT_OUT_C', 'BCR', 'Product out', '°C', 20, 14, 32, TRUE, 'Above about 26°C the cream is not set and caramel will smear it.'),
  ('BCR_FAN_PCT', 'BCR', 'Fan speed', '%', 80, 0, 100, FALSE, 'A fan fault shows up as a warm product-out.'),
  ('CAR_TEMP_C', 'CAR', 'Caramel temperature', '°C', 52, 40, 64, TRUE, 'Cold caramel will not spread and leaves skips. Hot caramel runs off the edges.'),
  ('CAR_VISCOSITY_CP', 'CAR', 'Caramel viscosity', 'cP', 8000, 4000, 14000, FALSE, 'Moves opposite to temperature.'),
  ('CAR_ROLLER_GAP_MM', 'CAR', 'Roller gap', 'mm', 1.8, 0.8, 3.2, TRUE, 'Sets caramel thickness and therefore part of the finished bar height.'),
  ('CAR_ROLLER_RPM', 'CAR', 'Roller speed', 'rpm', 12, 0, 18, FALSE, 'Unstable speed is an early sign of a wrap.'),
  ('CAR_FLOW_KG_MIN', 'CAR', 'Caramel flow', 'kg/min', 3.2, 0, 4.8, FALSE, 'Drops when the feed pot is empty or the roller is wrapped.'),
  ('GUI_CUT_LENGTH_MM', 'GUI', 'Cut length setpoint', 'mm', 100, 96, 104, TRUE, 'The target length. The laser, not this setpoint, is the measurement.'),
  ('GUI_STROKE_PER_MIN', 'GUI', 'Stroke rate', '1/min', 60, 0, 64, TRUE, 'Must equal line speed (mm/min) divided by cut length. 6000 / 100 = 60.'),
  ('GUI_LINE_SPEED_M_MIN', 'GUI', 'Line speed', 'm/min', 6, 0, 6.4, FALSE, 'The knife follows this speed. A mismatch becomes phase error.'),
  ('GUI_KNIFE_TEMP_C', 'GUI', 'Knife temperature', '°C', 40, 25, 55, FALSE, 'A cold knife smears caramel and can throw short or torn ends.'),
  ('GUI_PHASE_ERROR_MS', 'GUI', 'Phase error', 'ms', 0, -30, 30, TRUE, '1 ms ≈ 0.1 mm of length. Beyond about ±15 ms the core length leaves the ±1.5 mm spec.'),
  ('ENR_CHOC_TEMP_C', 'ENR', 'Chocolate temperature', '°C', 30.5, 27.5, 34, TRUE, 'Temper window. Outside it, the coating is thick, dull, or unset.'),
  ('ENR_VISCOSITY_CP', 'ENR', 'Chocolate viscosity', 'cP', 1800, 900, 3200, TRUE, 'High viscosity leaves a heavy coat and feet. Low viscosity leaves a thin coat.'),
  ('ENR_CURTAIN_KG_MIN', 'ENR', 'Curtain flow', 'kg/min', 18, 0, 26, TRUE, 'Top and side chocolate. A break in the curtain is a top skip.'),
  ('ENR_BOTTOM_KG_MIN', 'ENR', 'Bottomer flow', 'kg/min', 12, 0, 18, TRUE, 'Chocolate under the bar. Zero flow is a missing bottom.'),
  ('ENR_BLOWER_PCT', 'ENR', 'Blower', '%', 45, 10, 80, FALSE, 'More air removes more chocolate from the top and lowers finished height.'),
  ('CCL_AIR_TEMP_C', 'CCL', 'Cooling air', '°C', 14, 8, 22, TRUE, 'Above about 18°C the coating does not set in the tunnel.'),
  ('CCL_PRODUCT_OUT_C', 'CCL', 'Product out', '°C', 16, 12, 24, TRUE, 'Target below 18°C so the bar can be packed.'),
  ('CCL_HUMIDITY_PCT', 'CCL', 'Relative humidity', '%', 45, 30, 75, FALSE, 'High humidity marks the coating and can bloom it later.');

INSERT INTO dim_lane (lane_no, lane_band) VALUES
  (1, 'edge'),
  (2, 'near_edge'),
  (3, 'centre'),
  (4, 'centre'),
  (5, 'centre'),
  (6, 'centre'),
  (7, 'centre'),
  (8, 'centre'),
  (9, 'centre'),
  (10, 'centre'),
  (11, 'centre'),
  (12, 'centre'),
  (13, 'centre'),
  (14, 'centre'),
  (15, 'centre'),
  (16, 'centre'),
  (17, 'centre'),
  (18, 'centre'),
  (19, 'centre'),
  (20, 'centre'),
  (21, 'centre'),
  (22, 'centre'),
  (23, 'centre'),
  (24, 'centre'),
  (25, 'centre'),
  (26, 'centre'),
  (27, 'centre'),
  (28, 'centre'),
  (29, 'centre'),
  (30, 'centre'),
  (31, 'near_edge'),
  (32, 'edge');

INSERT INTO dim_spec (product_stage, measure_name, nominal_mm, lsl_mm, usl_mm) VALUES
  ('core', 'length', 100, 98.5, 101.5),
  ('core', 'width', 30, 29, 31),
  ('core', 'height', 14, 13, 15),
  ('bar', 'length', 103, 101, 105),
  ('bar', 'width', 34, 32.5, 35.5),
  ('bar', 'height', 18, 16.6, 19.4);

INSERT INTO dim_defect (defect_code, defect_name, family, detected_at, lane_pattern, mechanism) VALUES
  ('Q-GAP-PRE', 'Dough gap before the oven', 'Gap', 'CAM_PRE', 'Lanes 1 and 32 first, then 2 and 31', 'The moulder is not filling the sheet. Low distributor pressure under-fills both edges. A side-balance offset starves one edge and loads the other.'),
  ('Q-GAP-POST', 'Dough gap after the oven', 'Gap', 'CAM_POST', 'Same lanes as the pre-oven gap, usually longer', 'A gap that entered the oven dries and pulls apart, so the void gets longer. A new post-oven gap without a pre-oven gap is rare and points at a belt or transfer tear, not at the moulder.'),
  ('Q-CRACK', 'Cracked biscuit', 'Crack', 'CAM_POST', 'All lanes, with no edge preference', 'The dough was dry, or the temperature step was large. Compare mixer moisture about 18 minutes earlier, zone 1 against dough temperature, the jump from zone 1 to zone 2, and product-in against cooler air.'),
  ('Q-LEN-CORE', 'Core length out of spec', 'Size', 'LSR_CORE', 'All lanes together, because one knife cuts them', 'The guillotine is out of phase with the belt. At 100 mm/s, 1 ms of phase error is 0.1 mm of length. Stroke rate must stay at line speed divided by cut length.'),
  ('Q-WID-CORE', 'Core width out of spec', 'Size', 'LSR_CORE', 'Edge lanes move first', 'Width is set at the moulder. Starved edges run narrow. A high moulding pressure squeezes dough sideways and can widen the centre lanes.'),
  ('Q-HGT-CORE', 'Core height out of spec', 'Size', 'LSR_CORE', 'All lanes, edges more variable', 'Roller gap sets the dough height. Bake and moisture change lift. Cream flow and caramel gap add the layers that are already on the core when this laser fires.'),
  ('Q-LEN-BAR', 'Finished bar length out of spec', 'Size', 'LSR_BAR', 'Follows the core length, plus a small chocolate overhang', 'Mostly inherited from the guillotine. A heavy curtain adds a little length on the ends. Feet of chocolate show up as length and as height.'),
  ('Q-WID-BAR', 'Finished bar width out of spec', 'Size', 'LSR_BAR', 'All lanes', 'Core width plus the side coat. High viscosity or a low blower leaves chocolate on the sides and pushes width out.'),
  ('Q-HGT-BAR', 'Finished bar height out of spec', 'Size', 'LSR_BAR', 'All lanes', 'Core height plus the coat. Curtain flow adds height. Blower air takes it off. A missing bottom drops height from below.'),
  ('Q-CREAM-SKIP', 'Barrier cream skip', 'Layer', 'BCM', 'Often a band of neighbouring lanes under a blocked nozzle', 'Cream temperature fell or a nozzle blocked. The dough cameras are already upstream, so this needs its own check or a later visual station.'),
  ('Q-CREAM-HEAVY', 'Heavy barrier cream', 'Layer', 'BCM', 'All lanes when the pump runs fast', 'Flow above target. Shows up as extra core height and as cream squeezed out at the guillotine.'),
  ('Q-CARAMEL-SKIP', 'Caramel skip', 'Layer', 'CAR', 'All lanes, or one side if the roller is not level', 'Cold caramel, an empty feed, or a roller wrap. The core height goes low and the finished bar looks pale at the edges.'),
  ('Q-CARAMEL-HEAVY', 'Heavy caramel', 'Layer', 'CAR', 'All lanes', 'Roller gap too open or caramel too hot, so it floods the rope. Core height and finished height both rise.'),
  ('Q-CHOC-TOP', 'Thin or missing top chocolate', 'Coat', 'ENR', 'All lanes when the curtain breaks', 'Curtain flow collapsed or the blower stripped the coat. Finished height falls toward the core height.'),
  ('Q-CHOC-BOTTOM', 'Missing bottom chocolate', 'Coat', 'ENR', 'All lanes', 'Bottomer starved or the pump tripped. The bar has a bare base and a lower height.'),
  ('Q-CHOC-FEET', 'Chocolate feet or tails', 'Coat', 'ENR', 'All lanes', 'Viscosity high or blower too low, so chocolate stays on the trailing edge. Length and height both grow a small tail.'),
  ('Q-UNSET', 'Chocolate not set', 'Coat', 'CCL', 'All lanes', 'Cooler air or product-out too warm, sometimes with high humidity. Bars smear and are rejected at packing even when the laser size was fine.'),
  ('Q-BROKEN', 'Broken core at the cut', 'Cut', 'GUI', 'Whichever lane was already cracked', 'A cracked biscuit meets the knife and breaks. Treat the crack camera as the cause and the guillotine as the place it becomes scrap.'),
  ('Q-DOUBLE', 'Double or missed cut', 'Cut', 'GUI', 'All lanes on the same stroke', 'Stroke rate and line speed diverge, or the knife stalls for a stroke. The core laser shows a length near 200 mm or a missing piece.');

INSERT INTO dim_problem (problem_code, problem_name, machine_code, loss_type, oee_factor, symptom) VALUES
  ('P-MIX-TORQUE', 'Mixer torque trip', 'MIX', 'Breakdown', 'Availability', 'Mixer speed falls to zero and torque spikes in the minute before the trip. Specific energy for that batch stops climbing.'),
  ('P-MIX-FEED', 'Ingredient feed fault', 'MIX', 'Breakdown', 'Availability', 'The batch does not start. Moisture, if a partial batch was dumped, sits away from 18.5%.'),
  ('P-DST-BRIDGE', 'Hopper bridge', 'DST', 'Minor stop', 'Performance', 'Hopper level looks healthy but mass flow and auger speed drop. The bridge collapses and the flow surges.'),
  ('P-DST-PRESSURE', 'Low feed pressure', 'DST', 'Reduced speed', 'Performance', 'Feed pressure below about 1.4 bar while the line is still moving. Not always a full stop.'),
  ('P-MLD-WRAP', 'Dough wrap on the roller', 'MLD', 'Breakdown', 'Availability', 'Roller speed wobbles, then the moulder stops. Side balance is meaningless while it is stopped.'),
  ('P-MLD-SIDE', 'Side balance drift', 'MLD', 'Reduced speed', 'Performance', 'Side balance walks away from zero. The line does not stop. One edge lane starts gapping.'),
  ('P-OVN-ZONE', 'Oven zone temperature fault', 'OVN', 'Breakdown', 'Availability', 'One zone leaves its band. The belt may keep moving, which still bakes a stretch of bad product, or the line is stopped.'),
  ('P-OVN-BELT', 'Oven belt tracking', 'OVN', 'Minor stop', 'Performance', 'Belt speed dips and returns. Lanes at one side can tear.'),
  ('P-BCL-FAN', 'Biscuit cooler fan fault', 'BCL', 'Breakdown', 'Availability', 'Fan speed at zero, product-out temperature climbing.'),
  ('P-BCM-BLOCK', 'Cream nozzle blockage', 'BCM', 'Minor stop', 'Performance', 'Manifold pressure up, cream flow down, pump still turning.'),
  ('P-BCM-COLD', 'Cream set in the pipe', 'BCM', 'Breakdown', 'Availability', 'Cream temperature falls through 40°C, viscosity spikes, flow goes to zero.'),
  ('P-BCR-FAN', 'Barrier cooler fan fault', 'BCR', 'Breakdown', 'Availability', 'Product-out above about 26°C.'),
  ('P-CAR-WRAP', 'Caramel wrap on the roller', 'CAR', 'Breakdown', 'Availability', 'Roller speed unstable, then a stop. Flow falls before the stop.'),
  ('P-CAR-COLD', 'Cold caramel', 'CAR', 'Reduced speed', 'Performance', 'Temperature under about 46°C and viscosity high, line still running.'),
  ('P-CAR-HOT', 'Caramel gap open', 'CAR', 'Process', 'Quality', 'Roller gap above about 3.5 mm and caramel hotter than 58°C. The rope is flooded and core height rises on every lane.'),
  ('P-GUI-SYNC', 'Guillotine out of sync', 'GUI', 'Reduced speed', 'Performance', 'Phase error walks past ±15 ms while stroke rate and line speed disagree.'),
  ('P-GUI-BLADE', 'Blunt or cold knife', 'GUI', 'Setup', 'Availability', 'A planned blade change, or an unplanned stop when the knife tears caramel.'),
  ('P-ENR-CURTAIN', 'Curtain break', 'ENR', 'Minor stop', 'Performance', 'Curtain flow collapses and recovers. Blower percentage does not explain it.'),
  ('P-ENR-TEMPER', 'Chocolate out of temper', 'ENR', 'Reduced speed', 'Performance', 'Chocolate temperature leaves 29.5–31.5°C and viscosity follows.'),
  ('P-ENR-BOTTOM', 'Bottomer starved', 'ENR', 'Breakdown', 'Availability', 'Bottomer flow at zero while the curtain is still running.'),
  ('P-CCL-WARM', 'Chocolate cooler too warm', 'CCL', 'Reduced speed', 'Performance', 'Air temperature above 18°C or product-out above 18°C.'),
  ('P-LINE-JAM', 'Transfer jam', 'LINE', 'Minor stop', 'Performance', 'A photoeye or a transfer stops the line briefly. No single machine parameter stays wrong afterwards.'),
  ('P-LINE-STARVE', 'Line starved from the hopper', 'DST', 'Breakdown', 'Availability', 'Hopper level at zero after a mixer stop. Downstream speeds go to zero together.'),
  ('P-LINE-BLOCK', 'Downstream block', 'LINE', 'Breakdown', 'Availability', 'Assets upstream of a stopped machine go to blocked. Their speeds are zero but they have not failed.');

INSERT INTO dim_scrap_point (collection_point_code, collection_point_name, after_machine_code, scrap_type, piece_kg, usd_per_kg, note) VALUES
  ('POST_CAM', 'Break-off after the post-oven camera', 'CAM_POST', 'Baked biscuit', 0.01212797619047619, 1.45, 'The dough is still wet before the oven, so operators leave it on the belt. After the biscuit cooler, any lane with a gap or a crack is broken off before the cream. The biscuit is collected beside this camera.'),
  ('GUI_CHUTE', 'Guillotine chute', 'GUI', 'Layered core', 0.016138392857142858, 3.1, 'A cut outside 98.5–101.5 mm, including a double cut, falls through the opening under the knife. Those pieces are not presented to either laser. Every kilogram in this chute costs the same.'),
  ('FIN_BAR', 'Reject after the finished-bar laser', 'LSR_BAR', 'Finished bar', 0.03176339285714286, 5.51, 'A finished bar outside its length, width, or height limit is diverted here, chocolate included. One bar is one piece even when it misses two limits. The coat is the curtain plus the bottomer at setpoint, 30 kg/min, at $8/kg. Biscuit, cream, and caramel keep the layered-core rate.'),
  ('MLD_DUMP', 'Dough dumped from the moulder', 'MLD', 'Raw dough', 224, 1.21, 'A wrap cleared within 10 minutes is peeled off the roller and the dough stays. Past 10 minutes the hopper behind the moulder has skinned, and the whole 224 kg charge is dumped. The rate is the baked-biscuit ingredients with the water taken out.');

CREATE VIEW v_line_oee_minute AS
SELECT
  minute_ts,
  shift_code,
  state_code,
  runtime_s / NULLIF(scheduled_s - planned_downtime_s, 0) AS availability,
  total_bars * 60.0 / NULLIF(ideal_bars_per_min * runtime_s, 0) AS performance,
  good_bars * 1.0 / NULLIF(total_bars, 0) AS quality,
  (runtime_s / NULLIF(scheduled_s - planned_downtime_s, 0))
    * (total_bars * 60.0 / NULLIF(ideal_bars_per_min * runtime_s, 0))
    * (good_bars * 1.0 / NULLIF(total_bars, 0)) AS oee
FROM fct_oee_minute
WHERE machine_code = 'LINE';
