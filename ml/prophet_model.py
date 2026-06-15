"""
prophet_model.py
────────────────
Facebook Prophet model for Emirates ISC corridor monthly load factor.

Model architecture:
  y(t) = g(t) × (1 + s(t)) × (1 + h(t))
  where:
    g(t) = piecewise linear trend with automatic changepoint detection
    s(t) = yearly seasonality (10 Fourier terms)
    h(t) = effects of 8 ISC-specific regressors

Regressors (all binary):
  - is_diwali_month, is_summer_holiday   (fixed calendar)
  - is_ramadan_month, is_eid_fitr_month, is_hajj_month  (lunar calendar)
  - is_covid, is_fuel_supply_disruption, is_regional_conflict  (shock periods)

Training data: 144 monthly observations (Apr 2014 - Mar 2026)
Target: load factor in [0, 1]
"""

import warnings
import pandas as pd
import numpy as np
from pathlib import Path
from prophet import Prophet
from data.ingestion.historical_traffic import get_prophet_training_data


# Suppress Prophet's verbose output
warnings.filterwarnings("ignore", category=FutureWarning)
import logging
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)


# ── REGRESSOR CONFIGURATION ──────────────────────────────────────────────────
REGRESSOR_COLS = [
    "is_covid",
    "is_fuel_supply_disruption",
    "is_regional_conflict",
]


def build_prophet_model() -> Prophet:
    """
    Construct the Prophet model with ISC-tuned hyperparameters.
    
    Hyperparameter rationale:
    - seasonality_mode='multiplicative' : LF effects are proportional, not additive
    - yearly_seasonality=10 (Fourier terms) : enough to capture multi-peaked ISC pattern
                                              (Diwali + summer + winter peaks)
    - changepoint_prior_scale=0.05 : moderate flexibility for trend changes
                                     higher = more flexible (overfits)
                                     lower = stiffer (underfits COVID)
    - seasonality_prior_scale=10.0 : default, allows strong seasonal patterns
    - holidays_prior_scale=10.0 : default, allows strong regressor effects
    - interval_width=0.80 : 80% prediction intervals (industry standard for biz)
    """
    model = Prophet(
        growth="linear",
        seasonality_mode="multiplicative",
        yearly_seasonality=10,
        weekly_seasonality=False,   # monthly data, irrelevant
        daily_seasonality=False,    # monthly data, irrelevant
        changepoint_prior_scale=0.05,
        seasonality_prior_scale=10.0,
        holidays_prior_scale=10.0,
        interval_width=0.80,
        mcmc_samples=0,             # MAP estimation, not full Bayesian (faster)
    )

    # Register each regressor
    for col in REGRESSOR_COLS:
        model.add_regressor(col, prior_scale=10.0, mode="multiplicative")

    return model


def fit_model(model: Prophet, training_data: pd.DataFrame) -> Prophet:
    """
    Fit the Prophet model on historical data.
    Returns the fitted model.
    """
    print("  Fitting Prophet model on historical data...")
    print(f"  Observations: {len(training_data)}")
    print(f"  Date range: {training_data['ds'].min().date()} to {training_data['ds'].max().date()}")
    print(f"  Regressors: {len(REGRESSOR_COLS)}")

    model.fit(training_data)
    print("  [OK] Model fit complete")
    return model


def generate_future_regressors(
    last_training_date: pd.Timestamp,
    forecast_months: int = 12,
    assume_conflict_continues: bool = False,
    assume_fuel_continues: bool = False,
) -> pd.DataFrame:
    """
    Build the future regressor frame for forecasting.

    Prophet requires every regressor's future value to be specified.
    All shocks default to 0 (assume disruption ends). Set assume_*
    flags to True to keep specific shocks active for stress scenarios.
    """
    start = last_training_date + pd.DateOffset(months=1)
    future_dates = pd.date_range(start=start, periods=forecast_months, freq="MS")
    future = pd.DataFrame({"ds": future_dates})

    future["is_covid"] = 0
    future["is_fuel_supply_disruption"] = 1 if assume_fuel_continues else 0
    future["is_regional_conflict"] = 1 if assume_conflict_continues else 0

    return future


def forecast(
    model: Prophet,
    training_data: pd.DataFrame,
    forecast_months: int = 12,
    assume_conflict_continues: bool = False,
    assume_fuel_continues: bool = False,
) -> pd.DataFrame:
    """
    Generate a forecast from a fitted Prophet model.
    
    Returns Prophet's standard forecast DataFrame with:
      - ds: date
      - yhat: point forecast
      - yhat_lower, yhat_upper: 80% prediction interval bounds
      - trend, yearly, multiplicative_terms: decomposition components
      - Each regressor's contribution
    """
    last_date = training_data["ds"].max()
    future_regressors = generate_future_regressors(
        last_training_date=last_date,
        forecast_months=forecast_months,
        assume_conflict_continues=assume_conflict_continues,
        assume_fuel_continues=assume_fuel_continues,
    )

    # Prophet wants historical + future concatenated for prediction
    historical_regressors = training_data[["ds"] + REGRESSOR_COLS].copy()
    future_full = pd.concat([historical_regressors, future_regressors], ignore_index=True)

    forecast_df = model.predict(future_full)

    # Clip predictions to valid LF range [0, 1]
    forecast_df["yhat"] = forecast_df["yhat"].clip(0.05, 0.99)
    forecast_df["yhat_lower"] = forecast_df["yhat_lower"].clip(0.05, 0.99)
    forecast_df["yhat_upper"] = forecast_df["yhat_upper"].clip(0.05, 0.99)

    return forecast_df


def extract_regressor_effects(model: Prophet, forecast_df: pd.DataFrame) -> dict:
    """
    Pull out the effect of each regressor WHEN IT IS ACTIVE.
    
    Bug fix: previously averaged across all months (including months where
    the regressor was 0), which diluted real effects by a factor of N.
    
    Correct approach: average the regressor's contribution ONLY in months
    where its indicator value was 1.
    
    Returns the multiplicative effect (in percentage points) that the regressor
    adds when active. E.g., +5% means "load factor is 5% higher in this month
    than the baseline trend + seasonality would predict."
    """
    effects = {}
    historical = forecast_df.copy()

    for col in REGRESSOR_COLS:
        if col not in historical.columns:
            continue

        # The regressor's contribution column appears in Prophet's output
        # when add_regressor() was called. In multiplicative mode it's a
        # proportional effect already.
        contrib_col = col  # Prophet stores it under the same name

        # Active months only (where indicator = 1)
        # Look up the binary indicator from the original training data alignment
        # via the model's history (Prophet stores this internally)
        train_history = model.history.copy()
        active_dates = train_history[train_history[col] == 1]["ds"].tolist()

        # Filter forecast to active dates and extract the contribution
        active_mask = historical["ds"].isin(active_dates)
        if active_mask.sum() == 0:
            effects[col] = {"mean_contribution": 0.0, "interpretation_pct": 0.0, "n_active": 0}
            continue

        active_contributions = historical.loc[active_mask, contrib_col]
        mean_effect = float(active_contributions.mean())

        effects[col] = {
            "mean_contribution": mean_effect,
            "interpretation_pct": mean_effect * 100,
            "n_active": int(active_mask.sum()),
        }

    return effects


def get_forecast_summary(forecast_df: pd.DataFrame, n_historical: int) -> dict:
    """
    Summarise the forecast in plain numbers.
    n_historical = number of rows that are historical (not forecast)
    """
    forecast_only = forecast_df.iloc[n_historical:].copy()

    return {
        "forecast_months": len(forecast_only),
        "mean_forecast_lf": float(forecast_only["yhat"].mean()),
        "min_forecast_lf": float(forecast_only["yhat"].min()),
        "max_forecast_lf": float(forecast_only["yhat"].max()),
        "interval_width_avg": float(
            (forecast_only["yhat_upper"] - forecast_only["yhat_lower"]).mean()
        ),
        "first_forecast_month": str(forecast_only["ds"].iloc[0].date()),
        "last_forecast_month": str(forecast_only["ds"].iloc[-1].date()),
    }


def train_and_forecast(
    forecast_months: int = 12,
    assume_conflict_continues: bool = False,
    assume_fuel_continues: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Main entry point: load data, fit, forecast, return everything needed downstream.
    
    Returns dict with:
      - model: fitted Prophet object
      - training_data: the historical DataFrame
      - forecast: full forecast DataFrame (historical + future)
      - summary: scalar summary of forecast
      - regressor_effects: learned coefficients
    """
    if verbose:
        print("\n  ── Prophet Model Training ──")

    training_data = get_prophet_training_data(include_regressors=True)
    model = build_prophet_model()
    model = fit_model(model, training_data)

    forecast_df = forecast(
        model,
        training_data,
        forecast_months=forecast_months,
        assume_conflict_continues=assume_conflict_continues,
        assume_fuel_continues=assume_fuel_continues,
    )

    summary = get_forecast_summary(forecast_df, n_historical=len(training_data))
    effects = extract_regressor_effects(model, forecast_df)

    if verbose:
        print(f"\n  ── Forecast Summary ──")
        print(f"  Forecast horizon: {summary['forecast_months']} months "
              f"({summary['first_forecast_month']} → {summary['last_forecast_month']})")
        print(f"  Mean forecast LF: {summary['mean_forecast_lf']:.3f}")
        print(f"  Range: [{summary['min_forecast_lf']:.3f}, {summary['max_forecast_lf']:.3f}]")
        print(f"  Avg 80% interval width: ±{summary['interval_width_avg']/2:.3f}")

        print(f"\n  ── Regressor Effects (effect when active) ──")
        for name, eff in effects.items():
            print(f"  {name:30s}: {eff['interpretation_pct']:+6.2f}%  "
                  f"(active in {eff['n_active']} months)")

    return {
        "model": model,
        "training_data": training_data,
        "forecast": forecast_df,
        "summary": summary,
        "regressor_effects": effects,
    }


if __name__ == "__main__":
    result = train_and_forecast(
        forecast_months=12,
        assume_conflict_continues=False,
        assume_fuel_continues=False,
        verbose=True,
    )

    # Show the forecast vs historical for the last 18 months
    f = result["forecast"]
    n_hist = len(result["training_data"])

    print("\n  ── Last 6 historical + all forecast months ──")
    print(f"  {'Date':<12} {'Forecast':>10} {'Lower 80%':>10} {'Upper 80%':>10} {'Actual':>10}")
    print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    # Last 6 historical
    for i in range(n_hist - 6, n_hist):
        row = f.iloc[i]
        actual = result["training_data"].iloc[i]["y"]
        print(f"  {str(row['ds'].date()):<12} {row['yhat']:>10.3f} "
              f"{row['yhat_lower']:>10.3f} {row['yhat_upper']:>10.3f} {actual:>10.3f}")

    # All forecast months
    for i in range(n_hist, len(f)):
        row = f.iloc[i]
        print(f"  {str(row['ds'].date()):<12} {row['yhat']:>10.3f} "
              f"{row['yhat_lower']:>10.3f} {row['yhat_upper']:>10.3f} {'—':>10}")