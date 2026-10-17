XOCOLETA LINE — WORKSHOP MATERIALS
====================================

What's included
---------------
  app/          Open index.html in any modern browser to explore the Xocoleta
                line: stations, machine parameters, data model, defect catalog,
                OEE, scrap, pre-computed analysis charts, and sample rows from
                every CSV file.

  data/         Nine CSV files with 13 weeks of simulated production history
                (July 7 – October 2, 2026, weekday shifts only). Load them into
                DuckDB or PostgreSQL using the schema below.

  schema/       xocoleta.sql — CREATE TABLE statements for all 15 tables plus
                INSERT statements for the catalog data. Compatible with DuckDB
                and PostgreSQL.

Getting started with DuckDB
----------------------------
  1. Install DuckDB (duckdb.org) or use the online shell at shell.duckdb.org
  2. Run the schema file:
       .read schema/xocoleta.sql
  3. Load the CSV files (example):
       COPY fct_production_minute FROM 'data/fct_production_minute.csv' (HEADER);
  4. Start querying!

CSV files
---------
  fct_production_minute.csv   — one row per minute (line spine)          64,350 rows
  fct_telemetry_minute.csv    — machine parameter readings per minute  3,281,850 rows
  fct_laser_bar_minute.csv    — finished bar laser measurements         2,059,200 rows
  fct_laser_core_minute.csv   — core laser measurements                 2,059,200 rows
  fct_oee_minute.csv          — OEE state per asset per minute             64,350 rows
  fct_scrap_minute.csv        — scrap events                               57,788 rows
  fct_vision_gap.csv          — gap events detected by cameras             21,217 rows
  fct_vision_crack.csv        — crack events detected by post-oven camera  13,971 rows
  fct_problem_event.csv       — machine stoppages and problem events           398 rows

The interactive app
-------------------
  Open app/index.html in Chrome, Edge, or Firefox (no server needed).
  All tabs work offline, including "Rows" which shows the first 50 rows
  of each CSV so you can see the data shape before loading into DuckDB.

Line at a glance
----------------
  Product    : Xocoleta bar (biscuit + caramel + chocolate)
  Stations   : 15 (MIX → DST → MLD → CAM_PRE → OVN → BCL → CAM_POST →
                    BCM → BCR → CAR → GUI → LSR_CORE → ENR → LSR_BAR → CCL)
  Lanes      : 32
  Speed      : 1,920 bars / minute at full capacity
  Shifts     : A 06:00–14:00 · B 14:30–22:30 (weekdays)
