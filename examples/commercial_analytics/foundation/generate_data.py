"""Generate the synthetic source files for the example foundation (deterministic, stdlib only)."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "data"
rng = random.Random(42)

REGIONS = [("NA-E", "North America East", "US"), ("NA-W", "North America West", "US"), ("EU-N", "Europe North", "SE"),
           ("EU-S", "Europe South", "IT"), ("APAC", "Asia Pacific", "SG"), ("LATAM", "Latin America", "BR")]
CATEGORIES = {"Analytics": 1200, "Storage": 300, "Compute": 800, "Networking": 450, "Security": 950}
SEGMENTS = ["Enterprise", "Mid-Market", "SMB", "Public Sector"]
FIRST = ["Avery", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Quinn", "Harper", "Rowan", "Sage", "Emerson", "Finley"]
LAST = ["Stone", "Rivera", "Patel", "Kim", "Nguyen", "Okafor", "Silva", "Novak", "Haddad", "Larsen", "Moreau", "Tanaka"]
CARRIERS = ["Northwind Freight", "BlueLine Logistics", "Contoso Express", "Fabrikam Cargo"]


def write(name, header, rows):
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{name}_2026_09.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def main():
    n = {}
    n["regions"] = write("regions", ["region_code", "region_name", "country"], REGIONS)

    products = []
    pid = 100
    for cat, base in CATEGORIES.items():
        for tier in ["Basic", "Standard", "Pro", "Enterprise", "Ultra", "Edge"]:
            pid += 1
            products.append((pid, f"{cat} {tier}", cat, round(base * (1 + 0.6 * len(products) % 7 / 7), 2)))
    n["products"] = write("products", ["product_id", "product_name", "category", "list_price"], products)

    customers = []
    for cid in range(1, 201):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        customers.append((cid, f"{fn} {ln} Co", f"{fn.lower()}.{ln.lower()}{cid}@example.com",
                          f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}", rng.choice(SEGMENTS),
                          rng.choice(REGIONS)[0], (date(2024, 1, 1) + timedelta(days=rng.randint(0, 600))).isoformat()))
    customers.append(customers[17])  # one exact duplicate: Silver must deduplicate
    n["customers"] = write("customers", ["customer_id", "customer_name", "email", "phone", "segment", "region_code", "created_date"], customers)

    orders, shipments = [], []
    sid = 50000
    for oid in range(10001, 13001):
        c = rng.choice(customers)
        p = rng.choice(products)
        qty = rng.randint(1, 40)
        od = date(2026, 6, 1) + timedelta(days=rng.randint(0, 120))
        status = rng.choices(["shipped", "open", "cancelled"], [0.9, 0.07, 0.03])[0]
        orders.append((oid, c[0], p[0], od.isoformat(), qty, round(p[3] * rng.uniform(0.85, 1.0), 2), c[5], status))
        if status == "shipped":
            sid += 1
            shipped = qty if rng.random() > 0.18 else max(0, qty - rng.randint(1, qty))
            shipments.append((sid, oid, (od + timedelta(days=rng.randint(1, 6))).isoformat(), qty, shipped, rng.choice(CARRIERS)))
    n["orders"] = write("orders", ["order_id", "customer_id", "product_id", "order_date", "quantity", "unit_price", "region_code", "status"], orders)
    n["shipments"] = write("shipments", ["shipment_id", "order_id", "ship_date", "units_ordered", "units_shipped", "carrier"], shipments)

    rr = random.Random(7)  # separate stream: adding returns leaves the earlier sources unchanged
    returns = []
    for i, s in enumerate(rr.sample(shipments, 240)):
        units = rr.randint(1, max(1, s[4]))
        returns.append((80001 + i, s[1], (date.fromisoformat(s[2]) + timedelta(days=rr.randint(3, 30))).isoformat(),
                        units, rr.choice(["damaged", "wrong_item", "not_needed", "late_delivery"]),
                        rr.choices(["refunded", "replaced", "rejected"], [0.7, 0.2, 0.1])[0]))
    n["returns"] = write("returns", ["return_id", "order_id", "return_date", "units_returned", "reason", "resolution"], returns)
    print(n)


if __name__ == "__main__":
    main()
