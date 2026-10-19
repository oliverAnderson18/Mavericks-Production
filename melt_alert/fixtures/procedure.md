# Illustrative SMED procedure — DEMO-1

Source: SIMULATED_OPERATIONAL_SCENARIO. Demo assumptions for the challenge.
Replace this document with the approved plant procedure before operational use.

## Scope
One planned cleaning stop on the Xocoleta line. The case data records a
30-minute stop from 14:00 to 14:30, without task timings or staffing details.

## External preparation
KIT_ENR, KIT_CAR and KIT_SHARED prepare kits and materials before the stop.
They do not authorize work on running equipment. If preparation is not completed
beforehand, it takes place during the stop using the same cleaning teams.

## Internal work
ISOLATE records scenario equipment isolation with maintenance. Cleaning tasks
require ISOLATE and their kit. Reassembly follows cleaning. INSPECT requires
both reassembly tasks, BCM_CLEAN and GUI_CHECK. CHECKLIST follows INSPECT.
All dependencies, skills and minimum durations are mandatory in this example.
A team cannot perform two tasks at once.

## Progress records
The operator logs task start and finish using the demo session clock.
A task cannot finish before its minimum duration. A simulated delay changes the
plan only; it does not modify or confirm recorded execution.

## Completion and restart
Finishing tasks does not release the line. A person must separately confirm
quality release and staff and material availability. The original production
schedule begins at 14:30. Without earlier availability, finishing cleaning early
recovers no production time. Logging a restart records human confirmation only;
it sends no commands to equipment.

## Product quality after cleaning
Height and scrap measurements help monitor product quality; they do not certify
hygiene. Model C may abstain for about 30 minutes after a segment change because
it lacks history. An absent forecast does not establish zero risk.

## Potential value
25,000 USD/hour is an assumed margin. Results represent the potential value of
usable capacity, subject to demand, product quality and added costs. They are not
realized savings. Check overlaps before adding cleaning value to scrap savings.
