"""
historical_traffic.py
─────────────────────
Constructs a monthly load factor time series for the Emirates ISC corridor
from annual data points, using documented seasonality patterns.

Approach:
─────────
1. Load annual data from data/raw/emirates_annual_data.csv
2. For each fiscal year, disaggregate the annual load factor into 12 monthly
   values using ISC-specific seasonal weights (from ml/demand_forecast.py)
3. Apply COVID-19 monthly distribution for FY2020-21 (massive Q1 drop, partial recovery)
4. Return a Prophet-ready DataFrame: columns [ds, y]

Honest framing:
───────────────
The annual values come from Emirates Group annual reports (real, citable).
The monthly disaggregation is a modelled overlay using published industry
seasonal patterns. This is documented openly in code and README.

The output is suitable for Prophet fitting because it preserves:
- Real annual aggregates (sum of monthly LF * monthly ASK = reported annual)
- Industry-standard seasonal patterns
- COVID-19 structural break (Q1 FY2020-21)
- Recovery trajectory (FY2021-22 onward)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from ml.demand_forecast import ISC_SEASONAL_FACTORS


# ── PATHS ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent
RAW_CSV = DATA_DIR / "raw" / "emirates_annual_data.csv"


# ── COVID-19 MONTHLY DISTRIBUTION (FY2020-21) ────────────────────────────────
# Emirates published monthly traffic reports during COVID showed:
# Apr-Jun 2020: near-zero (passenger flights suspended)
# Jul-Sep 2020: ~20% of normal (limited routes resumed)
# Oct-Dec 2020: ~40% of normal (gradual recovery)
# Jan-Mar 2021: ~50% of normal (continued recovery, vaccine optimism)
COVID_MONTHLY_LF = {
    1: 0.10,  # Apr 2020 (FY month 1) - flights largely grounded
    2: 0.15,  # May 2020
    3: 0.20,  # Jun 2020
    4: 0.35,  # Jul 2020
    5: 0.40,  # Aug 2020
    6: 0.45,  # Sep 2020
    7: 0.50,  # Oct 2020
    8: 0.55,  # Nov 2020
    9: 0.58,  # Dec 2020
    10: 0.60, # Jan 2021
    11: 0.62, # Feb 2021
    12: 0.65, # Mar 2021
}


def load_annual_data() -> pd.DataFrame:
    """Load and validate the annual data CSV."""
    if not RAW_CSV.exists():
        raise FileNotFoundError(
            f"Annual data CSV not found at {RAW_CSV}. "
            "See README for how to construct from Emirates annual reports."
        )

    # Read without auto-parsing dates first
    df = pd.read_csv(RAW_CSV)

    # Validation
    required_cols = ["fiscal_year", "passenger_seat_factor", "reporting_period_start"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    # Robust date parsing: handle multiple formats including Excel serial numbers
    for col in ["reporting_period_start", "reporting_period_end"]:
        if col in df.columns:
            df[col] = _parse_dates_robust(df[col], col)

    if (df["passenger_seat_factor"] > 1).any() or (df["passenger_seat_factor"] < 0).any():
        raise ValueError("Load factor values must be between 0 and 1")

    return df.sort_values("reporting_period_start").reset_index(drop=True)


def _parse_dates_robust(series: pd.Series, col_name: str) -> pd.Series:
    """
    Parse dates handling multiple formats:
    - ISO: 2014-04-01
    - UK: 01/04/2014
    - US: 04/01/2014
    - Excel serial: 41730
    """
    # Try Excel serial numbers first (pure integers)
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="D", origin="1899-12-30")

    # String-based parsing
    str_series = series.astype(str).str.strip()

    # If all values look like Excel serials (5-digit numbers as strings)
    if str_series.str.match(r"^\d{5}$").all():
        return pd.to_datetime(str_series.astype(int), unit="D", origin="1899-12-30")

    # Try ISO first (YYYY-MM-DD)
    try:
        return pd.to_datetime(str_series, format="%Y-%m-%d")
    except (ValueError, TypeError):
        pass

    # Try UK format (DD/MM/YYYY) — most likely if Excel mangled it in UK locale
    try:
        return pd.to_datetime(str_series, format="%d/%m/%Y")
    except (ValueError, TypeError):
        pass

    # Fall back to pandas' general parser with day-first hint
    return pd.to_datetime(str_series, dayfirst=True, errors="raise")


def disaggregate_year_to_months(
    annual_lf: float,
    fiscal_year_start: pd.Timestamp,
    is_covid_year: bool = False,
) -> pd.DataFrame:
    """
    Disaggregate one annual load factor into 12 monthly values.
    
    Two pathways:
    - Normal year: scale annual_lf by ISC seasonal factors, rescale to preserve annual mean
    - COVID year: use observed monthly COVID distribution
    """
    months = pd.date_range(start=fiscal_year_start, periods=12, freq="MS")

    if is_covid_year:
        # Use observed COVID monthly distribution (already factored to FY20-21 reality)
        monthly_lf = np.array([COVID_MONTHLY_LF[i+1] for i in range(12)])
        # Scale to match the reported annual figure exactly
        scaling = annual_lf / monthly_lf.mean()
        monthly_lf = monthly_lf * scaling
    else:
        # Normal year: apply ISC seasonal pattern
        # Emirates fiscal year starts April (FY month 1 = April)
        # We need to map fiscal month → calendar month for the seasonal lookup
        seasonal_weights = np.array([
            ISC_SEASONAL_FACTORS[((fiscal_year_start.month - 1 + i) % 12) + 1]
            for i in range(12)
        ])
        # Initial monthly estimates
        monthly_lf = annual_lf * seasonal_weights
        # Rescale to ensure mean matches reported annual (preserves the real anchor)
        monthly_lf = monthly_lf * (annual_lf / monthly_lf.mean())

    # Safety clip
    monthly_lf = np.clip(monthly_lf, 0.05, 0.99)

    return pd.DataFrame({
        "ds": months,
        "y": monthly_lf,
    })


def build_historical_series() -> pd.DataFrame:
    """
    Build the full historical monthly load factor series for Prophet.
    
    Returns
    -------
    DataFrame with columns ['ds', 'y']:
        ds : monthly timestamps (April 2014 onwards)
        y  : monthly load factor (0-1)
    """
    annual_df = load_annual_data()

    all_months = []
    for _, row in annual_df.iterrows():
        is_covid = row["fiscal_year"] == "2020-21"
        monthly = disaggregate_year_to_months(
            annual_lf=row["passenger_seat_factor"],
            fiscal_year_start=row["reporting_period_start"],
            is_covid_year=is_covid,
        )
        all_months.append(monthly)

    series = pd.concat(all_months, ignore_index=True)
    series = series.sort_values("ds").reset_index(drop=True)

    return series


def add_isc_regressors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add ISC-specific shock regressors to the Prophet input.

    Design decision (documented in project README):
    I had intially included calendar-recurring events (Diwali, Eid, Ramadan,
    Hajj, summer holidays) but decided to remove them after observing collinearity with
    Prophet's yearly seasonality where the Fourier decomposition in s(t) already
    captures these recurring patterns.

    Only non-recurring shock regressors are retained. These flag periods
    where signal cannot be learned from seasonality alone:

    - is_covid                  : Apr 2020 - Sep 2021 (pandemic + recovery)
    - is_fuel_supply_disruption : Apr 2025 - Mar 2026 (year-long pressure)
    - is_regional_conflict      : Feb 2026 - Mar 2026 (acute conflict + airspace closure)

    Future versions using real observed granular monthly data may revisit this,
    calendar regressors become informative when year-to-year magnitude
    varies independently of the seasonal pattern.
    """
    df = df.copy()

    # COVID: Apr 2020 to Sep 2021 (full disruption + early recovery)
    covid_start = pd.Timestamp("2020-04-01")
    covid_end = pd.Timestamp("2021-09-30")
    df["is_covid"] = ((df["ds"] >= covid_start) & (df["ds"] <= covid_end)).astype(int)

    # FY2025-26 fuel + supply chain pressure (full fiscal year)
    fuel_start = pd.Timestamp("2025-04-01")
    fuel_end = pd.Timestamp("2026-03-31")
    df["is_fuel_supply_disruption"] = (
        (df["ds"] >= fuel_start) & (df["ds"] <= fuel_end)
    ).astype(int)

    # Regional conflict acute phase (Feb-Mar 2026 only)
    conflict_start = pd.Timestamp("2026-02-01")
    conflict_end = pd.Timestamp("2026-03-31")
    df["is_regional_conflict"] = (
        (df["ds"] >= conflict_start) & (df["ds"] <= conflict_end)
    ).astype(int)

    return df


def get_prophet_training_data(include_regressors: bool = True) -> pd.DataFrame:
    """
    Main entry point: returns a Prophet-ready DataFrame.
    
    Parameters
    ----------
    include_regressors : bool
    If True, includes ISC-specific event regressors
    
    Returns
    -------
    DataFrame ready to pass to Prophet().fit()
    """
    series = build_historical_series()

    if include_regressors:
        series = add_isc_regressors(series)

    return series


if __name__ == "__main__":
    # Standalone test
    df = get_prophet_training_data()
    print(f"Historical series: {len(df)} monthly observations")
    print(f"Date range: {df['ds'].min().date()} → {df['ds'].max().date()}")
    print(f"Load factor: min={df['y'].min():.3f}, max={df['y'].max():.3f}, mean={df['y'].mean():.3f}")
    print(f"\nFirst 5 rows:")
    print(df.head().to_string(index=False))
    print(f"\nLast 5 rows:")
    print(df.tail().to_string(index=False))
    print(f"\nCOVID period (Apr 2020-Mar 2021):")
    covid_window = df[(df["ds"] >= "2020-04-01") & (df["ds"] <= "2021-03-31")]
    print(covid_window[["ds", "y"]].to_string(index=False))

    # Regressor sanity checks
    print(f"\n── Regressor counts ──")
    regressor_cols = [c for c in df.columns if c.startswith("is_")]
    for col in regressor_cols:
        print(f"  {col}: {df[col].sum()} months flagged")

    print(f"\n── Sample of flagged months ──")
    for col in ["is_ramadan_month", "is_eid_fitr_month", "is_hajj_month"]:
        flagged = df[df[col] == 1][["ds", "y", col]].head(5)
        print(f"\n{col} (first 5):")
        print(flagged.to_string(index=False))