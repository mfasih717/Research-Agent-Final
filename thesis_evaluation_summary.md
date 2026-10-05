# Thesis Evaluation Summary

## Table 3.4 / Overall Metrics
- Attempted retrieval requests (N): 81
- Execution success rate: 97.53%
- Execution Accuracy: 85.19%
- Answer Accuracy: 69.14%
- Schema accuracy: 91.36%
- JOIN Accuracy: 86.27%
- Mean response time: 1.71 s
- Median response time: 1.708 s
- P95 response time: 2.957 s

## Table 4.4 / Query Category Results
| Category | Attempted N | Execution Accuracy | Answer Accuracy |
|---|---:|---:|---:|
| Aggregation | 9 | 4/9 (44.44%) | 3/9 (33.33%) |
| Comparison and ranking | 9 | 7/9 (77.78%) | 4/9 (44.44%) |
| Date filtering | 9 | 9/9 (100.0%) | 9/9 (100.0%) |
| Department relation | 9 | 9/9 (100.0%) | 9/9 (100.0%) |
| Designation relation | 9 | 9/9 (100.0%) | 9/9 (100.0%) |
| Direct retrieval | 9 | 9/9 (100.0%) | 9/9 (100.0%) |
| Grouping | 9 | 9/9 (100.0%) | 9/9 (100.0%) |
| Multiple conditions | 9 | 4/9 (44.44%) | 1/9 (11.11%) |
| Multiple relations | 9 | 9/9 (100.0%) | 3/9 (33.33%) |

## Table 4.5 / Language Results
| Language | Attempted | Execution Accuracy | Answer Accuracy |
|---|---:|---:|---:|
| Urdu | 27 | 85.19% | 70.37% |
| Roman Urdu | 27 | 85.19% | 70.37% |
| English | 27 | 85.19% | 66.67% |

## Section 4.7 / JOIN Evaluation
- JOIN Accuracy: 86.27%

## Table 4.7 / Urdu Voice Evaluation
- Attempted voice cases: 0
- Mean WER: Not measured (audio files missing)
- Entity preservation: Not measured (audio files missing)
- Voice query success: Not measured (audio files missing)

## Table 4.8 / Security and Session
- Security Rejection Rate: 100.0%
- Benign false-rejection rate: 0.0%
- invalid_credentials_rejected: PASS
- configured_inactivity_timeout: PASS
- one_minute_inactivity_expiry: PASS

## Table 4.9 / Response Time
| Request group | Timed attempts | Mean | Median | P95 |
|---|---:|---:|---:|---:|
| Direct retrieval | 9 | 1.675 | 1.741 | 2.747 |
| Relational JOIN | 27 | 1.682 | 1.86 | 2.819 |
| Aggregate/grouped | 27 | 1.64 | 1.621 | 2.844 |
| Date/multiple filters | 18 | 1.876 | 1.774 | 3.357 |
| Urdu voice | 0 | Not measured | Not measured | Not measured |

## Notes
- SQL Generation Correctness in Table 3.4 still requires researcher/expert semantic review of generated SQL; the runner records every generated SQL for that review.
- Diagnostics for ambiguity, spelling variants, duplicate names and NULL values are reported separately and are not mixed into the primary paired-language accuracy denominator.
- If voice audio files V01.wav–V09.wav are absent, voice metrics remain unmeasured rather than being fabricated.