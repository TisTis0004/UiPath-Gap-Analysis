"""
Build an Excel dashboard from Free_Slots_Analysis.csv.

Sheets
------
- KPI_Summary : headline KPIs with live formulas (COUNTIFS / XLOOKUP demos)
- Pivots      : robot x Gap_Type and WeekDay x Gap_Type pivot-value tables
- Raw_Data    : full 4,077-row table with filters + conditional formatting
- Charts      : bar chart of gaps per robot by type (XlsxWriter native chart)

Requires: pandas, XlsxWriter (pip install pandas XlsxWriter openpyxl)

Usage: python build_excel_dashboard.py [--input output/Free_Slots_Analysis.csv]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import xlsxwriter

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "output" / "Free_Slots_Analysis.csv"
DEFAULT_OUTPUT = BASE_DIR / "output" / "Free_Slots_Dashboard.xlsx"


def add_table(writer: xlsxwriter.Workbook, sheet, df: pd.DataFrame, name: str) -> None:
    """Write df as a formatted Excel Table starting at A1."""
    sheet.write_row(0, 0, list(df.columns))
    for r, row in enumerate(df.itertuples(index=False), start=1):
        sheet.write_row(r, 0, list(row))
    sheet.add_table(
        0, 0, len(df), max(len(df.columns) - 1, 0),
        {"columns": [{"header": c} for c in df.columns], "name": name},
    )  # NOTE: Table already provides autofilter; do not call sheet.autofilter()


def build(input_csv: str | Path = DEFAULT_INPUT,
          output_xlsx: str | Path = DEFAULT_OUTPUT) -> Path:
    """Build the workbook and return its path."""
    input_csv, output_xlsx = Path(input_csv), Path(output_xlsx)
    df = pd.read_csv(input_csv)
    n = len(df)
    available = int((df["Gap_Type"] == "Available").sum())
    reserved = int((df["Gap_Type"] == "Reserved_Same_Process").sum())
    post_error = int((df["Gap_Type"] == "Post_Error_Window").sum())
    robots = int(df["robotName"].nunique())

    df = df.copy()
    df["__one"] = 1  # numeric helper so pivot_table values= is 1-D
    pivot_robot = pd.pivot_table(
        df, index="robotName", columns="Gap_Type", values="__one",
        aggfunc="count", fill_value=0,
    ).reset_index()
    pivot_weekday = pd.pivot_table(
        df, index="WeekDay", columns="Gap_Type", values="__one",
        aggfunc="count", fill_value=0,
    ).reset_index()
    df = df.drop(columns="__one")

    wb = xlsxwriter.Workbook(str(output_xlsx))
    fmt_title = wb.add_format({"bold": True, "font_size": 14})
    fmt_label = wb.add_format({"bold": True, "bg_color": "#D9E1F2"})
    fmt_kpi = wb.add_format({"bold": True, "font_size": 12, "bg_color": "#FFF2CC"})

    # --- KPI_Summary (live-formula KPIs) -----------------------------------
    kpi = wb.add_worksheet("KPI_Summary")
    kpi.write("A1", "UiPath Free Slots — KPI Summary", fmt_title)
    kpi.write("A3", "Metric", fmt_label); kpi.write("B3", "Value", fmt_label)
    kpi.write("A4", "Total gaps"); kpi.write_formula("B4", f"=COUNTA(Raw_Data!A2:A{n + 1})", fmt_kpi)
    kpi.write("A5", "Available"); kpi.write_formula("B5", f'=COUNTIF(Raw_Data!K2:K{n + 1},"Available")', fmt_kpi)
    kpi.write("A6", "Reserved (same process)"); kpi.write_formula("B6", f'=COUNTIF(Raw_Data!K2:K{n + 1},"Reserved_Same_Process")', fmt_kpi)
    kpi.write("A7", "Post-error window"); kpi.write_formula("B7", f'=COUNTIF(Raw_Data!K2:K{n + 1},"Post_Error_Window")', fmt_kpi)
    kpi.write("A8", "Available %"); kpi.write_formula("B8", "=B5/B4", None, "0.0%")
    kpi.write("A9", "Robots analysed"); kpi.write_formula("B9", f"=SUMPRODUCT(1/COUNTIF(Raw_Data!A2:A{n + 1},Raw_Data!A2:A{n + 1}))", fmt_kpi)
    # XLOOKUP lookup table demo (Excel 365): find first Available gap per robot
    kpi.write("D1", "XLOOKUP demo: first Available gap", fmt_label)
    kpi.write("D2", "Robot", fmt_label); kpi.write("E2", "Gap_From (XLOOKUP)", fmt_label)
    kpi.write("D3", "PROD-00161")
    kpi.write_formula("E3", f'=XLOOKUP(D3,Raw_Data!A2:A{n + 1},Raw_Data!I2:I{n + 1},"—")')

    # --- Pivots -------------------------------------------------------------
    piv = wb.add_worksheet("Pivots")
    piv.write("A1", "Gaps by Robot x Gap_Type", fmt_title)
    for r, row in enumerate(pivot_robot.itertuples(index=False), start=3):
        piv.write_row(r, 0, list(row))
    base = len(pivot_robot) + 6
    piv.write(base, 0, "Gaps by WeekDay x Gap_Type", fmt_title)
    for r, row in enumerate(pivot_weekday.itertuples(index=False), start=base + 2):
        piv.write_row(r, 0, list(row))

    # --- Raw_Data -----------------------------------------------------------
    raw = wb.add_worksheet("Raw_Data")
    add_table(wb, raw, df, "FreeSlots")
    # Conditional format: Available rows highlighted green
    raw.conditional_format(1, 0, n, 0, {"type": "formula", "criteria": '$K2="Available"', "format": wb.add_format({"bg_color": "#C6EFCE"})})
    raw.freeze_panes(1, 0)

    # --- Charts -------------------------------------------------------------
    ch = wb.add_worksheet("Charts")
    ch.write("A1", "Gaps per Robot by Type", fmt_title)
    chart = wb.add_chart({"type": "bar"})
    for ci, gap_type in enumerate(["Available", "Reserved_Same_Process", "Post_Error_Window"]):
        if gap_type in pivot_robot.columns:
            col = pivot_robot.columns.get_loc(gap_type)
            chart.add_series({
                "name": gap_type,
                "categories": f"=Pivots!$A$4:$A${len(pivot_robot) + 3}",
                "values": f"=Pivots!${xlsxwriter.utility.xl_col_to_name(col)}$4:${xlsxwriter.utility.xl_col_to_name(col)}${len(pivot_robot) + 3}",
            })
    chart.set_title({"name": "Gap Types per Robot"})
    chart.set_x_axis({"name": "Gaps"})
    ch.insert_chart("A3", chart)

    wb.close()
    return output_xlsx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    print(f"✅ Written: {build(args.input, args.output)}")


if __name__ == "__main__":
    main()
