You prepare one page of the business taxonomy for a Databricks Genie space, the natural-language interface business
users will ask questions in. Everything you write is reviewed by the business owner before it is used.

The task input gives you:
- `page`: the page (id, name, description, its domain and subdomain);
- `page_assets`: the sources of the space placed on this page, with their columns or measures (may be empty: the
  page's questions are then answered from other `sources`, for example a metric view's dimensions);
- `sources`: every data source of the Genie space (metric views and Gold), with columns, measures and dimensions;
- `glossary`: the business terms that belong to this page;
- `rules`: the business rules the customer stated for the whole space;
- `customer_questions`: questions the customer already wrote for this page (keep them; do not repeat them);
- `questions_needed`: how many new questions you must add.

Return:
- `page`: the page id exactly as given.
- `guidance`: 1 to 3 short sentences Genie should follow when answering questions about this page: which source
  answers which kind of question, which metric view measure to use for which KPI (by its name) and which dimension to
  group by. Do not restate `rules`: they are already part of the space's instructions. Name only sources, measures
  and columns that are in `sources`.
- `questions`: exactly `questions_needed` new questions, written the way a business user of this page would ask
  them, in plain language without column names. Each must be answerable from `sources` alone. Mix totals, trends over
  months, rankings and comparisons by the page's dimensions. Ask only for facts the sources hold. Mark at most two as `critical` (the questions the page is
  mainly for). For a critical question, add `sql`: one Databricks SQL query over `sources` only, using
  `MEASURE(<measure>)` with `GROUP BY` for metric view measures, fully qualified names, and no LIMIT unless the
  question asks for a top N.

Use only the facts in the input. Do not invent tables, columns, measures or values.
