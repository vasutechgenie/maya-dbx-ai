"""G7 checks. They read back what the bundle delivered and what the check job recorded, and test-fire every alert,
so they certify the monitoring that actually runs."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from maya.core.workspace import SqlError, ident, lit

from ..code import live
from ..code.common import ALERTS, JOB_KEY, TABLES, VIEWS, alert_key, bundle_ids, dev_mode, plan, q, sensitive_columns
from ..code.deliver import _sql, alert_queries, definition, lakeview

ORDER = {"CAN_READ": 0, "CAN_RUN": 1, "CAN_EDIT": 2, "CAN_MANAGE": 3}


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _ids(ctx):
    cache = ctx.__dict__.setdefault("_g7_ids", {})
    if "ids" not in cache:
        cache["ids"] = bundle_ids(ctx)
    return cache["ids"]


def _s(ctx):
    return q(plan(ctx)["schema"])


def missing_dq_tables(ctx):
    cat, sch = plan(ctx)["schema"].split(".")
    try:
        have = {r["table_name"] for r in ctx.ws.sql(f"SELECT table_name FROM {ident(cat)}.information_schema.tables "
                                                    f"WHERE table_schema = {lit(sch)}")}
    except SqlError as e:
        return _result([{"kind": "tables", "problem": f"cannot read the quality schema: {e}"}])
    return _result([{"kind": "tables", "object": n, "problem": "missing"} for n in TABLES + VIEWS if n not in have])


def rule_differences(ctx):
    want = {r["id"]: json.dumps(definition(r), sort_keys=True) for r in plan(ctx)["rules"]}
    try:
        have = {r["rule_id"]: r["definition"] for r in ctx.ws.sql(f"SELECT rule_id, definition FROM {_s(ctx)}.dq_rules")}
    except SqlError as e:
        return _result([{"kind": "rules", "problem": f"cannot read dq_rules: {e}"}])
    items = [{"kind": "rules", "rule": r, "problem": "not deployed"} for r in want if r not in have]
    items += [{"kind": "rules", "rule": r, "problem": "deployed but not approved"} for r in have if r not in want]
    items += [{"kind": "rules", "rule": r, "problem": "deployed definition differs"} for r in want
              if r in have and json.loads(have[r]) != json.loads(want[r])]
    return _result(items)


def _latest_run(ctx):
    rows = ctx.ws.sql(f"SELECT max_by(check_run_id, started_at) AS id, max(started_at) AS started_at "
                      f"FROM {_s(ctx)}.dq_runs WHERE finished_at IS NOT NULL")
    return rows[0] if rows and rows[0]["id"] else None


def rules_without_results(ctx):
    spec, applied = plan(ctx), (ctx.read_artefact("apply.json") or {}).get("applied_at")
    try:
        run = _latest_run(ctx)
    except SqlError as e:
        return _result([{"kind": "results", "problem": f"cannot read dq_runs: {e}"}])
    if not run:
        return _result([{"kind": "results", "problem": "the check job has not finished a run"}])
    started = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
    started = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    if applied and started < datetime.fromisoformat(applied).replace(microsecond=0) - _skew():
        return _result([{"kind": "results", "problem": f"the latest check run ({run['started_at']}) is older than this delivery"}])
    have = {r["rule_id"] for r in ctx.ws.sql(f"SELECT DISTINCT rule_id FROM {_s(ctx)}.dq_results WHERE check_run_id = {lit(run['id'])}")}
    fresh = {r["table_name"] for r in ctx.ws.sql(f"SELECT DISTINCT table_name FROM {_s(ctx)}.dq_freshness "
                                                 f"WHERE check_run_id = {lit(run['id'])}")}
    items = [{"kind": "results", "rule": r["id"], "problem": "no result in the latest check run"} for r in spec["rules"] if r["id"] not in have]
    items += [{"kind": "results", "table": f["table"], "problem": "no freshness in the latest check run"}
              for f in spec["freshness"] if f["table"] not in fresh]
    return _result(items, check_run=run["id"], started_at=str(run["started_at"]))


def _skew():
    from datetime import timedelta
    return timedelta(minutes=5)


def sensitive_quarantine_columns(ctx):
    spec = plan(ctx)
    hidden = sensitive_columns(ctx, {r["table"] for r in spec["rules"]})
    try:
        rows = ctx.ws.sql(f"""SELECT DISTINCT table_name, k FROM (
              SELECT table_name, explode(json_object_keys(row_json)) AS k FROM {_s(ctx)}.dq_quarantine WHERE row_json IS NOT NULL
              UNION ALL
              SELECT table_name, explode(json_object_keys(row_key)) AS k FROM {_s(ctx)}.dq_quarantine WHERE row_key IS NOT NULL)""")
    except SqlError as e:
        return _result([{"kind": "quarantine", "problem": f"cannot read dq_quarantine: {e}"}])
    items = [{"kind": "quarantine", "table": r["table_name"], "column": r["k"], "problem": "a sensitive column is quarantined"}
             for r in rows if r["k"] in set(hidden.get(r["table_name"]) or ())]
    return _result(items, columns_checked=len(rows))


def dashboard_gaps(ctx):
    spec = plan(ctx)
    sid, dash = live.dashboard(ctx, spec)
    if not sid:
        return _result([{"kind": "dashboard", "problem": f"no dashboard {spec['dashboard']['title']!r}"}])
    items = [{"kind": "dashboard", "problem": d} for d in live.differences(lakeview(spec), dash["serialized"])]
    pub = live.published(ctx, sid)
    if not pub:
        items.append({"kind": "dashboard", "problem": "the dashboard is not published"})
    elif (pub.revision_create_time or "") < (dash["update_time"] or ""):
        items.append({"kind": "dashboard", "problem": "the latest version is not published"})
    elif bool(pub.embed_credentials) != (spec["dashboard"]["credentials"] == "embedded"):
        items.append({"kind": "dashboard", "problem": "published with the wrong credential mode"})
    for name, (title, sql) in _sql(spec).items():
        try:
            ctx.ws.sql(f"SELECT * FROM ({sql}) LIMIT 1")
        except SqlError as e:
            items.append({"kind": "dashboard", "dataset": title, "problem": f"query fails: {str(e).splitlines()[0][:200]}"})
    return _result(items, dashboard_id=sid, url=live.url(ctx, sid))


def missing_access(ctx):
    spec = plan(ctx)
    if not spec["readers"]:
        return _result([])
    cat, sch = spec["schema"].split(".")
    held = {}
    for r in ctx.ws.sql(f"SELECT grantee, privilege_type FROM {ident(cat)}.information_schema.schema_privileges "
                        f"WHERE schema_name = {lit(sch)}"):
        held.setdefault(r["grantee"], set()).add(r["privilege_type"].replace("_", " "))
    sid, _ = live.dashboard(ctx, spec)
    perms = live.dashboard_permissions(ctx, sid) if sid else {}
    items = []
    for p in spec["readers"]:
        who, level = p.get("group") or p["service_principal"], p.get("level", "CAN_RUN")
        lacking = {"USE SCHEMA", "SELECT"} - held.get(who, set()) - ({"USE SCHEMA", "SELECT"} if "ALL PRIVILEGES" in held.get(who, ()) else set())
        if lacking:
            items.append({"kind": "access", "principal": who, "problem": f"lacks {', '.join(sorted(lacking))} on {spec['schema']}"})
        if max((ORDER.get(x, -1) for x in perms.get(who, ())), default=-1) < ORDER[level]:
            items.append({"kind": "access", "principal": who, "problem": f"does not hold {level} on the dashboard"})
    return _result(items)


def job_differences(ctx):
    spec = plan(ctx)
    jid = (_ids(ctx).get("jobs") or {}).get(JOB_KEY)
    j = live.job(ctx, jid) if jid else None
    if not j:
        return _result([{"kind": "job", "problem": "the check job is not deployed"}])
    items, notes = [], []
    want, have = spec["schedule"], j.get("schedule") or {}
    if have.get("quartz_cron_expression") != want["cron"] or have.get("timezone_id") != want["timezone"]:
        items.append({"kind": "job", "problem": f"schedule {have.get('quartz_cron_expression')} {have.get('timezone_id')}, "
                                                f"declared {want['cron']} {want['timezone']}"})
    pause = "PAUSED" if want.get("paused") else "UNPAUSED"
    if have.get("pause_status") != pause:
        if dev_mode(ctx) and have.get("pause_status") == "PAUSED":
            notes.append("the schedule is paused because the bundle target is in development mode; "
                         "production targets run it as declared")
        else:
            items.append({"kind": "job", "problem": f"schedule is {have.get('pause_status')}, declared {pause}"})
    users = {x["user"] for x in spec["recipients"] if x.get("user")}
    missing = users - set((j.get("email_notifications") or {}).get("on_failure") or [])
    if missing:
        items.append({"kind": "job", "problem": f"failures do not notify {', '.join(sorted(missing))}"})
    dests = {x["destination"] for x in spec["recipients"] if x.get("destination")}
    missing = dests - {d.get("id") for d in (j.get("webhook_notifications") or {}).get("on_failure") or []}
    if missing:
        items.append({"kind": "job", "problem": f"failures do not notify destinations {', '.join(sorted(missing))}"})
    task = next((t for t in j.get("tasks") or [] if t.get("task_key") == "checks"), None)
    params = ((task or {}).get("spark_python_task") or {}).get("parameters") or []
    if not task or not any(str(p).endswith("/jobs/G7") for p in params):
        items.append({"kind": "job", "problem": "the job does not run the delivered checks (jobs/G7)"})
    return _result(items, job_id=jid, notes=notes)


def _subs(a):
    subs = ((a.get("evaluation") or {}).get("notification") or {}).get("subscriptions") or []
    return sorted(json.dumps({k: s[k] for k in ("user_email", "destination_id") if s.get(k)}, sort_keys=True) for s in subs)


def _ws(text):
    return " ".join(str(text or "").split())


def alert_differences(ctx):
    spec = plan(ctx)
    queries = alert_queries(spec)
    want_subs = sorted(json.dumps({"user_email": x["user"]} if x.get("user") else {"destination_id": x["destination"]},
                                  sort_keys=True) for x in spec["recipients"])
    items = []
    for name in ALERTS:
        aid = (_ids(ctx).get("alerts") or {}).get(alert_key(name))
        a = live.alert(ctx, aid) if aid else None
        if not a:
            items.append({"kind": "alert", "alert": name, "problem": "not deployed"})
            continue
        ev = a.get("evaluation") or {}
        if _ws(a.get("query_text")) != _ws(queries[name]):
            items.append({"kind": "alert", "alert": name, "problem": "query differs from the declared one"})
        if ev.get("comparison_operator") != "GREATER_THAN" or \
                float(((ev.get("threshold") or {}).get("value") or {}).get("double_value", -1)) != 0:
            items.append({"kind": "alert", "alert": name, "problem": "condition is not 'value > 0'"})
        if _subs(a) != want_subs:
            items.append({"kind": "alert", "alert": name, "problem": "subscribers differ from the recipients"})
    return _result(items)


def _fire(ctx, name, aid):
    s = _s(ctx)
    note = f"MAYA test of {name} ({ctx.run_id})"
    out = {"alert": name, "alert_id": aid}
    try:
        ctx.ws.sql(f"INSERT INTO {s}.dq_alert_tests VALUES ({lit(name)}, {lit(note)}, current_timestamp())")
        try:
            r = live.evaluate(ctx, aid)
            out.update(test_state=r["state"], test_run_id=r["run_id"])
        finally:
            ctx.ws.sql(f"DELETE FROM {s}.dq_alert_tests WHERE alert = {lit(name)} AND note = {lit(note)}")
        r = live.evaluate(ctx, aid)
        out.update(state=r["state"], run_id=r["run_id"])
    except Exception as e:
        out["error"] = str(e)[:500]
    return out


def alerts_not_firing(ctx):
    ids = _ids(ctx).get("alerts") or {}
    todo = [(n, ids.get(alert_key(n))) for n in ALERTS]
    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        tests = list(pool.map(lambda x: _fire(ctx, *x) if x[1] else {"alert": x[0], "error": "not deployed"}, todo))
    ctx.write_artefact("alert_tests.json", {"tested_at": datetime.now(timezone.utc).isoformat(), "tests": tests})
    items = [{"kind": "fire", "alert": t["alert"], "problem": t.get("error") or f"state {t.get('test_state')} with a test row"}
             for t in tests if t.get("test_state") != "TRIGGERED"]
    return _result(items, tests=tests)


def failing_rules(ctx):
    try:
        rows = ctx.ws.sql(f"SELECT rule_id, severity, failed_rows, total_rows FROM {_s(ctx)}.dq_latest WHERE NOT passed "
                          f"ORDER BY severity, rule_id")
    except SqlError:
        return _result([])
    return _result([{"rule": r["rule_id"], "severity": r["severity"],
                     "problem": f"{r['failed_rows']} of {r['total_rows']} rows fail"} for r in rows])


def stale_tables(ctx):
    try:
        rows = ctx.ws.sql(f"SELECT table_name, age_hours, max_hours, error FROM {_s(ctx)}.dq_freshness_latest WHERE stale")
    except SqlError:
        return _result([])
    return _result([{"table": r["table_name"], "problem": r["error"] or f"last changed {r['age_hours']} hours ago "
                                                                         f"(limit {r['max_hours']})"} for r in rows])


def notification_gaps(ctx):
    return _result([] if plan(ctx)["recipients"] else
                   [{"problem": "no recipients declared (goals.G7.recipients): alerts fire but notify nobody"}])
