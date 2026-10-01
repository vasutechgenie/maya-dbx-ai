# Databricks notebook source
# Silver: typed, deduplicated entities (latest _ingest_ts wins), upserted with MERGE.

# COMMAND ----------
dbutils.widgets.text("catalog", "solution_builder")
dbutils.widgets.text("bronze", "maya_bronze")
dbutils.widgets.text("silver", "maya_silver")
c, b, s = dbutils.widgets.get("catalog"), dbutils.widgets.get("bronze"), dbutils.widgets.get("silver")

ENTITIES = {
    "region": ("raw_regions", "region_code", """region_code STRING, region_name STRING, country STRING""",
               "region_code, region_name, country"),
    "product": ("raw_products", "product_id", """product_id BIGINT, product_name STRING, category STRING, list_price DECIMAL(10,2)""",
                "CAST(product_id AS BIGINT), product_name, category, CAST(list_price AS DECIMAL(10,2))"),
    "customer": ("raw_customers", "customer_id",
                 """customer_id BIGINT, customer_name STRING, email STRING, phone STRING, segment STRING, region_code STRING, created_date DATE""",
                 "CAST(customer_id AS BIGINT), customer_name, lower(email), phone, segment, region_code, CAST(created_date AS DATE)"),
    "orders": ("raw_orders", "order_id",
               """order_id BIGINT, customer_id BIGINT, product_id BIGINT, order_date DATE, quantity INT, unit_price DECIMAL(10,2), region_code STRING, status STRING""",
               "CAST(order_id AS BIGINT), CAST(customer_id AS BIGINT), CAST(product_id AS BIGINT), CAST(order_date AS DATE), CAST(quantity AS INT), CAST(unit_price AS DECIMAL(10,2)), region_code, status"),
    "shipment": ("raw_shipments", "shipment_id",
                 """shipment_id BIGINT, order_id BIGINT, ship_date DATE, units_ordered INT, units_shipped INT, carrier STRING""",
                 "CAST(shipment_id AS BIGINT), CAST(order_id AS BIGINT), CAST(ship_date AS DATE), CAST(units_ordered AS INT), CAST(units_shipped AS INT), carrier"),
    "returns": ("raw_returns", "return_id",
                """return_id BIGINT, order_id BIGINT, return_date DATE, units_returned INT, reason STRING, resolution STRING""",
                "CAST(return_id AS BIGINT), CAST(order_id AS BIGINT), CAST(return_date AS DATE), CAST(units_returned AS INT), reason, resolution"),
}

# COMMAND ----------
import re

out = {}
for entity, (raw, key, ddl, select) in ENTITIES.items():
    cols = [x.strip().split()[0] for x in re.split(r",(?![^(]*\))", ddl)]
    exprs = [x.strip() for x in re.split(r",(?![^(]*\))", select)]
    aliased = ", ".join(f"{e} AS {col}" for e, col in zip(exprs, cols))
    spark.sql(f"CREATE TABLE IF NOT EXISTS {c}.{s}.{entity} ({ddl}, _updated_at TIMESTAMP)")
    spark.sql(f"""CREATE OR REPLACE TEMP VIEW src AS
        SELECT * FROM (SELECT {aliased}, ROW_NUMBER() OVER (PARTITION BY {key} ORDER BY _ingest_ts DESC) AS _rn
                       FROM {c}.{b}.{raw}) WHERE _rn = 1""")
    sets = ", ".join(f"t.{x} = s.{x}" for x in cols if x != key)
    spark.sql(f"""MERGE INTO {c}.{s}.{entity} t USING src s ON t.{key} = s.{key}
        WHEN MATCHED THEN UPDATE SET {sets}, t._updated_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT ({", ".join(cols)}, _updated_at) VALUES ({", ".join("s." + x for x in cols)}, current_timestamp())""")
    out[entity] = spark.table(f"{c}.{s}.{entity}").count()

dbutils.notebook.exit(str(out))
