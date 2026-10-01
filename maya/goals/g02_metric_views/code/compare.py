"""Measure versus reference SQL: the metric view is queried with MEASURE(), the user's SQL is run independently, and
the two results are compared overall and per group of the reference's 'by' dimensions."""
from decimal import Decimal, InvalidOperation

from maya.core.workspace import SqlError, ident

from .common import q


def _num(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _close(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale == 0 or abs(a - b) / scale <= Decimal(str(tol))


def _key(row, by):
    return tuple("" if row.get(b) is None else str(row[b]) for b in by)


def check_measure(ws, fn, m, tol) -> dict:
    by = m["reference"].get("by") or []
    cols = ", ".join([ident(b) for b in by] + [f"MEASURE({ident(m['name'])}) AS value"])
    view_sql = f"SELECT {cols} FROM {q(fn)}" + (" GROUP BY ALL" if by else "")
    out = {"metric_view": fn, "measure": m["name"], "by": by, "view_sql": view_sql,
           "reference_sql": m["reference"]["sql"]}
    try:
        got = ws.sql(view_sql)
    except SqlError as e:
        return {**out, "ok": False, "problem": f"metric view query failed: {str(e).splitlines()[0][:240]}"}
    try:
        want = ws.sql(m["reference"]["sql"].strip().rstrip(";"))
    except SqlError as e:
        return {**out, "ok": False, "problem": f"reference SQL failed: {str(e).splitlines()[0][:240]}"}
    g = {_key(r, by): _num(r.get("value")) for r in got}
    w = {_key(r, by): _num(r.get("value")) for r in want}
    mismatches, worst = [], Decimal(0)
    for k in sorted(set(g) | set(w)):
        a, b = g.get(k), w.get(k)
        if k not in g or k not in w:
            mismatches.append({"group": dict(zip(by, k)), "metric_view": None if k not in g else str(a),
                               "reference": None if k not in w else str(b), "problem": "group only on one side"})
        elif not _close(a, b, tol):
            mismatches.append({"group": dict(zip(by, k)), "metric_view": str(a), "reference": str(b)})
        if a is not None and b is not None and max(abs(a), abs(b)) > 0:
            worst = max(worst, abs(a - b) / max(abs(a), abs(b)))
    total = next(iter(g.values())) if not by and g else None
    res = {**out, "ok": not mismatches, "groups": len(w), "max_relative_difference": float(worst),
           "mismatches": mismatches[:10], "mismatch_count": len(mismatches)}
    if total is not None:
        res["value"] = str(total)
    if mismatches:
        res["problem"] = f"{len(mismatches)} of {len(set(g) | set(w))} groups differ from the reference"
    return res
