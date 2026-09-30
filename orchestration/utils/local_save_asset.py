"""LocalSaveAsset factory — the local stand-in for a cloud save-asset factory.

For a given endpoint it generates the downstream persistence asset pair:
    save_<endpoint>_to_parquet_<suffix>   (cloud equivalent: save to object storage)
    save_<endpoint>_to_duckdb_<suffix>    (cloud equivalent: load to the warehouse)

Both use replace-per-partition write semantics for daily data, matching the
reference factory behaviour, and carry RetryPolicy(max_retries=3).
"""

from __future__ import annotations

import pandas as pd
from dagster import AssetIn, RetryPolicy, asset

from ingestion.data_handler import DataHandler
from schemas.utils import schema_mapper

RETRY = RetryPolicy(max_retries=3)


def build_save_assets(endpoint: str, api: str, suffix: str, partitions_def,
                      group_name: str):
    """Return [save_to_parquet_asset, save_to_duckdb_asset] for an endpoint."""
    handler = DataHandler(schema_mapper[endpoint], api, endpoint)
    upstream = f"get_{endpoint}_{suffix}"
    parquet_name = f"save_{endpoint}_to_parquet_{suffix}"
    duckdb_name = f"save_{endpoint}_to_duckdb_{suffix}"

    @asset(name=parquet_name, ins={"df": AssetIn(upstream)},
           partitions_def=partitions_def, group_name=group_name,
           retry_policy=RETRY)
    def save_to_parquet(context, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            context.log.warning("empty dataframe — nothing to save")
            return df
        path = handler.save_parquet(df, context.partition_key)
        context.log.info("parquet written: %s (%s rows)", path, len(df))
        return df

    @asset(name=duckdb_name, ins={"df": AssetIn(parquet_name)},
           partitions_def=partitions_def, group_name=group_name,
           retry_policy=RETRY)
    def save_to_duckdb(context, df: pd.DataFrame) -> None:
        n = handler.load_duckdb(df)
        context.log.info("duckdb loaded: %s rows into %s", n,
                         handler.schema.table_name())

    return [save_to_parquet, save_to_duckdb]
