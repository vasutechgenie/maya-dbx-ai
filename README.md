# MAYA Databricks AI Accelerator

A goal engine that takes an existing Bronze/Silver/Gold data foundation and makes it AI Enabled and AI Ready.
Each goal is declared in YAML, runs as a graph harness (deterministic steps, agents, validated gates), is checked
by its own validator and is certified. Every deliverable is a script delivered through the project's Databricks
Asset Bundle; promotion between environments is left to your CI/CD.

## Goals

| Goal | Title | Status |
|------|-------|--------|
| G0 | Foundation intake | Available |
| G1 | Metadata | Available |
| G2 | Metric views | Available |
| G3 | Semantic model | Available |
| G4 | Governance and access | Available |
| G5 | Genie space | Coming later |
| G6 | AI/BI dashboards | Coming later |
| G7 | Data quality monitoring | Coming later |
| G8 | Agent tools | Coming later |
| G9 | Ops MCP server | Coming later |
| G10 | Agents | Coming later |
| G11 | Evaluation | Coming later |
| G12 | Operations | Coming later |

G5 to G12 will be released later.

## How it works

- `maya.yaml` declares the project, its foundation and the inputs of each goal. Inputs are validated against each
  goal's `inputs.schema.json` before anything runs.
- A goal runs only when its prerequisites are certified. Certification is frictionless: gates and sign-off are
  approved automatically once validation passes (`certification.approvals: auto`), and a goal that is already done
  can be self-certified in `maya.yaml` under `certification.self_certified`.
- Goal outputs are written as scripts into the project's Asset Bundle and deployed by it. MAYA writes only its own
  state and registry directly.
- Models are called through the Databricks AI Gateway.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
export DATABRICKS_CONFIG_PROFILE=<profile> MAYA_WAREHOUSE_ID=<warehouse-id> MAYA_APPROVER=<you@example.com>
# G4 roles in the commercial_analytics example: two account-level groups in your workspace
export MAYA_CONSUMER_GROUP=<consumer-group> MAYA_ENGINEER_GROUP=<engineer-group>

maya --system examples/commercial_analytics/maya.yaml init
maya --system examples/commercial_analytics/maya.yaml goals
maya --system examples/commercial_analytics/maya.yaml run G0
maya --system examples/commercial_analytics/maya.yaml status
```

Other commands: `validate`, `plan`, `review`, `bundle`, `migrate`, `projects`, `portfolio` (see `maya --help`).

## Layout

- `maya/core`: engine (spec, runner, harness, state, certification, bundle)
- `maya/goals/gNN_*`: one folder per goal with `goal.yaml`, `inputs.schema.json`, `code/`, `harness/` and `validator/`
- `maya/bundle`: the script runner deployed with the Asset Bundle
- `maya/status`: status report and dashboards
- `examples/`: `commercial_analytics` (G0 to G4) and `supply_chain`

Workspace-specific values (profile, warehouse, approver, groups) come from environment variables referenced as
`${env:NAME}` in `maya.yaml`. The examples use the catalog `solution_builder`; change `foundation.catalog`,
`target.state_schema`, `target.registry` and the SQL in `kpis/metric_views.yaml` to use another. Each example's Asset Bundle (`examples/*/bundle/`) is
generated in your checkout by `maya run` and `maya bundle` and is not committed here.

## License

Apache-2.0
