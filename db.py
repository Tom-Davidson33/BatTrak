import os
import oracledb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


def get_me_market():
    return oracledb.connect(
        user=os.getenv("ME_MARKET_USER", "elecreader"),
        password=os.getenv("ME_MARKET_PWD", "reader4elec"),
        dsn=os.getenv("ME_MARKET_DSN", "MEL_MARKET.WORLD"),
    )


def query(sql, params=None):
    """Run a query via a cursor and return a DataFrame.

    Uses oracledb's cursor directly (not pd.read_sql) to avoid the
    pandas SQLAlchemy-only warning for raw DBAPI2 connections.
    """
    conn = get_me_market()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or {})
        cols = [d[0].upper() for d in cur.description]
        data = cur.fetchall()
        return pd.DataFrame(data, columns=cols)
    finally:
        conn.close()