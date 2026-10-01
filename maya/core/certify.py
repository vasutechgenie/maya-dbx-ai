"""Certification gate: all mandatory checks pass + approver sign-off -> certification record, UC tags, evidence pack."""
import re

from .graph import Paused
from .workspace import lit


def certify(ctx, approve_as_tester=False) -> dict:
    cert = ctx.goal.spec["spec"].get("certification") or {}
    checks = ctx.state.latest_checks(ctx.goal.id)
    failed = [c["check_id"] for c in checks if c["severity"] == "mandatory" and str(c["passed"]).lower() != "true"]
    if failed:
        raise RuntimeError(f"cannot certify {ctx.goal.id}: mandatory checks failing {failed}")

    gate = "certification"
    roles = cert.get("approvers") or ["data_owner"]
    approver = ctx.system.approver(roles[0]) or roles[0]
    nodes = ctx.goal.graph()["nodes"]
    sign_off_gates = [n for n, s in nodes.items() if s["type"] == "gate" and s.get("approver") in roles]
    appr = next((a for a in (ctx.state.approval(ctx.run_id, g) for g in sign_off_gates)
                 if a and a["status"] == "approved"), None) or ctx.state.approval(ctx.run_id, gate)
    if not appr:
        aid = ctx.state.request_approval(ctx.run_id, ctx.goal.id, gate, approver,
                                         {"checks": {c["check_id"]: c["observed"] for c in checks}})
        appr = {"approval_id": aid, "status": "pending"}
    if appr["status"] == "rejected":
        raise RuntimeError(f"certification rejected: {appr.get('note')}")
    if appr["status"] != "approved":
        if not (approve_as_tester or ctx.system.auto_approve()):
            ctx.state.set_status(ctx.goal.id, "awaiting_sign_off", run_id=ctx.run_id, detail=f"sign-off: {approver}")
            raise Paused(gate, f"waiting for {approver} to sign off {ctx.goal.id} ({appr['approval_id']})")
        by = ctx.ws.user if approve_as_tester else f"auto ({approver})"
        ctx.state.decide(appr["approval_id"], "approved", by,
                         "signed off in tester mode (--approve-as-tester)" if approve_as_tester
                         else "auto-signed: all mandatory checks pass (certification.approvals: auto)")
        appr["decided_by"] = by
    signed_by = appr.get("decided_by") or ctx.ws.user

    evidence = ctx.write_artefact("evidence.json", {
        "goal": ctx.goal.id, "run_id": ctx.run_id, "config": ctx.inputs, "checks": checks,
        "node_outputs": {k: v for k, v in ctx.outputs.items()}})
    days = int(cert.get("valid_days", 180))
    from .harness import config_hash
    ctx.state.certify(ctx.run_id, ctx.goal.id, signed_by, days, config_hash(ctx.goal, ctx.inputs, ctx.system),
                      str(evidence), checks)

    from .bundle import record_certification
    manifest = record_certification(ctx, signed_by)

    project = re.sub(r"[^a-z0-9]+", "_", ctx.system.name.lower()).strip("_")
    tag = (cert.get("status_tag") or f"maya_{project}_{ctx.goal.id.lower()}_{ctx.goal.name}")
    for layer in ctx.system.layers.values():
        for s in layer["sources"]:
            ctx.ws.sql(f"ALTER SCHEMA {s['catalog']}.{s['schema']} SET TAGS ({lit(tag)} = 'certified')")
    return {"certified_by": signed_by, "valid_days": days, "status_tag": tag, "evidence": str(evidence),
            "bundle_manifest": str(manifest) if manifest else None}
