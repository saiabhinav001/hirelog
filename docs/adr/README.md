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

See [../architecture.md](../architecture.md) for the system diagram and request flow.
