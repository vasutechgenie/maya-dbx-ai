"""G6 gates. Neither item is editable: the page content file and the goal inputs are the source of truth, so a change
is made there and re-planned, never patched at a gate."""
from .common import definition


def dashboard(ctx, data):
    want = definition(ctx)
    out = [f"{k} differs from the assembled dashboard"
           for k in ("marker", "title", "parent_path", "credentials", "schedule", "subscribers", "access", "datasets", "pages")
           if data.get(k) != want.get(k)]
    out += [f"page {p['id']} has no tile" for p in data["pages"] if not p["tiles"]]
    return out


def tile_results(ctx, data):
    out = []
    if data["failing"]:
        out.append(f"{data['failing']} of {data['tiles']} tiles fail")
    if len(data["results"]) != data["tiles"]:
        out.append(f"{len(data['results'])} results for {data['tiles']} tiles")
    return out
