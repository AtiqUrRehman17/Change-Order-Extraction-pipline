# Confidence calibration

Fitted 2026-10-07T15:26:33 on 697 values from 20 evaluation documents. Prior strength 10 (each block is shrunk toward its raw score as if that many more values had been seen).

## Does it help? (leave-one-document-out cross-validation)

Each document is calibrated with a table fitted on the *other* documents only, so these numbers are not just memorized. ECE = average gap between confidence and accuracy (lower is better).

| Kind of value | Values | Right | ECE raw | ECE calibrated |
|---|---|---|---|---|
| both | 199 | 198 | 0.053 | 0.039 |
| conflict | 2 | 2 | 0.685 | 0.685 |
| rules_unconfirmed | 1 | 1 | 0.320 | 0.320 |
| llm_unconfirmed | 46 | 37 | 0.130 | 0.089 |
| rules_alone | 202 | 201 | 0.284 | 0.059 |
| llm_alone | 247 | 236 | 0.227 | 0.047 |
| **all** | 697 | 675 | **0.189** | **0.045** |

## The fitted table

Raw scores between two points are interpolated.

| Kind of value | Raw score | Calibrated | Values | Right |
|---|---|---|---|---|
| both | 0.94 | 0.99 | 199 | 198 |
| conflict | 0.26 | 0.33 | 1 | 1 |
| conflict | 0.37 | 0.43 | 1 | 1 |
| rules_unconfirmed | 0.68 | 0.71 | 1 | 1 |
| llm_unconfirmed | 0.46 | 0.40 | 4 | 1 |
| llm_unconfirmed | 0.61 | 0.45 | 8 | 2 |
| llm_unconfirmed | 0.68 | 0.90 | 21 | 21 |
| llm_unconfirmed | 0.77 | 0.90 | 13 | 13 |
| rules_alone | 0.34 | 0.66 | 12 | 11 |
| rules_alone | 0.41 | 0.76 | 15 | 15 |
| rules_alone | 0.59 | 0.76 | 2 | 2 |
| rules_alone | 0.65 | 0.76 | 1 | 1 |
| rules_alone | 0.68 | 0.84 | 10 | 10 |
| rules_alone | 0.77 | 0.99 | 162 | 162 |
| llm_alone | 0.52 | 0.63 | 30 | 20 |
| llm_alone | 0.76 | 0.98 | 217 | 216 |

Caveat: about 20 documents, most of them generated samples. The table is only as good as that data; refit it (`cox calibrate --mode all`) when documents are added.
