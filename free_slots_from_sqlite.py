import os
import sqlite3
import logging
from datetime import timedelta, datetime
import pandas as pd


# =========================
# Config
# =========================
def InitSettings(ConfigPath, SheetName, Key, Value):
    df = pd.read_excel(ConfigPath, sheet_name=SheetName, dtype=str)
    df[Key] = df[Key].astype(str).str.strip()
    df[Value] = df[Value].astype(str).str.strip()
    return df.set_index(Key)[Value].to_dict()


# =========================
# Logging helpers
# =========================
def CreateFormatter(fmt: str):
    return logging.Formatter(fmt)


def CreateLogFileHandler(logFilePath, formatter):
    os.makedirs(os.path.dirname(logFilePath), exist_ok=True)
    fh = logging.FileHandler(logFilePath, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    return fh


def setup_loggers(logFilePath):
    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = CreateFormatter(fmt)
    fileHandler = CreateLogFileHandler(logFilePath, formatter)

    # Clear handlers (avoid duplicate logs)
    for name in ["___main___", "___Init___", "___Processing___"]:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(fileHandler)

    return (
        fileHandler,
        logging.getLogger("___main___"),
        logging.getLogger("___Init___"),
        logging.getLogger("___Processing___"),
    )


# =========================
# SQLite helpers
# =========================
def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,)
    )
    return cur.fetchone() is not None


def format_duration(td):
    minutes = int(td.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours} hr {mins} min" if hours else f"{mins} min"


# =========================
# Main
# =========================
def run_free_slots():
    fileHandler = None
    conn = None

    ConfigPath = r"C:\Users\rpa.user\.spyder-py3\RPA_Alarming\Uipath_Data_Pipeline\Data\Config.xlsx"

    try:
        # 1) Load config
        Config = InitSettings(ConfigPath, "Settings", "Name", "Value")

        # 2) Build log file path
        # expecting in config: logFilePath (folder)
        log_base = Config.get("logFilePath", "").strip()
        if not log_base:
            raise KeyError("Missing config key: logFilePath")

        logFilePath = os.path.join(
            log_base,
            "FREE_SLOTS",
            f"FREE_SLOTS__{datetime.now().strftime('%m-%d-%Y')}.log",
        )

        # 3) Setup loggers
        fileHandler, loggerMain, loggerInit, loggerProcess = setup_loggers(logFilePath)

        loggerMain.info("=======================================")
        loggerMain.info("Free Slots calculation started")
        loggerMain.info("=======================================")

        # 4) Read required configs
        curated_db = Config["CURATED_DB"]
        output_excel = Config["BASE_DATA_LAKE"]
        pbi_Free_Slots = os.path.join(
            output_excel, "outputs", "powerbi", "All_Robots_Free_Slots.xlsx"
        )
        pbi_Free_Slots_shared = (
            r"\\cctfil01\RPA_technical\RPA Power BI\Data\All_Robots_Free_Slots.xlsx"
        )
        if not curated_db:
            raise KeyError("Missing config key: CURATED_DB")
        if not pbi_Free_Slots:
            raise KeyError("Missing config key: FREE_SLOTS_OUTPUT")

        min_gap_minutes = int(Config.get("MIN_GAP_MINUTES", "30"))
        max_gap_hours = int(Config.get("MAX_GAP_HOURS", "12"))
        retry_window_minutes = int(Config.get("RETRY_WINDOW_MINUTES", "35"))
        merge_tolerance_minutes = int(Config.get("MERGE_TOLERANCE_MINUTES", "0"))

        if not os.path.exists(curated_db):
            raise FileNotFoundError(f"SQLite DB not found: {curated_db}")

        os.makedirs(os.path.dirname(pbi_Free_Slots), exist_ok=True)

        # 5) Load data from SQLite
        conn = sqlite3.connect(curated_db)

        if not table_exists(conn, "jobs_all"):
            raise RuntimeError("Table 'jobs_all' does not exist. Run ETL first.")

        df = pd.read_sql(
            """
            SELECT robotName, jobStart, jobEnd, processName, jobState, releaseId
            FROM jobs_all
            WHERE jobStart IS NOT NULL AND jobEnd IS NOT NULL
        """,
            conn,
        )

        loggerProcess.info(f"Rows loaded from SQLite: {len(df)}")

        if df.empty:
            loggerProcess.warning(
                "No data loaded from jobs_all. Output will not be generated."
            )
            return

        # 6) Parse datetimes
        df["jobStart"] = pd.to_datetime(df["jobStart"], errors="coerce")
        df["jobEnd"] = pd.to_datetime(df["jobEnd"], errors="coerce")
        df = df.dropna(subset=["robotName", "jobStart", "jobEnd"])

        if df.empty:
            loggerProcess.warning("After datetime parsing, dataset became empty.")
            return

        # 7) Filter to last full month
        latest_date = df["jobStart"].max()
        last_month = (latest_date.replace(day=1) - pd.DateOffset(days=1)).to_period("M")
        loggerProcess.info(f"Using last full month: {last_month}")
        df = df[df["jobStart"].dt.to_period("M") == last_month]

        if df.empty:
            loggerProcess.warning("No rows in last full month after filtering.")
            return

        # 8) Sort + previous fields
        df = df.sort_values(["robotName", "jobStart"])
        df["PrevEnd"] = df.groupby("robotName")["jobEnd"].shift(1)
        df["PrevProc"] = df.groupby("robotName")["processName"].shift(1)
        df["PrevRelease"] = df.groupby("robotName")["releaseId"].shift(1)
        df["PrevState"] = df.groupby("robotName")["jobState"].shift(1)

        df["Gap"] = df["jobStart"] - df["PrevEnd"]

        # Remove overlaps/negative
        df = df[df["Gap"].isna() | (df["Gap"] > timedelta(seconds=0))]

        retry_window = timedelta(minutes=retry_window_minutes)
        FAILED_STATES = {"Failed", "Canceled", "Faulted", "Stopped"}

        df["IsRetryGap"] = (
            df["Gap"].notna()
            & (df["Gap"] <= retry_window)
            & (
                df["PrevState"].isin(FAILED_STATES)
                | (df["releaseId"] == df["PrevRelease"])
            )
        )

        # 9) Segments
        df["NewSegment"] = (~df["IsRetryGap"]).astype(int)
        df["SegmentId"] = df.groupby("robotName")["NewSegment"].cumsum()

        seg = df.groupby(["robotName", "SegmentId"], as_index=False).agg(
            SegmentStart=("jobStart", "min"),
            SegmentEnd=("jobEnd", "max"),
            SegmentProcess=(
                "processName",
                lambda s: s.mode().iat[0] if not s.mode().empty else s.iloc[0],
            ),
        )

        # 10) Free time between segments
        seg = seg.sort_values(["robotName", "SegmentStart"])
        seg["PrevSegEnd"] = seg.groupby("robotName")["SegmentEnd"].shift(1)
        seg["PrevSegProc"] = seg.groupby("robotName")["SegmentProcess"].shift(1)
        seg["FreeDuration"] = seg["SegmentStart"] - seg["PrevSegEnd"]

        seg = seg.dropna(subset=["PrevSegEnd"])
        seg = seg[seg["FreeDuration"] > timedelta(minutes=min_gap_minutes)]
        seg = seg[seg["FreeDuration"] < timedelta(hours=max_gap_hours)]
        seg = seg[seg["PrevSegEnd"].dt.date == seg["SegmentStart"].dt.date]

        if seg.empty:
            loggerProcess.warning("No free slots found after filtering rules.")
            return

        # 11) Merge overlaps
        seg["FreeFromDT"] = seg["PrevSegEnd"]
        seg["FreeToDT"] = seg["SegmentStart"]
        seg["Date"] = seg["FreeFromDT"].dt.date
        seg["GapKey"] = (
            seg["PrevSegProc"].astype(str) + " → " + seg["SegmentProcess"].astype(str)
        )

        seg = seg.sort_values(["robotName", "Date", "GapKey", "FreeFromDT", "FreeToDT"])

        tolerance = timedelta(minutes=merge_tolerance_minutes)
        merged_rows = []

        for (robot, day, gapkey), g in seg.groupby(
            ["robotName", "Date", "GapKey"], sort=False
        ):
            starts = g["FreeFromDT"].tolist()
            ends = g["FreeToDT"].tolist()

            cur_start, cur_end = starts[0], ends[0]

            for s, e in zip(starts[1:], ends[1:]):
                if s <= (cur_end + tolerance):
                    if e > cur_end:
                        cur_end = e
                else:
                    merged_rows.append((robot, day, gapkey, cur_start, cur_end))
                    cur_start, cur_end = s, e

            merged_rows.append((robot, day, gapkey, cur_start, cur_end))

        merged = pd.DataFrame(
            merged_rows,
            columns=["robotName", "Date", "ProcessGap", "FreeFromDT", "FreeToDT"],
        )
        merged["FreeDuration"] = merged["FreeToDT"] - merged["FreeFromDT"]
        merged["Weekday"] = pd.to_datetime(merged["FreeFromDT"]).dt.day_name()
        merged["FreeFrom"] = merged["FreeFromDT"].dt.strftime("%H:%M")
        merged["FreeTo"] = merged["FreeToDT"].dt.strftime("%H:%M")
        merged["FreeDurationFormatted"] = merged["FreeDuration"].apply(format_duration)

        merged = (
            merged.sort_values("FreeDuration")
            .groupby(["robotName", "Date"], as_index=False)
            .tail(1)
        )

        out_cols = [
            "robotName",
            "Weekday",
            "Date",
            "FreeFrom",
            "FreeTo",
            "FreeDurationFormatted",
            "ProcessGap",
        ]
        merged[out_cols].to_excel(pbi_Free_Slots, index=False)
        merged[out_cols].to_excel(pbi_Free_Slots_shared, index=False)

        loggerProcess.info(f"Output saved: {pbi_Free_Slots} | rows={len(merged)}")
        loggerProcess.info(
            f"Output saved: {pbi_Free_Slots_shared} | rows={len(merged)}"
        )

        loggerMain.info("Free Slots calculation finished ✅")

    except Exception as e:
        # Make sure errors go to log
        if "loggerMain" in locals():
            loggerMain.exception(f"Free Slots failed: {e}")
        else:
            print("Free Slots failed:", e)

    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

        try:
            if fileHandler:
                fileHandler.close()
        except Exception:
            pass

        logging.shutdown()


if __name__ == "__main__":
    run_free_slots()
