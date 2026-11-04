"""
UiPath Robot Capacity & Gap Analysis — free_slots_v2.py
========================================================

Identify idle "free slots" between consecutive UiPath jobs on the same robot.

Pipeline overview
-----------------
1. Load thresholds from ``Config.xlsx`` (``Settings`` sheet) with safe defaults.
2. Load raw ``Jobs.csv`` and clean: coerce datetimes, drop rows missing
   ``robotName`` / ``jobStart`` / ``jobEnd``.
3. Keep only the latest ``LOOKBACK_MONTHS`` window anchored at ``max(jobStart)``.
4. For each robot, sort by ``jobStart`` and compute the gap to the *next* job
   (``NextJobStart - jobEnd``) with a grouped ``shift(-1)``.
5. Keep gaps in ``[MIN_GAP_MINUTES, MAX_GAP_HOURS]`` and drop short post-failure
   retry windows (failed state + gap <= ``RETRY_WINDOW_MINUTES``).
6. Classify each surviving gap as ``Available`` / ``Reserved_Same_Process`` /
   ``Post_Error_Window`` and write ``output/Free_Slots_Analysis.csv`` plus
   three matplotlib/seaborn charts.

Every threshold, filter and adjustment is mirrored in the generated
``output/ASSUMPTIONS.md`` (see :func:`write_assumptions_log`) so a reviewer
can reproduce the result without reading the code.

Usage
-----
    python free_slots_v2.py [--config Config.xlsx] [--input Jobs.csv] [--output output]

Inputs
------
- ``Jobs.csv`` — UiPath job export (see README for schema).
- ``Config.xlsx`` — ``Settings`` sheet with Name/Value rows.

Outputs
-------
- ``output/Free_Slots_Analysis.csv``
- ``output/robot_capacity_breakdown.png``
- ``output/gap_frequency_heatmap.png``
- ``output/gap_duration_distribution.png``
- ``output/ASSUMPTIONS.md``
- ``output/analysis.log``
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless-safe (Streamlit Cloud / CI)
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults — keep in sync with Config.xlsx (Settings sheet)
# ---------------------------------------------------------------------------
DEFAULT_MIN_GAP_MINUTES = 30     # A1: shorter windows are not schedulable
DEFAULT_MAX_GAP_HOURS = 12       # A2: exclude overnight / idle gaps
DEFAULT_RETRY_WINDOW_MINUTES = 35  # A3: failed→retry restart window
DEFAULT_LOOKBACK_MONTHS = 1      # A5: anchor at max(jobStart), not calendar month

FAILED_STATES = {"Failed", "Canceled", "Faulted", "Stopped"}  # A3

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "Config.xlsx"
CSV_PATH = BASE_DIR / "Jobs.csv"
OUTPUT_DIR = BASE_DIR / "output"


@dataclass(frozen=True)
class GapConfig:
    """Typed thresholds loaded from Config.xlsx."""

    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES
    max_gap_hours: int = DEFAULT_MAX_GAP_HOURS
    retry_window_minutes: int = DEFAULT_RETRY_WINDOW_MINUTES
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def format_duration(td: timedelta) -> str:
    """Convert a ``timedelta`` to a human-readable string.

    Examples: ``34 secs``, ``39 min 8 sec``, ``12 hrs 33 mins``.
    ``NaT`` / non-timedelta values render as ``"0 secs"``.
    """
    if pd.isna(td) or not isinstance(td, timedelta):
        return "0 secs"

    total_seconds = int(td.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds} secs"
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes} min {seconds} sec" if seconds else f"{minutes} min"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{hours} hrs {minutes} mins" if minutes else f"{hours} hrs"


def load_config(path: str | Path) -> GapConfig:
    """Load thresholds from Config.xlsx ``Settings`` sheet.

    Falls back to documented defaults when the file or a key is missing,
    and logs a warning so the fallback is never silent (Assumptions A1–A5).
    """
    logger = logging.getLogger(__name__)
    try:
        cfg_df = pd.read_excel(path, sheet_name="Settings", dtype=str)
        cfg_df["Name"] = cfg_df["Name"].astype(str).str.strip()
        cfg_df["Value"] = cfg_df["Value"].astype(str).str.strip()
        cfg = cfg_df.set_index("Name")["Value"].to_dict()
    except Exception as exc:  # pragma: no cover - exercised in CI with missing file
        logger.warning("Config load failed (%s) — using defaults", exc)
        return GapConfig()

    def _int(key: str, default: int) -> int:
        try:
            return int(float(cfg.get(key, default)))
        except (TypeError, ValueError):
            logger.warning("Invalid %s=%r — using %d", key, cfg.get(key), default)
            return default

    return GapConfig(
        min_gap_minutes=_int("MIN_GAP_MINUTES", DEFAULT_MIN_GAP_MINUTES),
        max_gap_hours=_int("MAX_GAP_HOURS", DEFAULT_MAX_GAP_HOURS),
        retry_window_minutes=_int("RETRY_WINDOW_MINUTES", DEFAULT_RETRY_WINDOW_MINUTES),
        lookback_months=_int("LOOKBACK_MONTHS", DEFAULT_LOOKBACK_MONTHS),
    )


def setup_logging(output_dir: Path) -> logging.Logger:
    """Configure file + console logging and return the module logger."""
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(output_dir / "analysis.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data cleaning & reshaping (pandas)
# ---------------------------------------------------------------------------
def clean_jobs(df: pd.DataFrame) -> pd.DataFrame:
    """Parse datetimes and drop rows missing the key fields (A6).

    Returns a new DataFrame; the caller's frame is never mutated.
    """
    df = df.copy()
    df["jobStart"] = pd.to_datetime(df["jobStart"], errors="coerce")
    df["jobEnd"] = pd.to_datetime(df["jobEnd"], errors="coerce")
    return df.dropna(subset=["robotName", "jobStart", "jobEnd"])


def compute_gaps(df: pd.DataFrame, cfg: GapConfig) -> pd.DataFrame:
    """Apply lookback, next-job shift, duration and retry filters."""
    df = df.sort_values(["robotName", "jobStart"]).copy()

    # Lookback window anchored at the newest jobStart (A5).
    cutoff = df["jobStart"].max() - pd.DateOffset(months=cfg.lookback_months)
    df = df[df["jobStart"] >= cutoff]

    # Next job per robot (A6: last job per robot has no gap and is dropped).
    grouped = df.groupby("robotName", sort=False)
    df["NextJobStart"] = grouped["jobStart"].shift(-1)
    df["NextProcess"] = grouped["processName"].shift(-1)
    df["GapDuration"] = df["NextJobStart"] - df["jobEnd"]

    df = df.dropna(subset=["NextJobStart"])
    df = df[df["GapDuration"] >= timedelta(minutes=cfg.min_gap_minutes)]
    df = df[df["GapDuration"] <= timedelta(hours=cfg.max_gap_hours)]

    # Drop short retry restarts after failure/cancel/fault/stop (A3).
    is_retry = df["jobState"].isin(FAILED_STATES) & (
        df["GapDuration"] <= timedelta(minutes=cfg.retry_window_minutes)
    )
    return df[~is_retry]


def classify_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``Gap_Type`` / ``Safe_For_New_Process`` columns (A4)."""
    df = df.copy()
    # Vectorised equivalent of row-wise classify_gap():
    #   same process as next job -> Reserved_Same_Process
    #   else if current job failed/cancelled/... -> Post_Error_Window
    #   else -> Available
    df["Gap_Type"] = "Available"
    df.loc[df["jobState"].isin(FAILED_STATES), "Gap_Type"] = "Post_Error_Window"
    df.loc[df["processName"] == df["NextProcess"], "Gap_Type"] = "Reserved_Same_Process"
    df["Safe_For_New_Process"] = df["Gap_Type"] == "Available"
    return df


def format_outputs(df: pd.DataFrame) -> pd.DataFrame:
    """Project to the stable 12-column reporting schema."""
    df = df.copy()
    df["Date"] = df["jobEnd"].dt.date
    df["WeekDay"] = df["jobEnd"].dt.day_name()
    df["Busy_Duration_Formatted"] = (df["jobEnd"] - df["jobStart"]).map(format_duration)
    df["Gap_Duration_Formatted"] = df["GapDuration"].map(format_duration)
    df["Busy_From"] = df["jobStart"].dt.strftime("%H:%M:%S")
    df["Busy_To"] = df["jobEnd"].dt.strftime("%H:%M:%S")
    df["Gap_From"] = df["jobEnd"].dt.strftime("%H:%M:%S")
    df["Gap_To"] = df["NextJobStart"].dt.strftime("%H:%M:%S")
    df["Process_Sequence"] = (
        df["processName"].astype(str) + " → " + df["NextProcess"].astype(str)
    )

    final_cols = [
        "robotName",
        "Date",
        "WeekDay",
        "Busy_Duration_Formatted",
        "Busy_From",
        "Busy_To",
        "Process_Sequence",
        "Gap_Duration_Formatted",
        "Gap_From",
        "Gap_To",
        "Gap_Type",
        "Safe_For_New_Process",
    ]
    output_df = df[final_cols].copy()
    output_df.rename(
        columns={
            "Busy_Duration_Formatted": "Busy Duration",
            "Gap_Duration_Formatted": "Gap Duration",
        },
        inplace=True,
    )
    return output_df


# ---------------------------------------------------------------------------
# Visualisations (never mutate the caller's DataFrame)
# ---------------------------------------------------------------------------
def plot_capacity_by_robot(df: pd.DataFrame, output_path: str | Path) -> None:
    """Stacked horizontal bar: total free hours per robot by Gap_Type."""
    if df.empty:
        return
    work = df.copy()
    work["Gap_Hours"] = work["GapDuration"].dt.total_seconds() / 3600
    summary = (
        work.groupby(["robotName", "Gap_Type"])["Gap_Hours"].sum().unstack(fill_value=0)
    )
    if summary.empty:
        return

    fig, ax = plt.subplots(figsize=(14, 8))
    summary.plot(
        kind="barh",
        stacked=True,
        ax=ax,
        color={
            "Available": "#2ecc71",
            "Reserved_Same_Process": "#f39c12",
            "Post_Error_Window": "#e74c3c",
        },
    )
    ax.legend(title="Gap Type", bbox_to_anchor=(1.05, 1), loc="upper left")
    ax.set_xlabel("Total Free Hours")
    ax.set_title("Robot Capacity Breakdown (Last Month)")
    ax.grid(axis="x", alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_hourly_heatmap(df: pd.DataFrame, output_path: str | Path) -> None:
    """Heatmap: gap frequency by hour-of-day (Gap_From) and robot."""
    if df.empty:
        return
    work = df.copy()
    work["Gap_Hour"] = work["jobEnd"].dt.hour  # == Gap_From hour
    heatmap_data = work.groupby(["robotName", "Gap_Hour"]).size().unstack(fill_value=0)
    if heatmap_data.empty:
        return

    plt.figure(figsize=(14, 8))
    sns.heatmap(heatmap_data, cmap="YlOrRd", cbar_kws={"label": "Number of Gaps"})
    plt.xlabel("Hour of Day (0-23)")
    plt.ylabel("Robot Name")
    plt.title("Gap Frequency by Hour & Robot")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


def plot_gap_duration_hist(
    df: pd.DataFrame, output_path: str | Path, cfg: GapConfig
) -> None:
    """Histogram of gap durations with the configured thresholds marked."""
    if df.empty:
        return
    gap_mins = df["GapDuration"].dt.total_seconds() / 60
    plt.figure(figsize=(10, 6))
    plt.hist(gap_mins, bins=30, edgecolor="black", color="#3498db", alpha=0.7)
    plt.axvline(
        x=cfg.min_gap_minutes,
        color="#e74c3c",
        linestyle="--",
        label=f"Min Threshold ({cfg.min_gap_minutes} min)",
    )
    plt.axvline(
        x=cfg.max_gap_hours * 60,
        color="#9b59b6",
        linestyle="--",
        label=f"Max Threshold ({cfg.max_gap_hours} hrs)",
    )
    plt.xlabel("Gap Duration (minutes)")
    plt.ylabel("Frequency")
    plt.title("Distribution of Free Slot Durations")
    plt.legend()
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()


# ---------------------------------------------------------------------------
# Assumptions & Adjustments Log (hiring-manager requirement)
# ---------------------------------------------------------------------------
def write_assumptions_log(
    output_dir: str | Path,
    cfg: GapConfig,
    stats: dict,
    input_rows: int,
) -> Path:
    """Write ``ASSUMPTIONS.md`` documenting every source, rule and adjustment.

    Parameters
    ----------
    output_dir : directory for the markdown file.
    cfg        : thresholds actually used (single source of truth).
    stats      : row counts at each stage (from :func:`analyze_slots`).
    input_rows : rows in the raw CSV before any filtering.

    Returns
    -------
    Path to the written file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown = f"""# Assumptions & Adjustments Log

Generated by `free_slots_v2.py` — every rule below is applied in code; this
file exists so the analysis can be checked without reading the source.

| ID | Assumption / adjustment | Value | Source / why | Impact |
|----|------------------------|-------|--------------|--------|
| A1 | Minimum schedulable gap | {cfg.min_gap_minutes} min | `MIN_GAP_MINUTES` in Config.xlsx — shorter windows are restart/setup noise | Gaps < {cfg.min_gap_minutes} min excluded |
| A2 | Maximum gap (overnight/idle cutoff) | {cfg.max_gap_hours} h | `MAX_GAP_HOURS` — longer idle time is not a usable slot | Gaps > {cfg.max_gap_hours} h excluded |
| A3 | Post-failure retry window | {cfg.retry_window_minutes} min | `RETRY_WINDOW_MINUTES` — `Failed/Canceled/Faulted/Stopped` jobs restarting within this window are retries, not free capacity | Short retry gaps dropped |
| A4 | Gap classification | same `processName` → `Reserved_Same_Process`; else failed state → `Post_Error_Window`; else `Available` | Domain rule: same-process successor means queued work; only `Available` is safe for a new process | Drives Available vs Reserved KPIs |
| A5 | Lookback window | last {cfg.lookback_months} month(s) from `max(jobStart)` | `LOOKBACK_MONTHS` — anchored at data, not calendar month, so re-runs are reproducible | Old jobs excluded |
| A6 | Invalid / overlapping rows | drop missing `robotName`/`jobStart`/`jobEnd`; drop last job per robot (no successor) | Data-quality rule — unparseable dates cannot form a gap | Rows removed before gap calc |
| A7 | Date attribution | gap dated by `jobEnd` (start of idle window) | Keeps `Date`/`WeekDay`/`Gap_From` consistent | Cross-midnight gaps counted on start date |

## Row-count audit

| Stage | Rows |
|-------|------|
| Raw `Jobs.csv` | {input_rows:,} |
| After datetime parse + dropna | {stats.get('after_clean', 0):,} |
| After lookback filter | {stats.get('after_lookback', 0):,} |
| After duration + retry filters | {stats.get('after_filters', 0):,} |
| Final `Free_Slots_Analysis.csv` | {stats.get('final', 0):,} |

## Known limitations

- Cross-midnight gaps are **kept** in this CSV pipeline (unlike the
  `free_slots_from_sqlite.py` variant, which keeps only same-day gaps).
  The 12 h cap bounds their size, but a gap starting at 21:53 still spans
  midnight — see `Gap_From`/`Gap_To` in the output before scheduling.
- `Post_Error_Window` gaps survive only when longer than the retry window;
  they are not flagged `Safe_For_New_Process`.
- Thresholds come from Config.xlsx; when missing, the defaults in the table
  above are used and a warning is logged.
"""
    path = output_dir / "ASSUMPTIONS.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def summarise(output_df: pd.DataFrame) -> dict:
    """Return KPI counts used by the log and the console summary."""
    total = len(output_df)
    available = int((output_df["Gap_Type"] == "Available").sum())
    reserved = int((output_df["Gap_Type"] == "Reserved_Same_Process").sum())
    post_error = int((output_df["Gap_Type"] == "Post_Error_Window").sum())
    return {
        "total": total,
        "available": available,
        "reserved": reserved,
        "post_error": post_error,
        "available_pct": (available / total * 100) if total else 0.0,
        "reserved_pct": (reserved / total * 100) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def analyze_slots(
    config_path: str | Path = CONFIG_PATH,
    csv_path: str | Path = CSV_PATH,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict:
    """Run the full pipeline and return the KPI summary dict.

    Kept as the public entry point for backward compatibility — the original
    script called ``analyze_slots()`` with no arguments.
    """
    output_dir = Path(output_dir)
    logger = setup_logging(output_dir)
    logger.info("🚀 Starting Free Slot Analysis")

    cfg = load_config(config_path)
    logger.info(
        "⚙️ Config: MinGap=%dm, MaxGap=%dh, Retry=%dm, Lookback=%dmo",
        cfg.min_gap_minutes,
        cfg.max_gap_hours,
        cfg.retry_window_minutes,
        cfg.lookback_months,
    )

    if not Path(csv_path).exists():
        logger.error("❌ CSV File not found: %s", csv_path)
        return {}

    raw = pd.read_csv(csv_path)
    input_rows = len(raw)
    logger.info("📄 Loaded %d rows from CSV", input_rows)

    cleaned = clean_jobs(raw)
    logger.info("🧹 After cleaning: %d rows", len(cleaned))

    # Row-count audit for ASSUMPTIONS.md (lookback stage isolated).
    cutoff = cleaned["jobStart"].max() - pd.DateOffset(months=cfg.lookback_months)
    after_lookback = int((cleaned["jobStart"] >= cutoff).sum())

    gaps = compute_gaps(cleaned, cfg)
    if gaps.empty:
        logger.warning("⚠️ No free slots found matching criteria")
        return {}

    classified = classify_gaps(gaps)
    output_df = format_outputs(classified)

    csv_out = output_dir / "Free_Slots_Analysis.csv"
    output_df.to_csv(csv_out, index=False)
    logger.info("✅ CSV saved: %s (%d rows)", csv_out, len(output_df))

    logger.info("📊 Generating visualizations...")
    plot_capacity_by_robot(classified, output_dir / "robot_capacity_breakdown.png")
    plot_hourly_heatmap(classified, output_dir / "gap_frequency_heatmap.png")
    plot_gap_duration_hist(classified, output_dir / "gap_duration_distribution.png", cfg)
    logger.info("   → Saved 3 charts")

    kpis = summarise(output_df)
    stats = {
        "after_clean": len(cleaned),
        "after_lookback": after_lookback,
        "after_filters": len(classified),
        "final": len(output_df),
    }
    assumptions_path = write_assumptions_log(output_dir, cfg, stats, input_rows)
    logger.info("📝 Assumptions log: %s", assumptions_path)

    logger.info(
        "\n📈 SUMMARY:\n"
        "   Total Gaps Found: %d\n"
        "   ✓ Available for New Processes: %d (%.1f%%)\n"
        "   ⚠ Reserved (Same Process): %d (%.1f%%)\n"
        "   🛠 Post_Error_Window: %d\n"
        "   📁 Outputs saved to: %s/",
        kpis["total"],
        kpis["available"],
        kpis["available_pct"],
        kpis["reserved"],
        kpis["reserved_pct"],
        kpis["post_error"],
        output_dir,
    )
    return kpis


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="UiPath Free Slot Analysis")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Config.xlsx path")
    parser.add_argument("--input", default=str(CSV_PATH), help="Jobs.csv path")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="Output directory")
    args = parser.parse_args()
    analyze_slots(args.config, args.input, args.output)


if __name__ == "__main__":
    main()
