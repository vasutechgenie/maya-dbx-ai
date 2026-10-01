"""Run a goal's declared checks (goal.yaml spec.validation) against the workspace."""
import importlib
import operator
import re

_OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">": operator.gt, "<": operator.lt}
_EXPECT = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(-?[0-9.]+)\s*$")


def compare(observed, expect: str) -> bool:
    m = _EXPECT.match(str(expect))
    if not m:
        raise ValueError(f"expect must look like '>= 100' or '== 0', got {expect!r}")
    if observed is None:
        return False
    return _OPS[m.group(1)](float(observed), float(m.group(2)))


def run_checks(ctx) -> list[dict]:
    module = importlib.import_module(f"{ctx.goal.package}.validator.checks")
    results = []
    for chk in ctx.goal.spec["spec"].get("validation") or []:
        fn = getattr(module, chk["check"])
        try:
            res = fn(ctx, **(chk.get("params") or {}))
            observed, evidence = res.get("observed"), res.get("evidence")
            passed = compare(observed, chk["expect"])
        except Exception as e:  # a broken check is a failed check, with the error as evidence
            observed, evidence, passed = None, {"error": str(e)}, False
        results.append({"id": chk["id"], "checklist": chk.get("checklist"), "severity": chk.get("severity", "mandatory"),
                        "passed": passed, "observed": observed, "expected": chk["expect"], "evidence": evidence})
    return results
