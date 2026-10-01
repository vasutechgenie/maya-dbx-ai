"""State tables this goal owns (created and upgraded with the project state schema) and its bundle export."""
from .ledger import COLS

STATE_TABLES = {"metric_view_ledger": COLS}
BUNDLE_EXPORT = "apply.export"
