"""
Real Exchange Rate (RER) app using IMF SDMX 3.0 data.

Definitions
-----------
S(t)   = units of Country 2 currency per 1 unit of Country 1 currency
P1(t)  = Country 1 monthly CPI,  P2(t) = Country 2 monthly CPI
RER(t) = S(t) * P1(t) / P2(t)
RER*   = average of RER(t) over valid months in the selected sample
Real Index(t) = 100 * RER(t) / RER*

IMF exchange rates (ER dataflow, key {ISO3}.XDC_USD.PA_RT.M) are local currency
units per USD (period average). With E1 = LCU1 per USD and E2 = LCU2 per USD:
    S(t) = E2(t) / E1(t)     (LCU2 per LCU1)
For the United States, E = 1 by definition.
"""

from __future__ import annotations

import io
import re

import pandas as pd
import plotly.graph_objects as go
import pycountry
import requests
import streamlit as st
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
ER_FLOW = "ER"
START_PERIOD = pd.Timestamp("1976-01-01")
EURO_AREA_CODE = "G163"
USA_CODE = "USA"
TIMEOUT = 60

st.set_page_config(page_title="Real Exchange Rate App", layout="wide")


# --------------------------------------------------------------------------
# Country list
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def country_options() -> dict[str, str]:
    """Return {display label: code} for all ISO countries plus the Euro area."""
    opts = {f"Euro area ({EURO_AREA_CODE})": EURO_AREA_CODE}
    extra = {"XKX": "Kosovo"}  # IMF reports Kosovo under XKX
    names = {c.alpha_3: getattr(c, "common_name", None) or c.name for c in pycountry.countries}
    names.update(extra)
    for code, name in sorted(names.items(), key=lambda kv: kv[1]):
        opts[f"{name} ({code})"] = code
    return opts


# --------------------------------------------------------------------------
# IMF data retrieval
# --------------------------------------------------------------------------
def _headers(accept: str) -> dict[str, str]:
    h = {"Accept": accept, "User-Agent": "real-exchange-rate-app/1.0 (Streamlit)"}
    # Optional: an IMF API subscription key can be stored in Streamlit Secrets.
    try:
        key = st.secrets.get("IMF_API_KEY", None)
    except Exception:
        key = None
    if key:
        h["Ocp-Apim-Subscription-Key"] = key
    return h


_PERIOD_RE = re.compile(r"^\s*(\d{4})\s*[-]?\s*M?\s*(\d{1,2})")


def _parse_period(p) -> pd.Timestamp | None:
    """Convert IMF period strings like '1976-M01', '1976-01', '1976M1' to month start."""
    if p is None:
        return None
    m = _PERIOD_RE.match(str(p))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Timestamp(year=year, month=month, day=1)


def _parse_sdmx_json(js: dict) -> pd.Series:
    """Parse an SDMX-JSON (1.0 or 2.0) data message into a monthly Series."""
    data = js.get("data", js)
    structs = data.get("structures")
    if structs is None and "structure" in data:
        structs = [data["structure"]]
    if not structs:
        structs = [js.get("structure", {})]
    datasets = data.get("dataSets", [])

    points: dict[pd.Timestamp, float] = {}
    for ds in datasets:
        s_idx = ds.get("structure", 0)
        struct = structs[s_idx] if isinstance(s_idx, int) and s_idx < len(structs) else structs[0]
        dims = struct.get("dimensions", {})

        if "series" in ds:
            obs_dims = dims.get("observation", [])
            time_pos = next(
                (i for i, d in enumerate(obs_dims) if d.get("id") == "TIME_PERIOD"), 0
            )
            time_vals = obs_dims[time_pos].get("values", []) if obs_dims else []
            series_items = list(ds["series"].values())
            # Exact keys should return one series; if several come back, keep the longest.
            series_items.sort(key=lambda s: len(s.get("observations", {})), reverse=True)
            for s in series_items[:1]:
                for okey, oval in s.get("observations", {}).items():
                    idx = int(str(okey).split(":")[time_pos])
                    tv = time_vals[idx] if idx < len(time_vals) else {}
                    period = _parse_period(tv.get("id") or tv.get("value") or tv.get("name"))
                    val = oval[0] if isinstance(oval, list) and oval else oval
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        continue
                    if period is not None:
                        points[period] = val
        elif "observations" in ds:
            obs_dims = dims.get("observation", [])
            time_pos = next(
                (i for i, d in enumerate(obs_dims) if d.get("id") == "TIME_PERIOD"),
                len(obs_dims) - 1,
            )
            time_vals = obs_dims[time_pos].get("values", []) if obs_dims else []
            for okey, oval in ds["observations"].items():
                idx = int(str(okey).split(":")[time_pos])
                tv = time_vals[idx] if idx < len(time_vals) else {}
                period = _parse_period(tv.get("id") or tv.get("value"))
                val = oval[0] if isinstance(oval, list) and oval else oval
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
                if period is not None:
                    points[period] = val

    return pd.Series(points, dtype="float64").sort_index()


def _parse_sdmx_csv(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    cols = {c.upper(): c for c in df.columns}
    tcol = cols.get("TIME_PERIOD")
    vcol = cols.get("OBS_VALUE")
    if tcol is None or vcol is None:
        return pd.Series(dtype="float64")
    out = {}
    for p, v in zip(df[tcol], df[vcol]):
        ts = _parse_period(p)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if ts is not None and pd.notna(v):
            out[ts] = v
    return pd.Series(out, dtype="float64").sort_index()


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_imf_series(flow: str, key: str) -> tuple[pd.Series, str]:
    """
    Fetch one monthly IMF series. Returns (series, message).
    An empty series means no data was retrieved; message explains why.
    """
    url = f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/+/{key}"
    attempts = [
        ("application/vnd.sdmx.data+json;version=2.0.0", "json"),
        ("application/json", "json"),
        ("application/vnd.sdmx.data+csv;version=2.0.0", "csv"),
    ]
    last_msg = ""
    for accept, kind in attempts:
        try:
            r = requests.get(url, headers=_headers(accept), timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_msg = f"Network error for {url}: {exc}"
            continue
        if r.status_code == 404:
            return pd.Series(dtype="float64"), f"No data found at IMF for {flow} key {key} (HTTP 404)."
        if r.status_code != 200:
            last_msg = f"HTTP {r.status_code} for {url}: {r.text[:200]}"
            continue
        try:
            s = _parse_sdmx_json(r.json()) if kind == "json" else _parse_sdmx_csv(r.text)
        except Exception as exc:  # parsing problem: try next format
            last_msg = f"Could not parse {kind} response for {key}: {exc}"
            continue
        if not s.empty:
            s = s[s.index >= START_PERIOD]
            return s, "ok"
        last_msg = f"IMF returned no observations for {flow} key {key}."
    return pd.Series(dtype="float64"), last_msg


def get_cpi(code: str) -> tuple[pd.Series, str]:
    return fetch_imf_series(CPI_FLOW, f"{code}.CPI._T.IX.M")


def get_fx_per_usd(code: str, like_index: pd.Index | None = None) -> tuple[pd.Series, str]:
    """Local currency units per USD. For the U.S. this is identically 1."""
    if code == USA_CODE:
        idx = pd.date_range(START_PERIOD, pd.Timestamp.today().normalize(), freq="MS")
        return pd.Series(1.0, index=idx), "ok (USD per USD = 1)"
    return fetch_imf_series(ER_FLOW, f"{code}.XDC_USD.PA_RT.M")


# --------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------
def build_table(cpi1, cpi2, fx1, fx2) -> pd.DataFrame:
    df = pd.concat(
        {
            "P1_CPI_country1": cpi1,
            "P2_CPI_country2": cpi2,
            "E1_country1_LCU_per_USD": fx1,
            "E2_country2_LCU_per_USD": fx2,
        },
        axis=1,
    ).sort_index()
    df.index.name = "month"
    # S(t): units of Country 2 currency per 1 unit of Country 1 currency
    df["S_country2_per_country1"] = df["E2_country2_LCU_per_USD"] / df["E1_country1_LCU_per_USD"]
    df["RER"] = df["S_country2_per_country1"] * df["P1_CPI_country1"] / df["P2_CPI_country2"]
    return df


def apply_sample(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, float]:
    sample = df.loc[(df.index >= start) & (df.index <= end)].copy()
    valid = sample["RER"].replace([float("inf"), float("-inf")], pd.NA).dropna()
    valid = valid[valid > 0]
    rer_star = float(valid.mean()) if len(valid) else float("nan")
    sample["RER_valid"] = sample.index.isin(valid.index)
    sample["RER_star"] = rer_star
    sample["Real_Index"] = 100 * sample["RER"] / rer_star
    sample.loc[~sample["RER_valid"], "Real_Index"] = pd.NA
    return sample, rer_star


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.title("Real Exchange Rate App")
st.caption(
    "Monthly IMF CPI and exchange-rate data (SDMX 3.0 API). "
    "S(t) = units of Country 2 currency per unit of Country 1 currency; "
    "RER(t) = S(t) × P1(t) / P2(t); Real Index(t) = 100 × RER(t) / RER*."
)

opts = country_options()
labels = list(opts.keys())
default1 = next(i for i, l in enumerate(labels) if l.endswith("(GBR)"))
default2 = next(i for i, l in enumerate(labels) if l.endswith("(USA)"))

c1, c2 = st.columns(2)
with c1:
    label1 = st.selectbox("Country 1", labels, index=default1)
with c2:
    label2 = st.selectbox("Country 2", labels, index=default2)

code1, code2 = opts[label1], opts[label2]
name1 = label1.rsplit(" (", 1)[0]
name2 = label2.rsplit(" (", 1)[0]

if code1 == code2:
    st.warning("Please choose two different countries.")
    st.stop()

with st.spinner("Downloading IMF data…"):
    cpi1, m_cpi1 = get_cpi(code1)
    cpi2, m_cpi2 = get_cpi(code2)
    fx1, m_fx1 = get_fx_per_usd(code1)
    fx2, m_fx2 = get_fx_per_usd(code2)

problems = []
for what, s, msg in [
    (f"CPI for {name1}", cpi1, m_cpi1),
    (f"CPI for {name2}", cpi2, m_cpi2),
    (f"Exchange rate for {name1}", fx1, m_fx1),
    (f"Exchange rate for {name2}", fx2, m_fx2),
]:
    if s.empty:
        problems.append(f"**{what}**: {msg}")
if problems:
    st.error("Some IMF data could not be retrieved:\n\n" + "\n\n".join(problems))
    st.info("IMF coverage varies by country and live downloads can fail. Try again or pick other countries.")
    st.stop()

full = build_table(cpi1, cpi2, fx1, fx2)
valid_months = full["RER"].dropna()
valid_months = valid_months[valid_months > 0]
if valid_months.empty:
    st.error("No months where both CPIs and both exchange rates are available.")
    st.stop()

first_valid, last_valid = valid_months.index.min(), valid_months.index.max()
month_list = list(pd.date_range(first_valid, last_valid, freq="MS"))
fmt = lambda d: d.strftime("%Y-%m")

st.subheader("Sample period")
start, end = st.select_slider(
    "Select start and end months (RER* is recalculated automatically)",
    options=month_list,
    value=(month_list[0], month_list[-1]),
    format_func=fmt,
)

sample, rer_star = apply_sample(full, start, end)
n_valid = int(sample["RER_valid"].sum())

m1, m2, m3, m4 = st.columns(4)
m1.metric("Valid months in sample", f"{n_valid}")
m2.metric("RER* (sample mean of RER)", f"{rer_star:,.6g}")
m3.metric("Mean Real Index (should be 100)", f"{sample['Real_Index'].astype(float).mean():.4f}")
m4.metric(f"Latest S ({name2} per {name1})", f"{sample['S_country2_per_country1'].dropna().iloc[-1]:,.6g}"
          if sample["S_country2_per_country1"].notna().any() else "n/a")

view = st.radio("Chart", ["Real", "Nominal", "Both"], horizontal=True)

real_name = "Real Index (RER*=100)"
nom_name = f"Nominal S: {name2} currency per 1 {name1} currency"
plot_df = sample.reset_index()

if view == "Real":
    fig = go.Figure(go.Scatter(x=plot_df["month"], y=plot_df["Real_Index"], name=real_name, mode="lines"))
    fig.add_hline(y=100, line_dash="dot", line_color="gray")
    fig.update_yaxes(title_text="Real Index (sample mean = 100)")
elif view == "Nominal":
    fig = go.Figure(go.Scatter(x=plot_df["month"], y=plot_df["S_country2_per_country1"],
                               name=nom_name, mode="lines", line=dict(color="#d62728")))
    fig.update_yaxes(title_text=f"{name2} currency per 1 {name1} currency")
else:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=plot_df["month"], y=plot_df["Real_Index"], name=real_name, mode="lines"),
                  secondary_y=False)
    fig.add_trace(go.Scatter(x=plot_df["month"], y=plot_df["S_country2_per_country1"], name=nom_name,
                             mode="lines", line=dict(color="#d62728")), secondary_y=True)
    fig.add_hline(y=100, line_dash="dot", line_color="gray", secondary_y=False)
    fig.update_yaxes(title_text="Real Index (sample mean = 100)", secondary_y=False)
    fig.update_yaxes(title_text=f"{name2} currency per 1 {name1} currency", secondary_y=True)

fig.update_layout(
    title=f"{name1} (Country 1) vs {name2} (Country 2), {fmt(start)} to {fmt(end)}",
    hovermode="x unified",
    legend=dict(orientation="h", y=-0.2),
    height=520,
    margin=dict(l=10, r=10, t=60, b=10),
)
fig.update_xaxes(title_text="Month")
st.plotly_chart(fig, width="stretch")

st.caption(
    "Interpretation: a higher Real Index means Country 1's goods are relatively more expensive "
    "than Country 2's (a real appreciation of Country 1's currency) compared with the sample average."
)

# Export -------------------------------------------------------------------
export = sample.reset_index().copy()
export.insert(0, "country1", f"{name1} ({code1})")
export.insert(1, "country2", f"{name2} ({code2})")
export["month"] = export["month"].dt.strftime("%Y-%m")
export = export[[
    "country1", "country2", "month",
    "P1_CPI_country1", "P2_CPI_country2",
    "E1_country1_LCU_per_USD", "E2_country2_LCU_per_USD",
    "S_country2_per_country1", "RER", "RER_valid", "RER_star", "Real_Index",
]]

st.download_button(
    "Download CSV of the selected sample",
    data=export.to_csv(index=False).encode("utf-8"),
    file_name=f"rer_{code1}_{code2}_{fmt(start)}_{fmt(end)}.csv",
    mime="text/csv",
)

with st.expander("Data table"):
    st.dataframe(export, width="stretch", hide_index=True)

with st.expander("Check one calculation by hand"):
    row = sample[sample["RER_valid"]].iloc[-1]
    st.markdown(
        f"""
**Month {fmt(row.name)}**

- E1 ({name1} currency per USD) = {row['E1_country1_LCU_per_USD']:.6g}
- E2 ({name2} currency per USD) = {row['E2_country2_LCU_per_USD']:.6g}
- S = E2 / E1 = {row['S_country2_per_country1']:.6g} {name2} currency per 1 {name1} currency
- P1 = {row['P1_CPI_country1']:.6g},  P2 = {row['P2_CPI_country2']:.6g}
- RER = S × P1 / P2 = {row['RER']:.6g}
- Real Index = 100 × RER / RER* = 100 × {row['RER']:.6g} / {rer_star:.6g} = {row['Real_Index']:.4f}
"""
    )

st.caption(
    "Source: IMF SDMX 3.0 API (dataflows IMF.STA:CPI, key {ISO3}.CPI._T.IX.M; "
    "IMF.STA:ER, key {ISO3}.XDC_USD.PA_RT.M). Euro area uses code G163. "
    "Coverage and start dates vary by country; months missing any input are excluded from RER*."
)
