"""Run state in Delta tables under target.state_schema: goal status, runs, nodes, checks, approvals, certifications.
Each project owns its state schema; maya.core.project creates and upgrades the tables (TABLES plus the goals' own)."""
import json
import uuid
from datetime import datetime, timezone

from .workspace import Workspace, lit

TABLES = {
    "goal_status": "system STRING, goal_id STRING, status STRING, config_hash STRING, run_id STRING, detail STRING, updated_at TIMESTAMP",
    "goal_runs": "run_id STRING, system STRING, goal_id STRING, status STRING, config_hash STRING, config_json STRING, started_at TIMESTAMP, ended_at TIMESTAMP, summary_json STRING",
    "node_runs": "run_id STRING, goal_id STRING, node STRING, node_type STRING, status STRING, attempt INT, started_at TIMESTAMP, ended_at TIMESTAMP, output_json STRING",
    "check_results": "run_id STRING, system STRING, goal_id STRING, check_id STRING, checklist STRING, severity STRING, passed BOOLEAN, observed STRING, expected STRING, evidence STRING, checked_at TIMESTAMP",
    "approvals": "approval_id STRING, run_id STRING, system STRING, goal_id STRING, gate STRING, approver STRING, status STRING, items_json STRING, decided_by STRING, decided_at TIMESTAMP, note STRING, created_at TIMESTAMP",
    "certifications": "run_id STRING, system STRING, goal_id STRING, certified_by STRING, certified_at TIMESTAMP, expires_at TIMESTAMP, config_hash STRING, evidence_path STRING, checks_json STRING",
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix):
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


class State:
    def __init__(self, ws: Workspace, schema: str, system: str):
        self.ws, self.schema, self.system = ws, schema, system

    def t(self, name):
        return f"{self.schema}.{name}"

    def _insert(self, table, values: list):
        self.ws.sql(f"INSERT INTO {self.t(table)} VALUES ({', '.join(values)})")

    # goal status ---------------------------------------------------------
    def set_status(self, goal_id, status, config_hash=None, run_id=None, detail=None):
        self.ws.sql(f"""MERGE INTO {self.t('goal_status')} t
            USING (SELECT {lit(self.system)} AS system, {lit(goal_id)} AS goal_id) s
            ON t.system = s.system AND t.goal_id = s.goal_id
            WHEN MATCHED THEN UPDATE SET status = {lit(status)}, config_hash = coalesce({lit(config_hash)}, t.config_hash),
                 run_id = coalesce({lit(run_id)}, t.run_id), detail = {lit(detail)}, updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (system, goal_id, status, config_hash, run_id, detail, updated_at)
                 VALUES ({lit(self.system)}, {lit(goal_id)}, {lit(status)}, {lit(config_hash)},
                 {lit(run_id)}, {lit(detail)}, current_timestamp())""")

    def statuses(self) -> dict:
        rows = self.ws.sql(f"SELECT * FROM {self.t('goal_status')} WHERE system = {lit(self.system)}")
        return {r["goal_id"]: r for r in rows}

    # runs and nodes ------------------------------------------------------
    def start_run(self, run_id, goal_id, config_hash, config):
        self._insert("goal_runs", [lit(run_id), lit(self.system), lit(goal_id), lit("running"), lit(config_hash),
                                   lit(json.dumps(config, default=str)), "current_timestamp()", "NULL", "NULL"])

    def end_run(self, run_id, status, summary):
        self.ws.sql(f"""UPDATE {self.t('goal_runs')} SET status = {lit(status)}, ended_at = current_timestamp(),
            summary_json = {lit(json.dumps(summary, default=str))} WHERE run_id = {lit(run_id)}""")

    def node(self, run_id, goal_id, node, node_type, status, attempt, started, output):
        self._insert("node_runs", [lit(run_id), lit(goal_id), lit(node), lit(node_type), lit(status), str(attempt),
                                   f"timestamp{lit(started)}", "current_timestamp()", lit(json.dumps(output, default=str)[:60000])])

    def checks(self, run_id, goal_id, results: list):
        if not results:
            return
        rows = ", ".join("(" + ", ".join([lit(run_id), lit(self.system), lit(goal_id), lit(r["id"]), lit(r.get("checklist")),
                                          lit(r["severity"]), lit(r["passed"]), lit(str(r["observed"])), lit(str(r["expected"])),
                                          lit(json.dumps(r.get("evidence"), default=str)[:20000]), "current_timestamp()"]) + ")"
                         for r in results)
        self.ws.sql(f"INSERT INTO {self.t('check_results')} VALUES {rows}")

    # approvals -----------------------------------------------------------
    def request_approval(self, run_id, goal_id, gate, approver, items) -> str:
        aid = new_id("apr")
        self._insert("approvals", [lit(aid), lit(run_id), lit(self.system), lit(goal_id), lit(gate), lit(approver), lit("pending"),
                                   lit(json.dumps(items, default=str)), "NULL", "NULL", "NULL", "current_timestamp()"])
        return aid

    def approval(self, run_id, gate):
        rows = self.ws.sql(f"""SELECT * FROM {self.t('approvals')} WHERE run_id = {lit(run_id)} AND gate = {lit(gate)}
            ORDER BY created_at DESC LIMIT 1""")
        return rows[0] if rows else None

    def pending_approvals(self, goal_id=None):
        extra = f"AND goal_id = {lit(goal_id)}" if goal_id else ""
        return self.ws.sql(f"""SELECT * FROM {self.t('approvals')} WHERE system = {lit(self.system)} AND status = 'pending' {extra}
            ORDER BY created_at""")

    def set_approval_items(self, approval_id, items):
        self.ws.sql(f"UPDATE {self.t('approvals')} SET items_json = {lit(json.dumps(items, default=str))} "
                    f"WHERE approval_id = {lit(approval_id)}")

    def decide(self, approval_id, decision, by, note):
        self.ws.sql(f"""UPDATE {self.t('approvals')} SET status = {lit(decision)}, decided_by = {lit(by)},
            decided_at = current_timestamp(), note = {lit(note)} WHERE approval_id = {lit(approval_id)}""")

    # certification -------------------------------------------------------
    def certify(self, run_id, goal_id, by, days, config_hash, evidence_path, checks):
        self._insert("certifications", [lit(run_id), lit(self.system), lit(goal_id), lit(by), "current_timestamp()",
                                        f"current_timestamp() + INTERVAL {int(days)} DAYS", lit(config_hash), lit(evidence_path),
                                        lit(json.dumps(checks, default=str)[:60000])])

    def latest_certification(self, goal_id):
        rows = self.ws.sql(f"""SELECT * FROM {self.t('certifications')} WHERE system = {lit(self.system)} AND goal_id = {lit(goal_id)}
            ORDER BY certified_at DESC LIMIT 1""")
        return rows[0] if rows else None

    def latest_checks(self, goal_id):
        return self.ws.sql(f"""SELECT * FROM {self.t('check_results')} WHERE system = {lit(self.system)} AND goal_id = {lit(goal_id)}
            AND run_id = (SELECT max_by(run_id, checked_at) FROM {self.t('check_results')}
                          WHERE system = {lit(self.system)} AND goal_id = {lit(goal_id)})
            QUALIFY row_number() OVER (PARTITION BY check_id ORDER BY checked_at DESC) = 1
            ORDER BY check_id""")

    def runs(self, goal_id=None, limit=20):
        extra = f"AND goal_id = {lit(goal_id)}" if goal_id else ""
        return self.ws.sql(f"""SELECT run_id, goal_id, status, config_hash, started_at, ended_at, summary_json FROM {self.t('goal_runs')}
            WHERE system = {lit(self.system)} {extra} ORDER BY started_at DESC LIMIT {int(limit)}""")

    def latest_run(self, goal_id):
        rows = self.ws.sql(f"""SELECT * FROM {self.t('goal_runs')} WHERE system = {lit(self.system)} AND goal_id = {lit(goal_id)}
            ORDER BY started_at DESC LIMIT 1""")
        return rows[0] if rows else None

    def agent_activity(self, run_id):
        return self.ws.sql(f"""SELECT node, output_json FROM {self.t('node_runs')} WHERE run_id = {lit(run_id)} AND node_type = 'agent'""")
