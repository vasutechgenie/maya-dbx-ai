# G8 · Agent tools

**Milestone:** AI Ready &nbsp;·&nbsp; **Prerequisites:** G2, G3 &nbsp;·&nbsp; **Certified by:** security
&nbsp;·&nbsp; **Certification valid:** 90 days

G8 turns the business actions agents may call into Unity Catalog SQL table functions with typed parameters and a description on the function, every parameter and every result column. The product owner lists the tools with example calls and what they must return; the tool_smith agent drafts each function's SQL on the metric views and Gold and its descriptions. MAYA dry-runs every example, delivers the functions and the EXECUTE grants through the project bundle, runs every example as the agent identity and lists the tools through the workspace's managed MCP server.

## Architecture

```mermaid
flowchart LR
    subgraph IN["Inputs"]
        direction LR
        tf["tools/tools.yaml<br/>tools, parameters,<br/>example calls, expectations"]
        gi["goals.G8<br/>schema, executors"]
        mv["G2 metric views,<br/>G3 lookup, Gold"]
        id["agent_identity<br/>service principal,<br/>OAuth secret scope"]
    end
    subgraph H["Harness graph (harness/graph.yaml)"]
        direction TB
        load["load<br/>tools file, readable<br/>sources, reuse drafts"] --> draft["draft<br/>tool_smith agent,<br/>one task per tool"]
        draft --> plan["plan<br/>compile, dry-run every<br/>example, redraft once"]
        plan --> review["review<br/>security approves functions<br/>and executors"]
        review --> apply["apply<br/>bundle scripts:<br/>schema, functions, grants"]
        apply --> validate["validate<br/>7 checks, tests run<br/>as the agent identity"]
        validate -->|"mandatory check failed"| repair["repair"] --> validate
        validate -->|"mandatory checks pass"| sign_off["sign_off<br/>security approves<br/>the test results"]
        sign_off --> mark["mark<br/>record ledger"]
        mark --> certify["certify"]
    end
    subgraph OUT["Outputs"]
        direction LR
        bun["bundle/scripts/G8"]
        fn["Tool functions<br/>typed, self-describing"]
        gr["EXECUTE grants<br/>agent identity, groups"]
        mcp["Managed MCP server<br/>/api/2.0/mcp/functions"]
        led["tools_ledger<br/>state table"]
        cert["G8 certification<br/>valid 90 days"]
    end
    IN --> H
    H -->|"apply: bundle deploy"| OUT
    bun -->|"maya_deploy job"| fn
    fn --> mcp

    classDef c fill:#ddf4ff,stroke:#0969da,color:#0a3069
    classDef a fill:#fbefff,stroke:#8250df,color:#3e1f79
    classDef g fill:#fff8c5,stroke:#bf8700,color:#4d2d00
    classDef v fill:#dafbe1,stroke:#1a7f37,color:#044f1e
    classDef io fill:#f6f8fa,stroke:#8c959f,color:#24292f
    class load,plan,apply,repair,mark c
    class draft a
    class review,sign_off g
    class validate,certify v
    class tf,gi,mv,id,bun,fn,gr,mcp,led,cert io
```

Node colours: blue = MAYA code, purple = Omnigent agent, yellow = approval gate, green = validator and certification, grey = inputs and outputs. See [the architecture overview](../../../docs/ARCHITECTURE.md) for how goals fit together.

## What the customer provides

- The tools agents may call (CR-AR-3.1): per tool its intent, typed parameters and example calls with what they must return (row counts, columns, or rows that equal independent SQL); optionally the SQL itself.
- The agent identity: a service principal with an OAuth secret in a secret scope the deployer can read, and its data access as a G4 role.
- Who else may run the tools, and a security approver for the functions and grants (CR-AR-3.2).

## Checks

The goal is certified only when all 6 mandatory checks pass against the live workspace and the approvers have
signed off. Advisory checks are reported and never block.

| Check | Severity | Checklist | What it proves |
|-------|----------|-----------|----------------|
| `tools_deployed` | mandatory | AR-3.1 | Every tool is a SQL table function with exactly the approved parameters and result columns |
| `sql_as_approved` | mandatory | AR-3.1 | Each function runs exactly the approved SQL |
| `self_describing` | mandatory | AR-3.2 | The function |
| `tests_pass` | mandatory | AR-3.3 | Every example call returns what the product owner expects |
| `execute_granted` | mandatory | AR-3.3 | Every executor |
| `mcp_listed` | mandatory | AR-3.4 | The workspace's managed MCP server lists every tool with its description and typed inputs |
| `numbers_tested` | advisory | AR-3.3 | Tools whose tests check only the shape of the result |

## Package contents

| File | Role |
|------|------|
| `code/apply.py` | Apply, repair, record and export: the approved plan as bundle scripts (schema, functions, grants), deployed |
| `code/common.py` | Shared helpers: the tools file, readable schemas, the SQL of a tool function (DDL, typed calls) and how a test result is compared |
| `code/gate_checks.py` | Extra checks on gate items and approver edits |
| `code/ledger.py` | Ledger state table: what each run delivered, used for drift |
| `code/tools.py` | Load and plan: the tools, the sources they may read, the tool_smith drafts and the dry run of every example |
| `goal.yaml` | The goal's standard: prerequisites, outputs, checks, certification |
| `harness/agents/tool_smith.md` | Agent instructions |
| `harness/agents/tool_smith.yaml` | Omnigent agent definition |
| `harness/graph.yaml` | The harness graph shown above |
| `harness/schemas/gates/tool_plan.schema.json` | Schema of an item an approver reviews at a gate |
| `harness/schemas/gates/tool_tests.schema.json` | Schema of an item an approver reviews at a gate |
| `harness/schemas/tool_draft.schema.json` | Schema an agent's result must match |
| `inputs.schema.json` | JSON Schema for this goal's block under `goals:` in `maya.yaml` |
| `validator/checks.py` | One function per check |

## Learn more

- Run it on the example: [Tutorial, 18.4 G8 Agent tools](../../../docs/TUTORIAL.md#184-g8-agent-tools)
- Run it on your own foundation: [Tutorial, 20. Next: G8 agent tools on your data product](../../../docs/TUTORIAL_YOUR_FOUNDATION.md#20-next-g8-agent-tools-on-your-data-product)
