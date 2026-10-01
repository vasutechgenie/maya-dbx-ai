"""G4 apply: the approved plan becomes bundle scripts, deployed through the project bundle.

scripts/G4/00_schema/governance.sql        governance schema
scripts/G4/05_remove/removed.sql           what certified plans delivered and this plan no longer declares
                                           (revokes, dropped policies, masks and row filters)
scripts/G4/10_functions/<function>.sql     mask and row filter functions
scripts/G4/20_policies/<schema>.<name>.sql tag-based (ABAC) column mask policies
scripts/G4/20_masks/<table>.sql            per-column masks of one table
scripts/G4/30_row_filters/<table>.sql      the table's row filter
scripts/G4/40_grants/<role>.sql            the role's grants (never a revoke of anything MAYA did not grant)
scripts/G4/50_audit/<view>.sql             audit view over system.access.audit for the product schemas"""
from maya.core import bundle
from maya.core.workspace import ident, lit

from . import ledger
from .common import plan as current_plan
from .common import q


def _securable(g):
    return f"{g['securable_type']} {q(g['securable'])}"


def _grant(g):
    return f"GRANT {g['privilege']} ON {_securable(g)} TO `{g['principal']}`"


def _revoke(g):
    return f"REVOKE {g['privilege']} ON {_securable(g)} FROM `{g['principal']}`"


def _drop_policy(p):
    return (f"BEGIN\n  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN END;\n"
            f"  DROP POLICY {ident(p['name'])} ON SCHEMA {q(p['schema'])};\nEND")


def _function(f):
    params = ", ".join(f"{ident(n)} {t}" for n, t in f["params"])
    return [f"CREATE OR REPLACE FUNCTION {q(f['full_name'])}({params}) RETURNS {f['returns']}\n"
            f"COMMENT {lit(f['comment'])}\nRETURN {f['body']}"]


def _policy(p):
    exc = f"\nEXCEPT {', '.join(f'`{x}`' for x in p['except'])}" if p["except"] else ""
    return [f"CREATE OR REPLACE POLICY {ident(p['name'])} ON SCHEMA {q(p['schema'])}\nCOMMENT {lit(p['comment'])}\n"
            f"COLUMN MASK {q(p['function'])}\nTO `account users`{exc}\nFOR TABLES\n"
            f"MATCH COLUMNS hasTagValue({lit(p['tag'])}, {lit(p['value'])}) AS c\nON COLUMN c"]


def _audit(a):
    where = " OR ".join(f"request_params['full_name_arg'] = {lit(s)} OR request_params['full_name_arg'] LIKE {lit(s + '.%')}"
                        for s in a["schemas"])
    return [f"""CREATE OR REPLACE VIEW {q(a['full_name'])}
COMMENT 'Access and change events on the data product schemas, last {a['days']} days (MAYA G4)'
AS SELECT event_time, event_date, user_identity.email AS principal, service_name, action_name,
       request_params['full_name_arg'] AS object_full_name, response.status_code AS status_code, source_ip_address
FROM system.access.audit
WHERE event_date >= current_date() - INTERVAL {int(a['days'])} DAYS AND ({where})"""]


def scripts(ctx, p) -> dict:
    files = {"00_schema/governance.sql": [f"CREATE SCHEMA IF NOT EXISTS {q(p['schema'])} "
                                          "COMMENT 'Governance: mask and row filter functions, audit view (MAYA G4)'"]}
    r = p["removed"]
    removal = ([_drop_policy(x) for x in r["policies"]]
               + [f"ALTER TABLE {q(x['table'])} ALTER COLUMN {ident(x['column'])} DROP MASK" for x in r["column_masks"]]
               + [f"ALTER TABLE {q(x['table'])} DROP ROW FILTER" for x in r["row_filters"]]
               + [_revoke(g) for g in r["grants"]])
    if removal:
        files["05_remove/removed.sql"] = removal
    files.update({f"10_functions/{f['name']}.sql": _function(f) for f in p["functions"]})
    files.update({f"20_policies/{x['schema']}.{x['name']}.sql": _policy(x) for x in p["policies"]})
    by_table = {}
    for m in p["column_masks"]:
        by_table.setdefault(m["table"], []).append(
            f"ALTER TABLE {q(m['table'])} ALTER COLUMN {ident(m['column'])} SET MASK {q(m['function'])}")
    files.update({f"20_masks/{t}.sql": s for t, s in by_table.items()})
    files.update({f"30_row_filters/{f['table']}.sql":
                  [f"ALTER TABLE {q(f['table'])} SET ROW FILTER {q(f['function'])} ON ({', '.join(ident(c) for c in f['columns'])})"]
                  for f in p["row_filters"]})
    files.update({f"40_grants/{role}.sql": [_grant(g) for g in gs] for role, gs in p["role_grants"].items() if gs})
    files[f"50_audit/{p['audit']['view']}.sql"] = _audit(p["audit"])
    return files


def _catalogs(p):
    names = [p["schema"], *p["audit"]["schemas"], *(m["table"] for m in p["column_masks"]),
             *(g["securable"] for g in p["grants"])]
    return {n.split(".")[0] for n in names}


def _deploy(ctx, changed_only):
    p = current_plan(ctx)
    files = scripts(ctx, p)
    base = bundle.scripts_dir(ctx.system, ctx.goal.id)
    stale = [str(x.relative_to(base)) for x in bundle.script_files(base) if str(x.relative_to(base)) not in files]
    written = bundle.write(ctx, files, catalogs=_catalogs(p), remove=stale)
    todo = bundle.undelivered(ctx, written) if changed_only and ctx.inputs.get("mode") != "full" else written
    res = bundle.deploy(ctx, todo)
    ctx.log(f"     {'apply' if changed_only else 'repair'}: {len(todo)} of {len(files)} scripts deployed")
    return {"scripts": len(files), "deployed_scripts": len(todo), "removed_files": stale, **res}


def apply(ctx):
    return _deploy(ctx, changed_only=True)


def repair(ctx):
    return _deploy(ctx, changed_only=False)


def record(ctx):
    ledger.record(ctx, current_plan(ctx))
    return {"recorded": True}


def export(ctx) -> dict:
    p = ledger.certified(ctx)
    return {"files": scripts(ctx, p), "catalogs": _catalogs(p)} if p else {"files": {}}
