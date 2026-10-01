"""G0 code nodes: generic, read-only discovery and assessment of the foundation declared in maya.yaml."""
from collections import Counter
from datetime import datetime, timezone

from maya.core import catalog
from maya.core.workspace import SqlError, lit

INVENTORY_COLS = ("system STRING, run_id STRING, layer STRING, full_name STRING, kind STRING, table_type STRING, "
                  "column_count INT, row_count BIGINT, last_updated TIMESTAMP, has_comment BOOLEAN, readable BOOLEAN, "
                  "error STRING, recorded_at TIMESTAMP")


def inventory_table(ctx):
    return ctx.state.t("asset_inventory")


def _last_updated(ctx, asset):
    if asset["kind"] == "table":
        try:
            h = ctx.ws.sql(f"DESCRIBE HISTORY {asset['full_name']} LIMIT 1")
            if h:
                return h[0]["timestamp"]
        except SqlError:
            pass
    return asset.get("last_altered")


def discover(ctx):
    assets = catalog.tables(ctx.ws, ctx.system)
    cols = Counter(c["full_name"] for c in catalog.columns(ctx.ws, ctx.system))
    inventory = []
    for a in assets:
        item = {"layer": a["layer"], "full_name": a["full_name"], "kind": a["kind"], "table_type": a["table_type"],
                "column_count": cols[a["full_name"]], "has_comment": bool(a.get("comment")),
                "row_count": None, "readable": True, "error": None, "last_updated": _last_updated(ctx, a)}
        try:
            if ctx.inputs.get("row_counts", True):
                item["row_count"] = int(ctx.ws.sql(f"SELECT COUNT(*) AS n FROM {a['full_name']}")[0]["n"])
            else:
                ctx.ws.sql(f"SELECT 1 FROM {a['full_name']} LIMIT 1")
        except SqlError as e:
            item.update(readable=False, error=str(e)[:300])
        inventory.append(item)
    ctx.log(f"     discovered {len(inventory)} assets in {len(ctx.system.layers)} layers")

    t = inventory_table(ctx)
    ctx.ws.sql(f"CREATE TABLE IF NOT EXISTS {t} ({INVENTORY_COLS})")
    ctx.ws.sql(f"DELETE FROM {t} WHERE system = {lit(ctx.system.name)} AND run_id = {lit(ctx.run_id)}")
    if inventory:
        rows = ", ".join(
            "(" + ", ".join([lit(ctx.system.name), lit(ctx.run_id), lit(i["layer"]), lit(i["full_name"]), lit(i["kind"]),
                             lit(i["table_type"]), lit(i["column_count"]), lit(i["row_count"]),
                             f"timestamp{lit(str(i['last_updated']))}" if i["last_updated"] else "NULL",
                             lit(i["has_comment"]), lit(i["readable"]), lit(i["error"]), "current_timestamp()"]) + ")"
            for i in inventory)
        ctx.ws.sql(f"INSERT INTO {t} VALUES {rows}")
    ctx.write_artefact("inventory.json", inventory)
    return {"assets": len(inventory), "inventory_table": t,
            "by_layer": dict(Counter(i["layer"] for i in inventory))}


def _age_hours(ts):
    if not ts:
        return None
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - d).total_seconds() / 3600, 1)


def assess(ctx):
    inv = ctx.read_artefact("inventory.json")
    minimum = ctx.inputs.get("min_assets_per_layer", 1)
    limits = ctx.inputs.get("freshness_hours") or {}
    layers = {}
    for layer in ctx.system.layers:
        items = [i for i in inv if i["layer"] == layer]
        tables = [i for i in items if i["kind"] == "table" and i["last_updated"]]
        newest = max((i["last_updated"] for i in tables), default=None)
        age = _age_hours(newest)
        layers[layer] = {"assets": len(items), "below_minimum": len(items) < minimum, "newest_update": newest,
                         "age_hours": age, "freshness_limit_hours": limits.get(layer),
                         "stale": bool(limits.get(layer)) and (age is None or age > limits[layer])}
    result = {
        "layers": layers,
        "unreadable": [i["full_name"] for i in inv if not i["readable"]],
        "empty": [i["full_name"] for i in inv if i["readable"] and i["row_count"] == 0],
        "undocumented": [i["full_name"] for i in inv if not i["has_comment"]],
    }
    ctx.write_artefact("assessment.json", result)
    return {"unreadable": len(result["unreadable"]), "empty": len(result["empty"]),
            "stale_layers": [k for k, v in layers.items() if v["stale"]]}


def drift(ctx):
    """Assets added, removed or re-shaped since the certified inventory (ctx.run_id is the certified run)."""
    rows = ctx.ws.sql(f"SELECT full_name, column_count FROM {inventory_table(ctx)} WHERE system = {lit(ctx.system.name)} "
                      f"AND run_id = {lit(ctx.run_id)}")
    certified = {r["full_name"]: int(r["column_count"]) for r in rows}
    cols = Counter(c["full_name"] for c in catalog.columns(ctx.ws, ctx.system))
    live = {a["full_name"]: cols[a["full_name"]] for a in catalog.tables(ctx.ws, ctx.system)}
    added, removed = sorted(set(live) - set(certified)), sorted(set(certified) - set(live))
    reshaped = sorted(n for n in set(live) & set(certified) if live[n] != certified[n])
    out = []
    for label, names in (("added", added), ("removed", removed), ("changed columns", reshaped)):
        if names:
            out.append(f"foundation {label}: {', '.join(names)}")
    return out


def attestation_input(ctx, item):
    return {"declared_layers": catalog.describe_layers(ctx.system),
            "inventory": ctx.read_artefact("inventory.json"),
            "assessment": ctx.read_artefact("assessment.json")}
