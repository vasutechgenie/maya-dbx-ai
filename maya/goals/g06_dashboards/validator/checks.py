"""G6 checks. They read the dashboard back from the workspace and run every tile's query, so they certify what
viewers actually get."""
import re

from ..code.common import (dashboard_permissions, definition, differences, find_dashboard, live_dashboard,
                           live_metric_views, published, schedules, semantic_model, serialized, subscriptions, tile_rows,
                           tile_sql, url, user_id)

ORDER = {"CAN_READ": 0, "CAN_RUN": 1, "CAN_EDIT": 2, "CAN_MANAGE": 3}
MEASURE = re.compile(r"^MEASURE\(`?([A-Za-z_]\w*)`?\)$", re.I)
DIMENSION = re.compile(r"^`?([A-Za-z_]\w*)`?$")


def _result(items, **extra):
    return {"observed": len(items), "evidence": {"items": items[:200], **extra}}


def _live(ctx):
    """The live dashboard, read once per validation."""
    cache = ctx.__dict__.setdefault("_g6_live", {})
    if "id" not in cache:
        sid = find_dashboard(ctx, definition(ctx))
        cache["id"], cache["dash"] = sid, (live_dashboard(ctx, sid) if sid else None)
    return cache["id"], cache["dash"]


def _datasets(dash):
    return {d.get("name"): d for d in dash["serialized"].get("datasets") or []}


def _tiles(dash):
    """(page, widget) for every widget that shows data (filters and text excluded)."""
    for p in dash["serialized"].get("pages") or []:
        for item in p.get("layout") or []:
            w = item.get("widget") or {}
            kind = (w.get("spec") or {}).get("widgetType") or ""
            if w.get("queries") and not kind.startswith("filter"):
                yield p, w


def missing_dashboard(ctx):
    sid, _ = _live(ctx)
    spec = definition(ctx)
    return _result([] if sid else [{"kind": "missing", "problem": f"no dashboard {spec['title']!r} in {spec['parent_path']}"}],
                   dashboard_id=sid)


def dashboard_differences(ctx):
    sid, dash = _live(ctx)
    if not sid:
        return _result([])
    return _result([{"kind": "definition", "problem": d} for d in differences(serialized(definition(ctx)), dash["serialized"])])


def non_metric_view_datasets(ctx):
    _, dash = _live(ctx)
    if not dash:
        return _result([])
    ds = list(_datasets(dash).values())
    schemas = {tuple(d["asset_name"].split(".")[:2]) for d in ds if d.get("asset_name", "").count(".") == 2}
    views = live_metric_views(ctx, schemas)
    items = []
    for d in ds:
        if d.get("queryLines") or not d.get("asset_name"):
            items.append({"kind": "dataset", "dataset": d.get("name"), "problem": "dataset is an SQL query, not a metric view"})
        elif d["asset_name"] not in views:
            items.append({"kind": "dataset", "dataset": d.get("name"), "problem": f"{d['asset_name']} is not a metric view"})
    return _result(items)


def _fields(w):
    q = (w.get("queries") or [{}])[0].get("query") or {}
    dims, measures, other = [], [], []
    for f in q.get("fields") or []:
        e = (f.get("expression") or "").strip()
        if (m := MEASURE.match(e)):
            measures.append(m.group(1))
        elif (d := DIMENSION.match(e)):
            dims.append(d.group(1))
        else:
            other.append(e)
    return q.get("datasetName"), dims, measures, other, [f.get("expression") for f in q.get("filters") or []]


def tiles_without_measures(ctx):
    _, dash = _live(ctx)
    if not dash:
        return _result([])
    items = []
    for p, w in _tiles(dash):
        _, _, measures, other, _ = _fields(w)
        if not measures:
            items.append({"kind": "tile", "page": p.get("displayName"), "tile": w.get("name"), "problem": "shows no measure"})
        if other:
            items.append({"kind": "tile", "page": p.get("displayName"), "tile": w.get("name"),
                          "problem": f"computes outside the metric view: {', '.join(other)}"})
    return _result(items)


def failing_tiles(ctx):
    sid, dash = _live(ctx)
    if not dash:
        return _result([])
    ds = _datasets(dash)
    results, items = [], []
    for p, w in _tiles(dash):
        name, dims, measures, _, filters = _fields(w)
        d = ds.get(name) or {}
        title = ((w.get("spec") or {}).get("frame") or {}).get("title") or w.get("name")
        if not d.get("asset_name"):
            rows, err = None, f"dataset {name!r} is not a metric view"
        else:
            rows, err = tile_rows(ctx, tile_sql(d["asset_name"], dims, measures, [f for f in filters if f]))
        results.append({"page": p.get("displayName"), "tile": title, "widget": w.get("name"), "rows": rows, "error": err})
        if err:
            items.append({"kind": "tile", "page": p.get("displayName"), "tile": title, "problem": err})
    ctx.write_artefact("tile_results.json", {"dashboard_id": sid, "url": url(ctx, sid), "tiles": len(results),
                                             "failing": len(items), "empty": sum(r["rows"] == 0 for r in results),
                                             "results": results})
    return _result(items, tiles=len(results))


def page_mismatches(ctx):
    _, dash = _live(ctx)
    if not dash:
        return _result([])
    want = [p["name"] for p in semantic_model(ctx)["pages"]]
    have = [p.get("displayName") for p in dash["serialized"].get("pages") or []]
    items = [{"kind": "pages", "page": n, "problem": "semantic page has no dashboard page"} for n in want if n not in have]
    items += [{"kind": "pages", "page": n, "problem": "dashboard page is not a semantic page"} for n in have if n not in want]
    items += [{"kind": "pages", "page": n, "problem": "dashboard page appears more than once"}
              for n in sorted({n for n in have if have.count(n) > 1})]
    return _result(items)


def content_gaps(ctx):
    _, dash = _live(ctx)
    if not dash:
        return _result([])
    ds = _datasets(dash)
    pages = {p.get("name"): p for p in dash["serialized"].get("pages") or []}
    items = []
    for p in (ctx.read_artefact("context.json") or {}).get("pages") or []:
        pid = p["page"]["id"]
        live = pages.get(pid) or {}
        shown, filters = set(), set()
        for item in live.get("layout") or []:
            w = item.get("widget") or {}
            kind = (w.get("spec") or {}).get("widgetType") or ""
            if kind.startswith("filter"):
                filters |= {f.get("fieldName") for f in (w["spec"].get("encodings") or {}).get("fields") or []}
            elif w.get("queries"):
                name, _, measures, _, _ = _fields(w)
                shown |= {((ds.get(name) or {}).get("asset_name"), m) for m in measures}
        for k in p["kpis"]:
            if (k["metric_view"], k["measure"]) not in shown:
                items.append({"kind": "content", "page": pid, "problem": f"KPI {k['metric_view']}.{k['measure']} is not shown"})
        for f in p["filters"]:
            if f not in filters:
                items.append({"kind": "content", "page": pid, "problem": f"no filter on {f!r}"})
    return _result(items)


def unpublished(ctx):
    sid, dash = _live(ctx)
    if not sid:
        return _result([])
    pub = published(ctx, sid)
    want = definition(ctx)["credentials"] == "embedded"
    if not pub:
        return _result([{"kind": "unpublished", "problem": "the dashboard is not published"}])
    items = []
    if (pub.revision_create_time or "") < (dash["update_time"] or ""):
        items.append({"kind": "unpublished", "problem": "the draft changed after it was last published"})
    if bool(pub.embed_credentials) != want:
        items.append({"kind": "unpublished", "problem": f"published with {'embedded' if pub.embed_credentials else 'viewer'} "
                                                         f"credentials; declared {definition(ctx)['credentials']}"})
    return _result(items, published_at=pub.revision_create_time)


def _maya_schedule(ctx, sid):
    m = definition(ctx)["marker"]
    return [s for s in schedules(ctx, sid) if m in (s.get("display_name") or "")]


def schedule_differences(ctx):
    sid, _ = _live(ctx)
    if not sid:
        return _result([])
    spec = definition(ctx)
    want, have = spec.get("schedule"), _maya_schedule(ctx, sid)
    items = []
    if not want:
        items += [{"kind": "schedule", "problem": f"schedule {s['schedule_id']} is not declared"} for s in have]
        return _result(items)
    if len(have) != 1:
        return _result([{"kind": "schedule", "problem": f"{len(have)} schedules named {spec['marker']}; expected 1"}])
    s = have[0]
    cron = s.get("cron_schedule") or {}
    for got, exp, what in ((cron.get("quartz_cron_expression"), want["cron"], "cron"),
                           (cron.get("timezone_id"), want["timezone"], "timezone"),
                           (s.get("pause_status"), "PAUSED" if want["paused"] else "UNPAUSED", "pause status")):
        if got != exp:
            items.append({"kind": "schedule", "problem": f"{what} is {got!r}; declared {exp!r}"})
    subs = subscriptions(ctx, sid, s["schedule_id"])
    users = {str((x.get("subscriber") or {}).get("user_subscriber", {}).get("user_id")) for x in subs
             if (x.get("subscriber") or {}).get("user_subscriber")}
    dests = {(x.get("subscriber") or {}).get("destination_subscriber", {}).get("destination_id") for x in subs
             if (x.get("subscriber") or {}).get("destination_subscriber")}
    want_users, want_dests = {}, {d["destination"] for d in spec.get("subscribers") or [] if d.get("destination")}
    for d in spec.get("subscribers") or []:
        if d.get("user"):
            want_users[d["user"]] = user_id(ctx, d["user"])
    items += [{"kind": "schedule", "problem": f"subscriber {u} is not a workspace user"} for u, i in want_users.items() if not i]
    items += [{"kind": "schedule", "problem": f"subscriber {u} is not subscribed"} for u, i in want_users.items()
              if i and i not in users]
    items += [{"kind": "schedule", "problem": f"destination {d} is not subscribed"} for d in want_dests - dests]
    extra = len(users - {i for i in want_users.values() if i}) + len(dests - want_dests)
    if extra:
        items.append({"kind": "schedule", "problem": f"{extra} subscribers are not declared"})
    return _result(items)


def missing_access(ctx):
    sid, _ = _live(ctx)
    if not sid:
        return _result([])
    held = dashboard_permissions(ctx, sid)
    items = []
    for a in definition(ctx).get("access") or []:
        who, level = a.get("group") or a.get("service_principal"), a.get("level", "CAN_RUN")
        if max((ORDER.get(x, -1) for x in held.get(who, ())), default=-1) < ORDER[level]:
            items.append({"kind": "access", "principal": who, "problem": f"does not hold {level}"})
    return _result(items)


def distribution_gaps(ctx):
    spec = definition(ctx)
    items = []
    if not spec.get("schedule"):
        items.append({"problem": "no refresh schedule declared (goals.G6.schedule)"})
    elif not spec.get("subscribers"):
        items.append({"problem": "a schedule but no subscribers declared (goals.G6.subscribers)"})
    return _result(items)


def empty_tiles(ctx):
    t = ctx.read_artefact("tile_results.json") or {}
    return _result([{"page": r["page"], "tile": r["tile"], "problem": "the tile's query returns no rows"}
                    for r in t.get("results") or [] if r.get("rows") == 0])
