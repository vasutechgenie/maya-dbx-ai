"""MAYA G11 evaluation task (run by the bundle job maya_g11_evaluation): ask the served agents and the Genie space every
question of the evaluation dataset, judge each answer against the result of the question's truth SQL, record the
results in Unity Catalog and in MLflow, and fail when a pass rate is below its threshold (so the job's notifications
tell the owners about a regression).

Reads eval_config.json next to this file (catalog tokens resolved for the bundle target). Prints one
MAYA_DEPLOY_RESULT line.
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

RESULT = "MAYA_DEPLOY_RESULT "
JUDGE = """You check whether an answer to a business question is correct, given the correct result.

Question: {question}

Correct result (rows from the governed data; this is the truth):
{expected}

Answer to check:
{answer}

The answer is correct when every figure the question asks for is in the answer and matches the correct result. Allow
rounding to three significant digits, units and formatting (8.46M, 8,461,249.08 and $8.5 million all match 8461249.08;
a fill rate of 0.957 matches 95.7%), a different order of rows, and extra information. The answer is wrong when a
figure is missing or differs, when it names the wrong top item, or when it does not answer.
Reply with JSON only: {{"correct": true or false, "reason": "one sentence"}}"""


def _rows(cols, data, limit=50):
    return [dict(zip(cols, r)) for r in (data or [])[:limit]]


class Evaluator:
    def __init__(self, w, cfg):
        self.w, self.cfg = w, cfg

    def truth(self, spark, q):
        df = spark.sql(q["sql"])
        rows = [r.asDict() for r in df.limit(51).collect()]
        return json.dumps(rows[:50], default=str)

    def ask_agent(self, question):
        body = {"input": [{"role": "user", "content": question}]}
        for attempt in range(4):
            try:
                r = self.w.api_client.do("POST", f"/serving-endpoints/{self.cfg['endpoint']}/invocations", body=body)
                text = " ".join(c.get("text", "") for o in r.get("output") or [] for c in o.get("content") or []
                                if isinstance(c, dict))
                co = r.get("custom_outputs") or {}
                return {"answer": text, "detail": {"routes": co.get("routes"), "config_version": co.get("config_version"),
                                                   "tools": [s.get("tool") for s in co.get("trace") or []]}}
            except Exception as e:
                err = str(e)[:500]
                time.sleep(20 * (attempt + 1))
        return {"error": err}

    def ask_genie(self, question):
        api, sid = self.w.api_client, self.cfg["genie_space_id"]
        for attempt in range(5):
            try:
                m = api.do("POST", f"/api/2.0/genie/spaces/{sid}/start-conversation", body={"content": question})
                cid, mid = m["conversation_id"], m["message_id"]
                deadline = time.time() + 600
                while True:
                    msg = api.do("GET", f"/api/2.0/genie/spaces/{sid}/conversations/{cid}/messages/{mid}")
                    if msg.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
                        break
                    if time.time() > deadline:
                        return {"error": "Genie did not answer in 10 minutes"}
                    time.sleep(3)
                if msg.get("status") != "COMPLETED":
                    return {"error": f"Genie {msg.get('status')}: {(msg.get('error') or {}).get('error', '')}"[:500]}
                parts, sql = [], None
                for a in msg.get("attachments") or []:
                    if a.get("text"):
                        parts.append(a["text"].get("content", ""))
                    if a.get("query"):
                        sql = a["query"].get("query")
                        res = api.do("GET", f"/api/2.0/genie/spaces/{sid}/conversations/{cid}/messages/{mid}/"
                                            f"attachments/{a['attachment_id']}/query-result")
                        sr = res.get("statement_response") or {}
                        cols = [c["name"] for c in ((sr.get("manifest") or {}).get("schema") or {}).get("columns") or []]
                        parts.append("Result rows: " + json.dumps(_rows(cols, (sr.get("result") or {}).get("data_array"))))
                return {"answer": "\n".join(p for p in parts if p), "detail": {"sql": sql, "conversation_id": cid}}
            except Exception as e:
                err = str(e)[:500]
                time.sleep(30 * (attempt + 1) if "429" in err or "RESOURCE_EXHAUSTED" in err else 10)
        return {"error": err}

    def judge(self, question, expected, answer):
        prompt = JUDGE.format(question=question, expected=expected, answer=answer[:6000])
        for attempt in range(4):
            try:
                r = self.w.api_client.do("POST", f"/serving-endpoints/{self.cfg['judge_endpoint']}/invocations",
                                         body={"messages": [{"role": "user", "content": prompt}], "temperature": 0,
                                               "max_tokens": 300})
                text = r["choices"][0]["message"]["content"]
                text = text if isinstance(text, str) else " ".join(c.get("text", "") for c in text if isinstance(c, dict))
                v = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
                return bool(v.get("correct")), str(v.get("reason", ""))[:500]
            except Exception as e:
                err = str(e)[:300]
                time.sleep(10 * (attempt + 1))
        return False, f"judge failed: {err}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-dir", required=True)
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--trigger", default="job")
    ap.add_argument("--catalog", action="append", default=[], help="dev=target catalog name")
    a = ap.parse_args(argv)
    cats = dict(x.split("=", 1) for x in a.catalog)
    sub = lambda s: re.sub(r"\{\{catalog:([A-Za-z0-9_\-]+)\}\}", lambda m: cats.get(m.group(1), m.group(1)), s)
    code_dir = a.code_dir if os.path.exists(a.code_dir) else "/Workspace" + a.code_dir
    cfg = json.loads(sub(open(os.path.join(code_dir, "eval_config.json")).read()))

    from databricks.sdk import WorkspaceClient
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    w = WorkspaceClient()
    if "genie" in cfg["targets"]:
        spaces = [s for s in (w.genie.list_spaces().spaces or []) if s.title == cfg["genie_title"]]
        if not spaces:
            raise RuntimeError(f"Genie space '{cfg['genie_title']}' not found")
        cfg["genie_space_id"] = spaces[0].space_id
    agent_version = None
    if "agent" in cfg["targets"]:
        e = w.serving_endpoints.get(cfg["endpoint"])
        agent_version = {t.key: t.value for t in e.tags or []}.get("maya_config_version")
    ev = Evaluator(w, cfg)
    run_id, run_at = uuid.uuid4().hex, datetime.now(timezone.utc)
    expected = {q["id"]: ev.truth(spark, q) for q in cfg["questions"]}

    def one(target, q):
        t0 = time.time()
        got = ev.ask_agent(q["question"]) if target == "agent" else ev.ask_genie(q["question"])
        ok, reason = (False, got["error"]) if got.get("error") else ev.judge(q["question"], expected[q["id"]], got["answer"])
        return {"target": target, "question_id": q["id"], "question": q["question"], "expected": expected[q["id"]],
                "answer": got.get("answer"), "correct": ok, "reason": reason, "error": got.get("error"),
                "detail": json.dumps(got.get("detail") or {}, default=str), "seconds": round(time.time() - t0, 1)}

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, "agent", q) for q in cfg["questions"] if "agent" in cfg["targets"] and "agent" in q["targets"]]
        genie = [q for q in cfg["questions"] if "genie" in cfg["targets"] and "genie" in q["targets"]]
        results += [one("genie", q) for q in genie]
        results += [f.result() for f in futures]

    summary = {}
    for target in cfg["targets"]:
        rs = [r for r in results if r["target"] == target]
        if not rs:
            continue
        correct = sum(r["correct"] for r in rs)
        threshold = cfg["thresholds"][target]
        summary[target] = {"questions": len(rs), "correct": correct, "pass_rate": round(correct / len(rs), 4),
                           "threshold": threshold, "passed": correct / len(rs) >= threshold}

    job_run = os.environ.get("DATABRICKS_JOB_RUN_ID") or a.trigger
    from pyspark.sql import Row
    spark.createDataFrame([Row(run_id=run_id, run_at=run_at, dataset_version=cfg["version"], agent_version=agent_version,
                               trigger=a.trigger, **{k: r[k] for k in ("target", "question_id", "question", "expected",
                               "answer", "correct", "reason", "error", "detail")}, seconds=float(r["seconds"]))
                           for r in results], schema=spark.table(cfg["results_table"]).schema) \
        .write.mode("append").saveAsTable(cfg["results_table"])
    spark.createDataFrame([Row(run_id=run_id, run_at=run_at, dataset_version=cfg["version"], agent_version=agent_version,
                               trigger=a.trigger, target=t, questions=s["questions"], correct=s["correct"],
                               pass_rate=float(s["pass_rate"]), threshold=float(s["threshold"]), passed=s["passed"])
                           for t, s in summary.items()], schema=spark.table(cfg["runs_table"]).schema) \
        .write.mode("append").saveAsTable(cfg["runs_table"])
    spark.createDataFrame([Row(dataset_version=cfg["version"], question_id=q["id"], question=q["question"], sql=q["sql"],
                               targets=",".join(q["targets"]), tags=",".join(q.get("tags") or []))
                           for q in cfg["questions"]], schema=spark.table(cfg["dataset_table"]).schema) \
        .write.mode("overwrite").saveAsTable(cfg["dataset_table"])

    mlflow_run = None
    try:
        import mlflow
        mlflow.set_experiment(a.experiment)
        with mlflow.start_run(run_name=f"maya-eval-{run_id[:8]}") as r:
            mlflow.set_tags({"maya_goal": "G11", "dataset_version": cfg["version"], "agent_version": str(agent_version),
                             "trigger": a.trigger})
            for t, s in summary.items():
                mlflow.log_metrics({f"{t}_pass_rate": s["pass_rate"], f"{t}_correct": s["correct"]})
            mlflow.log_table({k: [r[k] for r in results] for k in ("target", "question", "correct", "reason", "answer")},
                             "eval_results.json")
            mlflow_run = r.info.run_id
    except Exception as e:
        print(f"MLflow logging skipped: {e}")

    return {"ok": True, "eval_run_id": run_id, "dataset_version": cfg["version"], "agent_version": agent_version,
            "summary": summary, "passed": all(s["passed"] for s in summary.values()), "mlflow_run_id": mlflow_run,
            "job_run": job_run}


if __name__ == "__main__":
    t0 = time.time()
    try:
        out = main()
    except Exception as e:
        traceback.print_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:3000]}
    out["seconds"] = round(time.time() - t0, 1)
    print(RESULT + json.dumps(out, default=str))
    if not (out["ok"] and out.get("passed")):  # a successful job task must not call sys.exit
        sys.exit(1)
