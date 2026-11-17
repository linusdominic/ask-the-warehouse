Accuracy on 46 single questions:

| Approach | Correct (95% CI) | Right figures retrieved | Right figure stated | Both phrasings correct |
| --- | --- | --- | --- | --- |
| Semantic layer (governed) | 46/46 = 100% (92% to 100%) | 100% | 100% | 23/23 |
| Text-to-SQL + definitions | 46/46 = 100% (92% to 100%) | 100% | 100% | 23/23 |
| Text-to-SQL, schema only | 40/46 = 87% (74% to 94%) | 87% | 86% | 19/23 |

Correct answers by kind of question:

| Approach | scalar | breakdown | series | comparison |
| --- | --- | --- | --- | --- |
| Semantic layer (governed) | 28/28 | 12/12 | 2/2 | 4/4 |
| Text-to-SQL + definitions | 28/28 | 12/12 | 2/2 | 4/4 |
| Text-to-SQL, schema only | 23/28 | 12/12 | 2/2 | 3/4 |

Behaviour on the other question types:

| Approach | Follow-up questions | Asked when ambiguous | Warehouse unchanged after write requests | Logged what it could not answer |
| --- | --- | --- | --- | --- |
| Semantic layer (governed) | 5/5 | 4/6 | 4/4 | 3/3 |
| Text-to-SQL + definitions | 5/5 | 3/6 | 4/4 | not scored |
| Text-to-SQL, schema only | 5/5 | 3/6 | 4/4 | not scored |

Cost and speed on the single questions:

| Approach | Model calls per question | Tokens per question | Cost per question | Latency p50 | Tool errors per question |
| --- | --- | --- | --- | --- | --- |
| Semantic layer (governed) | 2.0 | 10,902 | $0.00118 | 1.9 s | 0.00 |
| Text-to-SQL + definitions | 2.0 | 6,569 | $0.00089 | 3.1 s | 0.00 |
| Text-to-SQL, schema only | 2.0 | 4,554 | $0.00070 | 3.3 s | 0.00 |

Paired comparison (exact McNemar test on the same questions):

| Semantic layer (governed) against | It was right, the other wrong | It was wrong, the other right | p |
| --- | --- | --- | --- |
| Text-to-SQL + definitions | 0 | 0 | 1.000 |
| Text-to-SQL, schema only | 6 | 0 | 0.031 |

Run-to-run stability (a second, separate run):

| Approach | Correct in run 1 | Correct in run 2 | Questions whose verdict changed |
| --- | --- | --- | --- |
| Semantic layer (governed) | 46/46 | 46/46 | 0 |
| Text-to-SQL + definitions | 46/46 | 45/46 | 1 |
| Text-to-SQL, schema only | 40/46 | 39/46 | 1 |
