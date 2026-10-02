# Tutorial: take your own data foundation to AI Enabled

This tutorial is for teams who already have a data foundation in Unity Catalog (Bronze, Silver and Gold, whatever
you call them) and want MAYA to make it **AI Enabled**: every asset described and classified, every KPI a certified
metric view, a business taxonomy over the data, and governed access. It walks through every step with your own
catalogs, schemas, tables, KPIs and groups.

If you want to try MAYA first on synthetic data, do the [example tutorial](TUTORIAL.md) first. It deploys a sample
foundation and runs the same goals; this tutorial does not repeat its explanations of each check, so keep it at hand
as a reference.

The AI Enabled goals G0 to G4 are available now. The AI Ready goals G5 to G12 will be released later.

Contents

1. [How this works on your foundation](#1-how-this-works-on-your-foundation)
2. [Before you start: a checklist](#2-before-you-start-a-checklist)
3. [Step 1 · Inventory your foundation](#3-step-1--inventory-your-foundation)
4. [Step 2 · Create the project folder](#4-step-2--create-the-project-folder)
5. [Step 3 · Write maya.yaml: project, target and foundation](#5-step-3--write-mayayaml-project-target-and-foundation)
6. [Step 4 · Validate and initialise](#6-step-4--validate-and-initialise)
7. [Step 5 · G0 Foundation intake (or self-certify it)](#7-step-5--g0-foundation-intake-or-self-certify-it)
8. [Step 6 · G1 Metadata on your tables](#8-step-6--g1-metadata-on-your-tables)
9. [Step 7 · G2 Your KPIs as metric views](#9-step-7--g2-your-kpis-as-metric-views)
10. [Step 8 · G3 Your business taxonomy](#10-step-8--g3-your-business-taxonomy)
11. [Step 9 · G4 Your access model](#11-step-9--g4-your-access-model)
12. [Step 10 · Confirm AI Enabled](#12-step-10--confirm-ai-enabled)
13. [Step 11 · Commit and promote with CI/CD](#13-step-11--commit-and-promote-with-cicd)
14. [Keeping it AI Enabled](#14-keeping-it-ai-enabled)
15. [Rolling out to more data products](#15-rolling-out-to-more-data-products)
16. [Common situations on real foundations](#16-common-situations-on-real-foundations)

---

## 1. How this works on your foundation

- **MAYA does not build or change your foundation.** It never ingests, transforms or deletes your data. It reads
  your tables, and it adds metadata (comments, tags, keys), metric views, a semantic registry and access controls.
- **Only what you declare is in scope.** You list the catalogs, schemas and tables in `maya.yaml`. MAYA never scans
  anything else.
- **What already exists is kept.** By default G1 never overwrites descriptions, sensitivity tags or keys that are
  already in Unity Catalog, and G4 never revokes grants it did not make.
- **Everything is delivered through an Asset Bundle in your repository.** MAYA writes idempotent SQL scripts into
  `bundle/` next to your `maya.yaml` and deploys them in development. Your CI/CD deploys the same bundle to test and
  production.
- **You go goal by goal.** G0, then G1, then G2, then G3 and G4. Each one is certified before the next can run.

Throughout this tutorial we use a sample foundation so the steps are concrete. Replace the names with yours:

| Your foundation (sample) | Layer in MAYA |
|--------------------------|---------------|
| Catalog `retail_prod` | |
| Schema `retail_prod.raw` (landing tables from the ERP and the web shop) | `bronze` |
| Schema `retail_prod.curated` (cleaned customers, products, stores, orders, order lines) | `silver` |
| Schemas `retail_prod.marts` and `finance_prod.reporting` (daily sales, store performance) | `gold` |

---

## 2. Before you start: a checklist

Work through this list before writing any YAML. Each line names the step that needs it.

**People and decisions**

- [ ] A **data owner** who can say which schemas and tables belong to the data product (step 1).
- [ ] A **data steward** who will check the generated descriptions and sensitivity classes (step 6).
- [ ] **KPI owners** who can define each KPI and say how it is computed (step 7).
- [ ] A **business owner** who can name the business domains and subdomains (step 8).
- [ ] **Security**, who decide which groups may read what, and how sensitive data is masked (step 9).
- [ ] Whether certification is **automatic** (the default: certified as soon as every check passes) or **manual**
      (named approvers approve each gate with `maya review`). See the example tutorial, section 15.

**Access**

- [ ] A Databricks CLI profile for a user or service principal that can read every in-scope table.
- [ ] `USE CATALOG` and `CREATE SCHEMA` on one catalog where MAYA may create its own schemas (state, metric views,
      semantic registry, governance functions). It can be the foundation catalog or a separate one.
- [ ] To add metadata and access controls, ownership of, or `MANAGE` on, the in-scope tables and schemas. Ask your
      platform team if you are not the owner.
- [ ] A SQL warehouse ID, and access to an AI Gateway model (default `system.ai.claude-sonnet-4-6`).
- [ ] Serverless jobs enabled (the deploy job runs on serverless).
- [ ] **Account-level** groups for each audience (consumers, engineers and so on). Workspace-local groups such as
      `admins` and `users` cannot hold Unity Catalog grants.
- [ ] The identity that runs your foundation jobs (a service principal in production). You will exempt it from masks.

**Policy**

- [ ] Whether sample values may be sent to the model (`allow_data`). Leave it off (the default) for regulated data:
      only names, types and statistics are sent.
- [ ] Your sensitivity classes, from least to most restrictive (for example `public, internal, confidential, pii`).
      If your account already has a governed tag for sensitivity, use its key and allowed values.

**Install MAYA** as in the [example tutorial, sections 3 and 4](TUTORIAL.md#3-install-maya).

---

## 3. Step 1 · Inventory your foundation

Decide exactly which assets form the data product. Start small: one data product, its Silver and Gold schemas, and
the Bronze schemas that feed them. You can add more later.

Run these in a SQL editor (replace the catalog and schema names):

```sql
-- Tables and views per schema, with their current descriptions
SELECT table_catalog, table_schema, table_name, table_type, comment
FROM retail_prod.information_schema.tables
WHERE table_schema IN ('raw', 'curated', 'marts')
ORDER BY table_schema, table_name;

-- How much is already described
SELECT table_schema,
       count(*) AS tables,
       count_if(comment IS NULL OR comment = '') AS undescribed
FROM retail_prod.information_schema.tables
WHERE table_schema IN ('raw', 'curated', 'marts')
GROUP BY table_schema;

-- Tags already on columns (existing sensitivity classes are kept by G1)
SELECT schema_name, table_name, column_name, tag_name, tag_value
FROM retail_prod.information_schema.column_tags
WHERE schema_name IN ('raw', 'curated', 'marts');

-- Existing primary and foreign keys
SELECT table_schema, table_name, constraint_name, constraint_type
FROM retail_prod.information_schema.table_constraints
WHERE table_schema IN ('raw', 'curated', 'marts');
```

Write down:

1. **Which schemas are which layer.** MAYA's layer keys are `bronze`, `silver` and `gold`. Map your names onto them
   (for example `raw` is `bronze`). The keys matter: G4 never lets consumer roles read `bronze` or `silver`, and G3
   places `silver` and `gold` assets on pages by default.
2. **Tables to leave out**: temporary, backup, audit or staging tables (for example `*_tmp`, `*_bak`,
   `ingestion_log`).
3. **Empty tables.** G0 fails on an empty in-scope table. Exclude them or load them.
4. **Primary keys you already know** (for example `curated.customer` is unique on `customer_id`).
5. **How fresh each layer must be** (for example Gold refreshed at least every 24 hours).

---

## 4. Step 2 · Create the project folder

A MAYA project is a folder in **your** repository, typically next to the code of the data product:

```
your-repo/
  data-products/
    retail-sales/
      maya.yaml                 the project (you write it)
      context/business.yaml     business context for G1 (you write it)
      kpis/metric_views.yaml    KPI definitions for G2 (you and the KPI owners)
      semantic/taxonomy.yaml    domains, subdomains and pages for G3 (you and the business owner)
      bundle/                   generated by MAYA; commit it
      .maya/                    local run files; do not commit
      reports/                  status reports; do not commit
```

Create it:

```bash
mkdir -p data-products/retail-sales/{context,kpis,semantic}
cd data-products/retail-sales
printf '.maya/\nreports/\n' >> .gitignore
```

From now on, run every `maya` command in this folder (or pass `--system data-products/retail-sales/maya.yaml`).

---

## 5. Step 3 · Write maya.yaml: project, target and foundation

Create `maya.yaml` with the parts every goal needs. Goal blocks are added in the later steps.

```yaml
apiVersion: maya/v1
kind: System
metadata:
  name: retail-sales                         # unique per workspace; used in names, tags and the dashboard
  owner: retail-data@yourcompany.com
  description: Sales of the retail stores and the web shop, from orders to daily store performance.

target:
  profile: ${env:DATABRICKS_CONFIG_PROFILE}
  warehouse_id: ${env:MAYA_WAREHOUSE_ID}
  state_schema: retail_prod.maya_state_retail_sales   # MAYA's own state for this project (created by maya init)
  registry: retail_prod.maya_registry                 # optional: one registry shared by all projects

ai_gateway:
  model: system.ai.claude-sonnet-4-6                  # any chat model served by your AI Gateway
  harness: openai-agents

foundation:
  catalog: retail_prod                                # default catalog for the schemas below
  layers:
    bronze:
      schemas: [raw]
      tables: all
      metadata_scope: tables                          # Bronze: table descriptions only
    silver:
      schemas: [curated]
      tables: all
      metadata_scope: full                            # tables and every column
    gold:
      schemas: [marts, finance_prod.reporting]        # 'catalog.schema' takes a schema from another catalog
      tables: [daily_sales, store_performance, "fct_*"]   # names, 'schema.name' or globs
      metadata_scope: full
  exclude: ["*_tmp", "*_bak", "ingestion_log"]        # applies to every layer; each layer may add its own

goals: {}                                             # filled in from step 5 on

certification:
  approvals: auto                                     # or manual
  approvers:
    data_owner: ${env:MAYA_DATA_OWNER}
    data_steward: ${env:MAYA_DATA_STEWARD}
    kpi_owner: ${env:MAYA_KPI_OWNER}
    business_owner: ${env:MAYA_BUSINESS_OWNER}
    security: ${env:MAYA_SECURITY}

status:
  formats: [html, json]
  out_dir: reports/
```

Rules for the foundation block:

| Key | Values |
|-----|--------|
| `schema` / `schemas` | One schema or a list; `catalog.schema` for a schema outside `foundation.catalog` |
| `catalog` (per layer) | Overrides `foundation.catalog` for that layer |
| `tables` | `all` (default) or a list of names, `schema.name` or globs |
| `exclude` | Names or globs left out (per layer, plus `foundation.exclude`) |
| `metadata_scope` | `full` (default; tables and columns) or `tables` (table descriptions only) |

A schema may belong to one layer only. You do not need all three layers: a foundation with only Silver and Gold is
fine.

Approvers can be people (e-mail) or the same person for every role in development. Literal values work too; using
environment variables keeps personal addresses out of the repository. Set them, with the connection:

```bash
export DATABRICKS_CONFIG_PROFILE=<profile> MAYA_WAREHOUSE_ID=<warehouse-id>
export MAYA_DATA_OWNER=<e-mail> MAYA_DATA_STEWARD=<e-mail> MAYA_KPI_OWNER=<e-mail> \
       MAYA_BUSINESS_OWNER=<e-mail> MAYA_SECURITY=<e-mail>
```

---

## 6. Step 4 · Validate and initialise

```bash
maya validate
```

With no goal blocks yet, G0 and G1 show `ok` (they have defaults) and G2 to G4 show `-- not configured`. Errors name
the key and the rule, for example `foundation.layers.gold: 'x.y.z' must be 'schema' or 'catalog.schema'`.

```bash
maya init        # creates the state schema and the project dashboard; registers the project
maya goals       # G0 ready, the others blocked
maya plan --goal G0
```

`maya plan` prints the resolved inputs and the steps, without touching anything. Check that the layers list exactly
your schemas.

---

## 7. Step 5 · G0 Foundation intake (or self-certify it)

*Milestone 1 · AI Enabled, step 1 of 5.*

You have two options.

**Option A: let MAYA check the foundation** (recommended the first time). Add:

```yaml
goals:
  G0:
    freshness_hours: {gold: 24, silver: 48}     # optional; omit a layer to skip its freshness check
    # min_assets_per_layer: 1
    # row_counts: false                         # skip row counts on very large foundations
```

```bash
maya run --goal G0
```

G0 fails if a declared layer has no assets, an asset cannot be read, an in-scope table is empty, or a layer is older
than its `freshness_hours`. The output names the assets. Typical fixes on real foundations:

| Failure | Fix |
|---------|-----|
| A table cannot be read | Grant `SELECT` to the MAYA identity, or exclude the table |
| An empty table | Exclude it (`exclude`), or load it first |
| A stale layer | Run your foundation jobs, or relax `freshness_hours` |
| A layer with no assets | Check the schema name and the `tables` patterns with `maya plan --goal G0` |

**Option B: self-certify it.** If your platform team already guarantees the foundation, attest it instead of
running G0:

```yaml
certification:
  self_certified:
    G0: {by: data-platform@yourcompany.com, note: foundation run and monitored by the platform team, valid_until: 2027-06-30}
```

`maya goals` then shows G0 `certified` ("self-certified by ..."), and G1 works on the tables your layers declare.

---

## 8. Step 6 · G1 Metadata on your tables

*Milestone 1 · AI Enabled, step 2 of 5.*

**6a. Write the business context.** The describer agent writes better descriptions when it knows your business.
Create `context/business.yaml`:

```yaml
business: >
  A retailer with 180 stores and a web shop in three countries. Orders come from the store tills and the web shop;
  order lines hold the products sold. Stores belong to regions.
glossary:
  net sales: Order line amount after discounts and before tax, excluding returned lines.
  basket: All order lines of one order.
  comparable store: A store open for at least 13 full months.
conventions:
  - Monetary amounts are in EUR unless a column name ends in _local.
  - Dates are store-local calendar dates; timestamps are UTC.
  - Columns ending in _sk are surrogate keys; _id are source system keys.
```

**6b. Decide the classification.** Write rules for the column names your organisation already knows are sensitive.
Rules run first; the agent classifies everything the rules do not cover.

**6c. Add the G1 block:**

```yaml
goals:
  G1:
    allow_data: false                       # true only if sample values may be sent to the model
    context: context/business.yaml
    declared_keys:                          # keys you know; they must hold in the data
      - {table: curated.customer, primary_key: [customer_id]}
      - {table: curated.order_line, primary_key: [order_id, line_no]}
    sensitivity_classes: [public, internal, confidential, pii]   # least to most restrictive
    sensitivity_rules:
      - {pattern: "(?i)(^|_)e_?mail(_|$)", class: pii}
      - {pattern: "(?i)(^|_)(phone|mobile)(_|$)", class: pii}
      - {pattern: "(?i)(first|last|full)_?name", class: pii}
      - {pattern: "(?i)(street|address|postcode|zip)", class: pii}
      - {pattern: "(?i)(iban|card_number|loyalty_card)", class: confidential}
      - {pattern: "(?i)(margin|cost_price)", class: confidential}
    # overwrite_existing: false             # default: never change existing descriptions, tags or keys
    # tag_names: {sensitivity: data_classification}   # if your account already uses another key
```

If your account has a governed tag for sensitivity, set `tag_names.sensitivity` to its key and
`sensitivity_classes` to (a subset of) its allowed values, in order. Otherwise G1 fails to set a value the governed
tag does not allow.

**6d. Run it:**

```bash
maya plan --goal G1        # which assets will be profiled and described
maya run --goal G1
```

On a large foundation this takes a while: one agent call per asset. If a run stops (a timeout, a permission you then
grant), continue with `maya run --goal G1 --resume`.

**6e. Review the result with your data steward.** In Catalog Explorer, open a few Silver and Gold tables and read the
descriptions, sensitivity tags and keys. Or list what is now sensitive, which you need in step 9:

```sql
SELECT catalog_name, schema_name, table_name, column_name, tag_value
FROM retail_prod.information_schema.column_tags
WHERE tag_name = 'sensitivity' AND tag_value IN ('pii', 'confidential')
ORDER BY schema_name, table_name, column_name;
```

To correct something:

- **A wrong description or class:** fix it in Unity Catalog (Catalog Explorer or `COMMENT ON`, `SET TAGS`). G1 keeps
  existing values, so your fix stays. Then run `maya run --goal G1` again so the certified state matches.
- **A whole class of columns classified wrongly:** add a rule, then run G1 again.
- **A key the agent missed:** add it to `declared_keys`.
- **With manual approvals:** export the proposals at the review gate, edit them and approve
  (`maya review --goal G1 --export review/`, then `--edit ... --approve`).

---

## 9. Step 7 · G2 Your KPIs as metric views

*Milestone 1 · AI Enabled, step 3 of 5.*

**7a. Collect the KPIs.** With each KPI owner, write down for every KPI: its name, the business definition, the Gold
(or Silver) table it should be computed from, the dimensions people slice it by, and how they would compute it by
hand. The last point becomes the **reference SQL**: an independent query MAYA compares the metric view against.

**7b. Write `kpis/metric_views.yaml`.** One metric view per subject, usually one per Gold fact table:

```yaml
metric_views:
  - name: store_sales
    comment: Net sales, orders and basket size of every store and the web shop, by day, store, region and product category.
    owner: sales-controlling@yourcompany.com
    source: marts.daily_sales                 # 'schema.table' in your foundation (Silver or Gold)
    filter: "channel IN ('store', 'web')"     # optional
    joins:                                    # optional; quote "on"
      - {name: store, source: curated.store, "on": source.store_sk = store.store_sk}
    dimensions:
      - {name: sales_date, expr: sales_date, display_name: Sales date, comment: Store-local calendar date of the sale., synonyms: [date, day]}
      - {name: region, expr: store.region_name, display_name: Region, comment: Sales region the store belongs to., synonyms: [area]}
      - {name: category, expr: product_category, display_name: Product category, comment: Top-level category of the product sold., synonyms: [department]}
    measures:
      - name: net_sales
        expr: SUM(net_sales_eur)
        display_name: Net sales
        comment: Order line amount after discounts and before tax, excluding returned lines, in EUR.
        synonyms: [sales, revenue, turnover]
        format: {type: currency, currency_code: EUR}
        reference:
          by: [region]
          sql: |
            SELECT s.region_name AS region, SUM(l.amount_eur - l.discount_eur) AS value
            FROM retail_prod.curated.order_line l
            JOIN retail_prod.curated.orders o ON l.order_id = o.order_id
            JOIN retail_prod.curated.store s ON o.store_sk = s.store_sk
            WHERE NOT l.is_returned AND o.channel IN ('store', 'web')
            GROUP BY s.region_name
      - name: basket_size
        expr: SUM(units) / SUM(orders)
        display_name: Average basket size
        comment: Units sold per order.
        synonyms: [units per order, UPT]
        format: {type: number}
        reference:
          sql: |
            SELECT SUM(l.quantity) / COUNT(DISTINCT l.order_id) AS value
            FROM retail_prod.curated.order_line l
            JOIN retail_prod.curated.orders o ON l.order_id = o.order_id
            WHERE NOT l.is_returned AND o.channel IN ('store', 'web')
```

Writing good reference SQL:

- Compute it from a **different layer** than the metric view where you can (Silver for a Gold-based view). If both
  read the same table the same way, the comparison proves little.
- Apply the **same business rules** (filters such as returns, cancelled orders, channels). A mismatch is usually a
  rule applied in one and not the other.
- Return a column named `value`. With `reference.by`, also return one column per listed dimension, named like the
  dimension.
- Use fully qualified names (`catalog.schema.table`).

Every dimension and measure needs a `display_name`, a `comment` and `synonyms`, and every measure a `format`
(`{type: number}`, `{type: currency, currency_code: ...}` or `{type: percentage}`). These are what Genie, AI/BI and
agents will use later.

**7c. Add the G2 block and run it:**

```yaml
goals:
  G2:
    schema: maya_metrics                      # created in foundation.catalog; or 'other_catalog.schema'
    definitions: kpis/metric_views.yaml
    # tolerance: 1e-6                         # relative difference allowed against the reference
    # tags: {data_product: retail-sales}      # tags set on every metric view
```

```bash
maya run --goal G2
```

If a measure does not match, the output shows both values and where they differ. Fix the definition or the
reference, and run G2 again; only the changed views are redeployed.

Try a certified view:

```sql
SELECT `Region`, MEASURE(`Net sales`), MEASURE(`Average basket size`)
FROM retail_prod.maya_metrics.store_sales
GROUP BY ALL;
```

Adding a KPI later: add it to the file and run `maya run`; G2 turns stale and only the new view is created.

---

## 10. Step 8 · G3 Your business taxonomy

*Milestone 1 · AI Enabled, step 4 of 5.*

**8a. Agree the domains and subdomains** with the business owner. Domains are broad areas of the business; subdomains
are the topics inside them. Each has an `id` (lowercase; it becomes a tag value), a name, a description and, for
domains, an owner.

**8b. Decide, per subdomain, how much you know about its pages.** A page is a business view of related assets (for
example "Store performance"). Every Silver, Gold and metric-view asset will sit on exactly one page.

| What you know | Write |
|---------------|-------|
| Nothing yet | `pages: auto` (agents design the pages and place the assets) |
| The page names | `pages: [{name: Store performance}, {name: Web shop}]` |
| Everything | `pages: [{id, name, description, assets: [...]}]` |
| Your pages, but there may be more | Add `allow_new_pages: true` |

**8c. Write `semantic/taxonomy.yaml`:**

```yaml
domains:
  - id: sales
    name: Sales
    description: Selling products to customers in the stores and the web shop, and how each store performs.
    owner: sales-controlling@yourcompany.com
    subdomains:
      - id: store_sales
        name: Store sales
        description: Daily sales, orders and baskets of every store and region.
        pages:
          - id: store_performance
            name: Store performance
            description: Net sales, orders and basket size by store, region and day.
            assets: [marts.store_performance, maya_metrics.store_sales]
      - id: web_shop
        name: Web shop
        description: Orders placed on the web shop and how they are fulfilled.
        pages: auto

  - id: customers
    name: Customers
    description: Who buys from the stores and the web shop, their loyalty membership and segments.
    owner: crm@yourcompany.com
    subdomains:
      - id: customer_base
        name: Customer base
        description: Customer master data, loyalty membership and segmentation.
        pages: [{name: Customer profile}]

  - id: reference
    name: Reference data
    description: Shared master data used by every domain - products, stores and the calendar.
    owner: retail-data@yourcompany.com
    subdomains:
      - id: master_data
        name: Master data
        description: Conformed product, store and calendar tables.
        pages: auto

glossary:
  - term: Net sales
    definition: Order line amount after discounts and before tax, excluding returned lines.
    synonyms: [revenue, turnover]
    links: [maya_metrics.store_sales.net_sales, marts.daily_sales.net_sales_eur]
  - term: Comparable store
    definition: A store open for at least 13 full months.
    synonyms: [like-for-like store, LFL]
    links: [curated.store.is_comparable]
```

Asset names are `schema.table` (or `catalog.schema.table`); glossary links may add a column or measure. Every
metric-view measure is added to the glossary automatically (`glossary_from_metric_views: true`).

**8d. Add the G3 block and run it:**

```yaml
goals:
  G3:
    schema: maya_semantic
    taxonomy: semantic/taxonomy.yaml
    governed_tags: true                       # false if you cannot create tag policies (account level)
    # layers: [silver, gold]                  # whose assets are placed on pages
    # exclude: ["curated.*_history"]          # assets kept off the model
    # tag_keys: {domain: retail_domain, subdomain: retail_subdomain, page: retail_page}
```

Tag keys default to `<project>_domain`, `<project>_subdomain` and `<project>_page` (here `retail_sales_domain` and so
on) because governed tags are account-wide and other teams may already use plain `domain`. If tag policy creation
fails with "not authorized", set `governed_tags: false`; the tags are then plain tags with the same values.

```bash
maya run --goal G3
```

**8e. Review and adjust the pages.** After the run, the complete taxonomy, including every agent-built page and
placement, is in `.maya/runs/G3/<run_id>/artefacts/taxonomy.resolved.yaml`, in the same format as your file. Review it
with the business owner. To change something, copy that part into `semantic/taxonomy.yaml` (for example, pin an
asset to another page, or rename a page) and run G3 again. Pinned parts are never redesigned; unchanged subdomains
need no agent calls.

Try the lookup:

```sql
SELECT * FROM retail_prod.maya_semantic.ontology_lookup('like-for-like');
```

---

## 11. Step 9 · G4 Your access model

*Milestone 1 · AI Enabled, step 5 of 5.*

**9a. List the audiences** and map each to an account group or service principal:

| Role (sample) | Principal | Reads | Executes |
|---------------|-----------|-------|----------|
| Consumers (analysts, Genie and dashboard users) | `grp-retail-analysts` | Gold, metric views, semantic | `ontology_lookup()` |
| Engineers | `grp-retail-data-eng` | Every layer | Semantic and governance functions |
| Pipelines (writers) | the foundation job's service principal | (they own the tables) | |

**9b. List the sensitive columns**: the query in step 6e. Each column tagged with a class you mask (by default
`pii`) must be covered by exactly one mask.

**9c. Choose how to mask each schema or column:**

- **ABAC policy** (`abac`): one policy per schema scope masks every column with a given tag value, including columns
  added later. Best for Bronze and wide Silver schemas.
- **Column mask** (`column`): one function on one column. Best where a column needs its own masking style (keep the
  e-mail domain, show the last 4 digits).

Do not cover the same column both ways: MAYA refuses the plan and names the column.

**9d. Add the G4 block:**

```yaml
goals:
  G4:
    schema: maya_governance
    writers: ["${env:MAYA_PIPELINE_SP}"]       # service principal application ID of your foundation jobs
    catalog_access: grant                      # 'existing' if the platform team manages USE CATALOG
    sensitive: {tag: sensitivity, values: [pii, confidential]}   # what must be masked (your tag key and classes)
    roles:
      consumers:
        principals: [grp-retail-analysts]
        consumer: true                         # refused if it would read bronze or silver
        read: [gold, metric_views, semantic]
        execute: [semantic]
      engineers:
        principals: [grp-retail-data-eng]
        read: [bronze, silver, gold, metric_views, semantic, governance]
        execute: [semantic, governance]
    mask_functions:
      redact:     {kind: redact,   unmasked_for: [grp-retail-data-eng]}
      email_mask: {kind: email,    unmasked_for: [grp-retail-data-eng]}
      card_mask:  {kind: last4,    unmasked_for: []}
      cost_mask:  {kind: "null",   type: "DECIMAL(18,2)", unmasked_for: [grp-retail-data-eng]}
    masks:
      - {abac: mask_pii_raw, scopes: [bronze], match: {tag: sensitivity, value: pii}, function: redact}
      - {abac: mask_conf_raw, scopes: [bronze], match: {tag: sensitivity, value: confidential}, function: redact}
      - {column: curated.customer.email, function: email_mask}
      - {column: curated.customer.phone, function: redact}
      - {column: curated.customer.full_name, function: redact}
      - {column: curated.loyalty_card.card_number, function: card_mask}
      - {column: curated.product.cost_price, function: cost_mask}
    row_filters:
      - table: marts.store_performance
        columns: [country_code]
        by_group: {grp-retail-analysts-de: [DE], grp-retail-analysts-fr: [FR]}
        unfiltered_for: [grp-retail-data-eng]
    # audit: {view: audit_access, days: 90}
    # jobs: [retail_foundation_daily]          # jobs whose run-as identity is checked
```

Notes for real foundations:

- **Writers** must include every identity that reads masked sources to build downstream tables. Otherwise it would
  write masked values into Silver or Gold. The advisory check `writers_exempt` lists identities that wrote from
  masked tables recently but are not exempt.
- **Mask function types**: a mask's `type` must match the column's type (default `STRING`). Use one function per type.
- **Quote YAML values that look special**: `kind: "null"` (unquoted `null` is an empty value), `type: "DECIMAL(18,2)"`
  (the comma splits a `{...}` mapping), every `"${env:...}"` inside `[...]` or `{...}`, and the key `"on"` in joins.
  `maya validate` reports these.
- **Existing grants are kept.** Grants MAYA did not make stay as they are; `other_grants` and `consumer_exposure`
  list them so you can clean them up yourself.
- **Individuals cannot be granted to.** Use groups and service principals only.

**9e. Run it:**

```bash
maya plan --goal G4
maya run --goal G4
```

The first step builds the plan and checks it before anything is deployed: coverage of every sensitive column,
principals that exist, types that match. Fix what it reports and run again. The plan is written to
`.maya/runs/G4/<run_id>/artefacts/governance_plan.json`; share it with security.

Check the result:

```sql
SHOW POLICIES ON SCHEMA retail_prod.raw;
SELECT * FROM retail_prod.information_schema.column_masks WHERE table_schema = 'curated';
SELECT * FROM retail_prod.information_schema.row_filters;
SHOW GRANTS `grp-retail-analysts` ON SCHEMA retail_prod.marts;
```

Ask a member of the consumer group to query `curated.customer` (they should be refused: consumers have no Silver
access) and `marts.store_performance` (they should see only their country). As a writer or engineer you see real
values; that is by design.

---

## 12. Step 10 · Confirm AI Enabled

```bash
maya goals
maya status
```

When G0 to G4 are certified, the report ends with `milestone AI Enabled: reached`. Open `reports/status.html`, or
the project dashboard (its link is printed by `maya init` and `maya projects`), and share it with the owners.

---

## 13. Step 11 · Commit and promote with CI/CD

**Commit** the project folder: `maya.yaml`, `context/`, `kpis/`, `semantic/` and `bundle/`. Do not commit `.maya/`
or `reports/`.

**Add your environments** to `bundle/databricks.yml`. MAYA writes this file once and never rewrites it. Catalog
names in the scripts are tokens resolved from variables, so each target can point at its own catalogs:

```yaml
targets:
  dev:
    mode: development
    default: true
    workspace: {host: https://<dev-workspace>}
  prod:
    mode: production
    workspace: {host: https://<prod-workspace>}
    run_as: {service_principal_name: <prod-service-principal-application-id>}
    variables:
      warehouse_id: <prod-warehouse-id>
      catalog_retail_prod: retail_prod          # one variable per catalog, named catalog_<dev name>
      catalog_finance_prod: finance_prod
```

`bundle/resources/maya_variables.yml` lists the variables your scripts use.

**In the pipeline**, deploy and run the bundle:

```bash
cd data-products/retail-sales/bundle
databricks bundle deploy -t prod
databricks bundle run maya_deploy -t prod
```

In production, `writers` should be the production pipeline's service principal. Because `maya.yaml` reads it from an
environment variable, set that variable in your CI/CD.

Also in CI, `maya validate` is a quick offline check of every change to the YAML files.

---

## 14. Keeping it AI Enabled

AI Enabled holds only while G0 to G4 stay certified. Run `maya goals` (or schedule `maya status`) regularly. A goal
turns stale when:

| Change | What to do |
|--------|------------|
| New tables in an in-scope schema | `maya run`: G0 and G1 describe and classify only the new ones; G3 places them on pages; G4 checks the new sensitive columns are covered |
| A new column with personal data | G4 reports it unprotected unless an ABAC policy already covers its tag; add a mask if needed |
| A new or changed KPI | Edit `kpis/metric_views.yaml`; `maya run` recreates only that view |
| Someone removed a comment, tag, mask or grant MAYA delivered | The goal shows drift; `maya run` restores it |
| A new team needs access | Add a role or principal in G4; `maya run` |
| Certification expires (90 days by default) | `maya run` re-validates and re-certifies |

Keep running `maya run` until it says there is nothing to run.

---

## 15. Rolling out to more data products

- **One project per data product**, each with its own folder, `maya.yaml`, state schema and bundle.
- **Share one registry** (`target.registry`) across projects, and run `maya portfolio` to get a dashboard over all of
  them; `maya projects` lists them.
- **Reuse your conventions**: copy the sensitivity rules, mask functions and role layout from your first project.
- **Shared Silver tables**: a schema may belong to only one project's layer at a time for G1 to G4 to own its
  metadata and masks without conflict. Put shared master data in its own project, and self-certify it (G0) in the
  projects that consume it.

---

## 16. Common situations on real foundations

| Situation | What to do |
|-----------|------------|
| Our layers are called raw / curated / marts | Map them to the keys `bronze`, `silver`, `gold` in `foundation.layers` (step 3) |
| We have no Bronze in Unity Catalog | Declare only `silver` and `gold` |
| Gold is spread over several catalogs | `schemas: [marts, finance_prod.reporting]` |
| Hundreds of tables, we want to start small | List a few in `tables`, certify, then widen to `all` |
| Descriptions already written by hand | Keep `overwrite_existing: false` (default); G1 only fills the gaps |
| A governed sensitivity tag already exists | Use its key in `tag_names.sensitivity` and its values in `sensitivity_classes` |
| Sample data may not leave the workspace | Keep `allow_data: false` (default) |
| We are not the table owners | Ask for `MANAGE` on the schemas, or run MAYA as the owning service principal |
| Tag policies cannot be created | `governed_tags: false` in G3 |
| USE CATALOG is managed by the platform team | `catalog_access: existing` in G4 |
| An approver must look at each change | `certification.approvals: manual` and `maya review` |
| The foundation is certified by another team | Self-certify G0 in `certification.self_certified` |
| A goal failed half-way | `maya run --goal <G> --resume` |

For every other error message, see the [troubleshooting table](TUTORIAL.md#20-troubleshooting) in the example
tutorial.
