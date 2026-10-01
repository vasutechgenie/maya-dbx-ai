"""Agent nodes run on Omnigent. MAYA materialises the goal's agent YAML with the executor from maya.yaml
(Unity Catalog model service through Databricks AI Gateway, workspace auth) plus the generic MAYA tools,
then runs it headless."""
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

from .agent_tools import TASKS_DIR, task_file

TASK_ID = {"type": "string", "description": "The task_id given in the prompt."}
MAYA_TOOLS = {
    "get_task_input": {
        "type": "function", "callable": "maya.core.agent_tools.get_task_input",
        "description": "Get the task input for this run: what to work on, business context, and the result schema.",
        "parameters": {"type": "object", "properties": {"task_id": TASK_ID}, "required": ["task_id"]},
    },
    "submit_result": {
        "type": "function", "callable": "maya.core.agent_tools.submit_result",
        "description": "Submit the final result as a JSON object matching result_schema. Resubmit if REJECTED.",
        "parameters": {"type": "object", "properties": {"task_id": TASK_ID, "result": {"type": "object"}},
                       "required": ["task_id", "result"]},
    },
}

PROMPT = ("Task: {task}. Your task_id is {task_id}. First call get_task_input with this task_id. Do the task using "
          "only that input. Then call submit_result with the task_id and one JSON object that matches result_schema "
          "exactly. If it is REJECTED, fix the problem and submit again. Do not ask questions; finish with a one-line "
          "summary.")


class AgentError(Exception):
    pass


def executor_for(ctx, model: str) -> dict:
    """Omnigent executor: the AI Gateway model service (e.g. system.ai.claude-sonnet-4-6) over the gateway's
    chat-completions surface, authenticated with the system's Databricks profile."""
    gw = ctx.system.spec.get("ai_gateway") or {}
    ex = {"harness": gw.get("harness", "openai-agents"), "model": model,
          "use_responses": bool(gw.get("use_responses", False))}
    profile = ctx.system.spec["target"].get("profile")
    if profile:
        ex["auth"] = {"type": "databricks", "profile": profile}
    return ex


def _omnigent_bin():
    exe = Path(sys.executable).parent / "omnigent"
    return str(exe) if exe.exists() else shutil.which("omnigent") or "omnigent"


def run_agent(ctx, agent: str, task: str, task_input, schema, model: str, label: str, timeout=900, attempts=2) -> dict:
    template_path = ctx.goal.agent_file(agent)
    spec = dict(yaml.safe_load(template_path.read_text()))
    spec["executor"] = executor_for(ctx, model)
    if isinstance(spec.get("instructions"), str) and spec["instructions"].endswith(".md"):
        spec["instructions"] = (template_path.parent / spec["instructions"]).read_text()
    spec["tools"] = {**(spec.get("tools") or {}), **MAYA_TOOLS}

    work = ctx.run_dir / "agents"
    work.mkdir(parents=True, exist_ok=True)
    agent_yaml = work / f"{label}.yaml"
    agent_yaml.write_text(yaml.safe_dump(spec, sort_keys=False))
    result_path = work / f"{label}.result.json"

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"{label.lower()}-{uuid.uuid4().hex[:12]}"
    task_file(task_id).write_text(json.dumps({"task": task, "input": task_input, "schema": schema,
                                              "result_path": str(result_path)}, default=str))
    t0, log = time.time(), work / f"{label}.log"
    try:
        for attempt in range(1, attempts + 1):
            result_path.unlink(missing_ok=True)
            p = subprocess.run([_omnigent_bin(), "run", str(agent_yaml), "-p", PROMPT.format(task=task, task_id=task_id)],
                               capture_output=True, text=True, timeout=timeout, cwd=str(work))
            with log.open("a") as f:
                f.write(f"=== attempt {attempt} exit {p.returncode}\n{p.stdout}\n--- stderr ---\n{p.stderr}\n")
            if result_path.exists():
                break
    finally:
        task_file(task_id).unlink(missing_ok=True)
    if not result_path.exists():
        raise AgentError(f"agent {agent} ({label}) returned no accepted result after {attempts} attempts; see {log}")
    session = (re.findall(r"\b(conv_[A-Za-z0-9]+)", log.read_text()) or [None])[-1]
    return {"result": json.loads(result_path.read_text()), "label": label, "agent": agent, "model": model,
            "seconds": round(time.time() - t0, 1), "session": session}
