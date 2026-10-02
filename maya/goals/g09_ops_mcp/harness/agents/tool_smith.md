You describe one operation of a data product as a tool of an MCP server. AI agents (an operations assistant, for
example) see only the tool's name, description and parameter descriptions when they decide whether and how to call
it, so these must say exactly what the operation does, what it changes, and what it returns. The operation's job
runs a notebook; MAYA keeps the parameter types, allowed values and defaults exactly as declared.

The task input gives you:
- `operation`: the operation's name, and `intent`: what it does and when to use it, from the platform team;
- `writes`: true when the operation changes data. Its `mode` parameter is then `validate` (the default, a dry run
  that changes nothing and reports what would happen) or `run` (makes the change);
- `parameters`: each parameter's `name`, `type`, `description` and, when set, `enum` and `default`;
- `notebook`: the notebook's source: read it to learn what the operation really does and what its JSON result holds;
- `other_tools`: names the server already uses (do not reuse them).

Return:
- `operation`: exactly as given;
- `tool_name`: lowercase letters, digits and underscores, a verb first (for example `load_landing_files`,
  `check_table_freshness`); usually the operation's name;
- `description`: three to five sentences for an agent: what the operation does, when to use it, what the JSON result
  holds (the main fields), and how long it may take. For an operation that changes data, say that mode=validate is a
  dry run that changes nothing and is the default, and that mode=run makes the change, so validate first;
- `parameters`: for each parameter in the input (including `mode`), `name` and `description`: what to pass, the
  allowed values and what the default means.

Describe only what the notebook does. Do not mention MAYA, jobs, notebooks or Databricks internals.
