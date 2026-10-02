"""G6 apply: the approved dashboard becomes one bundle declaration, deployed through the project bundle.

scripts/G6/10_dashboard/dashboard.json   {"dashboards": [...]}: the job creates the dashboard at its path or updates it
                                         in place, publishes it, makes its refresh schedule and subscribers exactly as
                                         declared, and adds the declared permissions
Catalog names in the metric views are tokens, so the same declaration deploys to every environment."""
from maya.core import bundle

from . import ledger
from .common import definition, serialized

FILE = "10_dashboard/dashboard.json"


def declaration(spec) -> dict:
    return {"dashboards": [{"marker": spec["marker"], "title": spec["title"], "parent_path": spec.get("parent_path") or "MAYA",
                            "serialized_dashboard": serialized(spec), "embed_credentials": spec["credentials"] == "embedded",
                            "schedule": spec.get("schedule"), "subscribers": spec.get("subscribers") or [],
                            "permissions": spec.get("access") or []}]}


def _catalogs(spec):
    return {d["metric_view"].split(".")[0] for d in spec["datasets"]}


def _deploy(ctx, spec, label):
    written = bundle.write(ctx, {FILE: declaration(spec)}, catalogs=_catalogs(spec))
    res = bundle.deploy(ctx, written)
    ctx.log(f"     {label}: dashboard {spec['title']!r}")
    return res


def apply(ctx):
    return _deploy(ctx, definition(ctx), "apply")


REDEPLOY = {"missing", "definition", "unpublished", "schedule", "access"}


def repair(ctx):
    """Re-deploy when the dashboard is missing, differs from the approved one, is not published as declared, or its
    schedule or permissions differ."""
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & REDEPLOY:
        return {"repaired": [], "note": "failures need a change to the inputs (not repairable by re-deploying)"}
    return {"repaired": sorted(kinds & REDEPLOY), **_deploy(ctx, definition(ctx), "repair")}


def record(ctx):
    """After the business owner's sign-off: record the approved dashboard and the tile results."""
    ledger.record(ctx, definition(ctx), ctx.read_artefact("tile_results.json") or {})
    return {"recorded": definition(ctx)["marker"]}


def export(ctx) -> dict:
    """The declaration of the certified dashboard, for `maya bundle`."""
    spec = ledger.certified_dashboard(ctx)
    if not spec:
        return {"files": {}}
    return {"files": {FILE: declaration(spec)}, "catalogs": _catalogs(spec)}
