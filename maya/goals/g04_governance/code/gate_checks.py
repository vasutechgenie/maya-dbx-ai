"""G4 gate. The plan is not editable: the YAML is the single source of truth for access and masking."""
from .common import principal_kind


def plan(ctx, p):
    out = []
    for c in p["coverage"]:
        if len(c["masked_by"]) != 1:
            out.append(f"sensitive column {c['table']}.{c['column']} is masked by {c['masked_by'] or 'nothing'}")
    out += [f"grant to an individual: {g['principal']} {g['privilege']} on {g['securable']}"
            for g in p["grants"] if principal_kind(g["principal"]) == "user"]
    funcs = {f["full_name"] for f in p["functions"]}
    out += [f"policy {x['name']} on {x['schema']} uses an undefined function" for x in p["policies"] if x["function"] not in funcs]
    out += [f"column mask on {x['table']}.{x['column']} uses an undefined function" for x in p["column_masks"]
            if x["function"] not in funcs]
    out += [f"row filter on {x['table']} uses an undefined function" for x in p["row_filters"] if x["function"] not in funcs]
    writers = set(p["writers"])
    out += [f"function {f['name']} does not exempt the writers {sorted(writers - set(f['exempt']))}"
            for f in p["functions"] if not writers <= set(f["exempt"])]
    return out
