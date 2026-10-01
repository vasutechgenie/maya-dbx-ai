"""Generic tools every MAYA agent gets. Omnigent runs them in its own runner process (which does not inherit
MAYA's environment), so a run is addressed by an opaque task_id that points at a context file. Agents cannot
reach the workspace: they read their task input and submit one schema-checked result."""
import json
import re
from pathlib import Path

import jsonschema

TASKS_DIR = Path.home() / ".maya" / "agent_tasks"
_TASK_ID = re.compile(r"^[a-z0-9-]{8,80}$")


def task_file(task_id: str) -> Path:
    if not _TASK_ID.match(task_id or ""):
        raise ValueError("invalid task_id")
    return TASKS_DIR / f"{task_id}.json"


def _ctx(task_id) -> dict:
    return json.loads(task_file(task_id).read_text())


def get_task_input(task_id: str) -> str:
    """Return the task input for this agent run as JSON (what to work on, context, and the required result schema)."""
    try:
        c = _ctx(task_id)
    except (ValueError, FileNotFoundError):
        return "ERROR: unknown task_id. Use the task_id given in the prompt."
    return json.dumps({"task": c["task"], "input": c["input"], "result_schema": c.get("schema")}, default=str)


def submit_result(task_id: str, result) -> str:
    """Submit the final result. It is validated against the result schema; fix and resubmit if it is rejected."""
    try:
        c = _ctx(task_id)
    except (ValueError, FileNotFoundError):
        return "ERROR: unknown task_id. Use the task_id given in the prompt."
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return "REJECTED: result must be a JSON object"
    if c.get("schema"):
        try:
            jsonschema.validate(result, c["schema"])
        except jsonschema.ValidationError as e:
            return f"REJECTED: {e.message} at {'/'.join(map(str, e.path)) or 'root'}. Fix it and call submit_result again."
    Path(c["result_path"]).write_text(json.dumps(result, indent=1))
    return "ACCEPTED"
