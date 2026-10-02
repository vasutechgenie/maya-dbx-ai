"""MAYA agents runtime (delivered by G10): a supervisor that routes each question to sub-agents with small tool sets.

The whole behaviour (prompts, tool sets, routing) is the approved configuration CONFIG, written into this file when
the model is logged. Tools:
  function   a Unity Catalog table function (G8), called with typed named parameters, run as the agent identity
  lookup     the semantic model's ontology_lookup (G3): what a business term means and where it lives
  genie      a question to the Genie space (G5)
  mcp        a tool of the operations MCP server (G9), called with the agent identity's OAuth token
  delegate   (supervisor only) hand a question to one sub-agent
Data tools act as the agent identity (OAuth machine-to-machine, credentials from MAYA_AGENT_CLIENT_ID / _SECRET);
the language model is called with the serving endpoint's own credentials. Every answer returns the trace of tool
calls (custom_outputs.trace), and each call is an MLflow span when MLflow tracing is on.
Needs only databricks-sdk and requests (and mlflow to serve).
"""
import json
import os
import time
import uuid

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem

CONFIG = None  # MAYA:CONFIG

mlflow = None
if CONFIG is not None:  # served; local runs (MAYA's dry run) neither trace nor need MLflow
    try:
        import mlflow
    except ImportError:
        mlflow = None


def _trace(fn=None, **kw):
    if mlflow is not None:
        return mlflow.trace(fn, **kw) if fn else mlflow.trace(**kw)
    return fn if fn else (lambda f: f)


class Clients:
    def __init__(self, config, data=None, llm=None):
        self.config = config
        self.llm = llm or WorkspaceClient()
        cid, secret = os.environ.get("MAYA_AGENT_CLIENT_ID"), os.environ.get("MAYA_AGENT_CLIENT_SECRET")
        if data is not None:
            self.data = data
        elif cid and secret:
            self.data = WorkspaceClient(host=config["host"], client_id=cid, client_secret=secret, auth_type="oauth-m2m")
        else:
            self.data = self.llm


def _json_type(sql_type):
    t = sql_type.upper()
    if t.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT")):
        return "integer"
    if t.startswith(("DOUBLE", "FLOAT", "DECIMAL")):
        return "number"
    if t.startswith("BOOLEAN"):
        return "boolean"
    return "string"


def _schema(tool) -> dict:
    if tool["type"] == "function":
        props = {p["name"]: {"type": _json_type(p["type"]), "description": p["comment"]
                             + (f" (SQL type {p['type']}" + (", for example 2026-09-01)" if p["type"] == "DATE" else ")"))}
                 for p in tool["parameters"]}
        return {"type": "object", "properties": props,
                "required": [p["name"] for p in tool["parameters"] if "default" not in p]}
    if tool["type"] == "mcp":
        return tool["input_schema"]
    if tool["type"] == "lookup":
        return {"type": "object", "properties": {"search_term": {"type": "string", "description": "A business term"}},
                "required": ["search_term"]}
    return {"type": "object", "properties": {"question": {"type": "string", "description": "The question, in full"}},
            "required": ["question"]}


class Tools:
    def __init__(self, config, clients):
        self.c, self.cl, self._mcp_id = config, clients, 0

    def _sql(self, statement, params=None, types=None):
        types = types or {}
        r = self.cl.data.statement_execution.execute_statement(
            warehouse_id=self.c["warehouse_id"], statement=statement, wait_timeout="50s",
            parameters=[StatementParameterListItem(name=k, value=None if v is None else str(v), type=types.get(k))
                        for k, v in (params or {}).items()] or None)
        while r.status.state.value in ("PENDING", "RUNNING"):
            time.sleep(1)
            r = self.cl.data.statement_execution.get_statement(r.statement_id)
        if r.status.state.value != "SUCCEEDED":
            return {"error": r.status.error.message if r.status.error else r.status.state.value}
        cols = [x.name for x in r.manifest.schema.columns]
        rows = (r.result.data_array if r.result else None) or []
        return {"columns": cols, "rows": rows[:200], "row_count": len(rows)}

    def function(self, tool, args):
        given = [p for p in tool["parameters"] if p["name"] in args]
        named = ", ".join(f"{p['name']} => CAST(:{p['name']} AS {p['type']})" for p in given)
        return self._sql(f"SELECT * FROM {tool['target']}({named})", {p["name"]: args[p["name"]] for p in given})

    def lookup(self, tool, args):
        return self._sql(f"SELECT * FROM {tool['target']}(:search_term)", {"search_term": args.get("search_term", "")})

    def genie(self, tool, args):
        msg = self.cl.data.genie.start_conversation_and_wait(self.c["genie_space_id"], args["question"])
        out = {"question": args["question"], "answer": None, "sql": None, "rows": None}
        for a in msg.attachments or []:
            if a.text and a.text.content:
                out["answer"] = a.text.content
            if a.query:
                out["sql"] = a.query.query
                try:
                    r = self.cl.data.genie.get_message_attachment_query_result(
                        self.c["genie_space_id"], msg.conversation_id, msg.message_id, a.attachment_id)
                    sr = r.statement_response
                    out["columns"] = [x.name for x in sr.manifest.schema.columns] if sr.manifest else None
                    out["rows"] = ((sr.result.data_array if sr.result else None) or [])[:50]
                except Exception as e:
                    out["rows"] = f"could not read the rows: {e}"
        return out

    def mcp(self, tool, args):
        self._mcp_id += 1
        headers = {**self.cl.data.config.authenticate(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        r = requests.post(self.c["mcp_url"].rstrip("/") + "/mcp", headers=headers, timeout=330,
                          json={"jsonrpc": "2.0", "id": self._mcp_id, "method": "tools/call",
                                "params": {"name": tool["target"], "arguments": args}})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            return {"error": body["error"]}
        res = body["result"]
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        return {"text": "".join(c.get("text", "") for c in res.get("content") or []), "is_error": res.get("isError")}

    @_trace(span_type="TOOL")
    def call(self, tool, args):
        return getattr(self, tool["type"])(tool, args)


class Agent:
    def __init__(self, spec, tools, clients, sub_agents=None):
        self.spec, self.tools, self.cl = spec, tools, clients
        self.sub_agents = sub_agents or {}
        self.by_name = {t["name"]: t for t in spec["tools"]}
        self.schema = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": _schema(t)}} for t in spec["tools"]]

    def _chat(self, messages):
        body = {"messages": messages, "tools": self.schema, "max_tokens": 2000, "temperature": 0}
        return self.cl.llm.api_client.do("POST", f"/serving-endpoints/{self.tools.c['llm_endpoint']}/invocations", body=body)

    @_trace(span_type="AGENT")
    def run(self, question, trace):
        messages = [{"role": "system", "content": self.spec["prompt"]}, {"role": "user", "content": question}]
        for _ in range(int(self.tools.c.get("max_turns", 6))):
            msg = self._chat(messages)["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""
            if not isinstance(content, str):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if not calls:
                return content
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                tool = self.by_name.get(name)
                step = {"agent": self.spec["name"], "tool": name, "args": args}
                trace.append(step)
                t0 = time.time()
                try:
                    if not tool:
                        result = {"error": f"unknown tool {name}"}
                    elif tool["type"] == "delegate":
                        result = {"answer": self.sub_agents[tool["target"]].run(args.get("question", question), trace)}
                    else:
                        result = self.tools.call(tool, args)
                except Exception as e:
                    result = {"error": str(e)[:1000]}
                text = json.dumps(result, default=str)
                step.update(seconds=round(time.time() - t0, 1), result=text[:4000])
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text[:12000]})
        return "I stopped: I reached the most tool calls I may make for one question."


class Supervisor:
    def __init__(self, config=None, data=None, llm=None):
        self.config = config or CONFIG
        cl = Clients(self.config, data=data, llm=llm)
        tools = Tools(self.config, cl)
        subs = {s["name"]: Agent(s, tools, cl) for s in self.config["sub_agents"]}
        self.agent = Agent(self.config["supervisor"], tools, cl, subs)

    def ask(self, question):
        trace = []
        answer = self.agent.run(question, trace)
        return answer, trace


if mlflow is not None and CONFIG is not None:
    from mlflow.pyfunc import ResponsesAgent
    from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

    def _text(content):
        if isinstance(content, str):
            return content
        return " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict))

    class MayaAgent(ResponsesAgent):
        def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
            items = [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in request.input]
            question = next((_text(i.get("content")) for i in reversed(items) if i.get("role") == "user"), "")
            answer, trace = Supervisor().ask(question)
            routes = list(dict.fromkeys(s["tool"] for s in trace if s["agent"] == CONFIG["supervisor"]["name"]))
            return ResponsesAgentResponse(output=[self.create_text_output_item(text=answer, id=str(uuid.uuid4()))],
                                          custom_outputs={"trace": trace, "routes": routes,
                                                          "config_version": CONFIG.get("version")})

    mlflow.models.set_model(MayaAgent())
