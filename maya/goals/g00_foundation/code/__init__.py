"""State tables this goal owns (created and upgraded with the project state schema)."""
from .intake import INVENTORY_COLS

STATE_TABLES = {"asset_inventory": INVENTORY_COLS}
