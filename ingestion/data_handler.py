"""DataHandler: normalize -> validate against schema -> persist (reference pattern).

The reference pattern persists to cloud object storage (parquet) + a cloud
warehouse. This one persists to
data/raw/ (parquet) + DuckDB. Write semantics mirror the reference save-asset
factory: *replace* per daily partition, so re-running a day is idempotent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import duckdb
import pandas as pd

from config.configs import STORAGE

logger = logging.getLogger(__name__)


class DataHandler:
    def __init__(self, schema, api: str, endpoint: str):
        self.schema = schema
        self.api = api
        self.endpoint = endpoint

    # ------------------------------------------------------------------ helpers
    def to_dataframe(self, records: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame.from_records(records)
        return self.schema.validate(df)

    def parquet_path(self, partition_label: str):
        d = STORAGE["raw_dir"] / self.api / self.endpoint
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{partition_label}.parquet"

    # ------------------------------------------------------------------- writes
    def save_parquet(self, df: pd.DataFrame, partition_label: str) -> str:
        """Write one partition's data as parquet (replace mode)."""
        path = self.parquet_path(partition_label)
        df.to_parquet(path, index=False)
        logger.info("wrote %s rows -> %s", len(df), path)
        return str(path)

    def load_duckdb(self, df: pd.DataFrame) -> int:
        """Load a validated dataframe into the DuckDB warehouse table.

        Replace semantics: delete existing rows for the covered dates
        (and, for weather, matching area/parameter) before inserting.
        """
        if df.empty:
            return 0
        table = self.schema.table_name()
        ts = self.schema.timestamp_field
        con = duckdb.connect(str(STORAGE["duckdb_path"]))
        try:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({self.schema.duckdb_schema()})"
            )
            con.register("incoming", df)
            tmin = df[ts].min()
            tmax = df[ts].max()
            con.execute(
                f'DELETE FROM {table} WHERE "{ts}" >= ? AND "{ts}" <= ?',
                [tmin.to_pydatetime(), tmax.to_pydatetime()],
            )
            con.execute(f"INSERT INTO {table} SELECT * FROM incoming")
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            logger.info("duckdb table %s now has %s rows", table, n)
            return len(df)
        finally:
            con.close()

    def persist(self, records: list[dict], partition_label: str) -> pd.DataFrame:
        """Full path: records -> schema-validated df -> parquet + DuckDB."""
        df = self.to_dataframe(records)
        if df.empty:
            logger.warning("no records for %s/%s %s", self.api, self.endpoint,
                           partition_label)
            return df
        self.save_parquet(df, partition_label)
        self.load_duckdb(df)
        return df


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
