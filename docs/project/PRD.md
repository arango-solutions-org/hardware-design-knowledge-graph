# Product Requirements Document: Integrated Circuit (IC) Design Knowledge Graph Demo

> **Scope Note (March 2026):** The original POC scope (§1–4) targeted OR1200 only. The Temporal Knowledge Graph Extension (below) expanded to four processors (OR1200, IBEX, MOR1KX, Marocchino) and is the current production scope. The `feature/temporal-kg` branch has been merged to `main`.

## Context

The source contains both structured and unstructured data. This PRD outlines an approach to ETL structured data into a Knowledge Graph and GraphRAG import unstructured data, harmonizing them in the same graph using Entity Resolution for cross-referencing. The project realizes the functional requirements of the **Design Knowledge Management System (DKMS)** research co-authored for the Air Force Materiel Command (see [docs/research/DKMS_Foundations.md](../research/DKMS_Foundations.md)).

## Approach

This approach integrates GitHub commit history (temporal data) with harmonized RTL (structured) and Documentation (unstructured) graphs, addressing key requirements for versioning and "Semantic Bridges."

## Value Demonstration Strategy

The POC demonstrates value through addressing common challenges in hardware design knowledge management:

1. **Semantic Bridge & Retrieval Precision**
   - Challenge: Vector stores may retrieve semantically similar but structurally unrelated documentation
   - Solution: Graph traversal constraints from RTL nodes to documentation chunks
   - Benefit: Reduced token consumption and improved accuracy

2. **Time-Travel & Design Evolution**
   - Challenge: Tracking requirement validity across design changes
   - Solution: Git history integration enabling temporal queries
   - Example: "Show which requirements became invalid when ALU connectivity changed in Commit #48"

3. **Distributed Scale**
   - Challenge: Managing billions of edges in graph databases
   - Solution: Sharded clusters with SmartGraph traversal
   - Benefit: Keep related IP blocks on same shard to minimize network hops

4. **Knowledge Risk Mitigation**
   - Challenge: "Bus Factor" risks where critical IP is understood by only one engineer
   - Solution: Automated graph analysis of commit history to identify single points of failure
   - Benefit: Proactive succession planning and accelerated onboarding for new engineers

--------------------------------------------------------------------------------

## Product Requirements Document (PRD) for POC Demo

**Title**: "Hierarchical Semantic Bridge" & Temporal Graph Demo (Project OR1200)  
**Target Audience**: Technical Stakeholders & Engineering Teams  
**Objective**: Demonstrate how ArangoDB can harmonize fine-grained RTL, temporal version history, and professional-grade GraphRAG documentation into a single, queryable "Digital Twin" of hardware design.

### 1. Data Sources & Ingestion Strategy

**Structured Source (RTL)**: openrisc/or1200 GitHub Repository
- ETL Action: Decompose Verilog files into Modules, Ports, Signals, and Logic Chunks (always blocks/assigns)
- Graph Schema: `(Module)-[CONTAINS]->(SubModule/LogicChunk)`, `(Module)-[HAS_PORT]->(Port)`, `(Module)-[HAS_SIGNAL]->(Signal)`

**Unstructured Source (Docs)**: ArangoGraphRAG Output
- ETL Action: Integrate with collections: `OR1200_Entities`, `OR1200_Chunks`, and `OR1200_Documents`
- Graph Schema: `(Entities)-[MENTIONS]->(Chunks)`

**Temporal Source (Git)**: Full commit history
- ETL Action: Capture hash, author, and timestamp
- Graph Schema: `(Commit)-[MODIFIED]->(Module)`

### 2. Functional Requirements (The "Harmonization")

**Feature A: Hierarchical Semantic Bridge**
- Requirement: Link RTL elements at all levels (Modules, Ports, Signals, Logic) to Documentation
- Implementation: Use the Arango Entity Resolution Library and ArangoSearch to bridge technical identifiers against technical terms in the specification
- Results:
  - `(RTL_Module/Port/Signal) -[RESOLVED_TO]-> (OR1200_Golden_Entities)`
  - `(RTL_LogicChunk) -[REFERENCES]-> (OR1200_Chunks)`
- Value: Allows AI agents to trace a specification requirement down to the specific line of code or logic block that implements it

**Feature B: Temporal Design Audit**
- Requirement: Support "Time-Travel" queries that filter by graph structure and history
- User Story: "Show me the documentation entities associated with the modules modified by a specific author during the last ALU redesign"
- Technical: Graph traversal from `Commit -> Module -> Documentation Entity`

**Feature C: Engineering Quality & Searchability**
- Requirement: All source code must be searchable and indexed
- Implementation: Store Verilog `code_content` on nodes and create an ArangoSearch View for hybrid semantic/lexical retrieval

**Feature D: Knowledge Transfer & Risk Assessment**
- Requirement: Analyze human factors and organizational risks in the design lifecycle
- User Story: "Identify high-risk modules with a 'Bus Factor' of 1 (single maintainer) and generate a knowledge transfer plan for new hires"
- Technical: Analysis of `(Author)-[MAINTAINS]->(Module)` and `(Author)-[AUTHORED]->(Commit)` edges to calculate expertise scores and risk metrics
- Value: proactive identification of knowledge gaps and automated onboarding support

### 3. Success Metrics (Acceptance Criteria)

1. **Granularity**: Successful creation of >4,500 vertices and >4,800 edges representing the hardware's internal structure
2. **Bridge Coverage**: Resolve at least 250+ semantic links across the design hierarchy (Modules, Ports, and Signals)
3. **Knowledge Insight**: Successfully map author expertise to 100% of modules and identify at least 3 "Bus Factor 1" risks
4. **Searchability**: Retrieval of source code snippets directly from documentation-driven queries in under 200ms

### 4. Demonstration Visualization

The demo should demonstrate a visualization (using ArangoDB Graph Viewer) showing:

1. **Blue Nodes**: The RTL hierarchy (Modules, Ports, Signals)
2. **Yellow Nodes**: Logic Chunks (the behavioral implementation)
3. **Green Nodes**: Documentation Entities (requirements and specifications)
4. **Gold Edges**: The "Hierarchical Bridge" linking RTL to documentation
5. **Temporal Filter**: Filtering by commit history to watch the design evolve

## Value Proposition

This POC demonstrates how to unify code and specifications into a single queryable graph, enabling engineers to:
- Trace every line of code to its corresponding specification
- Track design evolution through Git history
- Ensure design never drifts from documented requirements
- Reduce LLM token usage through precise graph-based retrieval

---

## Temporal Knowledge Graph Extension

**Branch:** merged to `main`  
**Status:** Phases 1–4 complete and in production; Phase 5 (agentic swarm) defined but not yet implemented.

### Background and Motivation

The OR1200 graph above captures a **static snapshot** of the design at its final state. This extension upgrades it into a **temporal knowledge graph** that records the design as it evolved *commit-by-commit* and extends ingestion to include related open-source IC designs from different eras and architectural lineages.

The primary new capability is **cross-repo Déjà Vu detection**: an agentic system working on a current design problem can recognize that an analogous design situation occurred in a prior open-source project at a comparable stage, surfacing what happened next in that project (solution, bug, rework, release).

### Goals

| ID | Goal | Status |
|---|---|---|
| G1 | Temporal Graph — ingest commit-by-commit with `valid_from`/`valid_to` epoch metadata | ✅ Complete |
| G2 | Multi-Repo Ingestion — ≥3 additional IC repos | ✅ Complete (4 repos) |
| G3 | Cross-Repo Semantic Bridge — `CROSS_REPO_SIMILAR_TO` / `CROSS_REPO_EVOLVED_FROM` edges | ✅ Structural bridges live |
| G4 | Local GraphRAG Pipeline — no AMP dependency, offline-capable | ✅ Pipeline built (`src/local_graphrag/`) |
| G5 | Design Situation Index — named `DesignSituation` nodes auto-generated from graph | ✅ 722 situations across 4 repos |
| G6 | Merged to `main` — temporal graph is the production scope | ✅ `feature/temporal-kg` merged to `main` |

### Ingested Repositories

| Repo | Architecture | Commits | Epochs | Situations |
|---|---|---|---|---|
| `openrisc/or1200` | OpenRISC 1000 (primary) | 10 | 5 | 2 |
| `openrisc/mor1kx` | OpenRISC 1000 successor | 819 | 100 | 179 |
| `openrisc/or1k_marocchino` | OpenRISC OOO w/ 64-bit FPU | 68 | 12 | 20 |
| `lowRISC/ibex` | RISC-V 32 (OpenTitan core) | 2,908 | 268 | 521 |

### Design Epoch Taxonomy

Each commit is assigned to a named epoch by `etl_epoch_detector.py` using four rules (in priority order):

| Epoch Type | Example Label | How Assigned |
|---|---|---|
| `initial_commit` | `initial_commit` | First commit in repo |
| `milestone_<tag>` | `milestone_5_2` | Git release tag present on this commit |
| `period_<YYYY_MM>` | `period_2022_09` | >180 days elapsed since last epoch boundary |
| `major_refactor_<sha7>` | `major_refactor_8b42024` | >15% of all RTL files changed in one commit |

### Design Situation Index

`DesignSituation` nodes represent named, cross-referenceable structural patterns. Auto-generated by `src/situation_detector.py`. Three heuristics:

- **`subsystem_addition`** — new top-level RTL module introduced at this epoch
- **`release_prep`** — epoch ends with a `milestone_*` git tag
- **`major_refactor`** — epoch label starts with `major_refactor_*`

### Déjà Vu of Design — User Stories

| ID | Story |
|---|---|
| DS-1 | As an engineer adding a new cache controller, I want the system to show how OR1200 implemented its data cache controller and which spec sections guided it. |
| DS-2 | As an agentic design assistant, when a multi-state FSM is added to a pipeline module, surface analogous FSM additions from OR1200 and mor1kx at the equivalent design stage. |
| DS-3 | As a principal architect, compare how OR1200 and ibex handled bus interface integration and what the outcome was for each project. |
| DS-4 | As a researcher, query: "Show all design situations across all ingested repos that involved adding out-of-order execution." |
| DS-5 | As an engineer, alert me when the current commit introduces a structural pattern that historically preceded a known bug or rework event in another project. |

### Success Criteria Update

| Metric | Target | Status |
|---|---|---|
| OR1200 temporal coverage | 100% commits + `valid_from_commit` on all RTL nodes | ✅ |
| Multi-repo ingestion | ≥3 repos | ✅ (4 repos, ~3,800 commits total) |
| Cross-repo bridges | `CROSS_REPO_SIMILAR_TO` edges (≥0.7 similarity) | ✅ 61 structural bridges |
| Architectural lineage | `CROSS_REPO_EVOLVED_FROM` edges | ✅ 8 lineage edges |
| Semantic bridges | `RESOLVED_TO` edges | ✅ 193 bridges |
| Design situation index | Auto-generated `DesignSituation` nodes | ✅ 721 situations across 381 epochs |
| Local GraphRAG | Full pipeline without AMP | ✅ `src/local_graphrag/` |
| Temporal query correctness | State-as-of-commit AQL templates | ✅ Validated (see `docs/project/TEMPORAL_IMPLEMENTATION.md §7`) |
| Test suite | Comprehensive unit tests | ✅ 213 tests, CI on Python 3.10 and 3.11 |
| Merged to main | `feature/temporal-kg` merged | ✅ |

### ChronoGraph on the Arango Platform

ChronoGraph (`viz/`, the temporal/provenance visualizer) can be hosted on an
Arango platform cluster as a user-defined service (`viz/DEPLOY.md`). There:

1. **Each person reads as themselves.** The service reads the temporal graph
   with the signed-in platform user's login, forwarded by the gateway, on the
   injected cluster endpoint with TLS verified against the injected CA. The
   default bundle carries no database account or password.
2. **Access follows database permissions.** A user without `ro`/`rw` on the
   database gets `403` naming the database; a missing, expired or rejected
   login gets `401`. Nothing cached from another user's request is served to
   a user without access; access is rechecked at least every five minutes.
3. **Diagnosable without secrets.** `GET /api/platform/diagnostics` reports
   what the platform provides (endpoint, CA, TLS result, forwarded login,
   sidecar identity) and never a token or claim value.
4. **Fallback for clusters without platform login.** `package.sh
   --with-credentials` bakes a dedicated, database-scoped service account
   (never `root` or a `_system` administrator unless deliberately allowed) and
   turns platform login off.

### Next Steps

1. **GraphRAG on doc directories** — run `src/local_graphrag/` on OR1200/mor1kx/ibex docs to populate `{PREFIX}Entities`, `{PREFIX}Communities`, and expand `CROSS_REPO_EVOLVED_FROM` lineage edges
2. **Phase 5 (optional)** — agentic blackboard architecture (CommitWatcher, PatternMatcher, DocDrift, AlertPublisher agents) — see `docs/project/TEMPORAL_IMPLEMENTATION.md §9`

*Full technical reference: [`docs/project/TEMPORAL_IMPLEMENTATION.md`](TEMPORAL_IMPLEMENTATION.md)*
