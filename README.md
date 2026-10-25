# Real Exchange Rate App

A Streamlit app that computes the bilateral real exchange rate (RER) between any two IMF countries (or the Euro area) using monthly IMF CPI and exchange-rate data from January 1976 where available.

## Economics

- **S(t)**: units of Country 2 currency per 1 unit of Country 1 currency
- **P1(t), P2(t)**: monthly CPIs of Country 1 and Country 2
- **RER(t) = S(t) × P1(t) / P2(t)**
- **RER\***: average of RER(t) over valid months in the user-selected sample
- **Real Index(t) = 100 × RER(t) / RER\***, so the index averages 100 over the sample

IMF exchange rates are local currency units per USD (E). The app converts them to the required direction:
S(t) = E2(t) / E1(t). For the United States, E = 1.

Example: Country 1 = United Kingdom, Country 2 = United States gives S = USD per GBP.

## Features

- Choose Country 1 and Country 2 from all ISO countries plus the Euro area (IMF code `G163`)
- Select the sample period; RER\* is recalculated whenever dates change
- Charts: Real, Nominal (S in actual currency units, not normalized), or Both (separate y-axes)
- CSV download of all inputs and calculations in the selected sample
- A built-in panel that walks through one RER calculation by hand

## Data

IMF SDMX 3.0 API: `https://api.imf.org/external/sdmx/3.0`

| Series | Dataflow | Key |
|---|---|---|
| CPI (index) | `IMF.STA/CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate (LCU per USD, period average) | `IMF.STA/ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Coverage varies by country and IMF downloads can occasionally fail; the app reports which series are missing. No API key is needed. If the IMF ever requires one, add `IMF_API_KEY = "..."` in Streamlit Secrets.

## Files

- `app.py`: the Streamlit app
- `requirements.txt`: Python dependencies (Python 3.12)
- `README.md`: this file

## Deploy

1. Put all three files at the top level of a public GitHub repository.
2. At share.streamlit.io choose Create app, select the repository, branch `main`, file `app.py`.
3. In Advanced settings choose Python 3.12 and deploy.
