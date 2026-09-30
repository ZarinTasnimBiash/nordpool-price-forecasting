"""Schema base model (reference pattern: schemas are mandatory contracts).

Every warehouse table has a schema class defining exactly which columns exist
and their types. The schema is injected into the ingestion path (DataHandler)
and the Dagster path, so both share one contract — mirroring the reference platform's
`schemas/schemas/base_model.py` convention (their `bigquery_schema()` /
`table_name()` methods become `duckdb_schema()` / `table_name()` here).
"""

from __future__ import annotations

import pandas as pd


class BaseSchema:
    """Base class for all table schemas.

    Subclasses define:
      columns:         ordered {column_name: pandas_dtype} mapping
      _table_name:     warehouse (DuckDB) table name
      timestamp_field: the column used for time-based partitioning/replacement
    """

    columns: dict[str, str] = {}
    _table_name: str = ""
    timestamp_field: str = ""

    # Mapping from pandas dtypes to DuckDB column types
    _DUCKDB_TYPES = {
        "datetime64[ns, UTC]": "TIMESTAMPTZ",
        "float64": "DOUBLE",
        "int64": "BIGINT",
        "object": "VARCHAR",
        "string": "VARCHAR",
        "bool": "BOOLEAN",
    }

    @classmethod
    def table_name(cls) -> str:
        return cls._table_name

    @classmethod
    def validate(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Coerce a dataframe to the schema contract.

        - adds any missing columns as nulls
        - drops unexpected columns (with the schema as single source of truth)
        - orders columns and casts dtypes
        """
        out = df.copy()
        for col, dtype in cls.columns.items():
            if col not in out.columns:
                out[col] = pd.Series([None] * len(out))
            if dtype.startswith("datetime64"):
                out[col] = pd.to_datetime(out[col], utc=True).astype(dtype)
            else:
                out[col] = out[col].astype(dtype)
        return out[list(cls.columns)]

    @classmethod
    def duckdb_schema(cls) -> str:
        """Column definition clause for CREATE TABLE in DuckDB."""
        cols = ", ".join(
            f'"{name}" {cls._DUCKDB_TYPES.get(dtype, "VARCHAR")}'
            for name, dtype in cls.columns.items()
        )
        return cols
