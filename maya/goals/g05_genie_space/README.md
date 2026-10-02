# G5 · Genie space

**Milestone:** AI Ready &nbsp;·&nbsp; **Prerequisites:** G2, G3 &nbsp;·&nbsp; **Certified by:** business owner
&nbsp;·&nbsp; **Certification valid:** 90 days

G5 delivers a Genie space over the metric views and Gold that answers business questions correctly. The customer supplies business rules, the questions users ask per page and benchmarks with expected answers. MAYA builds the space from the certified semantic model, the bi_author agent fills each page up to the minimum number of questions, and Genie's answers must pass the benchmark threshold before the business owner signs off.

## Architecture

```mermaid
flowchart LR
    subgraph IN["Inputs"]
        direction LR
        qf["genie/questions.yaml<br/>business rules,<br/>questions per page"]
        bf["genie/benchmarks.yaml<br/>questions with<br/>expected answer SQL"]
        gi["goals.G5<br/>title, pass_threshold,<br/>access"]
        sm["G3 semantic model<br/>pages, page objects,<br/>glossary"]
        mv["G2 metric views<br/>measures, dimensions"]
    end
    subgraph H["Harness graph (harness/graph.yaml)"]
        direction TB
        load["load<br/>sources, pages, rules;<br/>every customer SQL runs"] --> author["author<br/>bi_author agent,<br/>one task per page"]
        author --> assemble["assemble<br/>instructions, sample questions,<br/>trusted SQL; agent SQL proven"]
        assemble --> review["review<br/>business owner approves<br/>the space"]
        review --> apply["apply<br/>write bundle declaration,<br/>deploy"]
        apply --> validate["validate<br/>11 checks, Genie answers<br/>the benchmarks"]
        validate -->|"mandatory check failed"| repair["repair"] --> validate
        validate -->|"mandatory checks pass"| sign_off["sign_off<br/>business owner approves<br/>benchmark results"]
        sign_off --> mark["mark<br/>record ledger"]
        mark --> certify["certify"]
    end
    subgraph OUT["Outputs"]
        direction LR
        bun["bundle/scripts/G5<br/>genie_space.json"]
        gs["Genie space<br/>sources, instructions, questions,<br/>trusted SQL, benchmarks, access"]
        br["benchmark_results.json<br/>evidence"]
        led["genie_ledger<br/>state table"]
        cert["G5 certification<br/>valid 90 days"]
    end
    IN --> H
    H -->|"apply: bundle deploy"| OUT
    bun -->|"maya_deploy job"| gs

    classDef c fill:#ddf4ff,stroke:#0969da,color:#0a3069
    classDef a fill:#fbefff,stroke:#8250df,color:#3e1f79
    classDef g fill:#fff8c5,stroke:#bf8700,color:#4d2d00
    classDef v fill:#dafbe1,stroke:#1a7f37,color:#044f1e
    classDef io fill:#f6f8fa,stroke:#8c959f,color:#24292f
    class load,assemble,apply,repair,mark c
    class author a
    class review,sign_off g
    class validate,certify v
    class qf,bf,gi,sm,mv,bun,gs,br,led,cert io
```

Node colours: blue = MAYA code, purple = Omnigent agent, yellow = approval gate, green = validator and certification, grey = inputs and outputs. See [the architecture overview](../../../docs/ARCHITECTURE.md) for how goals fit together.

## What the customer provides

- The business rules Genie must follow (what revenue means, which defaults apply, how names are shown).
- The questions users ask, ideally at least five per semantic page, in their own words; trusted SQL for the most important ones.
- Ten or more benchmark questions with the SQL that gives the right answer, and the pass threshold.
- Which groups may use the space (their data access comes from G4), and a business owner who approves the space and the benchmark results.

## Checks

The goal is certified only when all 9 mandatory checks pass against the live workspace and the approvers have
signed off. Advisory checks are reported and never block.

| Check | Severity | Checklist | What it proves |
|-------|----------|-----------|----------------|
| `space_exists` | mandatory | AR-1.1 | The Genie space exists and is the one MAYA manages for this project |
| `space_as_approved` | mandatory | AR-1.2 | Sources |
| `curated_sources_only` | mandatory | AR-1.1 | Every data source is a metric view or a Gold asset; none is Bronze (Silver only where allowed) |
| `instructions_complete` | mandatory | AR-1.2 | The instructions state every business rule |
| `questions_per_page` | mandatory | AR-1.2 | Every page of the semantic model has at least the minimum number of sample questions |
| `benchmarks_defined` | mandatory | AR-1.3 | At least the minimum number of benchmark questions |
| `benchmarks_pass` | mandatory | AR-1.3 | Genie answers at least the pass threshold of the benchmark questions correctly |
| `trusted_sql_runs` | mandatory | AR-1.4 | Every trusted SQL in the space runs on the warehouse and reads only the space's sources |
| `access_granted` | mandatory | AR-1.1 | Every declared group or service principal holds its permission on the space |
| `critical_questions_trusted` | advisory | AR-1.4 | Questions marked critical without trusted SQL (informational) |
| `benchmark_review` | advisory | AR-1.3 | Benchmark answers Genie's evaluation could not grade automatically (informational) |

## Package contents

| File | Role |
|------|------|
| `code/apply.py` | Apply: the approved space becomes one bundle declaration, deployed through the project bundle |
| `code/common.py` | Shared helpers: the certified semantic model, the space's sources, SQL checks, the space definition and its comparison with the live space |
| `code/gate_checks.py` | Extra checks on gate items and approver edits |
| `code/ledger.py` | Ledger state table: what each run delivered, used for drift |
| `code/space.py` | Load, author and assemble: checks the customer's files, prepares each page for the bi_author agent and builds the space |
| `goal.yaml` | The goal's standard: prerequisites, outputs, checks, certification |
| `harness/agents/bi_author.md` | Agent instructions |
| `harness/agents/bi_author.yaml` | Omnigent agent definition |
| `harness/graph.yaml` | The harness graph shown above |
| `harness/schemas/authoring.schema.json` | Schema an agent's result must match |
| `harness/schemas/gates/benchmark_results.schema.json` | Schema of an item an approver reviews at a gate |
| `harness/schemas/gates/space.schema.json` | Schema of an item an approver reviews at a gate |
| `inputs.schema.json` | JSON Schema for this goal's block under `goals:` in `maya.yaml` |
| `validator/checks.py` | One function per check |

## Learn more

- Run it on the example: [Tutorial, 18.1 G5 Genie space](../../../docs/TUTORIAL.md#181-g5-genie-space)
- Run it on your own foundation: [Tutorial, 17. Next: G5 Genie space on your data product](../../../docs/TUTORIAL_YOUR_FOUNDATION.md#17-next-g5-genie-space-on-your-data-product)
