# Databricks notebook source
# Gold: business marts and a customer view on top of Silver.

# COMMAND ----------
dbutils.widgets.text("catalog", "solution_builder")
dbutils.widgets.text("silver", "maya_silver")
dbutils.widgets.text("gold", "maya_gold")
c, s, g = dbutils.widgets.get("catalog"), dbutils.widgets.get("silver"), dbutils.widgets.get("gold")

# COMMAND ----------
spark.sql(f"""CREATE OR REPLACE TABLE {c}.{g}.sales_daily AS
SELECT o.order_date, r.region_name, p.product_name, p.category,
       COUNT(*) AS orders, SUM(o.quantity) AS units, CAST(SUM(o.quantity * o.unit_price) AS DECIMAL(18,2)) AS revenue
FROM {c}.{s}.orders o
JOIN {c}.{s}.product p ON o.product_id = p.product_id
JOIN {c}.{s}.region r ON o.region_code = r.region_code
WHERE o.status <> 'cancelled'
GROUP BY ALL""")

spark.sql(f"""CREATE OR REPLACE TABLE {c}.{g}.shipment_fill_daily AS
SELECT sh.ship_date, r.region_name, p.product_name,
       SUM(sh.units_ordered) AS units_ordered, SUM(sh.units_shipped) AS units_shipped,
       CAST(SUM(sh.units_shipped) / SUM(sh.units_ordered) AS DECIMAL(6,4)) AS fill_rate
FROM {c}.{s}.shipment sh
JOIN {c}.{s}.orders o ON sh.order_id = o.order_id
JOIN {c}.{s}.product p ON o.product_id = p.product_id
JOIN {c}.{s}.region r ON o.region_code = r.region_code
GROUP BY ALL""")

spark.sql(f"""CREATE OR REPLACE VIEW {c}.{g}.customer_360 AS
SELECT cu.customer_id, cu.customer_name, cu.segment, r.region_name,
       COUNT(o.order_id) AS lifetime_orders,
       CAST(COALESCE(SUM(o.quantity * o.unit_price), 0) AS DECIMAL(18,2)) AS lifetime_revenue,
       MAX(o.order_date) AS last_order_date
FROM {c}.{s}.customer cu
JOIN {c}.{s}.region r ON cu.region_code = r.region_code
LEFT JOIN {c}.{s}.orders o ON o.customer_id = cu.customer_id AND o.status <> 'cancelled'
GROUP BY ALL""")

spark.sql(f"""CREATE OR REPLACE TABLE {c}.{g}.returns_daily AS
SELECT rt.return_date, r.region_name, p.product_name, rt.reason,
       COUNT(*) AS returns, SUM(rt.units_returned) AS units_returned,
       CAST(SUM(rt.units_returned * o.unit_price) AS DECIMAL(18,2)) AS returned_value
FROM {c}.{s}.returns rt
JOIN {c}.{s}.orders o ON rt.order_id = o.order_id
JOIN {c}.{s}.product p ON o.product_id = p.product_id
JOIN {c}.{s}.region r ON o.region_code = r.region_code
GROUP BY ALL""")

dbutils.notebook.exit(str({t: spark.table(f"{c}.{g}.{t}").count() for t in ["sales_daily", "shipment_fill_daily", "customer_360", "returns_daily"]}))
