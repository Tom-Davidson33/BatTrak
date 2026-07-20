import os
import re
import pandas as pd
from dotenv import load_dotenv
from db import query

load_dotenv()

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "168"))   # default integration window
DISPLAY_HOURS = int(os.getenv("DISPLAY_HOURS", "48"))      # max chart window
RTE = float(os.getenv("RTE", "0.88"))
DEGRADE_PER_YEAR = float(os.getenv("DEGRADE_PER_YEAR", "0.025"))
DEGRADE_FLOOR = float(os.getenv("DEGRADE_FLOOR", "0.70"))
INTERVAL_HRS = 5.0 / 60.0  # 5-minute SCADA intervals

# NOTE: AEMO does not publish state of charge. SoC is integrated from
# SCADAVALUE over the chosen window ending at 'as_at'; the lowest point is
# anchored to empty (assumes each battery hits ~0 at least once in the window).
# Capacity is de-rated for age (calendar+cycle fade). Estimate only.


def base_duid(duid):
    """Strip trailing G/L unit suffix so split battery DUIDs net together."""
    return re.sub(r"(G|L)(\d*)$", "", duid)


def get_batteries():
    sql = """
        SELECT d.DUID,
               d.MAXSTORAGECAPACITY,
               d.REGISTEREDCAPACITY,
               s.REGIONID,
               s.FIRST_START
        FROM TESTER.DUDETAIL d
        JOIN (
            SELECT DUID, MAX(LASTCHANGED) AS LC
            FROM TESTER.DUDETAIL
            WHERE MAXSTORAGECAPACITY > 0
            GROUP BY DUID
        ) latest ON d.DUID = latest.DUID AND d.LASTCHANGED = latest.LC
        JOIN (
            SELECT DUID, REGIONID,
                   MAX(END_DATE) AS ED,
                   MIN(START_DATE) AS FIRST_START
            FROM TESTER.DUDETAILSUMMARY
            GROUP BY DUID, REGIONID
        ) s ON d.DUID = s.DUID
        WHERE d.MAXSTORAGECAPACITY > 0
    """
    df = query(sql)
    df.columns = [c.upper() for c in df.columns]
    df = df.sort_values("DUID").drop_duplicates("DUID", keep="last")
    df["BASE"] = df["DUID"].apply(base_duid)
    return df


def get_scada(duids, as_at=None, lookback_hours=None):
    if as_at is None:
        as_at = pd.Timestamp.now()
    if lookback_hours is None:
        lookback_hours = LOOKBACK_HOURS
    start = as_at - pd.Timedelta(hours=lookback_hours)
    binds = {f"d{i}": d for i, d in enumerate(duids)}
    placeholders = ",".join(f":{k}" for k in binds)
    binds["start_dt"] = start.to_pydatetime()
    binds["end_dt"] = as_at.to_pydatetime()
    sql = f"""
        SELECT SETTLEMENTDATE, DUID, SCADAVALUE
        FROM TESTER.DISPATCH_UNIT_SCADA
        WHERE DUID IN ({placeholders})
          AND SETTLEMENTDATE BETWEEN :start_dt AND :end_dt
        ORDER BY DUID, SETTLEMENTDATE
    """
    df = query(sql, binds)
    df.columns = [c.upper() for c in df.columns]
    return df


def estimate_soc(as_at=None, lookback_hours=None):
    ref = pd.Timestamp.now() if as_at is None else pd.Timestamp(as_at)
    if lookback_hours is None:
        lookback_hours = LOOKBACK_HOURS

    bats = get_batteries()
    if bats.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Age-based capacity de-rating (age measured to the 'as at' reference)
    age = (ref - pd.to_datetime(bats["FIRST_START"], errors="coerce")).dt.days / 365.25
    bats["AGE_YRS"] = age.fillna(0).clip(lower=0)
    bats["DEGRADE"] = (1 - DEGRADE_PER_YEAR * bats["AGE_YRS"]).clip(lower=DEGRADE_FLOOR)
    bats["EFF_CAP"] = bats["MAXSTORAGECAPACITY"] * bats["DEGRADE"]

    scada = get_scada(bats["DUID"].tolist(), as_at=ref, lookback_hours=lookback_hours)
    if scada.empty:
        return pd.DataFrame(), pd.DataFrame()

    base_map = bats.set_index("DUID")["BASE"].to_dict()
    grp = bats.groupby("BASE").agg(
        CAPACITY_MWH=("EFF_CAP", "sum"),
        POWER_MW=("REGISTEREDCAPACITY", "sum"),
        REGIONID=("REGIONID", "first"),
    ).to_dict("index")

    scada["BASE"] = scada["DUID"].map(base_map)
    netted = scada.groupby(["BASE", "SETTLEMENTDATE"], as_index=False)["SCADAVALUE"].sum()

    rows = []
    for base, g in netted.groupby("BASE"):
        meta = grp.get(base)
        if not meta or meta["CAPACITY_MWH"] <= 0:
            continue
        cap = meta["CAPACITY_MWH"]
        g = g.sort_values("SETTLEMENTDATE").copy()
        mw = g["SCADAVALUE"].fillna(0.0).values
        deltas = [(-v * INTERVAL_HRS) if v >= 0 else (-v * INTERVAL_HRS * RTE) for v in mw]
        cum = pd.Series(deltas).cumsum()
        soc = (cum - cum.min()).clip(0, cap)  # anchor trough to empty, clamp to cap
        g["SOC_MWH"] = soc.values
        g["CAPACITY_MWH"] = cap
        g["POWER_MW"] = meta["POWER_MW"]
        g["REGIONID"] = meta["REGIONID"]
        g["SOC_PCT"] = 100.0 * g["SOC_MWH"] / cap
        g["BATTERY"] = base
        rows.append(g)

    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    detail = pd.concat(rows, ignore_index=True)

    # Summary from the latest interval in the window
    latest = detail.sort_values("SETTLEMENTDATE").groupby("BATTERY").tail(1)
    summary = (
        latest.groupby("REGIONID")
        .agg(STORED_MWH=("SOC_MWH", "sum"),
             CAPACITY_MWH=("CAPACITY_MWH", "sum"),
             POWER_MW=("POWER_MW", "sum"),
             N_UNITS=("BATTERY", "nunique"))
        .reset_index()
    )
    summary["SOC_PCT"] = 100.0 * summary["STORED_MWH"] / summary["CAPACITY_MWH"]
    summary["CHARGE_MWH"] = summary["CAPACITY_MWH"] - summary["STORED_MWH"]
    summary["DISCHARGE_HRS"] = summary["STORED_MWH"] / summary["POWER_MW"]
    summary["CHARGE_HRS"] = summary["CHARGE_MWH"] / summary["POWER_MW"]

    # Trim time-series to the display window (no longer than the lookback itself)
    disp_hours = min(DISPLAY_HOURS, lookback_hours)
    cutoff = detail["SETTLEMENTDATE"].max() - pd.Timedelta(hours=disp_hours)
    disp = detail[detail["SETTLEMENTDATE"] >= cutoff].copy()

    return disp, summary