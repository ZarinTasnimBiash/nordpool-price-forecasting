"""Offline tests for normalizers, schema contracts, and the DataHandler.

Uses synthetic API payloads so tests run without network access
(reference pattern: `pytest orchestration_tests`).
"""

import pandas as pd
import pytest

from ingestion.data_handler import DataHandler
from ingestion.nordpool.nordpool_data import normalize_prices, normalize_volumes
from ingestion.smhi.smhi_data import fetch_archive  # noqa: F401  (import check)
from schemas.utils import schema_mapper

PRICES_PAYLOAD = {
    "deliveryDateCET": "2025-06-10",
    "currency": "EUR",
    "updatedAt": "2025-06-09T11:05:00Z",
    "multiAreaEntries": [
        {
            "deliveryStart": "2025-06-09T22:00:00Z",
            "deliveryEnd": "2025-06-09T23:00:00Z",
            "entryPerArea": {"SE1": 10.5, "SE2": 10.5, "SE3": 27.9, "SE4": 41.02},
        },
        {
            "deliveryStart": "2025-06-09T23:00:00Z",
            "deliveryEnd": "2025-06-10T00:00:00Z",
            "entryPerArea": {"SE1": 9.1, "SE2": 9.1, "SE3": 22.4, "SE4": 35.6},
        },
    ],
}

QUARTER_PAYLOAD = {
    "deliveryDateCET": "2025-11-01",
    "currency": "EUR",
    "multiAreaEntries": [
        {
            "deliveryStart": "2025-10-31T23:00:00Z",
            "deliveryEnd": "2025-10-31T23:15:00Z",
            "entryPerArea": {"SE3": 30.0},
        }
    ],
}


def test_normalize_prices_hourly():
    rows = normalize_prices(PRICES_PAYLOAD)
    assert len(rows) == 8  # 2 periods x 4 areas
    r = rows[0]
    assert r["delivery_area"] == "SE1"
    assert r["price_eur_mwh"] == 10.5
    assert r["resolution_minutes"] == 60


def test_normalize_prices_quarter_hour():
    rows = normalize_prices(QUARTER_PAYLOAD)
    assert rows[0]["resolution_minutes"] == 15


def test_normalize_volumes_handles_dict_and_number():
    payload = {
        "deliveryDateCET": "2025-06-10",
        "multiAreaEntries": [
            {"deliveryStart": "2025-06-09T22:00:00Z",
             "deliveryEnd": "2025-06-09T23:00:00Z",
             "entryPerArea": {"SE3": {"buy": 100.0, "sell": 95.0}}},
            {"deliveryStart": "2025-06-09T23:00:00Z",
             "deliveryEnd": "2025-06-10T00:00:00Z",
             "entryPerArea": {"SE3": 123.4}},
        ],
    }
    rows = normalize_volumes(payload)
    assert rows[0]["volume_buy_mwh"] == 100.0
    assert rows[0]["volume_sell_mwh"] == 95.0
    assert rows[1]["volume_buy_mwh"] == 123.4


def test_schema_validate_orders_and_casts():
    schema = schema_mapper["day_ahead_prices"]
    rows = normalize_prices(PRICES_PAYLOAD)
    df = schema.validate(pd.DataFrame(rows))
    assert list(df.columns) == list(schema.columns)
    assert str(df["delivery_start_utc"].dtype) == "datetime64[ns, UTC]"
    assert str(df["price_eur_mwh"].dtype) == "float64"


def test_data_handler_roundtrip(tmp_path, monkeypatch):
    """persist() writes parquet + loads DuckDB, and re-running replaces rows."""
    import config.configs as cfg
    monkeypatch.setitem(cfg.STORAGE, "raw_dir", tmp_path / "raw")
    monkeypatch.setitem(cfg.STORAGE, "duckdb_path", tmp_path / "wh.duckdb")

    schema = schema_mapper["day_ahead_prices"]
    handler = DataHandler(schema, "nordpool", "day_ahead_prices")
    rows = normalize_prices(PRICES_PAYLOAD)
    for r in rows:
        r.setdefault("updated_at_utc", "2025-06-09T11:05:00Z")

    df1 = handler.persist(rows, "2025-06-10")
    df2 = handler.persist(rows, "2025-06-10")  # idempotent re-run
    assert len(df1) == len(df2) == 8

    import duckdb
    con = duckdb.connect(str(cfg.STORAGE["duckdb_path"]))
    n = con.execute("SELECT COUNT(*) FROM nordpool_day_ahead_prices").fetchone()[0]
    con.close()
    assert n == 8  # replaced, not duplicated


def test_smhi_archive_csv_parsing(monkeypatch):
    """Parse a synthetic corrected-archive CSV without network."""
    from ingestion.smhi import smhi_data

    csv_text = (
        "Stationsnamn;Klimatnummer;...\n"
        "Some;metadata;lines\n"
        "Datum;Tid (UTC);Lufttemperatur;Kvalitet;;Tidsutsnitt:\n"
        "2024-01-01;00:00:00;-5.2;G;;\n"
        "2024-01-01;01:00:00;-5.6;G;;\n"
        "2020-01-01;00:00:00;-1.0;G;;\n"  # before `since` -> filtered out
    )

    class FakeResp:
        text = csv_text

    monkeypatch.setattr(smhi_data, "_get", lambda url, max_retries=3: FakeResp())
    rows = smhi_data.fetch_archive("air_temperature", 1, "SE3", 98230,
                                  since="2023-09-01")
    assert len(rows) == 2
    assert rows[0]["value"] == -5.2
    assert rows[0]["quality"] == "G"


ELPRISET_PAYLOAD = [
    {"SEK_per_kWh": 0.50531, "EUR_per_kWh": 0.04493, "EXR": 11.246702,
     "time_start": "2024-02-14T00:00:00+01:00",
     "time_end": "2024-02-14T01:00:00+01:00"},
    {"SEK_per_kWh": 0.48445, "EUR_per_kWh": 0.04309, "EXR": 11.246702,
     "time_start": "2024-02-14T01:00:00+01:00",
     "time_end": "2024-02-14T02:00:00+01:00"},
]


def test_elpriset_normalize_converts_units_and_timezone():
    from ingestion.elpriset.elpriset_data import normalize

    rows = normalize(ELPRISET_PAYLOAD, "SE3")
    assert len(rows) == 2
    r = rows[0]
    # kWh -> MWh
    assert r["price_eur_mwh"] == pytest.approx(44.93)
    # local +01:00 -> UTC
    assert str(r["delivery_start_utc"]) == "2024-02-13 23:00:00+00:00"
    # the CET calendar date, not the UTC one
    assert r["delivery_date_cet"] == "2024-02-14"
    assert r["resolution_minutes"] == 60
    assert r["delivery_area"] == "SE3"


def test_elpriset_schema_contract():
    from ingestion.elpriset.elpriset_data import normalize

    schema = schema_mapper["elpriset_day_ahead_prices"]
    df = schema.validate(pd.DataFrame(normalize(ELPRISET_PAYLOAD, "SE3")))
    assert list(df.columns) == list(schema.columns)
    assert str(df["delivery_start_utc"].dtype) == "datetime64[ns, UTC]"


def test_dagster_definitions_load():
    """The global registry must import and validate (reference: definitions.py)."""
    from orchestration.definitions import defs
    names = {a.key.to_user_string() for a in defs.assets}
    assert "get_day_ahead_prices_daily" in names
    assert "save_day_ahead_prices_to_duckdb_daily" in names
    assert "get_weather_observations_daily" in names
    assert len(defs.jobs) >= 2


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
