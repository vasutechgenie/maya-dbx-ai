"""`maya status`: where a system stands - goals, configuration, runs, checks, certification, next actions."""
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from maya.core.catalog import describe_layers


def collect(engine) -> dict:
    states = engine.goal_states()
    st = engine.state
    goals = []
    for gid, s in states.items():
        g = s["goal"]
        run = st.latest_run(gid)
        checks = st.latest_checks(gid)
        cert = s["certification"]
        goals.append({
            "id": gid, "title": g.title, "milestone": g.milestone, "prerequisites": g.prerequisites,
            "status": s["status"], "detail": s["detail"], "missing_prerequisites": s["missing_prerequisites"],
            "stale_reasons": s["stale_reasons"], "config": s["inputs"], "config_hash": s["config_hash"],
            "config_error": s["config_error"],
            "model": engine.system.model(g.spec["spec"]["harness"].get("model")),
            "last_run": {k: run[k] for k in ("run_id", "status", "started_at", "ended_at")} if run else None,
            "checks": [{"id": c["check_id"], "checklist": c["checklist"], "severity": c["severity"],
                        "passed": str(c["passed"]).lower() == "true", "observed": c["observed"], "expected": c["expected"]}
                       for c in checks],
            "certification": {k: cert[k] for k in ("certified_by", "certified_at", "expires_at", "config_hash", "run_id")}
            if cert else None,
        })
    milestones = {}
    for gl in goals:
        if gl["milestone"]:
            upto = [x for x in goals if x["id"][1:].zfill(3) <= gl["id"][1:].zfill(3)]
            milestones[gl["milestone"]] = all(x["status"] == "certified" for x in upto)
    pending = st.pending_approvals()
    nxt = engine.next_goal(states)
    actions = []
    for p in pending:
        actions.append(f"{p['goal_id']}: approve '{p['gate']}' ({p['approver']}) -> maya review --approve")
    if nxt and not pending:
        actions.append(f"run {nxt} -> maya run --goal {nxt}")
    for gl in goals:
        if gl["status"] == "stale":
            actions.append(f"{gl['id']} is stale ({'; '.join(gl['stale_reasons'])}) -> re-run and re-certify")
        if gl["status"] == "invalid_config":
            actions.append(f"{gl['id']}: fix configuration: {gl['config_error']}")
    implemented = [g["id"] for g in goals]
    return {"system": engine.system.name, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "spec_hash": engine.system.spec_hash, "workspace": engine.ws.host, "catalogs": engine.system.catalogs,
            "foundation": describe_layers(engine.system),
            "ai_gateway_model": engine.system.model(), "goals": goals, "goals_implemented": implemented,
            "milestones": milestones, "pending_approvals": [{k: p[k] for k in ("goal_id", "gate", "approver", "approval_id", "created_at")}
                                                         for p in pending],
            "next_actions": actions}


_COLORS = {"certified": "#1a7f37", "ready": "#0969da", "stale": "#bf8700", "blocked": "#6e7781", "failed": "#cf222e",
           "running": "#8250df", "awaiting_approval": "#bf8700", "awaiting_sign_off": "#bf8700", "invalid_config": "#cf222e"}


def _e(v):
    return html.escape("" if v is None else str(v))


def render_html(r: dict) -> str:
    rows = []
    for g in r["goals"]:
        color = _COLORS.get(g["status"], "#444")
        passed = sum(c["passed"] for c in g["checks"])
        cert = g["certification"]
        rows.append(f"""<tr><td><b>{_e(g['id'])}</b></td><td>{_e(g['title'])}</td><td>{_e(', '.join(g['prerequisites']) or '-')}</td>
<td><span class="pill" style="background:{color}">{_e(g['status'])}</span><div class="sub">{_e(g['detail'] or '; '.join(g['stale_reasons']) or ('needs ' + ', '.join(g['missing_prerequisites']) if g['missing_prerequisites'] else ''))}</div></td>
<td>{f'{passed}/{len(g["checks"])}' if g['checks'] else '-'}</td>
<td>{_e(cert['certified_by']) + '<div class="sub">' + _e(cert['certified_at']) + ' &rarr; ' + _e(cert['expires_at']) + '</div>' if cert else '-'}</td>
<td><code>{_e(g['config_hash'])}</code></td></tr>""")
    details = []
    for g in r["goals"]:
        checks = "".join(f"<tr><td>{'&#10003;' if c['passed'] else '&#10007;'}</td><td>{_e(c['id'])}</td><td>{_e(c['checklist'])}</td>"
                         f"<td>{_e(c['severity'])}</td><td>{_e(c['observed'])}</td><td>{_e(c['expected'])}</td></tr>" for c in g["checks"])
        details.append(f"""<h3>{_e(g['id'])} {_e(g['title'])}</h3>
<p class="sub">model: {_e(g['model'])} &middot; last run: {_e((g['last_run'] or {}).get('run_id'))} ({_e((g['last_run'] or {}).get('status'))})</p>
<details><summary>configuration</summary><pre>{_e(json.dumps(g['config'], indent=1))}</pre></details>
{'<table><tr><th></th><th>check</th><th>checklist</th><th>severity</th><th>observed</th><th>expected</th></tr>' + checks + '</table>' if checks else '<p class="sub">no checks recorded</p>'}""")
    ms = " ".join(f'<span class="pill" style="background:{"#1a7f37" if v else "#6e7781"}">{_e(k)}: {"reached" if v else "not yet"}</span>'
                  for k, v in r["milestones"].items())
    actions = "".join(f"<li>{_e(a)}</li>" for a in r["next_actions"]) or "<li>nothing pending</li>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>MAYA status - {_e(r['system'])}</title><style>
body{{font:14px -apple-system,Segoe UI,sans-serif;margin:28px;color:#1f2328}} table{{border-collapse:collapse;width:100%;margin:8px 0 18px}}
td,th{{border-bottom:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f6f8fa}}
.pill{{color:#fff;border-radius:10px;padding:2px 9px;font-size:12px}} .sub{{color:#57606a;font-size:12px}} pre{{background:#f6f8fa;padding:8px}}
</style></head><body>
<h1>MAYA status &middot; {_e(r['system'])}</h1>
<p class="sub">{_e(r['workspace'])} &middot; catalogs {_e(', '.join(r['catalogs']))} &middot; AI Gateway model {_e(r['ai_gateway_model'])} &middot; spec {_e(r['spec_hash'])} &middot; generated {_e(r['generated_at'])}</p>
<p>{ms}</p>
<h2>Next actions</h2><ul>{actions}</ul>
<h2>Goals</h2><table><tr><th>goal</th><th>title</th><th>needs</th><th>status</th><th>checks</th><th>certified</th><th>config</th></tr>{''.join(rows)}</table>
<h2>Goal detail</h2>{''.join(details)}
</body></html>"""


def render_text(r: dict) -> str:
    lines = [f"MAYA status  {r['system']}   model={r['ai_gateway_model']}   {r['generated_at']}", ""]
    for layer, f in r["foundation"].items():
        pick = "all tables" if f["tables"] == "all" else ", ".join(f["tables"])
        lines.append(f"  {layer:<8} {', '.join(f['sources'])}  ({pick}"
                     + (f"; excluding {', '.join(f['exclude'])})" if f["exclude"] else ")"))
    lines.append("")
    for g in r["goals"]:
        passed = sum(c["passed"] for c in g["checks"])
        cert = g["certification"]
        lines.append(f"  {g['id']:<4} {g['title'][:34]:<34} {g['status']:<18} checks {passed}/{len(g['checks'])}"
                     + (f"  certified by {cert['certified_by']} until {str(cert['expires_at'])[:10]}" if cert else ""))
    lines.append("")
    lines += [f"  milestone {k}: {'reached' if v else 'not yet'}" for k, v in r["milestones"].items()]
    lines += ["", "Next actions:"] + [f"  - {a}" for a in r["next_actions"] or ["nothing pending"]]
    return "\n".join(lines)


def write(engine, report: dict) -> list[Path]:
    cfg = engine.system.spec.get("status") or {}
    out = engine.system.base_dir / cfg.get("out_dir", "reports")
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for fmt in cfg.get("formats", ["html", "json"]):
        p = out / f"status.{fmt}"
        p.write_text(render_html(report) if fmt == "html" else json.dumps(report, indent=1, default=str))
        paths.append(p)
    return paths
