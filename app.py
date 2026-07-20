import math
from datetime import datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, dcc, html, Input, Output
from battery_soc import estimate_soc, NP_SEED_HOURS
from market import get_region_prices, get_p5min_forecast

app = Dash(__name__)
app.title = "NEM Battery SoC Tracker (estimated)"

CTRL_LABEL = {"fontSize": "12px", "color": "#555", "display": "block"}

# --- Shared visual theme ---
PLOT_BG = "#ffffff"
PAPER_BG = "#ffffff"
GRID = "#e6e6e6"
FONT = dict(family="Segoe UI, Arial, sans-serif", size=13, color="#222")
STATE_COLOURS = {
    "NSW1": "#1f6feb", "QLD1": "#d62728", "SA1": "#2ca02c",
    "VIC1": "#7b3fe4", "TAS1": "#e8830c",
}
PALETTE = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24

app.layout = html.Div(
    style={"fontFamily": "Segoe UI, sans-serif", "margin": "24px",
           "background": "#f7f8fa"},
    children=[
        html.H2("NEM Battery State of Charge \u2014 Estimated"),
        html.P("Estimate only. Integrated from DISPATCH_UNIT_SCADA. Window anchors "
               "pin the window minimum to empty (assumes \u22651 full cycle); the "
               "NEMpulse method runs a continuously clamped integration that "
               "re-anchors whenever a battery hits full or empty. Capacity "
               "de-rated for age (2.5%/yr, floor 70%).",
               style={"color": "#555", "maxWidth": "900px"}),
        html.Div(id="last-update", style={"color": "#888", "fontSize": "12px"}),

        # --- Timeline + window controls ---
        html.Div(
            style={"display": "flex", "gap": "20px", "alignItems": "flex-end",
                   "margin": "12px 0 18px 0", "padding": "14px",
                   "border": "1px solid #e3e3e3", "borderRadius": "10px",
                   "background": "#ffffff", "width": "fit-content",
                   "boxShadow": "0 1px 3px rgba(0,0,0,0.06)"},
            children=[
                html.Div([
                    html.Label("View as at date", style=CTRL_LABEL),
                    dcc.DatePickerSingle(
                        id="as-at-date",
                        date=datetime.now().date(),
                        max_date_allowed=datetime.now().date(),
                        display_format="DD MMM YYYY"),
                ]),
                html.Div([
                    html.Label("Time", style=CTRL_LABEL),
                    dcc.Dropdown(
                        id="as-at-hour",
                        options=[{"label": "Live (now)", "value": "live"}] +
                                [{"label": f"{h:02d}:00", "value": h} for h in range(24)],
                        value="live", clearable=False,
                        style={"width": "150px"}),
                ]),
                html.Div([
                    html.Label("Anchor window", style=CTRL_LABEL),
                    dcc.RadioItems(
                        id="lookback",
                        options=[
                            {"label": " 7 days", "value": 168},
                            {"label": " 48 hours", "value": 48},
                            {"label": " NEMpulse method", "value": "nempulse"},
                        ],
                        value="nempulse", inline=True,
                        labelStyle={"marginRight": "12px"}),
                ]),
                html.Button("Reset to live", id="reset-live", n_clicks=0,
                            style={"height": "36px", "cursor": "pointer",
                                   "borderRadius": "6px", "border": "1px solid #ccc",
                                   "background": "#f3f3f3"}),
            ],
        ),

        html.H4("State summary", style={"marginTop": "4px"}),
        html.Div(id="state-cards",
                 style={"display": "flex", "gap": "16px", "flexWrap": "wrap",
                        "margin": "8px 0 20px 0"}),
        dcc.Interval(id="refresh", interval=5 * 60 * 1000, n_intervals=0),
        dcc.Graph(id="duid-lines", style={"height": "820px"},
                  config={"displaylogo": False}),
        dcc.Graph(id="capability-bar", config={"displaylogo": False}),
        dcc.Graph(id="price-soc", style={"height": "1150px"},
                  config={"displaylogo": False}),
    ],
)


def compute_as_at(date_str, hour_val):
    """Return a timestamp anchor, or None for live/now."""
    today = pd.Timestamp.now().normalize()
    if not date_str:
        return None
    d = pd.Timestamp(date_str).normalize()
    if hour_val == "live":
        if d >= today:
            return None
        return d + pd.Timedelta(hours=23, minutes=55)
    return d + pd.Timedelta(hours=int(hour_val))


def build_soc_figure(detail):
    regions = sorted(detail["REGIONID"].unique())
    n = len(regions)
    cols = min(3, n)
    rows = math.ceil(n / cols)
    titles = [f"<b>{r}</b>" for r in regions]
    fig = make_subplots(
        rows=rows, cols=cols, subplot_titles=titles,
        shared_yaxes=True, vertical_spacing=0.14, horizontal_spacing=0.06,
        x_title="<b>Time</b>", y_title="<b>State of Charge (%)</b>",
    )

    # Stable colour per battery across panels
    all_batts = sorted(detail["BATTERY"].unique())
    colour_map = {b: PALETTE[i % len(PALETTE)] for i, b in enumerate(all_batts)}

    for i, region in enumerate(regions):
        r = i // cols + 1
        c = i % cols + 1
        rdf = detail[detail["REGIONID"] == region]

        # Individual BESS — coloured, faded, toggleable
        for batt, g in rdf.groupby("BATTERY"):
            g = g.sort_values("SETTLEMENTDATE")
            fig.add_trace(go.Scatter(
                x=g["SETTLEMENTDATE"], y=g["SOC_PCT"],
                mode="lines", name=batt,
                legendgroup=region, legendgrouptitle_text=f"\u2014 {region} \u2014",
                line=dict(width=1.3, dash="dot", color=colour_map[batt]),
                opacity=0.55,
                hovertemplate=f"<b>{batt}</b><br>%{{x|%d %b %H:%M}}<br>"
                              f"SoC %{{y:.1f}}%<extra></extra>",
                showlegend=True,
            ), row=r, col=c)

        # State total — bold, on top
        state_ts = (rdf.groupby("SETTLEMENTDATE")
                       .agg(STORED=("SOC_MWH", "sum"), CAP=("CAPACITY_MWH", "sum"))
                       .reset_index().sort_values("SETTLEMENTDATE"))
        state_ts["PCT"] = 100.0 * state_ts["STORED"] / state_ts["CAP"]
        col = STATE_COLOURS.get(region, "#111111")
        fig.add_trace(go.Scatter(
            x=state_ts["SETTLEMENTDATE"], y=state_ts["PCT"],
            mode="lines", name=f"{region} total", legendgroup=region,
            line=dict(width=4, color=col),
            hovertemplate=f"<b>{region} TOTAL</b><br>%{{x|%d %b %H:%M}}<br>"
                          f"SoC %{{y:.1f}}%<extra></extra>",
            showlegend=False,
        ), row=r, col=c)

        last = state_ts.iloc[-1]
        fig.add_annotation(
            x=last["SETTLEMENTDATE"], y=last["PCT"],
            xref=f"x{i+1}" if i > 0 else "x",
            yref=f"y{i+1}" if i > 0 else "y",
            text=f"<b>{region} {last['PCT']:.0f}%</b>",
            showarrow=False, xanchor="left", xshift=8,
            font=dict(size=12, color="#ffffff"),
            bgcolor=col, borderpad=4, opacity=0.95,
        )

    fig.update_xaxes(
        showgrid=True, gridcolor=GRID, ticks="outside", tickformat="%H:%M\n%d %b",
        showline=True, linecolor="#cccccc",
    )
    fig.update_yaxes(
        range=[0, 100], dtick=20, ticksuffix="%", showgrid=True, gridcolor=GRID,
        zeroline=True, zerolinecolor="#bbbbbb", showline=True, linecolor="#cccccc",
    )
    fig.update_layout(
        title=dict(text="<b>Estimated SoC by State</b>  "
                        "<span style='font-size:13px;color:#888'>"
                        "(bold = state total, dotted = individual BESS, click legend to toggle)</span>",
                   x=0.01, xanchor="left"),
        height=820, font=FONT, plot_bgcolor=PLOT_BG, paper_bgcolor=PAPER_BG,
        margin=dict(t=80, b=70, r=140, l=80),
        legend=dict(groupclick="toggleitem", font=dict(size=11),
                    bordercolor="#e0e0e0", borderwidth=1, bgcolor="#fcfcfc"),
        hovermode="x unified",
    )
    return fig


def build_capability_figure(summary):
    s = summary.sort_values("REGIONID")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=s["REGIONID"], x=s["STORED_MWH"], orientation="h",
        name="Dischargeable (deliver)", marker_color="#2ca02c",
        marker_line=dict(color="#1d6f1d", width=1),
        text=[f"{e:.0f} MWh \u00b7 {h:.1f}h @ {p:.0f} MW"
              for e, h, p in zip(s.STORED_MWH, s.DISCHARGE_HRS, s.POWER_MW)],
        textposition="outside", textfont=dict(size=11),
        hovertemplate="<b>%{y}</b><br>Can deliver: %{x:.0f} MWh<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=s["REGIONID"], x=-s["CHARGE_MWH"], orientation="h",
        name="Charge headroom (absorb)", marker_color="#d62728",
        marker_line=dict(color="#9e1c1d", width=1),
        text=[f"{e:.0f} MWh \u00b7 {h:.1f}h @ {p:.0f} MW"
              for e, h, p in zip(s.CHARGE_MWH, s.CHARGE_HRS, s.POWER_MW)],
        textposition="outside", textfont=dict(size=11),
        hovertemplate="<b>%{y}</b><br>Can absorb: %{customdata:.0f} MWh<extra></extra>",
        customdata=s["CHARGE_MWH"],
    ))
    maxext = max(s["STORED_MWH"].max(), s["CHARGE_MWH"].max()) * 1.5
    fig.update_layout(
        title=dict(text="<b>State Trading Capability</b>  "
                        "<span style='font-size:13px;color:#888'>"
                        "(\u2190 absorb / charge   |   deliver / discharge \u2192)</span>",
                   x=0.01, xanchor="left"),
        barmode="relative", height=420, font=FONT,
        plot_bgcolor=PLOT_BG, paper_bgcolor=PAPER_BG,
        xaxis=dict(title="<b>Energy (MWh)</b>  \u2014  negative = charge headroom, positive = dischargeable",
                   range=[-maxext, maxext], showgrid=True, gridcolor=GRID,
                   zeroline=True, zerolinecolor="#888888", zerolinewidth=2,
                   ticks="outside", showline=True, linecolor="#cccccc"),
        yaxis=dict(title="<b>Region</b>", showline=True, linecolor="#cccccc"),
        margin=dict(l=80, r=60, t=70, b=60),
        legend=dict(orientation="h", y=1.14, x=0, font=dict(size=12)),
        bargap=0.35,
    )
    return fig


PRICE_COL = "#1f6feb"      # actual dispatch price
FORECAST_COL = "#7fa8f5"   # P5MIN forecast (same hue, lighter, dashed)
SOC_COL = "#2ca02c"        # matches 'dischargeable' green used elsewhere


def build_price_soc_figure(detail, prices, forecast):
    """Per-region price + average SoC overlay, with P5MIN forecast extension."""
    regions = sorted(set(detail["REGIONID"].unique()) |
                     set(prices["REGIONID"].unique() if not prices.empty else []))
    n = len(regions)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=True,
        subplot_titles=[f"<b>{r}</b>" for r in regions],
        vertical_spacing=0.045,
        specs=[[{"secondary_y": True}] for _ in range(n)],
    )

    # State-average SoC time-series (energy-weighted across the fleet)
    soc_ts = (detail.groupby(["REGIONID", "SETTLEMENTDATE"])
                    .agg(STORED=("SOC_MWH", "sum"), CAP=("CAPACITY_MWH", "sum"))
                    .reset_index())
    soc_ts["PCT"] = 100.0 * soc_ts["STORED"] / soc_ts["CAP"]

    forecast_start = None
    if not forecast.empty:
        forecast_start = forecast["RUN_DATETIME"].iloc[0]

    for i, region in enumerate(regions):
        row = i + 1
        show = (i == 0)  # one legend entry per series type

        p = prices[prices["REGIONID"] == region] if not prices.empty else prices
        if not p.empty:
            p = p.sort_values("SETTLEMENTDATE")
            fig.add_trace(go.Scatter(
                x=p["SETTLEMENTDATE"], y=p["RRP"],
                mode="lines", name="Price (dispatch)",
                line=dict(width=2, color=PRICE_COL),
                legendgroup="price", showlegend=show,
                hovertemplate="Price $%{y:,.0f}/MWh<extra></extra>",
            ), row=row, col=1, secondary_y=False)

        f = forecast[forecast["REGIONID"] == region] if not forecast.empty else forecast
        if not f.empty:
            f = f.sort_values("INTERVAL_DATETIME")
            fx, fy = list(f["INTERVAL_DATETIME"]), list(f["RRP"])
            if not p.empty:  # join the forecast onto the last actual point
                fx = [p["SETTLEMENTDATE"].iloc[-1]] + fx
                fy = [p["RRP"].iloc[-1]] + fy
            fig.add_trace(go.Scatter(
                x=fx, y=fy,
                mode="lines", name="Price (P5MIN forecast)",
                line=dict(width=2, color=FORECAST_COL, dash="dash"),
                legendgroup="p5min", showlegend=show,
                hovertemplate="P5MIN $%{y:,.0f}/MWh<extra></extra>",
            ), row=row, col=1, secondary_y=False)

        s = soc_ts[soc_ts["REGIONID"] == region]
        if not s.empty:
            s = s.sort_values("SETTLEMENTDATE")
            fig.add_trace(go.Scatter(
                x=s["SETTLEMENTDATE"], y=s["PCT"],
                mode="lines", name="Avg SoC",
                line=dict(width=2, color=SOC_COL),
                legendgroup="soc", showlegend=show,
                hovertemplate="Avg SoC %{y:.1f}%<extra></extra>",
            ), row=row, col=1, secondary_y=True)

        fig.update_yaxes(title_text="$/MWh", secondary_y=False,
                         showgrid=True, gridcolor=GRID,
                         zeroline=True, zerolinecolor="#bbbbbb",
                         showline=True, linecolor="#cccccc", row=row, col=1)
        fig.update_yaxes(title_text="SoC %", secondary_y=True,
                         range=[0, 100], dtick=25, ticksuffix="%",
                         showgrid=False, color=SOC_COL, row=row, col=1)

    if forecast_start is not None:
        fig.add_vline(x=forecast_start, line_dash="dot", line_color="#999999",
                      annotation_text="forecast →",
                      annotation_font=dict(size=11, color="#888"))

    fig.update_xaxes(showgrid=True, gridcolor=GRID, ticks="outside",
                     tickformat="%H:%M\n%d %b", showline=True, linecolor="#cccccc")
    fig.update_layout(
        title=dict(text="<b>Regional Price vs Average BESS SoC</b>  "
                        "<span style='font-size:13px;color:#888'>"
                        "(solid blue = dispatch price, dashed = 5-min predispatch "
                        "forecast, green = fleet-average SoC)</span>",
                   x=0.01, xanchor="left"),
        height=1150, font=FONT, plot_bgcolor=PLOT_BG, paper_bgcolor=PAPER_BG,
        margin=dict(t=80, b=60, r=80, l=80),
        legend=dict(orientation="h", y=1.03, x=0, font=dict(size=12),
                    bordercolor="#e0e0e0", borderwidth=1, bgcolor="#fcfcfc"),
        hovermode="x unified",
    )
    return fig


@app.callback(
    Output("as-at-date", "date"),
    Output("as-at-hour", "value"),
    Input("reset-live", "n_clicks"),
    prevent_initial_call=True,
)
def reset_live(_):
    return datetime.now().date(), "live"


@app.callback(
    Output("state-cards", "children"),
    Output("duid-lines", "figure"),
    Output("capability-bar", "figure"),
    Output("price-soc", "figure"),
    Output("last-update", "children"),
    Input("refresh", "n_intervals"),
    Input("as-at-date", "date"),
    Input("as-at-hour", "value"),
    Input("lookback", "value"),
)
def update(_, as_at_date, as_at_hour, lookback):
    as_at = compute_as_at(as_at_date, as_at_hour)
    if lookback == "nempulse":
        method = "nempulse"
        window_txt = f"NEMpulse clamped ({NP_SEED_HOURS // 24}d seed)"
        detail, summary = estimate_soc(as_at=as_at, method=method)
    else:
        lookback = int(lookback)
        window_txt = "7-day anchor" if lookback == 168 else "48h anchor"
        detail, summary = estimate_soc(as_at=as_at, lookback_hours=lookback)

    if as_at is None:
        stamp = (f"Live \u2014 last refreshed: "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  {window_txt}")
    else:
        stamp = ("Viewing as at: " + pd.Timestamp(as_at).strftime("%Y-%m-%d %H:%M") +
                 f"  (historical \u2014 auto-refresh has no effect)  |  {window_txt}")

    if detail.empty:
        empty = px.scatter(title="No data returned for this window")
        return [html.Div("No data")], empty, empty, empty, stamp

    ref = pd.Timestamp.now() if as_at is None else pd.Timestamp(as_at)
    prices = get_region_prices(detail["SETTLEMENTDATE"].min(), ref)
    forecast = get_p5min_forecast(ref)

    cards = []
    for _, r in summary.iterrows():
        col = STATE_COLOURS.get(r["REGIONID"], "#444")
        cards.append(html.Div(
            style={"border": "1px solid #e3e3e3", "borderTop": f"4px solid {col}",
                   "borderRadius": "10px", "padding": "12px 18px", "minWidth": "190px",
                   "background": "#ffffff", "boxShadow": "0 1px 3px rgba(0,0,0,0.06)"},
            children=[
                html.H4(r["REGIONID"], style={"margin": "0 0 6px 0", "color": col}),
                html.Div(f"{r['STORED_MWH']:.0f} / {r['CAPACITY_MWH']:.0f} MWh"),
                html.Div(f"{r['SOC_PCT']:.0f}%",
                         style={"fontSize": "26px", "fontWeight": "bold", "color": col}),
                html.Div(f"{r['POWER_MW']:.0f} MW rated",
                         style={"fontSize": "12px", "color": "#555"}),
                html.Div(f"deliver {r['DISCHARGE_HRS']:.1f}h  |  charge {r['CHARGE_HRS']:.1f}h",
                         style={"fontSize": "12px", "color": "#555"}),
                html.Div(f"{int(r['N_UNITS'])} units",
                         style={"fontSize": "12px", "color": "#888"}),
            ]))

    return (cards, build_soc_figure(detail), build_capability_figure(summary),
            build_price_soc_figure(detail, prices, forecast), stamp)


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8051)
