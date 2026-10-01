# Databricks notebook source
# Bronze: land each source file as-is (all strings) plus ingest metadata; skip files already loaded.

# COMMAND ----------
dbutils.widgets.text("catalog", "solution_builder")
dbutils.widgets.text("bronze", "maya_bronze")
catalog, bronze = dbutils.widgets.get("catalog"), dbutils.widgets.get("bronze")
landing = f"/Volumes/{catalog}/{bronze}/landing"
run_id = spark.sql("SELECT uuid()").first()[0]

# COMMAND ----------
from pyspark.sql import functions as F

spark.sql(f"""CREATE TABLE IF NOT EXISTS {catalog}.{bronze}.ingestion_log (
  run_id STRING, source STRING, source_file STRING, rows_loaded BIGINT, loaded_at TIMESTAMP)""")
loaded = {r.source_file for r in spark.table(f"{catalog}.{bronze}.ingestion_log").select("source_file").collect()}

results = []
for source in ["regions", "products", "customers", "orders", "shipments", "returns"]:
    df = (spark.read.option("header", True).option("inferSchema", False).csv(f"{landing}/{source}/")
          .withColumn("_source_file", F.col("_metadata.file_path"))
          .withColumn("_ingest_ts", F.current_timestamp())
          .withColumn("_batch_id", F.lit(run_id)))
    new = df.filter(~F.col("_source_file").isin(list(loaded)) if loaded else F.lit(True))
    count = new.count()
    if count:
        new.write.mode("append").option("mergeSchema", True).saveAsTable(f"{catalog}.{bronze}.raw_{source}")
        for f in [r._source_file for r in new.select("_source_file").distinct().collect()]:
            spark.sql(f"INSERT INTO {catalog}.{bronze}.ingestion_log VALUES ('{run_id}', '{source}', '{f}', {count}, current_timestamp())")
    results.append((source, count))

dbutils.notebook.exit(str(dict(results)))
