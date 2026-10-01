"""maya CLI: init | migrate | validate | goals | plan | run | review | status | projects | portfolio."""
import argparse
import json
import sys

from .core.project import ProjectError
from .core.spec import SpecError, load_goals, load_system


def _engine(args, open_project=True):
    from .core.runner import Engine
    e = Engine(load_system(args.system))
    if open_project:
        _report_bootstrap(e.open(), verbose=False)
    return e


def _report_bootstrap(b, verbose):
    st, dash = b["state"], b.get("dashboard") or {}
    if verbose or st["statements"]:
        if st["from_version"] == 0:
            print(f"state schema {st['schema']}: created at version {st['version']}")
        elif st["from_version"] == st["version"] and st["statements"]:
            print(f"state schema {st['schema']}: version {st['version']}, {len(st['statements'])} additive changes "
                  f"(tables or columns of newly installed goals); restore points {st['restore_points']}")
        elif st["statements"] or st["from_version"] != st["version"]:
            print(f"state schema {st['schema']}: upgraded {st['from_version']} -> {st['version']} "
                  f"({len(st['statements'])} additive changes; restore points {st['restore_points']})")
        else:
            print(f"state schema {st['schema']}: version {st['version']}, up to date")
    for w in st["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    if dash and (verbose or dash["action"] != "reused"):
        print(f"dashboard {dash['action']}: {dash['url']}")


def cmd_validate(args):
    system = load_system(args.system)
    goals = load_goals()
    declared = system.spec.get("goals") or {}
    ok = True
    for g in goals.values():
        try:
            from .core.gates import lint
            from .core.graph import parse
            parse(g.graph())
            problems = lint(g)
            if problems:
                raise ValueError("gate validators: " + "; ".join(problems))
            try:
                g.effective_inputs(system)
            except Exception as e:
                if g.id in declared:
                    raise
                print(f"  --   {g.id} {g.title}: not configured in maya.yaml ({e})")
                continue
            print(f"  ok   {g.id} {g.title}")
        except Exception as e:
            ok = False
            print(f"  FAIL {g.id}: {e}")
    print(f"system {system.name}: {'valid' if ok else 'invalid'}  (model {system.model()})")
    return 0 if ok else 1


def cmd_goals(args):
    e = _engine(args)
    for gid, s in e.goal_states().items():
        extra = f"   ({s['detail']})" if s.get("self_certified") and s.get("detail") else ""
        if s.get("config_error") and s["status"] == "invalid_config":
            extra = f"   ({s['config_error']})"
        print(f"  {gid:<4} {s['goal'].title:<36} {s['status']:<18} needs: {', '.join(s['goal'].prerequisites) or '-'}{extra}")
    return 0


def cmd_plan(args):
    e = _engine(args)
    gid = args.goal or e.next_goal()
    if not gid:
        print("nothing to run")
        return 0
    g = e.goals[gid]
    nodes = g.graph()["nodes"]
    print(f"{gid} {g.title}  (prerequisites: {', '.join(g.prerequisites) or 'none'})")
    print(f"inputs: {json.dumps(g.effective_inputs(e.system))}")
    for name, n in nodes.items():
        print(f"  {n['type']:<9} {name:<22} {n.get('run') or n.get('agent') or n.get('approver') or ''}")
    for edge in g.graph().get("edges") or []:
        print(f"    {edge}")
    return 0


def cmd_run(args):
    e = _engine(args)
    gid = args.goal or e.next_goal()
    if not gid:
        print("nothing to run: all implemented goals are certified")
        return 0
    res = e.run_goal(gid, resume=args.resume, approve_as_tester=args.approve_as_tester, force=args.force)
    _publish(e)
    return 0 if res["status"] in ("certified", "completed", "paused") else 1


def cmd_review(args):
    """Pending approvals. Every gate validates its items (schema + goal check) before anything is recorded: an edit
    is accepted and an approval is recorded only when everything validates; otherwise nothing changes."""
    from pathlib import Path

    import yaml

    from .core import gates
    e = _engine(args)
    pending = [p for p in e.state.pending_approvals(args.goal) if not args.id or p["approval_id"] == args.id]
    if not pending:
        print("no pending approvals")
        return 0
    if (args.edit or args.export) and len(pending) != 1:
        print("--edit / --export work on one approval: select it with --id", file=sys.stderr)
        return 2
    refused, approved = 0, set()
    for p in pending:
        meta = json.loads(p["items_json"] or "{}")
        print(f"  {p['approval_id']}  {p['goal_id']}  gate={p['gate']}  approver={p['approver']}  "
              f"items={meta.get('summary') or meta.get('checks') or {}}")
        ctx = e.context(p["goal_id"], p["run_id"])
        if args.export:
            out = Path(args.export)
            out.mkdir(parents=True, exist_ok=True)
            for name in gates.specs(gates.gate_spec(ctx, p["gate"])):
                f = out / f"{Path(name).stem}.yaml"
                f.write_text(yaml.safe_dump(ctx.read_artefact(name), sort_keys=False, allow_unicode=True, width=120))
                editable = gates.specs(gates.gate_spec(ctx, p["gate"]))[name].get("editable")
                print(f"    {name} -> {f}" + ("   (editable: --edit " + f"{name}={f})" if editable else ""))
            continue
        if args.reject:
            e.state.decide(p["approval_id"], "rejected", e.ws.user, args.note or "")
            e.state.set_status(p["goal_id"], "failed", run_id=p["run_id"],
                               detail=f"{p['gate']} rejected by {e.ws.user}: {args.note or ''}")
            e.state.end_run(p["run_id"], "rejected", {"gate": p["gate"], "note": args.note})
            print(f"    -> rejected by {e.ws.user}")
            continue
        edits, problems = {}, []
        for spec in args.edit or []:
            item, _, path = spec.partition("=")
            try:
                edits[item] = gates.load_edit(path) if path else None
            except Exception as ex:
                problems.append(f"{item}: cannot read {path}: {str(ex).splitlines()[0]}")
            if not path:
                problems.append(f"--edit expects ITEM=FILE, got {spec!r}")
        problems += gates.approval_problems(ctx, p["gate"], meta.get("digests") or {}, edits)
        if problems:
            print(f"    validation: {len(problems)} problem(s); " + ("nothing recorded" if args.approve else "fix before approving"))
            for x in problems[:25]:
                print(f"      - {x}")
            refused += 1
            continue
        print("    validation: all items valid" + (f" (edited: {', '.join(edits)})" if edits else ""))
        if not args.approve:
            continue
        for item, data in edits.items():
            gates.accept_edit(ctx, p["gate"], item, data)
        digests = {n: gates.digest(ctx.read_artefact(n)) for n in gates.specs(gates.gate_spec(ctx, p["gate"]))}
        e.state.set_approval_items(p["approval_id"], {**meta, "digests": digests, "edited": sorted(edits)})
        note = (args.note or "") + (f" [edited: {', '.join(sorted(edits))}]" if edits else "")
        e.state.decide(p["approval_id"], "approved", e.ws.user, note.strip())
        print(f"    -> approved by {e.ws.user}")
        approved.add(p["goal_id"])
    if approved and not args.no_resume:
        for gid in sorted(approved):
            e.run_goal(gid, resume=True)
    _publish(e)
    return 1 if refused and (args.approve or args.edit) else 0


def cmd_bundle(args):
    """Write the project bundle from every certified goal; --deploy also deploys it and runs the whole deploy job."""
    from .core import bundle
    e = _engine(args)
    for gid, files in bundle.export_all(e).items():
        print(f"  {gid}: {len(files)} scripts")
    print(f"bundle: {bundle.root(e.system)}")
    if not args.deploy:
        return 0
    res = bundle.deploy_all(e.system, e.ws)
    for task, r in res["tasks"].items():
        print(f"  {task}: {'ok' if r.get('ok') else 'FAILED'}  "
              + (f"{r.get('statements')} statements in {len(r.get('done') or [])} scripts" if r.get("ok")
                 else f"{r.get('failed')} statement {r.get('statement')}: {str(r.get('error'))[:400]}"))
    print(f"deploy job run {res['job_run_id']}: {'ok' if res['ok'] else 'FAILED'}")
    if res.get("output"):
        print(res["output"], file=sys.stderr)
    return 0 if res["ok"] else 1


def _publish(e, report=None, quiet=True):
    """Refresh this project's rows in the shared state schema (read by the dashboard)."""
    from . import status
    from .status.publish import publish
    from .status.publish import register
    try:
        report = report or status.collect(e)
        warnings = publish(e, report)
        warnings += register(e, report, ((e.bootstrap or {}).get("dashboard") or {}).get("url"))
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
    except Exception as ex:  # a dashboard refresh must never fail a run
        print(f"warning: could not publish status to {e.state.schema}: {ex}", file=sys.stderr)
    else:
        if not quiet:
            reg = e.system.spec["target"].get("registry")
            print(f"published to {e.state.schema}" + (f" and registry {reg}.projects" if reg else ""))


def cmd_status(args):
    from . import status
    e = _engine(args)
    report = status.collect(e)
    paths = status.write(e, report)
    print(status.render_text(report))
    print("\nreport: " + ", ".join(str(p) for p in paths))
    _publish(e, report, quiet=False)
    return 0


def cmd_init(args):
    """Project start-up: state schema created or upgraded, dashboard reused or created, status published."""
    e = _engine(args, open_project=False)
    _report_bootstrap(e.open(), verbose=True)
    _publish(e, quiet=False)
    return 0


def cmd_migrate(args):
    """Copy this project's history out of an older shared state schema into its own (the source is left as is)."""
    from .core.project import migrate_from
    e = _engine(args, open_project=False)
    if args.source == e.state.schema:
        print("--from must differ from this project's target.state_schema")
        return 2
    _report_bootstrap({"state": e.ensure_state()}, verbose=True)
    copied = migrate_from(e.ws, args.source, e.state.schema, e.system.name, e.goals)
    for t, n in copied.items():
        print(f"  {t:<18} {n} rows")
    _report_bootstrap(e.open(), verbose=True)
    _publish(e, quiet=False)
    return 0


def cmd_projects(args):
    e = _engine(args)
    reg = e.system.spec["target"].get("registry")
    if not reg:
        print("no target.registry configured: only this project is known")
        rows = [{"project": e.system.name, "state_schema": e.state.schema}]
    else:
        rows = e.ws.sql(f"SELECT * FROM {reg}.projects ORDER BY project")
        print(f"MAYA projects in {reg}.projects")
    for p in rows:
        print(f"\n  {p['project']}  MAYA {p.get('maya_version', '?')}  state {p['state_schema']}  "
              f"{p.get('goals_certified', '?')}/{p.get('goals_total', '?')} goals certified  "
              f"refreshed {str(p.get('refreshed_at'))[:19]}\n    dashboard: {p.get('dashboard_url')}"
              + (f"\n    next: {p['next_action']}" if p.get("next_action") else ""))
    return 0


def cmd_portfolio(args):
    """Create or update the portfolio dashboard over the workspace registry."""
    from .status.dashboard import build_portfolio, publish_dashboard
    e = _engine(args)
    reg = e.system.spec["target"].get("registry")
    if not reg:
        print("set target.registry in maya.yaml to use a portfolio dashboard")
        return 2
    _publish(e)
    r = publish_dashboard(e.ws, build_portfolio(f"{reg}.projects"), e.system.spec["target"]["warehouse_id"],
                          args.name, args.path or f"/Users/{e.ws.user}/MAYA")
    print(f"portfolio dashboard published: {r['url']}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="maya")
    ap.add_argument("--system", default="maya.yaml", help="path to the system spec (maya.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("goals")
    p = sub.add_parser("plan"); p.add_argument("--goal")
    p = sub.add_parser("run")
    p.add_argument("--goal"); p.add_argument("--next", action="store_true")
    p.add_argument("--resume", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--approve-as-tester", action="store_true",
                   help="record approvals and sign-off as the current user (testing only)")
    p = sub.add_parser("review")
    p.add_argument("--goal"); p.add_argument("--approve", action="store_true"); p.add_argument("--reject", action="store_true")
    p.add_argument("--note"); p.add_argument("--no-resume", action="store_true")
    p.add_argument("--id", help="one approval id (required with --edit / --export)")
    p.add_argument("--export", metavar="DIR", help="write the gate's items as YAML files for review or editing")
    p.add_argument("--edit", action="append", metavar="ITEM=FILE",
                   help="replace an editable item with the YAML / JSON in FILE (validated before it is accepted)")
    sub.add_parser("status")
    p = sub.add_parser("bundle", help="write the project's Asset Bundle from every certified goal")
    p.add_argument("--deploy", action="store_true", help="also deploy it and run the whole deploy job")
    sub.add_parser("init", help="create / upgrade the project state schema and dashboard")
    p = sub.add_parser("migrate", help="copy this project's history from an older shared state schema")
    p.add_argument("--from", dest="source", required=True)
    sub.add_parser("projects", help="every MAYA project in the workspace registry")
    p = sub.add_parser("portfolio", help="create or update the portfolio dashboard over the registry")
    p.add_argument("--name", default="MAYA projects"); p.add_argument("--path", help="workspace folder (default /Users/<you>/MAYA)")
    args = ap.parse_args(argv)
    try:
        return {"validate": cmd_validate, "goals": cmd_goals, "plan": cmd_plan, "run": cmd_run,
                "review": cmd_review, "status": cmd_status, "bundle": cmd_bundle,
                "projects": cmd_projects, "portfolio": cmd_portfolio,
                "init": cmd_init, "migrate": cmd_migrate}[args.cmd](args)
    except SpecError as e:
        print(f"spec error: {e}", file=sys.stderr)
        return 2
    except ProjectError as e:
        print(f"project error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
