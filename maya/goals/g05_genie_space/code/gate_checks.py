"""G5 gates. Neither item is editable: the customer's questions and benchmarks files are the source of truth, so a
change is made there and re-planned, never patched at a gate."""
from .common import definition


def space(ctx, data):
    want = definition(ctx)
    out = []
    for k in ("marker", "title", "sources", "instructions", "sample_questions", "trusted_sql", "benchmarks", "access"):
        if data.get(k) != want.get(k):
            out.append(f"{k} differs from the assembled space")
    minimum = ctx.inputs.get("min_questions_per_page", 5)
    out += [f"page {p['id']} has {p['questions']} sample questions; at least {minimum} are needed"
            for p in data["pages"] if p["questions"] < minimum]
    return out


def benchmark_results(ctx, data):
    out = []
    if data["correct"] < data["needed"]:
        out.append(f"{data['correct']} of {data['questions']} benchmark questions answered correctly; {data['needed']} needed")
    if len(data["results"]) != data["questions"]:
        out.append(f"{len(data['results'])} results for {data['questions']} questions")
    return out
