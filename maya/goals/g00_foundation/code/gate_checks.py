"""G0 sign-off gate: what the data owner signs must describe exactly the configured foundation."""
from maya.core import catalog


def inventory(ctx, data):
    out, seen = [], set()
    for a in data:
        if a["full_name"] in seen:
            out.append(f"{a['full_name']} listed twice")
        seen.add(a["full_name"])
        if a["layer"] not in ctx.system.layers:
            out.append(f"{a['full_name']}: layer {a['layer']!r} is not a configured layer")
            continue
        c, s, _ = a["full_name"].split(".")
        if catalog.layer_of(ctx.system, c, s) != a["layer"]:
            out.append(f"{a['full_name']}: not in the {a['layer']} layer's configured schemas")
    return out


def assessment(ctx, data):
    want, got = set(ctx.system.layers), set(data["layers"])
    out = [f"layer {l} missing from the assessment" for l in sorted(want - got)]
    out += [f"layer {l} is not a configured layer" for l in sorted(got - want)]
    counts = _counts(ctx)
    out += [f"layer {l}: assessment says {data['layers'][l]['assets']} assets, inventory has {counts.get(l, 0)}"
            for l in sorted(got & want) if counts is not None and data["layers"][l]["assets"] != counts.get(l, 0)]
    return out


def attestation(ctx, data):
    att = data[0]
    by_layer = {l["layer"]: l for l in att["layers"]}
    out = [f"layer {l} not attested" for l in sorted(set(ctx.system.layers) - set(by_layer))]
    out += [f"layer {l} is not a configured layer" for l in sorted(set(by_layer) - set(ctx.system.layers))]
    counts = _counts(ctx)
    if counts is not None:
        out += [f"layer {l}: attestation says {v['asset_count']} assets, inventory has {counts.get(l, 0)}"
                for l, v in by_layer.items() if l in ctx.system.layers and v["asset_count"] != counts.get(l, 0)]
    blocking = [g for g in att["gaps"] if g["severity"] == "blocking"]
    if att["ready_for_metadata"] and blocking:
        out.append(f"ready_for_metadata is true but {len(blocking)} blocking gap(s) are listed")
    return out


def _counts(ctx):
    inv = ctx.read_artefact("inventory.json")
    if inv is None:
        return None
    counts = {}
    for a in inv:
        counts[a["layer"]] = counts.get(a["layer"], 0) + 1
    return counts
