"""Runs one goal's SQL scripts on a SQL warehouse, in path order. Deployed by the bundle as the task of its deploy job.

Scripts name catalogs with tokens ({{catalog:<name in dev>}}) so the same files deploy to any environment: each
token is replaced by the value passed with --catalog <name in dev>=<name here> (the bundle passes its variables).
A file holds statements separated by '-- @statement' lines; a statement may be a SQL scripting block (BEGIN ... END).
A .json file declares workspace objects SQL cannot create (catalog tokens are replaced in it too):
{"tag_policies": [{"tag_key", "description", "values"}]} and
{"genie_spaces": [{"marker", "title", "description", "parent_path", "serialized_space", "permissions"}]}: each space is
found by the marker in its description, then updated in place or created; permissions are added, never removed
creates each governed tag or sets it to exactly the declared description and allowed values.
Every statement is idempotent, so a file can be re-run.

Standalone on purpose (needs only databricks-sdk): it runs in any workspace, without MAYA.

  python run_scripts.py --scripts <dir> --warehouse-id <id> --catalog dev_name=target_name [--only a.sql|b.sql] [--dry-run]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

MARK = re.compile(r"^-- @statement[ \t]*$", re.M)
TOKEN = re.compile(r"\{\{catalog:([A-Za-z0-9_\-]+)\}\}")
RESULT = "MAYA_DEPLOY_RESULT "


def statements(text: str) -> list[str]:
    out = []
    for chunk in MARK.split(text)[1:]:
        s = chunk.strip()
        if s.endswith(";"):
            s = s[:-1].rstrip()
        if s:
            out.append(s)
    return out


def substitute(sql: str, catalogs: dict) -> str:
    def one(m):
        if m.group(1) not in catalogs:
            raise KeyError(f"no --catalog value for {m.group(1)!r}")
        return catalogs[m.group(1)]
    return TOKEN.sub(one, sql)


def files(root: Path, only: str) -> list[Path]:
    every = sorted(p for p in root.rglob("*") if p.suffix in (".sql", ".json"))
    if not only.strip():
        return every
    wanted = {w.strip() for w in only.split("|") if w.strip()}
    chosen = [p for p in every if str(p.relative_to(root)) in wanted]
    missing = wanted - {str(p.relative_to(root)) for p in chosen}
    if missing:
        raise FileNotFoundError(f"scripts not found under {root}: {sorted(missing)}")
    return chosen


def execute(client, warehouse_id, sql):
    from databricks.sdk.service.sql import StatementState
    r = client.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse_id, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = client.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else str(r.status.state))


def tag_policy(client, p):
    from databricks.sdk.errors import NotFound
    body = {"tag_key": p["tag_key"], "description": p.get("description") or "",
            "values": [{"name": v} for v in p.get("values") or []]}
    path = "/api/2.1/tag-policies"
    try:
        client.api_client.do("GET", f"{path}/{p['tag_key']}")
    except NotFound:
        client.api_client.do("POST", path, body=body)
        return
    client.api_client.do("PATCH", f"{path}/{p['tag_key']}", query={"update_mask": "description,values"}, body=body)


def find_space(client, marker):
    token = None
    while True:
        r = client.api_client.do("GET", "/api/2.0/genie/spaces", query={"page_size": 100, **({"page_token": token} if token else {})})
        for sp in r.get("spaces") or []:
            if marker in (sp.get("description") or ""):
                return sp["space_id"]
        token = r.get("next_page_token")
        if not token:
            return None


def genie_space(client, warehouse_id, sp):
    parent = sp.get("parent_path") or "MAYA"
    if not parent.startswith("/"):
        parent = f"/Workspace/Users/{client.current_user.me().user_name}/{parent}"
    body = {"title": sp["title"], "description": f"{(sp.get('description') or '').rstrip()}\n\n{sp['marker']}".lstrip(),
            "serialized_space": json.dumps(sp["serialized_space"]), "warehouse_id": sp.get("warehouse_id") or warehouse_id}
    sid = find_space(client, sp["marker"])
    if sid:
        client.api_client.do("PATCH", f"/api/2.0/genie/spaces/{sid}", body=body)
    else:
        client.workspace.mkdirs(parent)
        sid = client.api_client.do("POST", "/api/2.0/genie/spaces", body={**body, "parent_path": parent})["space_id"]
    acl = [{("group_name" if p.get("group") else "service_principal_name"): p.get("group") or p["service_principal"],
            "permission_level": p.get("level", "CAN_RUN")} for p in sp.get("permissions") or []]
    if acl:
        client.api_client.do("PATCH", f"/api/2.0/permissions/genie/{sid}", body={"access_control_list": acl})
    return sid


def run_json(client, warehouse_id, rel, doc, dry_run):
    n = 0
    for i, sp in enumerate(doc.get("genie_spaces") or []):
        if dry_run:
            print(f"-- {rel} #{i + 1}: Genie space {sp['title']!r} ({sp['marker']}), "
                  f"{len(sp['serialized_space'].get('data_sources', {}).get('tables', []))} sources\n")
            continue
        try:
            print(f"   Genie space {sp['title']!r}: {genie_space(client, warehouse_id, sp)}")
        except Exception as e:
            return n, {"failed": rel, "statement": i + 1, "error": str(e)[:2000], "sql": sp["title"]}
        n += 1
    for i, p in enumerate(doc.get("tag_policies") or []):
        if dry_run:
            print(f"-- {rel} #{i + 1}: tag policy {p['tag_key']} values {p.get('values')}\n")
            continue
        try:
            tag_policy(client, p)
        except Exception as e:
            return n, {"failed": rel, "statement": i + 1, "error": str(e)[:2000], "sql": json.dumps(p)[:2000]}
        n += 1
    return n, None


def run_file(client, warehouse_id, root, f, catalogs, dry_run):
    """Statements of one file, in order. Returns (statements run, failure or None)."""
    rel = str(f.relative_to(root))
    if f.suffix == ".json":
        try:
            doc = json.loads(substitute(f.read_text(), catalogs))
        except KeyError as e:
            return 0, {"failed": rel, "statement": 0, "error": str(e), "sql": ""}
        return run_json(client, warehouse_id, rel, doc, dry_run)
    n = 0
    for i, s in enumerate(statements(f.read_text())):
        sql = substitute(s, catalogs)
        if dry_run:
            print(f"-- {rel} #{i + 1}\n{sql};\n")
            continue
        try:
            execute(client, warehouse_id, sql)
        except Exception as e:
            return n, {"failed": rel, "statement": i + 1, "error": str(e)[:2000], "sql": sql[:2000]}
        n += 1
    return n, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", required=True)
    ap.add_argument("--warehouse-id", required=True)
    ap.add_argument("--catalog", action="append", default=[], metavar="DEV=TARGET")
    ap.add_argument("--only", default="", help="'|'-separated script paths relative to --scripts (default: all)")
    ap.add_argument("--parallel", type=int, default=8, help="files of one step run in parallel (steps run in order)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    catalogs = dict(c.split("=", 1) for c in a.catalog)
    root = Path(a.scripts)
    todo = files(root, a.only) if root.exists() else []
    client = None
    if not a.dry_run:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
    steps = {}
    for f in todo:
        rel = f.relative_to(root)
        steps.setdefault(rel.parts[0] if len(rel.parts) > 1 else "", []).append(f)
    from concurrent.futures import ThreadPoolExecutor
    done, count = [], 0
    for step in sorted(steps):
        workers = 1 if a.dry_run else max(1, a.parallel)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda f: (f, *run_file(client, a.warehouse_id, root, f, catalogs, a.dry_run)),
                                    steps[step]))
        failures = [fail for _, _, fail in results if fail]
        for f, n, fail in results:
            count += n
            if not fail:
                done.append(str(f.relative_to(root)))
                print(f"ok {f.relative_to(root)}")
        if failures:
            for fail in failures:
                print(f"FAILED {fail['failed']} statement {fail['statement']}: {fail['error']}\n{fail['sql']}", file=sys.stderr)
            first = failures[0]
            print(RESULT + json.dumps({"ok": False, "done": done, "statements": count, "failed": first["failed"],
                                       "statement": first["statement"], "error": first["error"],
                                       "failures": len(failures)}))
            return 1
    print(RESULT + json.dumps({"ok": True, "done": done, "statements": count}))
    return 0


if __name__ == "__main__":
    code = main()
    if code:  # a successful job task must not call sys.exit
        sys.exit(code)
