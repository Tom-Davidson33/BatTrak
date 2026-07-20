import os, textwrap

BASE = "battery_tracker"
os.makedirs(BASE, exist_ok=True)

files = {}

files[".env"] = """\
ME_MARKET_DSN=MEL_MARKET.WORLD
ME_MARKET_USER=elecreader
ME_MARKET_PWD=reader4elec
LOOKBACK_HOURS=48
RTE=0.88
"""

files["requirements.txt"] = """\
oracledb
pandas
dash
plotly
python-dotenv
"""

files["db.py"] = '''\
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
    conn = get_me_market()
    try:
        return pd.read_sql(sql, conn, params=params or {})
    finally:
        conn.close()
'''

files["battery_soc.py"] = '''\
import os
import re
import pandas as pd
from dotenv import load_dotenv
from db import query

load_dotenv()

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))
RTE = float(os.getenv("RTE", "0.88"))
INTERVAL_HRS = 5.0 / 60.0  # 5-minute SCADA intervals

# NOTE: AEMO does not publish state of charge. This integrates SCADAVALUE
# and anchors the lowest point of the window to empty (0 MWh), assuming each
# battery cycles to empty at least once in the lookback window.


def base_duid(duid):
    """Strip trailing G/L unit suffix so split battery DUIDs net together."""
    return re.sub(r"(G|L)(\\\\d*)$", "", duid)


def get_batteries():
    sql = """
        SELECT d.DUID, d.MAXSTORAGECAPACITY, s.REGIONID
        FROM TESTER.DUDETAIL d
        JOIN (
            SELECT DUID, MAX(LASTCHANGED) AS LC
            FROM TESTER.DUDETAIL
            WHERE MAXSTORAGECAPACITY > 0
            GROUP BY DUID
        ) latest ON d.DUID = latest.DUID AND d.LASTCHANGED = latest.LC
        JOIN (
            SELECT DUID, REGIONID, MAX(END_DATE) AS ED
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


def get_scada(duids):
    binds = {f"d{i}": d for i, d in enumerate(duids)}
    placeholders = ",".join(f":{k}" for k in binds)
    sql = f"""
        SELECT SETTLEMENTDATE, DUID, SCADAVALUE
        FROM TESTER.DISPATCH_UNIT_SCADA
        WHERE DUID IN ({placeholders})
          AND SETTLEMENTDATE >= SYSDATE - {LOOKBACK_HOURS / 24.0}
        ORDER BY DUID, SETTLEMENTDATE
    """
    df = query(sql, binds)
    df.columns = [c.upper() for c in df.columns]
    return df


def estimate_soc():
    bats = get_batteries()
    if bats.empty:
        return pd.DataFrame(), pd.DataFrame()

    scada = get_scada(bats["DUID"].tolist())
    if scada.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Map DUID -> base group, capacity (summed per group), region
    base_map = bats.set_index("DUID")["BASE"].to_dict()
    grp = bats.groupby("BASE").agg(
        CAPACITY_MWH=("MAXSTORAGECAPACITY", "sum"),
        REGIONID=("REGIONID", "first"),
    ).to_dict("index")

    scada["BASE"] = scada["DUID"].map(base_map)
    # Net paired DUIDs at each timestamp (charge negative, discharge positive)
    netted = scada.groupby(["BASE", "SETTLEMENTDATE"], as_index=False)["SCADAVALUE"].sum()

    rows = []
    for base, g in netted.groupby("BASE"):
        meta = grp.get(base)
        if not meta or meta["CAPACITY_MWH"] <= 0:
            continue
        capacity = meta["CAPACITY_MWH"]
        g = g.sort_values("SETTLEMENTDATE").copy()

        mw = g["SCADAVALUE"].fillna(0.0).values
        # energy delta per interval: charge (mw<0) adds *RTE; discharge subtracts
        deltas = []
        for v in mw:
            if v >= 0:
                deltas.append(-v * INTERVAL_HRS)
            else:
                deltas.append(-v * INTERVAL_HRS * RTE)
        cum = pd.Series(deltas).cumsum()
        # anchor trough to empty (0 MWh), then clamp to capacity
        soc = (cum - cum.min()).clip(0, capacity)

        g["SOC_MWH"] = soc.values
        g["CAPACITY_MWH"] = capacity
        g["REGIONID"] = meta["REGIONID"]
        g["SOC_PCT"] = 100.0 * g["SOC_MWH"] / capacity
        g["BATTERY"] = base
        rows.append(g)

    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    detail = pd.concat(rows, ignore_index=True)

    latest = detail.sort_values("SETTLEMENTDATE").groupby("BATTERY").tail(1)
    summary = (
        latest.groupby("REGIONID")
        .agg(STORED_MWH=("SOC_MWH", "sum"),
             CAPACITY_MWH=("CAPACITY_MWH", "sum"),
             N_UNITS=("BATTERY", "nunique"))
        .reset_index()
    )
    summary["SOC_PCT"] = 100.0 * summary["STORED_MWH"] / summary["CAPACITY_MWH"]
    return detail, summary
'''

files["app.py"] = '''\
import plotly.express as px
from dash import Dash, dcc, html, Input, Output
from battery_soc import estimate_soc

app = Dash(__name__)
app.title = "NEM Battery SoC Tracker (estimated)"

app.layout = html.Div(
    style={"fontFamily": "Segoe UI, sans-serif", "margin": "24px"},
    children=[
        html.H2("NEM Battery State of Charge \\u2014 Estimated"),
        html.P("Estimate only. Integrated from DISPATCH_UNIT_SCADA over 48h, "
               "lowest point anchored to empty (assumes \\u22651 cycle). "
               "Not an AEMO-published value."),
        html.Div(id="last-update", style={"color": "#888", "fontSize": "12px"}),
        dcc.Interval(id="refresh", interval=5 * 60 * 1000, n_intervals=0),
        html.Div(id="state-cards",
                 style={"display": "flex", "gap": "16px", "flexWrap": "wrap",
                        "margin": "16px 0"}),
        dcc.Graph(id="state-bar"),
        dcc.Graph(id="duid-lines"),
    ],
)


@app.callback(
    Output("state-cards", "children"),
    Output("state-bar", "figure"),
    Output("duid-lines", "figure"),
    Output("last-update", "children"),
    Input("refresh", "n_intervals"),
)
def update(_):
    from datetime import datetime
    detail, summary = estimate_soc()
    stamp = "Last refreshed: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if detail.empty:
        empty = px.scatter(title="No data returned")
        return [html.Div("No data")], empty, empty, stamp

    cards = []
    for _, r in summary.iterrows():
        cards.append(html.Div(
            style={"border": "1px solid #ddd", "borderRadius": "8px",
                   "padding": "12px 18px", "minWidth": "150px"},
            children=[
                html.H4(r["REGIONID"]),
                html.Div(f"{r['STORED_MWH']:.0f} / {r['CAPACITY_MWH']:.0f} MWh"),
                html.Div(f"{r['SOC_PCT']:.1f}%",
                         style={"fontSize": "22px", "fontWeight": "bold"}),
                html.Div(f"{int(r['N_UNITS'])} units"),
            ]))

    bar = px.bar(summary, x="REGIONID", y="STORED_MWH", color="REGIONID",
                 title="Estimated stored energy by state (latest interval)",
                 labels={"STORED_MWH": "Stored energy (MWh)"})

    lines = px.line(detail, x="SETTLEMENTDATE", y="SOC_PCT", color="BATTERY",
                    facet_col="REGIONID", facet_col_wrap=3,
                    title="Estimated SoC % by battery (48h)")
    lines.update_yaxes(range=[0, 100])
    return cards, bar, lines, stamp


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8051)
'''

files["run.bat"] = """\
@echo off
cd /d "%~dp0"
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\\Scripts\\activate.bat
echo Installing dependencies...
pip install -q -r requirements.txt
echo.
echo Starting NEM Battery Tracker at http://127.0.0.1:8051
python app.py
"""

for name, content in files.items():
    path = os.path.join(BASE, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("wrote", path)

print("\\nDone. Open the 'battery_tracker' folder and double-click run.bat")
