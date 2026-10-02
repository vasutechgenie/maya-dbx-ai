"""G5 apply: the approved space becomes one bundle declaration, deployed through the project bundle.

scripts/G5/10_space/genie_space.json   {"genie_spaces": [...]}: the job finds the space by the marker in its
                                       description, updates it in place or creates it, and adds the declared permissions
Catalog names in sources and SQL are tokens, so the same declaration deploys to every environment."""
from maya.core import bundle

from . import ledger
from .common import definition, serialized

FILE = "10_space/genie_space.json"


def declaration(space) -> dict:
    return {"genie_spaces": [{"marker": space["marker"], "title": space["title"],
                              "description": space.get("description") or "", "parent_path": space.get("parent_path") or "MAYA",
                              "serialized_space": serialized(space), "permissions": space.get("access") or []}]}


def _catalogs(space):
    return {s.split(".")[0] for s in space["sources"]}


def _deploy(ctx, space, label):
    written = bundle.write(ctx, {FILE: declaration(space)}, catalogs=_catalogs(space))
    res = bundle.deploy(ctx, written)
    ctx.log(f"     {label}: Genie space {space['title']!r}")
    return res


def apply(ctx):
    return _deploy(ctx, definition(ctx), "apply")


def repair(ctx):
    """Re-deploy when the space is missing, differs from the approved one, or lacks a declared permission."""
    items = (ctx.outputs.get("validate") or {}).get("failed_items") or []
    kinds = {i.get("kind") for i in items if isinstance(i, dict)}
    if not kinds & {"missing", "definition", "access"}:
        return {"repaired": [], "note": "failures need a change to the inputs (not repairable by re-deploying)"}
    return {"repaired": sorted(kinds & {"missing", "definition", "access"}), **_deploy(ctx, definition(ctx), "repair")}


def record(ctx):
    """After the business owner's sign-off: record the approved space and the benchmark result."""
    ledger.record(ctx, definition(ctx), ctx.read_artefact("benchmark_results.json") or {})
    return {"recorded": definition(ctx)["marker"]}


def export(ctx) -> dict:
    """The declaration of the certified space, for `maya bundle`."""
    space = ledger.certified_space(ctx)
    if not space:
        return {"files": {}}
    return {"files": {FILE: declaration(space)}, "catalogs": _catalogs(space)}
