# MAYA architecture

MAYA is a goal engine. It takes an existing Bronze / Silver / Gold foundation in Unity Catalog and works through a chain
of goals until the data product is **AI Enabled** (goals G0 to G4, available now) and then **AI Ready** (goals G5 to G12,
to be released later). This page shows how the pieces fit together; each goal's own page shows its internals:

| Goal | Architecture |
|------|--------------|
| G0 Foundation intake | [maya/goals/g00_foundation](../maya/goals/g00_foundation/README.md) |
| G1 Metadata | [maya/goals/g01_metadata](../maya/goals/g01_metadata/README.md) |
| G2 Metric views | [maya/goals/g02_metric_views](../maya/goals/g02_metric_views/README.md) |
| G3 Semantic model | [maya/goals/g03_semantic_model](../maya/goals/g03_semantic_model/README.md) |
| G4 Governance and access | [maya/goals/g04_governance](../maya/goals/g04_governance/README.md) |

## 1. Overall architecture

```mermaid
flowchart LR
    subgraph people["People"]
        eng["Data engineer<br/>runs maya"]
        appr["Approvers<br/>data owner, steward, KPI owner,<br/>business owner, security"]
        cicd["CI/CD pipeline"]
    end

    subgraph project["Project repository"]
        spec["maya.yaml<br/>foundation, roles, goals"]
        cust["Customer inputs<br/>context, KPIs, taxonomy"]
        bundle["bundle/<br/>Asset Bundle written by MAYA"]
    end

    subgraph engine["MAYA engine (laptop or CI runner)"]
        cli["CLI and spec loader"] --> runner["Goal runner<br/>goal graph,<br/>prerequisite rule"]
        runner --> harness["Goal harness<br/>runs the goal's<br/>graph.yaml"]
        harness --> vc["Validator and<br/>certification"]
        vc --> status["Status and<br/>publish"]
    end

    omni["Omnigent<br/>agent runtime"]

    subgraph ws["Databricks workspace"]
        gw["AI Gateway<br/>model service"]
        job["Job maya_deploy<br/>one task per goal"]
        found["Unity Catalog foundation<br/>bronze, silver, gold"]
        deliv["Unity Catalog deliverables<br/>comments, tags, keys, metric views,<br/>ontology, masks, policies, grants"]
        state["MAYA state schema<br/>status, runs, checks, approvals,<br/>certifications, ledgers"]
        dash["Dashboards<br/>project and portfolio"]
    end

    eng --> cli
    appr -->|"maya review"| cli
    spec --> cli
    cust --> cli
    harness -->|"agent tasks"| omni --> gw
    harness -->|"read-only SQL"| found
    harness -->|"writes scripts"| bundle
    bundle -->|"bundle deploy and run"| job --> deliv
    vc -->|"checks"| deliv
    status --> state
    status -->|"republish"| dash
    cicd -->|"same bundle to test and prod"| bundle
```

Five rules hold the design together:

1. **The foundation is never changed directly.** MAYA reads it; everything a goal delivers to Unity Catalog is an
   idempotent SQL script in the project's Asset Bundle, run by the bundle's `maya_deploy` job. The same scripts are
   what CI/CD promotes.
2. **MAYA does not know environments.** It works in one (dev) workspace. Scripts name catalogs with tokens, and the
   bundle's variables map them to each environment's catalogs.
3. **Agents cannot reach the workspace.** An agent node reads a task input prepared by MAYA code and returns one
   result that must validate against a JSON Schema. Every change it suggests goes through code, a gate and the bundle.
4. **Every goal proves itself.** A goal is certified only when every mandatory check passes against the live workspace
   and its approvers have signed off (or the project approves automatically with `certification.approvals: auto`). Any later change (configuration, prerequisite, workspace drift, expiry) makes it
   stale.
5. **MAYA leaves what it did not make.** Grants, tags and comments it did not create are reported, never revoked.

## 2. Two levels of graph

The **goal graph** says which goals may run: a goal runs only when its prerequisites are certified. Inside each goal,
the **harness graph** (`harness/graph.yaml`) says how the goal does its work.

```mermaid
flowchart LR
    subgraph AE["Milestone 1 · AI Enabled (available)"]
        G0["G0 Foundation intake"] --> G1["G1 Metadata"]
        G1 --> G2["G2 Metric views"]
        G1 --> G3["G3 Semantic model"]
        G2 --> G3
        G1 --> G4["G4 Governance and access"]
        G2 --> G4
    end
    subgraph AR["Milestone 2 · AI Ready (released later; every goal also requires AI Enabled)"]
        G5["G5 Genie space"]
        G6["G6 AI/BI dashboards"]
        G7["G7 DQ monitoring"]
        G8["G8 Agent tools"]
        G9["G9 Ops MCP server"]
        G10["G10 Agents"]
        G11["G11 Evaluation"]
        G12["G12 Operations and docs"]
        G5 --> G10
        G8 --> G10
        G7 --> G9 --> G10
        G10 --> G11
        G10 --> G12
        G11 --> G12
    end
    G2 --> G5
    G3 --> G5
    G2 --> G6
    G3 --> G6
    G1 --> G7
    G2 --> G8
    G3 --> G8
    G4 --> G9
```

## 3. Inside a goal

Every goal is one package under `maya/goals/`:

| Part | What it holds |
|------|---------------|
| `goal.yaml` | The goal's standard: id, milestone, prerequisites, inputs schema, harness graph, outputs, validation checks (each tied to a checklist item), certification approvers and validity |
| `inputs.schema.json` | JSON Schema for the goal's block under `goals:` in `maya.yaml` |
| `harness/graph.yaml` | The harness graph: nodes and edges, including conditional and repair edges |
| `harness/agents/` | Omnigent agent definitions and their instructions |
| `harness/schemas/` | JSON Schemas for agent results and for every item a gate shows its approver |
| `code/` | Python the code nodes run, plus the goal's state tables and its drift check |
| `validator/checks.py` | One function per check in `goal.yaml`, each returning an observed value and evidence |

The harness runs five node types:

```mermaid
flowchart LR
    code["code<br/>MAYA Python: discover, plan,<br/>render, write bundle scripts"]
    agent["agent<br/>Omnigent agent, one task per item,<br/>schema-checked result"]
    gate["gate<br/>pauses for an approver;<br/>items schema-checked, editable"]
    validator["validator<br/>runs the goal's checks<br/>against Unity Catalog"]
    certify["certify<br/>records the certification,<br/>tags, evidence pack"]
    code --> agent --> gate --> code
    code --> validator --> certify
    validator -->|"mandatory check failed"| repair["code: repair"] --> validator

    classDef c fill:#ddf4ff,stroke:#0969da,color:#0a3069
    classDef a fill:#fbefff,stroke:#8250df,color:#3e1f79
    classDef g fill:#fff8c5,stroke:#bf8700,color:#4d2d00
    classDef v fill:#dafbe1,stroke:#1a7f37,color:#044f1e
    class code,repair c
    class agent a
    class gate g
    class validator,certify v
```

The same colours are used in every goal's page.

## 4. A goal run, end to end

```mermaid
sequenceDiagram
    autonumber
    actor E as Data engineer
    actor A as Approver
    participant M as MAYA (runner and harness)
    participant O as Omnigent agent
    participant B as Asset Bundle and maya_deploy job
    participant U as Unity Catalog (SQL warehouse)
    participant S as State schema and dashboard

    E->>M: maya run --goal G1
    M->>S: prerequisites certified? configuration valid?
    M->>U: read-only discovery and profiling
    M->>O: one task per asset
    O-->>M: schema-checked results
    M->>S: open gate, record pending approval
    M-->>E: paused at gate "review"
    A->>M: maya review --approve
    M->>B: write SQL scripts, bundle deploy, run task G1
    B->>U: comments, tags, constraints
    M->>U: run every check
    alt a mandatory check fails
        M->>B: repair scripts, deploy again
        M->>U: run the checks again
    end
    M->>S: certification, check results, evidence
    M->>S: publish status, republish dashboard
    M-->>E: G1 certified
```

## 5. Goal status

`maya status` derives each goal's status from its configuration, the state schema and the workspace:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> not_configured: no goals entry in maya.yaml
    [*] --> invalid_config: inputs do not validate
    not_configured --> blocked: configured
    invalid_config --> blocked: fixed
    [*] --> blocked: prerequisite not certified
    blocked --> ready: prerequisites certified
    ready --> running: maya run
    running --> awaiting_approval: gate reached
    awaiting_approval --> running: maya review --approve
    running --> failed: a node or mandatory check failed
    failed --> running: maya run again
    running --> certified: checks pass and approvers signed
    certified --> stale: configuration, prerequisite, drift or expiry
    stale --> running: maya run
```

## 6. Delivery and promotion

Goal code never writes deliverables to the workspace directly. It writes scripts into the project bundle; the bundle
deploys them. CI/CD deploys the same, committed bundle to other environments.

```mermaid
flowchart LR
    subgraph dev["MAYA in dev"]
        gc["Goal code<br/>apply and repair nodes"] --> sc["bundle/scripts/G1 to G4<br/>idempotent SQL with<br/>catalog tokens"]
        gc --> mf["bundle/maya_manifest.json<br/>certified run and approver<br/>per goal"]
        sc --> dep["databricks bundle deploy<br/>bundle run maya_deploy"]
        dep --> devuc["Dev Unity Catalog"]
    end
    sc --> git["Commit bundle/<br/>pull request"]
    mf --> git
    git --> ci["CI/CD<br/>milestone gate, then<br/>bundle deploy per target"]
    ci --> test["Test catalogs"]
    ci --> prod["Prod catalogs"]
```

`run_scripts.py`, the job task, replaces each `{{catalog:<dev name>}}` token with the target's catalog from the
bundle variables, so one set of files deploys anywhere. `maya status` writes `reports/status.json`, whose
`milestones` entry says whether AI Enabled is reached, so a pipeline can gate promotion on it.

## 7. State model

Each project owns its state schema (`target.state_schema`). MAYA creates and upgrades it additively on every command,
recording each table's Delta version first as a restore point.

```mermaid
erDiagram
    goal_runs ||--o{ node_runs : "has"
    goal_runs ||--o{ check_results : "produces"
    goal_runs ||--o{ approvals : "pauses for"
    goal_runs ||--o| certifications : "may earn"
    goal_status }o--|| goal_runs : "latest"
    goal_runs {
        string run_id
        string goal_id
        string status
        string config_hash
    }
    node_runs {
        string run_id
        string node
        string node_type
        int attempt
    }
    check_results {
        string run_id
        string check_id
        string checklist
        boolean passed
    }
    approvals {
        string approval_id
        string gate
        string approver
        string status
    }
    certifications {
        string run_id
        string certified_by
        timestamp expires_at
        string evidence_path
    }
    goal_status {
        string goal_id
        string status
        string run_id
    }
```

Goals add their own tables: `asset_inventory` (G0), `metadata_ledger` (G1), `metric_view_ledger` (G2),
`semantic_ledger` (G3) and `governance_ledger` (G4). The ledgers record what each certified run delivered, which is
how drift is detected. `systems` and `goal_overview` feed the project dashboard; the optional workspace registry feeds
the portfolio dashboard across projects.

## 8. Where the code lives

| Module | Role |
|--------|------|
| `maya/cli.py` | The `maya` command |
| `maya/core/spec.py` | Loads `maya.yaml` and the goal packages; resolves `${env:...}` |
| `maya/core/runner.py` | Goal graph, statuses, prerequisite rule, run and resume |
| `maya/core/harness.py`, `graph.py` | Harness graph execution and the five node types |
| `maya/core/agents.py`, `agent_tools.py` | Omnigent agents on the AI Gateway model; the tools every agent gets |
| `maya/core/gates.py` | Gate items: schema checks, approver edits, byte-for-byte approval |
| `maya/core/validation.py`, `certify.py` | Checks and certification |
| `maya/core/bundle.py`, `maya/bundle/run_scripts.py` | The project bundle and its job task |
| `maya/core/state.py`, `project.py` | State tables, upgrades, project dashboard |
| `maya/core/catalog.py`, `workspace.py` | Unity Catalog discovery and SQL access |
| `maya/status/` | Status report, publishing, dashboards |
