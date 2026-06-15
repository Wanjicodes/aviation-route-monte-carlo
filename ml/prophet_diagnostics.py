"""
prophet_diagnostics.py
──────────────────────
Validation and diagnostic tools for the Prophet model.

Provides:
- Rolling-origin cross-validation (Prophet's built-in CV)
- Performance metrics: MAPE, RMSE, coverage of prediction intervals
- Component decomposition: trend, seasonality, regressor contributions
- Residual analysis: autocorrelation, normality, heteroscedasticity

These functions answer the senior DS interview question:
  "How do you know your model is any good?"
"""

import warnings
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend (saves to file, no popup window)
import matplotlib.pyplot as plt
from pathlib import Path
from prophet import Prophet
from prophet.diagnostics import cross_validation, performance_metrics

warnings.filterwarnings("ignore", category=FutureWarning)
import logging
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)


OUTPUTS_DIR = Path(__file__).parent.parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)


def run_cross_validation(
    model: Prophet,
    training_data: pd.DataFrame,
    initial_months: int = 96,        # Was 84 — now 8 years
    period_months: int = 6,           # Was 12 — now 6 months between cutoffs
    horizon_months: int = 6,          # Was 12 — now 6-month forecasts
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Rolling-origin cross-validation.

    Parameters
    ----------
    model : a fitted Prophet model
    training_data : the full historical data
    initial_months : minimum training window size (5 years here)
    period_months : step size between cutoffs (1 year apart)
    horizon_months : forecast horizon at each cutoff

    Returns
    -------
    DataFrame with columns: ds, yhat, yhat_lower, yhat_upper, y, cutoff
    """
    if verbose:
        print("\n  ── Cross-Validation ──")
        print(f"  Initial training window: {initial_months} months")
        print(f"  Step between cutoffs:    {period_months} months")
        print(f"  Forecast horizon:        {horizon_months} months")

    # Prophet's CV expects horizon as a pandas-compatible duration string
    # 30.44 days per month average (365.25 / 12)
    cv_results = cross_validation(
        model,
        initial=f"{initial_months * 30} days",
        period=f"{period_months * 30} days",
        horizon=f"{horizon_months * 30} days",
        parallel=None,  # Single-process multi-process can be unreliable on Windows
        disable_tqdm=True,
    )

    if verbose:
        n_cutoffs = cv_results["cutoff"].nunique()
        n_forecasts = len(cv_results)
        print(f"  Cutoffs tested:          {n_cutoffs}")
        print(f"  Total forecasts made:    {n_forecasts}")

    return cv_results


def compute_metrics(cv_results: pd.DataFrame) -> dict:
    """
    Compute key performance metrics from CV results.

    Returns
    -------
    dict with MAPE, RMSE, MAE, coverage, and metrics by horizon
    """
    actual = cv_results["y"].values
    predicted = cv_results["yhat"].values
    lower = cv_results["yhat_lower"].values
    upper = cv_results["yhat_upper"].values

    # Core error metrics
    errors = actual - predicted
    mae = np.mean(np.abs(errors))
    rmse = np.sqrt(np.mean(errors ** 2))
    mape = np.mean(np.abs(errors / actual)) * 100

    # Coverage: % of actuals that fall within the 80% prediction interval
    in_interval = (actual >= lower) & (actual <= upper)
    coverage = in_interval.mean() * 100

    # Bias: are predictions systematically high or low?
    bias = np.mean(errors)

    return {
        "mae": float(mae),
        "rmse": float(rmse),
        "mape_pct": float(mape),
        "coverage_pct": float(coverage),
        "expected_coverage_pct": 80.0,
        "bias": float(bias),
        "n_forecasts": int(len(cv_results)),
    }


def metrics_by_horizon(cv_results: pd.DataFrame) -> pd.DataFrame:
    """
    Performance metrics broken down by forecast horizon.
    Forecasts further out should be less accurate — this lets you see by how much.
    """
    df = cv_results.copy()
    df["horizon_days"] = (df["ds"] - df["cutoff"]).dt.days
    df["horizon_months"] = (df["horizon_days"] / 30).round().astype(int)
    df["abs_error"] = (df["y"] - df["yhat"]).abs()
    df["pct_error"] = (df["abs_error"] / df["y"]) * 100
    df["squared_error"] = (df["y"] - df["yhat"]) ** 2

    summary = df.groupby("horizon_months").agg(
        n=("abs_error", "count"),
        mae=("abs_error", "mean"),
        mape_pct=("pct_error", "mean"),
        rmse=("squared_error", lambda x: np.sqrt(x.mean())),
    ).round(4)

    return summary.reset_index()


def plot_cross_validation(cv_results: pd.DataFrame, save_path: Path = None) -> Path:
    """Plot actual vs predicted across all CV folds."""
    save_path = save_path or OUTPUTS_DIR / "cv_actual_vs_predicted.png"

    fig, ax = plt.subplots(figsize=(11, 5))

    cutoffs = sorted(cv_results["cutoff"].unique())
    colors = plt.cm.viridis(np.linspace(0, 1, len(cutoffs)))

    for cutoff, color in zip(cutoffs, colors):
        sub = cv_results[cv_results["cutoff"] == cutoff].sort_values("ds")
        ax.plot(sub["ds"], sub["yhat"], color=color, alpha=0.7, lw=1.2,
                label=f"Cutoff {pd.Timestamp(cutoff).date()}")
        ax.fill_between(sub["ds"], sub["yhat_lower"], sub["yhat_upper"],
                        color=color, alpha=0.08)

    # Actual values
    actuals = cv_results.drop_duplicates(subset="ds").sort_values("ds")
    ax.plot(actuals["ds"], actuals["y"], "k.", markersize=4, label="Actual")

    first_cutoff = pd.Timestamp(cutoffs[0]).date()
    last_cutoff = pd.Timestamp(cutoffs[-1]).date()
    ax.set_title(
    f"Cross-Validation: {len(cutoffs)} rolling cutoffs ({first_cutoff} to {last_cutoff})\n"
    f"Validation restricted to post-recovery stable periods"
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("Load Factor")
    ax.legend(loc="lower right", fontsize=7, ncol=2, framealpha=0.9, title="Cutoff date")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=120)
    plt.close(fig)
    return save_path


def plot_components(model: Prophet, forecast_df: pd.DataFrame, save_path: Path = None) -> Path:
    """Decomposition plot: trend, yearly seasonality, regressor effects."""
    save_path = save_path or OUTPUTS_DIR / "prophet_components.png"

    fig = model.plot_components(forecast_df, figsize=(10, 8))
    fig.suptitle("Prophet Decomposition: Trend, Seasonality, Regressors", y=1.02)
    plt.tight_layout()
    plt.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return save_path


def residual_analysis(cv_results: pd.DataFrame, save_path: Path = None) -> dict:
    """
    Analyse residuals (forecast errors) for structural patterns.

    Checks:
    - Are residuals normally distributed? (Shapiro-Wilk)
    - Do residuals show autocorrelation? (Durbin-Watson)
    - Are residual magnitudes constant over time? (heteroscedasticity)
    """
    save_path = save_path or OUTPUTS_DIR / "residual_analysis.png"

    residuals = (cv_results["y"] - cv_results["yhat"]).values

    # ── Statistical tests ────────────────────────────────────────────────────
    # Shapiro-Wilk: H0 = residuals are normally distributed
    from scipy.stats import shapiro
    shapiro_stat, shapiro_p = shapiro(residuals)

    # Durbin-Watson: roughly 2 = no autocorrelation, <2 = positive, >2 = negative
    diff = np.diff(residuals)
    dw_stat = (diff ** 2).sum() / (residuals ** 2).sum()

    # ── Visualisation ────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(11, 7))

    # 1. Residuals over time
    axes[0, 0].scatter(cv_results["ds"], residuals, alpha=0.5, s=15)
    axes[0, 0].axhline(0, color="red", linestyle="--", linewidth=1)
    axes[0, 0].set_title("Residuals over time")
    axes[0, 0].set_xlabel("Date")
    axes[0, 0].set_ylabel("Actual - Predicted")
    axes[0, 0].grid(True, alpha=0.3)

    # 2. Residuals histogram with normal overlay
    axes[0, 1].hist(residuals, bins=20, density=True, alpha=0.7, edgecolor="white")
    mu, sigma = residuals.mean(), residuals.std()
    x = np.linspace(residuals.min(), residuals.max(), 100)
    axes[0, 1].plot(x, (1/(sigma * np.sqrt(2 * np.pi))) *
                    np.exp(-0.5 * ((x - mu) / sigma) ** 2), "r-", lw=2, label="Normal fit")
    axes[0, 1].set_title(f"Residual distribution (Shapiro p={shapiro_p:.3f})")
    axes[0, 1].set_xlabel("Residual")
    axes[0, 1].legend()
    axes[0, 1].grid(True, alpha=0.3)

    # 3. Residuals vs predicted (heteroscedasticity check)
    axes[1, 0].scatter(cv_results["yhat"], residuals, alpha=0.5, s=15)
    axes[1, 0].axhline(0, color="red", linestyle="--", linewidth=1)
    axes[1, 0].set_title("Residuals vs predicted (heteroscedasticity check)")
    axes[1, 0].set_xlabel("Predicted value")
    axes[1, 0].set_ylabel("Residual")
    axes[1, 0].grid(True, alpha=0.3)

    # 4. Lag-1 autocorrelation
    axes[1, 1].scatter(residuals[:-1], residuals[1:], alpha=0.5, s=15)
    axes[1, 1].axhline(0, color="red", linestyle="--", linewidth=1)
    axes[1, 1].axvline(0, color="red", linestyle="--", linewidth=1)
    axes[1, 1].set_title(f"Lag-1 autocorrelation (DW={dw_stat:.2f})")
    axes[1, 1].set_xlabel("Residual at t-1")
    axes[1, 1].set_ylabel("Residual at t")
    axes[1, 1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=120)
    plt.close(fig)

    return {
        "shapiro_stat": float(shapiro_stat),
        "shapiro_p": float(shapiro_p),
        "residuals_normal": bool(shapiro_p > 0.05),
        "durbin_watson": float(dw_stat),
        "autocorrelation_detected": bool(dw_stat < 1.5 or dw_stat > 2.5),
        "mean_residual": float(residuals.mean()),
        "std_residual": float(residuals.std()),
    }


def run_full_diagnostics(model: Prophet, training_data: pd.DataFrame, forecast_df: pd.DataFrame) -> dict:
    """
    Run all diagnostic checks and produce report-ready outputs.

    Returns a dict with all metrics, plots saved to outputs/.
    """
    print("\n  ── Running Full Diagnostics ──")

    # Cross-validation
    cv_results = run_cross_validation(
        model, training_data,
        initial_months=96, period_months=6, horizon_months=6,
        verbose=True,
    )

    # Aggregate metrics
    metrics = compute_metrics(cv_results)
    print(f"\n  ── Performance Metrics ──")
    print(f"  MAPE:                    {metrics['mape_pct']:.2f}%")
    print(f"  RMSE:                    {metrics['rmse']:.4f}")
    print(f"  MAE:                     {metrics['mae']:.4f}")
    print(f"  Coverage (80% PI):       {metrics['coverage_pct']:.1f}% "
          f"(expected ~80%)")
    print(f"  Bias:                    {metrics['bias']:+.4f}")
    print(f"  N forecasts evaluated:   {metrics['n_forecasts']}")

    # By horizon
    by_horizon = metrics_by_horizon(cv_results)
    print(f"\n  ── Error by forecast horizon ──")
    print(by_horizon.to_string(index=False))

    # Plots
    cv_plot = plot_cross_validation(cv_results)
    print(f"\n  [OK] CV plot saved: {cv_plot.name}")

    comp_plot = plot_components(model, forecast_df)
    print(f"  [OK] Component decomposition: {comp_plot.name}")

    # Residual analysis
    resid = residual_analysis(cv_results)
    print(f"\n  ── Residual Analysis ──")
    print(f"  Shapiro-Wilk p-value:    {resid['shapiro_p']:.3f} "
          f"({'NORMAL' if resid['residuals_normal'] else 'NON-NORMAL'})")
    print(f"  Durbin-Watson statistic: {resid['durbin_watson']:.2f} "
          f"({'autocorrelated' if resid['autocorrelation_detected'] else 'no autocorrelation'})")
    print(f"  Mean residual:           {resid['mean_residual']:+.4f}")
    print(f"  Std residual:            {resid['std_residual']:.4f}")
    print(f"  [OK] Residual plots saved: residual_analysis.png")

    return {
        "metrics": metrics,
        "metrics_by_horizon": by_horizon.to_dict(orient="records"),
        "residual_analysis": resid,
        "n_cv_cutoffs": int(cv_results["cutoff"].nunique()),
    }


if __name__ == "__main__":
    # Standalone test: load training, fit, run diagnostics
    from ml.prophet_model import train_and_forecast

    result = train_and_forecast(forecast_months=12, verbose=True)
    diag = run_full_diagnostics(
        model=result["model"],
        training_data=result["training_data"],
        forecast_df=result["forecast"],
    )

    print("\n  ── Diagnostics complete ──")
    print(f"  All outputs saved to: outputs/")

    # Diagnostic: which forecasts are worst?
    from ml.prophet_diagnostics import run_cross_validation
    cv_results = run_cross_validation(
        result["model"], result["training_data"],
        initial_months=96, period_months=6, horizon_months=6, verbose=False,
    )
    cv_results["horizon_months"] = ((cv_results["ds"] - cv_results["cutoff"]).dt.days / 30).round().astype(int)
    cv_results["abs_pct_error"] = ((cv_results["y"] - cv_results["yhat"]).abs() / cv_results["y"]) * 100

    print("\n  ── Worst 5 forecasts (highest % error) ──")
    worst = cv_results.nlargest(5, "abs_pct_error")[
        ["cutoff", "ds", "horizon_months", "y", "yhat", "abs_pct_error"]
    ]
    print(worst.to_string(index=False))