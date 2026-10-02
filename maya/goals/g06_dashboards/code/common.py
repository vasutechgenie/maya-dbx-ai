"""Shared helpers for G6: the metric views and semantic pages it builds on, a tile's query, the dashboard in AI/BI
(Lakeview) format, and the live dashboard read back from the workspace."""
import json
import re

import yaml

from maya.core.workspace import SqlError, ident, lit

TEMPORAL = ("DATE", "TIMESTAMP", "TIMESTAMP_NTZ")


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def marker(ctx) -> str:
    """The name of the dashboard's refresh schedule: how the bundle and MAYA find the schedule they manage."""
    return f"maya:{slug(ctx.system.name)}:G6"


def metric_view_schema(ctx):
    name = (ctx.system.goal_settings("G2") or {}).get("schema")
    if not name:
        raise RuntimeError("G6 needs G2's schema (goals.G2.schema in maya.yaml)")
    parts = name.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (ctx.system.catalogs[0], parts[0])


def semantic_model(ctx) -> dict:
    from maya.goals.g05_genie_space.code.common import semantic_model as model
    return model(ctx)


# ---------------------------------------------------------------- metric views (G2, as stored in Unity Catalog)
def live_metric_views(ctx, schemas=None) -> set[str]:
    """Full names of every metric view in the given (catalog, schema) pairs (default: G2's schema)."""
    out = set()
    for c, s in schemas or [metric_view_schema(ctx)]:
        try:
            rows = ctx.ws.sql(f"SELECT table_name FROM {ident(c)}.information_schema.tables "
                              f"WHERE table_schema = {lit(s)} AND table_type = 'METRIC_VIEW'")
        except SqlError:
            continue
        out |= {f"{c}.{s}.{r['table_name']}" for r in rows}
    return out


def metric_views(ctx) -> dict:
    """full name -> {name, comment, measures: [...], dimensions: [... temporal]} for the views the dashboard may use."""
    from maya.goals.g02_metric_views.code.common import read_back
    c, s = metric_view_schema(ctx)
    every = sorted(live_metric_views(ctx))
    wanted = ctx.inputs.get("metric_views")
    if wanted:
        every = [fn for fn in every if fn in wanted or fn.split(".")[2] in wanted or fn.split(".", 1)[1] in wanted]
    types = {}
    if every:
        for r in ctx.ws.sql(f"SELECT table_name, column_name, data_type FROM {ident(c)}.information_schema.columns "
                            f"WHERE table_schema = {lit(s)}"):
            types[(f"{c}.{s}.{r['table_name']}", r["column_name"])] = (r["data_type"] or "").upper()
    out = {}
    for fn in every:
        b = read_back(ctx, fn) or {}
        keep = ("name", "display_name", "comment", "format")
        out[fn] = {"full_name": fn, "name": fn.split(".")[2], "comment": b.get("comment"),
                   "measures": [{k: f[k] for k in keep if f.get(k)} for f in b.get("measures") or []],
                   "dimensions": [{**{k: f[k] for k in keep if f.get(k) and k != "format"},
                                   "temporal": types.get((fn, f["name"]), "") in TEMPORAL}
                                  for f in b.get("dimensions") or []]}
    return out


def measure_names(mv) -> list[str]:
    return [m["name"] for m in mv["measures"]]


def dimension_names(mv) -> list[str]:
    return [d["name"] for d in mv["dimensions"]]


def field(mv, name) -> dict:
    return next((f for f in mv["measures"] + mv["dimensions"] if f["name"] == name), {"name": name})


def label(mv, name) -> str:
    f = field(mv, name)
    return f.get("display_name") or name.replace("_", " ").capitalize()


# ---------------------------------------------------------------- customer inputs
def content_file(ctx) -> tuple[dict, list[str]]:
    ref = ctx.inputs.get("content")
    if not ref:
        return {}, []
    path = ctx.system.base_dir / ref
    if not path.exists():
        return {}, [f"content file {ref} not found"]
    data = yaml.safe_load(path.read_text()) or {}
    import jsonschema
    schema = ctx.goal.inputs_schema()
    errors = sorted(jsonschema.Draft202012Validator({"$ref": "#/$defs/content_file", "$defs": schema["$defs"]})
                    .iter_errors(data), key=lambda e: list(e.path))
    return data, [f"{ref}: {e.message} at {'/'.join(map(str, e.path)) or 'root'}" for e in errors[:8]]


def resolve_kpi(mvs, ref) -> tuple[str | None, str | None, str | None]:
    """(metric view, measure, error) for '<view>.<measure>', '<catalog>.<schema>.<view>.<measure>' or a measure name
    (or display name) only one view has."""
    parts = ref.split(".")
    if len(parts) >= 2:
        view, measure = ".".join(parts[:-1]), parts[-1]
        hits = [fn for fn in mvs if fn == view or fn.split(".", 1)[1] == view or fn.split(".")[2] == view]
        if len(hits) != 1:
            return None, None, f"KPI {ref!r}: no metric view {view!r}" if not hits else f"KPI {ref!r}: {view!r} is ambiguous"
        if measure not in measure_names(mvs[hits[0]]):
            return None, None, f"KPI {ref!r}: {hits[0]} has no measure {measure!r} ({', '.join(measure_names(mvs[hits[0]]))})"
        return hits[0], measure, None
    hits = [(fn, m["name"]) for fn, mv in mvs.items() for m in mv["measures"]
            if ref.lower() in (m["name"].lower(), (m.get("display_name") or "").lower())]
    if len(hits) == 1:
        return hits[0][0], hits[0][1], None
    if not hits:
        return None, None, f"KPI {ref!r} is not a measure of any metric view"
    return None, None, f"KPI {ref!r} is a measure of several metric views ({', '.join(fn for fn, _ in hits)}): write <view>.<measure>"


# ---------------------------------------------------------------- tiles
def tile_shape_problem(tile, mvs) -> str | None:
    mv = mvs.get(tile.get("metric_view"))
    if not mv:
        return f"metric view {tile.get('metric_view')!r} is not one the dashboard may use"
    kind, ms, dim, series = tile["kind"], tile.get("measures") or [], tile.get("dimension"), tile.get("series")
    unknown = [m for m in ms if m not in measure_names(mv)]
    if unknown:
        return f"{mv['full_name']} has no measure {', '.join(unknown)}"
    for d in (dim, series):
        if d and d not in dimension_names(mv):
            return f"{mv['full_name']} has no dimension {d!r}"
    if kind == "counter" and len(ms) != 1:
        return "a counter shows exactly one measure"
    if kind in ("line", "bar") and len(ms) > 3:
        return f"a {kind} shows at most three measures"
    if series and len(ms) > 1:
        return "a tile with a series shows one measure"
    if kind == "counter" and (dim or series):
        return "a counter has no dimension"
    if kind in ("line", "bar", "table") and not dim:
        return f"a {kind} needs a dimension"
    if kind == "line" and not field(mv, dim).get("temporal"):
        return f"a line needs a temporal dimension; {dim!r} is not one"
    if kind == "table" and series:
        return "a table has no series"
    if series and series == dim:
        return "series and dimension are the same"
    return None


def tile_sql(mv_full_name, dims, measures, filters=()) -> str:
    cols = [f"`{d}`" for d in dims] + [f"MEASURE(`{m}`) AS `{m}`" for m in measures]
    where = f" WHERE {' AND '.join(f'({f})' for f in filters)}" if filters else ""
    group = " GROUP BY ALL" if dims else ""
    return f"SELECT {', '.join(cols)} FROM {ident(*mv_full_name.split('.'))}{where}{group}"


def tile_rows(ctx, sql) -> tuple[int | None, str | None]:
    """(rows the tile's query returns, error)."""
    try:
        rows = ctx.ws.sql(f"SELECT count(*) AS n FROM ({sql}) AS maya_tile")
    except SqlError as e:
        return None, str(e).splitlines()[0][:300]
    return int(rows[0]["n"]), None


def tile_dims(tile) -> list[str]:
    return [d for d in (tile.get("dimension"), tile.get("series")) if d]


# ---------------------------------------------------------------- the dashboard in AI/BI (Lakeview) format
def _format(f):
    fmt = (f or {}).get("format") or {}
    t = fmt.get("type")
    if t == "currency":
        return {"type": "number-currency", "currencyCode": fmt.get("currency_code", "USD"), "abbreviation": "compact",
                "decimalPlaces": {"type": "exact", "places": 2}}
    if t in ("percentage", "percent"):
        return {"type": "number-percent", "decimalPlaces": {"type": "max", "places": 1}}
    if t == "number":
        return {"type": "number-plain", "abbreviation": "compact", "decimalPlaces": {"type": "max", "places": 2}}
    return None


def _m(name):
    return f"measure({name})"


def _enc(field_name, mv=None, name=None, scale=None, measure=False, compact=True):
    e = {"fieldName": field_name}
    if scale:
        e["scale"] = {"type": scale}
    if mv is not None:
        e["displayName"] = label(mv, name)
        fmt = _format(field(mv, name)) if measure else None
        if fmt and not compact:
            fmt["abbreviation"] = "none"
        if fmt:
            e["format"] = fmt
    return e


def _widget(name, ds, tile, mv):
    kind, ms, dim, series = tile["kind"], tile["measures"], tile.get("dimension"), tile.get("series")
    fields = [{"name": d, "expression": f"`{d}`"} for d in tile_dims(tile)] + \
             [{"name": _m(m), "expression": f"MEASURE(`{m}`)"} for m in ms]
    spec = {"widgetType": kind, "frame": {"showTitle": True, "title": tile["title"]}}
    if kind == "counter":
        spec.update(version=2, encodings={"value": _enc(_m(ms[0]), mv, ms[0], measure=True)})
    elif kind in ("line", "bar"):
        enc = {"x": _enc(dim, mv, dim, "temporal" if kind == "line" else "categorical"),
               "y": _enc(_m(ms[0]), mv, ms[0], "quantitative", measure=True) if len(ms) == 1 else
               {"scale": {"type": "quantitative"}, "fields": [_enc(_m(m), mv, m, measure=True) for m in ms]}}
        if series:
            enc["color"] = _enc(series, mv, series, "categorical")
        spec.update(version=3, encodings=enc)
    else:
        spec.update(version=2, encodings={"columns": [{"fieldName": dim, "displayName": label(mv, dim)}] +
                                          [_enc(_m(m), mv, m, measure=True, compact=False) for m in ms]})
    return {"name": name, "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": fields,
                                                                      "disaggregated": False}}], "spec": spec}


def _filter_widget(name, dim, title, datasets, temporal=False):
    queries = [{"name": f"f_{dim}_{ds}", "query": {"datasetName": ds, "fields": [{"name": dim, "expression": f"`{dim}`"}],
                                                   "disaggregated": False}} for ds in datasets]
    return {"name": name, "queries": queries,
            "spec": {"version": 2, "widgetType": "filter-date-range-picker" if temporal else "filter-multi-select",
                     "frame": {"showTitle": True, "title": title},
                     "encodings": {"fields": [{"fieldName": dim, "displayName": title, "queryName": q["name"]}
                                              for q in queries]}}}


def _rows(items, per_row):
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def page_layout(page, ds_of, mvs) -> list[dict]:
    """Widgets and positions of one page on the 6-column grid: header, filters, counters, charts, tables."""
    out, y = [], 0
    desc = (page.get("description") or "").strip()
    if page.get("audience"):
        desc = f"{desc} Audience: {page['audience']}.".strip()
    out.append({"widget": {"name": f"{page['id']}__header", "multilineTextboxSpec": {"lines": [f"## {page['name']}\n", desc]}},
                "position": {"x": 0, "y": y, "width": 6, "height": 2}})
    y += 2
    tiles = page["tiles"]
    used = sorted({ds_of[t["metric_view"]] for t in tiles})
    if page["filters"]:
        for row in _rows(page["filters"], 3):
            for i, dim in enumerate(row):
                dss = sorted({ds_of[t["metric_view"]] for t in tiles if dim in dimension_names(mvs[t["metric_view"]])})
                owner = next((mvs[t["metric_view"]] for t in tiles if dim in dimension_names(mvs[t["metric_view"]])), None)
                title = label(owner, dim) if owner else dim
                temporal = bool(owner and field(owner, dim).get("temporal"))
                out.append({"widget": _filter_widget(f"{page['id']}__f_{dim}", dim, title, dss or used[:1], temporal),
                            "position": {"x": 2 * i, "y": y, "width": 2, "height": 1}})
            y += 1
    n = 0

    def place(group, per_row, height):
        nonlocal y, n
        for row in _rows(group, per_row):
            w = 6 // len(row)
            for i, t in enumerate(row):
                n += 1
                out.append({"widget": _widget(f"{page['id']}__{n:02d}_{t['kind']}", ds_of[t["metric_view"]], t, mvs[t["metric_view"]]),
                            "position": {"x": i * w, "y": y, "width": w, "height": height}})
            y += height

    counters = [t for t in tiles if t["kind"] == "counter"]
    place(counters, 2 if len(counters) in (2, 4) else 3, 3)
    place([t for t in tiles if t["kind"] in ("line", "bar")], 2, 6)
    place([t for t in tiles if t["kind"] == "table"], 1, 6)
    return out


def dataset_names(datasets) -> dict:
    return {d["metric_view"]: d["name"] for d in datasets}


def lakeview(spec: dict, mvs: dict) -> dict:
    """The approved dashboard (dashboard.json) as an AI/BI serialized dashboard."""
    ds_of = dataset_names(spec["datasets"])
    return {"datasets": [{"name": d["name"], "displayName": d.get("display_name") or d["name"], "asset_name": d["metric_view"]}
                         for d in spec["datasets"]],
            "pages": [{"name": p["id"], "displayName": p["name"], "pageType": "PAGE_TYPE_CANVAS",
                       "layout": page_layout(p, ds_of, mvs)} for p in spec["pages"]]}


def spec_mvs(spec) -> dict:
    """The metric view fields the approved dashboard was built with (stored in it, so it renders without a lookup)."""
    return spec.get("metric_views") or {}


def serialized(spec) -> dict:
    return lakeview(spec, spec_mvs(spec))


def _ws(text):
    return " ".join(str(text).split())


def canonical(s: dict) -> dict:
    """Comparable form of a serialized dashboard (key order, text line breaks and whitespace ignored)."""
    s = s or {}
    pages = {}
    for p in s.get("pages") or []:
        widgets = {}
        for item in p.get("layout") or []:
            w = item.get("widget") or {}
            if "multilineTextboxSpec" in w:
                body = ("text", _ws("".join(w["multilineTextboxSpec"].get("lines") or [])))
            else:
                spec = w.get("spec") or {}
                body = (spec.get("widgetType"), _ws((spec.get("frame") or {}).get("title") or ""),
                        json.dumps(spec.get("encodings") or {}, sort_keys=True),
                        tuple(sorted((q["query"].get("datasetName"), tuple(f.get("expression") for f in q["query"].get("fields") or []),
                                      tuple(f.get("expression") for f in q["query"].get("filters") or []))
                                     for q in w.get("queries") or [])))
            pos = item.get("position") or {}
            widgets[w.get("name")] = (body, tuple(pos.get(k) for k in ("x", "y", "width", "height")))
        pages[p.get("name")] = {"displayName": p.get("displayName"), "widgets": widgets}
    return {"datasets": sorted((d.get("name"), d.get("asset_name"), tuple(d.get("queryLines") or ()))
                               for d in s.get("datasets") or []),
            "pages": pages}


def differences(want: dict, live: dict) -> list[str]:
    a, b = canonical(want), canonical(live)
    out = []
    if a["datasets"] != b["datasets"]:
        out.append("datasets differ: " + ", ".join(sorted({str(x[1] or x[0]) for x in set(a["datasets"]) ^ set(b["datasets"])})))
    for name in sorted(set(a["pages"]) | set(b["pages"])):
        pa, pb = a["pages"].get(name), b["pages"].get(name)
        if not pb:
            out.append(f"page {name}: missing")
            continue
        if not pa:
            out.append(f"page {name} ({pb['displayName']}): not approved")
            continue
        if pa["displayName"] != pb["displayName"]:
            out.append(f"page {name}: named {pb['displayName']!r}, approved {pa['displayName']!r}")
        for w in sorted(set(pa["widgets"]) | set(pb["widgets"])):
            wa, wb = pa["widgets"].get(w), pb["widgets"].get(w)
            if wa != wb:
                out.append(f"page {name}: tile {w} " + ("missing" if not wb else "not approved" if not wa else "changed"))
    return out


# ---------------------------------------------------------------- the live dashboard
def home(ctx) -> str:
    return f"/Workspace/Users/{ctx.ws.client.current_user.me().user_name}"


def dashboard_path(ctx, spec) -> str:
    parent = spec.get("parent_path") or "MAYA"
    if not parent.startswith("/"):
        parent = f"{home(ctx)}/{parent}"
    return f"{parent}/{spec['title']}.lvdash.json"


def find_dashboard(ctx, spec) -> str | None:
    from databricks.sdk.errors import NotFound
    try:
        return ctx.ws.client.workspace.get_status(dashboard_path(ctx, spec)).resource_id
    except NotFound:
        return None


def live_dashboard(ctx, dashboard_id) -> dict:
    d = ctx.ws.client.lakeview.get(dashboard_id)
    return {"id": dashboard_id, "update_time": d.update_time, "warehouse_id": d.warehouse_id,
            "serialized": json.loads(d.serialized_dashboard or "{}")}


def published(ctx, dashboard_id):
    from databricks.sdk.errors import NotFound
    try:
        return ctx.ws.client.lakeview.get_published(dashboard_id)
    except NotFound:
        return None


def schedules(ctx, dashboard_id) -> list[dict]:
    return ctx.ws.client.api_client.do("GET", f"/api/2.0/lakeview/dashboards/{dashboard_id}/schedules").get("schedules") or []


def subscriptions(ctx, dashboard_id, schedule_id) -> list[dict]:
    return ctx.ws.client.api_client.do(
        "GET", f"/api/2.0/lakeview/dashboards/{dashboard_id}/schedules/{schedule_id}/subscriptions").get("subscriptions") or []


def user_id(ctx, user_name) -> str | None:
    r = ctx.ws.client.api_client.do("GET", "/api/2.0/preview/scim/v2/Users",
                                    query={"filter": f'userName eq "{user_name}"', "attributes": "id"})
    found = r.get("Resources") or []
    return str(found[0]["id"]) if found else None


def dashboard_permissions(ctx, dashboard_id) -> dict:
    r = ctx.ws.client.api_client.do("GET", f"/api/2.0/permissions/dashboards/{dashboard_id}")
    out = {}
    for a in r.get("access_control_list") or []:
        who = a.get("group_name") or a.get("service_principal_name") or a.get("user_name")
        out.setdefault(who, set()).update(p["permission_level"] for p in a.get("all_permissions") or [])
    return out


def url(ctx, dashboard_id) -> str:
    return f"{ctx.ws.host}/dashboardsv3/{dashboard_id}/published"


def definition(ctx) -> dict:
    return ctx.read_artefact("dashboard.json") or {}
