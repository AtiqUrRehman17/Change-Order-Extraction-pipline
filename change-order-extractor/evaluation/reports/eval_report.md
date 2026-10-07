# Evaluation report

Generated 2026-10-07 15:25. Document sets: real, samples.

How values are compared: money within 1 cent; CO and project numbers after cleanup ('CO-009' = '9'); names by word overlap >= 0.8; description and reason by word overlap >= 0.5; dates, days and status exactly; line items by amount. 'Correct empty' means the document doesn't state the value and none was returned; 'hallucinated' means a value was returned that the document doesn't state.

Skipped (no ground truth): SOURCES.md

## Summary

| Metric | Rules only |
|---|---|
| Documents evaluated | 20 |
| Gemini answered | - |
| Documents fully correct | 14/20 |
| Field accuracy | 88% |
| Precision (returned values right) | 100% |
| Recall (stated values found) | 83% |
| Wrong values | 1 |
| Missed values | 41 |
| Hallucinated values | 0 |
| Line items found (recall) | 33/41 (80%) |
| Line items extra | 0 |
| Calibration error (ECE, lower is better) | 0.05 |
| Review: errors caught | 5/6 (83%) |
| Review: false alarms | 2/14 (14%) |
| Not flagged and fully correct | 12/13 (92%) |
| Avg seconds per document | 0.25 |

## Accuracy per field

| Field | Rules only |
|---|---|
| change_order_number | 18/20 (90%) |
| project_name | 15/20 (75%) |
| project_number | 17/18 (94%) |
| date | 18/20 (90%) |
| contractor | 17/20 (85%) |
| owner | 18/20 (90%) |
| description | 13/18 (72%) |
| reason | 16/19 (84%) |
| line_items | 16/20 (80%) |
| subtotal | 19/20 (95%) |
| markup_amount | 19/20 (95%) |
| tax_amount | 20/20 (100%) |
| total | 18/20 (90%) |
| schedule_impact_days | 17/20 (85%) |
| status | 16/20 (80%) |
| requested_by | 19/20 (95%) |
| approved_by | 18/19 (95%) |
| approval_date | 18/20 (90%) |

## Rules only (`rules`)

### Outcomes per field

| Field | Correct | Correct empty | Wrong | Missed | Hallucinated | Precision | Recall |
|---|---|---|---|---|---|---|---|
| change_order_number | 17 | 1 | 0 | 2 | 0 | 100% | 89% |
| project_name | 15 | 0 | 0 | 5 | 0 | 100% | 75% |
| project_number | 10 | 7 | 0 | 1 | 0 | 100% | 91% |
| date | 17 | 1 | 0 | 2 | 0 | 100% | 89% |
| contractor | 16 | 1 | 0 | 3 | 0 | 100% | 84% |
| owner | 15 | 3 | 0 | 2 | 0 | 100% | 88% |
| description | 13 | 0 | 0 | 5 | 0 | 100% | 72% |
| reason | 10 | 6 | 0 | 3 | 0 | 100% | 77% |
| line_items | 12 | 4 | 1 | 3 | 0 | 92% | 75% |
| subtotal | 8 | 11 | 0 | 1 | 0 | 100% | 89% |
| markup_amount | 9 | 10 | 0 | 1 | 0 | 100% | 90% |
| tax_amount | 1 | 19 | 0 | 0 | 0 | 100% | 100% |
| total | 18 | 0 | 0 | 2 | 0 | 100% | 90% |
| schedule_impact_days | 16 | 1 | 0 | 3 | 0 | 100% | 84% |
| status | 12 | 4 | 0 | 4 | 0 | 100% | 75% |
| requested_by | 2 | 17 | 0 | 1 | 0 | 100% | 67% |
| approved_by | 5 | 13 | 0 | 1 | 0 | 100% | 83% |
| approval_date | 5 | 13 | 0 | 2 | 0 | 100% | 71% |

### Confidence calibration

Every returned value, grouped by its confidence. A well-calibrated system has accuracy close to the average confidence in each row (gap > 0 = overconfident).

| Confidence | Values | Avg confidence | Accuracy | Gap |
|---|---|---|---|---|
| 0.0-0.5 | 1 | 0.36 | 100% | -0.64 |
| 0.5-0.7 | 5 | 0.62 | 100% | -0.38 |
| 0.7-0.9 | 34 | 0.78 | 97% | -0.19 |
| 0.9-1.0 | 162 | 0.99 | 100% | -0.01 |

Expected calibration error (ECE): **0.053**

### Review flag

| A document 'has errors' when… | Flagged | Errors caught | False alarms | Not flagged and correct |
|---|---|---|---|---|
| any scored field is wrong | 7/20 | 5/6 (83%) | 2/14 (14%) | 12/13 (92%) |
| total, CO number or date is wrong | 7/20 | 3/3 (100%) | 4/17 (24%) | 13/13 (100%) |

### Accuracy by merge source

| Source | Values | Right | Accuracy |
|---|---|---|---|
| rules | 202 | 201 | 100% |

### Validation checks (errors and review warnings)

'Real problem' = the document itself has it. 'Caught our error' = the document is fine, but a field the check names is wrong in our output, so the alarm was still useful. 'False alarm' = nothing was wrong.

| Check | Raised | Real problem | Caught our error | False alarm | Missed | Useful |
|---|---|---|---|---|---|---|
| ambiguous_date | 1 | 0 | 0 | 1 | 0 | 0% |
| approval_before_date | 1 | 1 | 0 | 0 | 0 | 100% |
| missing_change_order_number | 3 | 1 | 2 | 0 | 0 | 100% |
| missing_date | 3 | 1 | 2 | 0 | 0 | 100% |
| missing_total | 2 | 0 | 2 | 0 | 0 | 100% |
| subtotal_mismatch | 1 | 0 | 1 | 0 | 0 | 100% |
| total_mismatch | 2 | 1 | 0 | 1 | 1 | 50% |

### Every wrong field (for error analysis)

| Document | Field | Outcome | Expected | Got | Conf. | Source |
|---|---|---|---|---|---|---|
| co_003_email.txt | project_name | missed | Harbor View Apartments | - | - | none |
| co_003_email.txt | contractor | missed | Kim Electric | - | - | none |
| co_003_email.txt | description | missed | Add 12 additional duplex outlets in units 4A-4D. | - | - | none |
| co_003_email.txt | reason | missed | Owner request | - | - | none |
| co_003_email.txt | status | missed | pending | - | - | none |
| co_006_missing_fields.txt | project_name | missed | Pinecrest Residence | - | - | none |
| co_006_missing_fields.txt | description | missed | Add 4 recessed lights in kitchen + dimmer switch. | - | - | none |
| co_007_shorthand.txt | project_name | missed | Riverside Clinic | - | - | none |
| co_007_shorthand.txt | description | missed | Additional outlets in rooms 204 and 206. | - | - | none |
| co_007_shorthand.txt | reason | missed | Owner request | - | - | none |
| co_007_shorthand.txt | line_items | missed | 2 items ($1,250.00): $800.00, $450.00 | - | - | none |
| co_008_scanned.pdf | change_order_number | missed | 4 | - | - | none |
| co_008_scanned.pdf | project_name | missed | Westgate Retail Center | - | - | none |
| co_008_scanned.pdf | project_number | missed | 1187 | - | - | none |
| co_008_scanned.pdf | date | missed | 2025-02-18 | - | - | none |
| co_008_scanned.pdf | contractor | missed | Delta Mechanical Inc. | - | - | none |
| co_008_scanned.pdf | owner | missed | Westgate Partners LP | - | - | none |
| co_008_scanned.pdf | description | missed | Relocate rooftop unit RTU-3 curb 6 ft east to clear new skylight opening. | - | - | none |
| co_008_scanned.pdf | reason | missed | Design change (Architect ASI-12) | - | - | none |
| co_008_scanned.pdf | line_items | missed | 3 items ($5,990.00): $3,200.00, $1,850.00, $940.00 | - | - | none |
| co_008_scanned.pdf | subtotal | missed | $5,990.00 | - | - | none |
| co_008_scanned.pdf | markup_amount | missed | $718.80 | - | - | none |
| co_008_scanned.pdf | total | missed | $6,708.80 | - | - | none |
| co_008_scanned.pdf | schedule_impact_days | missed | 2 days | - | - | none |
| co_008_scanned.pdf | status | missed | approved | - | - | none |
| co_008_scanned.pdf | approved_by | missed | M. Alvarez | - | - | none |
| co_008_scanned.pdf | approval_date | missed | 2025-02-21 | - | - | none |
| hidalgo_tx_co1.pdf | project_name | missed | McAllen Health Center Improvement Project | - | - | none |
| hidalgo_tx_co1.pdf | date | missed | 2025-10-21 | - | - | none |
| hidalgo_tx_co1.pdf | contractor | missed | RGV Construction LLC | - | - | none |
| hidalgo_tx_co1.pdf | owner | missed | The County of Hidalgo | - | - | none |
| hidalgo_tx_co1.pdf | line_items | missed | 2 items ($0.00): $3,075.00, -$3,075.00 | - | - | none |
| hidalgo_tx_co1.pdf | total | missed | $0.00 | - | - | none |
| hidalgo_tx_co1.pdf | schedule_impact_days | missed | 8 days | - | - | none |
| hidalgo_tx_co1.pdf | status | missed | pending | - | - | none |
| sumter_county_fl_notice9.pdf | change_order_number | missed | 9 | - | - | none |
| sumter_county_fl_notice9.pdf | description | missed | Steel changes per ASI#5: slip connections at one end of each truss, East and South walls | - | - | none |
| sumter_county_fl_notice9.pdf | line_items | wrong | 2 items ($9,700.00): $9,700.00, $0.00 | 1 items ($0.00): $0.00 | 0.72 | rules |
| sumter_county_fl_notice9.pdf | schedule_impact_days | missed | 0 days | - | - | none |
| sumter_county_fl_notice9.pdf | status | missed | approved | - | - | none |
| sumter_county_fl_notice9.pdf | requested_by | missed | Jay Veillette | - | - | none |
| sumter_county_fl_notice9.pdf | approval_date | missed | 2025-01-02 | - | - | none |
