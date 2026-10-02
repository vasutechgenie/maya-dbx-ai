"""G12 delivery: pure functions of the approved plan. The bundle gets the monitoring dashboard (a deploy declaration,
like G6's) and the documentation as workspace files (jobs/G12/docs: product documentation, onboarding page, runbooks)."""
from maya.core import bundle

from .common import doc_path
from .monitoring import serialized

FILE = "10_dashboard/dashboard.json"


def declaration(system_name, spec) -> dict:
    d = spec["dashboard"]
    return {"dashboards": [{"marker": d["marker"], "title": d["title"], "parent_path": d["parent_path"],
                            "serialized_dashboard": serialized(d, system_name), "embed_credentials": True,
                            "schedule": None, "subscribers": [],
                            "permissions": [{"group": g, "level": "CAN_RUN"} for g in d["viewers"]]}]}


def index(spec) -> str:
    lines = ["# Documentation\n", "Maintained by MAYA G12 from what the goals certified.\n"]
    for i, doc in sorted(spec["documents"].items()):
        lines.append(f"- [{doc['title']}]({doc_path(i).split('/', 1)[1]})")
    return "\n".join(lines) + "\n"


def job_files(spec) -> dict:
    out = {doc_path(i): doc["markdown"].rstrip() + "\n" for i, doc in spec["documents"].items()}
    out["docs/README.md"] = index(spec)
    return out


def bundle_content(system, spec) -> dict:
    return {"files": {FILE: declaration(system.name, spec)}, "jobs": job_files(spec), "resources": None,
            "catalogs": sorted(set(system.catalogs))}


def write(ctx, spec) -> list[str]:
    content = bundle_content(ctx.system, spec)
    base, jbase = bundle.scripts_dir(ctx.system, ctx.goal.id), bundle.jobs_dir(ctx.system, ctx.goal.id)
    stale = [str(p.relative_to(base)) for p in bundle.script_files(base) if str(p.relative_to(base)) not in content["files"]]
    written = bundle.write(ctx, content["files"], catalogs=content["catalogs"], remove=stale)
    jstale = [str(p.relative_to(jbase)) for p in bundle.script_files(jbase, any_file=True)
              if str(p.relative_to(jbase)) not in content["jobs"]]
    bundle.write(ctx, content["jobs"], remove=jstale, jobs=True)
    return written
