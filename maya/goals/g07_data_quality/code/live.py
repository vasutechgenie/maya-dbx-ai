"""What G7 delivered, read back from the workspace: the check job, the alerts and the dashboard."""
from maya.bundle import run_scripts
from maya.goals.g06_dashboards.code.common import (dashboard_permissions, differences, find_dashboard, live_dashboard,
                                                   published, url)

from .deliver import lakeview


def job(ctx, job_id) -> dict | None:
    from databricks.sdk.errors import NotFound
    try:
        return ctx.ws.client.api_client.do("GET", "/api/2.2/jobs/get", query={"job_id": job_id}).get("settings")
    except NotFound:
        return None
    except Exception as e:
        if "does not exist" in str(e).lower():
            return None
        raise


def alert(ctx, alert_id) -> dict | None:
    from databricks.sdk.errors import NotFound
    try:
        a = ctx.ws.client.api_client.do("GET", f"/api/2.0/alerts/{alert_id}")
    except NotFound:
        return None
    return None if a.get("lifecycle_state") == "DELETED" else a


def evaluate(ctx, alert_id) -> dict:
    return run_scripts.evaluate_alert(ctx.ws.client, ctx.system.spec["target"]["warehouse_id"], alert_id,
                                      name=f"maya G7 alert test ({ctx.run_id})")


def dashboard(ctx, spec) -> tuple[str | None, dict | None]:
    sid = find_dashboard(ctx, spec["dashboard"])
    return sid, (live_dashboard(ctx, sid) if sid else None)


def dashboard_differences(ctx, spec) -> list[str]:
    sid, dash = dashboard(ctx, spec)
    if not sid:
        return ["the dashboard no longer exists"]
    return differences(lakeview(spec), dash["serialized"])

