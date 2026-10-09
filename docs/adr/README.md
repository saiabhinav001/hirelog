# Architecture Decision Records

Each ADR captures one significant decision: its context, the choice made, and the
trade-offs accepted. They document *why* the system is shaped the way it is —
including decisions to deliberately **not** build something yet.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-hybrid-search.md) | Hybrid semantic + lexical search | Accepted |
| [0002](0002-anonymity-invariant.md) | Anonymity invariant for contributions | Accepted |
| [0003](0003-cache-tiers.md) | Multi-tier caching for dashboard & search | Accepted |
| [0004](0004-faiss-flatip-choice.md) | FAISS `IndexFlatIP` (exact) over ANN | Accepted |
| [0005](0005-rate-limiting.md) | Distributed sliding-window rate limiting | Accepted |
| [0006](0006-search-evaluation.md) | Search-quality evaluation as a CI gate | Accepted |
| [0007](0007-role-escalation-fix.md) | Fix public profile role-escalation before adding exam authority | Accepted |
| [0008](0008-exam-domain-state-machine.md) | Exam domain state machine and bounded final-submit transaction | Accepted |
| [0009](0009-kt-ingestion-and-generation.md) | KT ingestion, source registry, and provider-neutral generation | Accepted |
| [0010](0010-institution-identity-and-login.md) | Institution identity, roster import, and OTP fallback login | Accepted |
| [0011](0011-judge-grading.md) | Isolated coding judge and reproducible grading | Accepted |
| [0012](0012-faculty-student-interfaces-and-live-control.md) | Faculty/student interfaces and live control | Accepted |
| [0013](0013-proctoring-evidence-and-appeals.md) | Proctoring evidence, warning block and appeals | Accepted |
| [0014](0014-gradebook-similarity-and-feedback.md) | Gradebook, similarity review, and learning feedback | Accepted |
| [0015](0015-pwa-accessibility-and-managed-lockdown.md) | PWA shell, accessibility, and managed-lockdown admission | Accepted |
| [0016](0016-production-qualification-and-release.md) | Production qualification and release package | Accepted |
| [0017](0017-exam-role-policy-and-resource-scope.md) | Server-owned exam role policy, verified staff bootstrap, and resource scope | Accepted |
| [0018](0018-roster-linking-otp-and-teacher-assignment.md) | Roster import, account linking, OTP delivery and teacher assignment | Accepted |
| [0019](0019-exam-domain-commands-and-finalization.md) | Exam domain: publication snapshot, commands, finalization and schedule | Accepted |
| [0020](0020-durable-source-ingestion.md) | Durable, private source ingestion | Accepted |

See [../architecture.md](../architecture.md) for the system diagram and request flow.
