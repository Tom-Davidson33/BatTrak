import pandas as pd
from db import query


def get_region_prices(start, end):
    """Actual 5-min dispatch prices (RRP) per region between start and end."""
    sql = """
        SELECT SETTLEMENTDATE, REGIONID, RRP
        FROM TESTER.DISPATCHPRICE
        WHERE SETTLEMENTDATE BETWEEN :start_dt AND :end_dt
          AND INTERVENTION = 0
        ORDER BY REGIONID, SETTLEMENTDATE
    """
    try:
        df = query(sql, {"start_dt": pd.Timestamp(start).to_pydatetime(),
                         "end_dt": pd.Timestamp(end).to_pydatetime()})
    except Exception as exc:  # table missing / no access: chart degrades gracefully
        print(f"get_region_prices failed: {exc}")
        return pd.DataFrame(columns=["SETTLEMENTDATE", "REGIONID", "RRP"])
    df.columns = [c.upper() for c in df.columns]
    return df


def get_p5min_forecast(as_at):
    """Latest 5-minute predispatch (P5MIN) price forecast run at/before as_at.

    Returns forward intervals only (INTERVAL_DATETIME > run time).
    """
    sql = """
        SELECT INTERVAL_DATETIME, REGIONID, RRP, RUN_DATETIME
        FROM TESTER.P5MIN_REGIONSOLUTION
        WHERE RUN_DATETIME = (
            SELECT MAX(RUN_DATETIME)
            FROM TESTER.P5MIN_REGIONSOLUTION
            WHERE RUN_DATETIME <= :as_at
        )
          AND INTERVAL_DATETIME > RUN_DATETIME
        ORDER BY REGIONID, INTERVAL_DATETIME
    """
    try:
        df = query(sql, {"as_at": pd.Timestamp(as_at).to_pydatetime()})
    except Exception as exc:
        print(f"get_p5min_forecast failed: {exc}")
        return pd.DataFrame(columns=["INTERVAL_DATETIME", "REGIONID", "RRP",
                                     "RUN_DATETIME"])
    df.columns = [c.upper() for c in df.columns]
    # Some schemas carry an INTERVENTION flag; dedupe defensively either way
    df = (df.sort_values(["REGIONID", "INTERVAL_DATETIME"])
            .drop_duplicates(["REGIONID", "INTERVAL_DATETIME"], keep="last"))
    return df
