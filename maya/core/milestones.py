"""Milestones: AI Enabled, then AI Ready. A milestone is reached when every goal it requires is certified, including
goals of the milestones it builds on and goals not released yet (which therefore keep it 'not yet')."""

MILESTONES = {
    "AI Enabled": {"requires": [], "goals": ["G0", "G1", "G2", "G3", "G4"]},
    "AI Ready": {"requires": ["AI Enabled"], "goals": ["G5", "G6", "G7", "G8", "G9", "G10", "G11", "G12"]},
}


def required_goals(name) -> list[str]:
    """Every goal the milestone needs, its own and those of the milestones it requires, in order."""
    m = MILESTONES[name]
    out = [g for r in m["requires"] for g in required_goals(r)]
    return out + [g for g in m["goals"] if g not in out]


def progress(name, statuses: dict) -> dict:
    """statuses: goal id -> status of every goal this MAYA has. Returns reached, counts and the unreleased goals."""
    need = required_goals(name)
    released = [g for g in need if g in statuses]
    certified = [g for g in released if statuses[g] == "certified"]
    return {"reached": len(certified) == len(need), "certified": len(certified), "required": len(need),
            "own_goals": MILESTONES[name]["goals"], "unreleased": [g for g in need if g not in statuses]}


def visible(statuses: dict) -> list[str]:
    """Milestones to report: those with at least one released goal of their own."""
    return [n for n, m in MILESTONES.items() if any(g in statuses for g in m["goals"])]
