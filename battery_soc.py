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
NP_SEED_HOURS = int(os.getenv("NP_SEED_HOURS", "336"))     # nempulse-method seed window
INTERVAL_HRS = 5.0 / 60.0  # 5-minute SCADA intervals

# NOTE: AEMO does not publish state of charge. Both methods integrate net
# SCADAVALUE energy with charge efficiency RTE; they differ in how the
# integral is anchored to an absolute level:
#
#   "anchor"   — legacy: cumulative sum over the lookback window, shifted so
#                the window minimum sits at empty, then clipped to [0, cap].
#                One bad anchor point (a battery that never actually empties,
#                or RTE drift over a long window) skews the whole trace.
#
#   "nempulse" — per nempulse.com.au/methodology + /glossary/soc: AEMO
#                publishes each unit's reported energy storage in
#                DISPATCHLOAD.ENERGYSTORAGE (next-day public). Where a
#                reported value exists it is used directly; beyond the last
#                reported interval (i.e. today) SOC is stepped forward from
#                that anchor by a running clamped integration of SCADA,
#                saturated at 0 and usable capacity so genuine full/empty
#                events re-anchor the estimate. If no reported data is
#                available at all, falls back to the pure clamped integration
#                seeded at 50% over NP_SEED_HOURS.
#
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


def get_reported_storage(duids, start, end):
    """AEMO-reported unit energy storage (MWh) from next-day-public
    DISPATCHLOAD. Empty frame if the column/table is unavailable."""
    binds = {f"d{i}": d for i, d in enumerate(duids)}
    placeholders = ",".join(f":{k}" for k in binds)
    binds["start_dt"] = pd.Timestamp(start).to_pydatetime()
    binds["end_dt"] = pd.Timestamp(end).to_pydatetime()
    sql = f"""
        SELECT SETTLEMENTDATE, DUID, ENERGYSTORAGE
        FROM TESTER.DISPATCHLOAD
        WHERE DUID IN ({placeholders})
          AND SETTLEMENTDATE BETWEEN :start_dt AND :end_dt
          AND INTERVENTION = 0
          AND ENERGYSTORAGE IS NOT NULL
        ORDER BY DUID, SETTLEMENTDATE
    """
    try:
        df = query(sql, binds)
    except Exception as exc:
        print(f"get_reported_storage failed (falling back to integration): {exc}")
        return pd.DataFrame(columns=["SETTLEMENTDATE", "DUID", "ENERGYSTORAGE"])
    df.columns = [c.upper() for c in df.columns]
    return df


def _soc_min_anchor(mw, cap):
    """Legacy: shift so the window minimum is empty, then clip."""
    deltas = [(-v * INTERVAL_HRS) if v >= 0 else (-v * INTERVAL_HRS * RTE) for v in mw]
    cum = pd.Series(deltas).cumsum()
    return (cum - cum.min()).clip(0, cap).values


def _soc_clamped(mw, cap):
    """NEMpulse-style running integration, saturated at [0, cap] each step.

    Starts at 50% of usable capacity; self-corrects to the true level the
    first time the unit hits full or empty, and re-anchors at every
    subsequent saturation, so drift cannot accumulate.
    """
    level = 0.5 * cap
    out = []
    for v in mw:
        d = (-v * INTERVAL_HRS) if v >= 0 else (-v * INTERVAL_HRS * RTE)
        level += d
        if level < 0.0:
            level = 0.0
        elif level > cap:
            level = cap
        out.append(level)
    return out


def _soc_hybrid(mw, cap, reported):
    """AEMO-reported storage where published, clamped integration beyond it.

    `reported` aligns with `mw` (NaN where no published value). A reported
    value overrides the integration and re-anchors it; intervals after the
    last reported point (typically today, before next-day publication) are
    stepped forward from that anchor.
    """
    level = None
    out = []
    for v, rep in zip(mw, reported):
        if rep == rep:  # not NaN
            level = min(max(float(rep), 0.0), cap)
        else:
            if level is None:
                level = 0.5 * cap
            d = (-v * INTERVAL_HRS) if v >= 0 else (-v * INTERVAL_HRS * RTE)
            level += d
            if level < 0.0:
                level = 0.0
            elif level > cap:
                level = cap
        out.append(level)
    return out


def estimate_soc(as_at=None, lookback_hours=None, method="anchor"):
    ref = pd.Timestamp.now() if as_at is None else pd.Timestamp(as_at)
    if lookback_hours is None:
        lookback_hours = NP_SEED_HOURS if method == "nempulse" else LOOKBACK_HOURS

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

    # AEMO-reported storage (next-day public); split G/L units report the same
    # store, so net by max rather than sum
    rep_map = {}
    if method == "nempulse":
        start = ref - pd.Timedelta(hours=lookback_hours)
        reported = get_reported_storage(bats["DUID"].tolist(), start, ref)
        if not reported.empty:
            reported["BASE"] = reported["DUID"].map(base_map)
            rn = (reported.groupby(["BASE", "SETTLEMENTDATE"])["ENERGYSTORAGE"]
                          .max())
            rep_map = {b: s.droplevel(0) for b, s in rn.groupby(level=0)}

    rows = []
    for base, g in netted.groupby("BASE"):
        meta = grp.get(base)
        if not meta or meta["CAPACITY_MWH"] <= 0:
            continue
        cap = meta["CAPACITY_MWH"]
        g = g.sort_values("SETTLEMENTDATE").copy()
        mw = g["SCADAVALUE"].fillna(0.0).values
        if method == "nempulse":
            rep = rep_map.get(base)
            if rep is not None and len(rep):
                # reported storage is ground truth: if it exceeds the derated
                # capacity, the derate was too aggressive — lift the cap
                cap = max(cap, float(rep.max()))
                aligned = g["SETTLEMENTDATE"].map(rep).values
                soc = _soc_hybrid(mw, cap, aligned)
            else:
                soc = _soc_clamped(mw, cap)
        else:
            soc = _soc_min_anchor(mw, cap)
        g["SOC_MWH"] = soc
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


def charge_discharge_prices(detail, prices):
    """Volume-weighted average charge / discharge price over the window.

    Returns (per_battery, per_state) frames with energy volumes, VWAPs and
    the realised spread. Empty frames if either input is empty.
    """
    cols = ["BATTERY", "REGIONID", "CHG_MWH", "DIS_MWH",
            "AVG_CHG", "AVG_DIS", "SPREAD"]
    if detail.empty or prices.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols[1:])

    m = detail.merge(prices, on=["REGIONID", "SETTLEMENTDATE"], how="inner")
    if m.empty:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols[1:])
    mw = m["SCADAVALUE"].fillna(0.0)
    m["CHG_MWH"] = (-mw).clip(lower=0) * INTERVAL_HRS   # energy drawn from grid
    m["DIS_MWH"] = mw.clip(lower=0) * INTERVAL_HRS      # energy delivered
    m["CHG_COST"] = m["CHG_MWH"] * m["RRP"]
    m["DIS_REV"] = m["DIS_MWH"] * m["RRP"]

    def _vwap(keys):
        g = m.groupby(keys).agg(
            CHG_MWH=("CHG_MWH", "sum"), DIS_MWH=("DIS_MWH", "sum"),
            CHG_COST=("CHG_COST", "sum"), DIS_REV=("DIS_REV", "sum"),
        ).reset_index()
        g["AVG_CHG"] = (g["CHG_COST"] / g["CHG_MWH"]).where(g["CHG_MWH"] > 0)
        g["AVG_DIS"] = (g["DIS_REV"] / g["DIS_MWH"]).where(g["DIS_MWH"] > 0)
        g["SPREAD"] = g["AVG_DIS"] - g["AVG_CHG"]
        return g.drop(columns=["CHG_COST", "DIS_REV"])

    return _vwap(["BATTERY", "REGIONID"]), _vwap(["REGIONID"])