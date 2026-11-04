# UiPath Robot Capacity & Gap Analysis

A data analysis and visualization project designed to identify idle time windows between UiPath robot jobs, classify available capacity, and determine which time slots are suitable for scheduling new automation processes.

## Overview

UiPath Orchestrator manages the execution of automation jobs across multiple robots. However, gaps between consecutive jobs do not necessarily represent usable capacity. Some gaps may be associated with queued executions of the same process, recovery periods following failed jobs, or operational constraints.

This project analyzes historical UiPath job execution data to identify these gaps and classify them according to their scheduling suitability.

The solution implements a data processing pipeline using Python and pandas, supported by SQL-based analysis, Excel reporting, and an interactive Streamlit dashboard.

## Live Demo

Explore the interactive dashboard:

**[Open UiPath Gap Analysis Dashboard](https://uipath-gap-analysis-abhbkzgfshuxjfb9a48cgh.streamlit.app/)**


## Key Results

| Metric | Result |
|---|---:|
| Total jobs analyzed | 248,057 |
| Robots analyzed | 24 |
| Free time slots identified | 4,077 |
| Available for new processes | 2,405 (59.0%) |
| Reserved for the same process | 1,630 (40.0%) |
| Post-error windows | 42 (1.0%) |

*Results are based on the analyzed dataset covering January and February 2026, with the gap analysis restricted to the most recent month of available job history.*

## Business Problem

Efficient robot utilization is an important aspect of Robotic Process Automation (RPA) operations.

Although robots may appear idle between consecutive executions, not every idle interval is suitable for scheduling additional workloads. For example:

- A subsequent job may belong to the same process, indicating that the available interval should remain reserved.
- A failed execution may require a recovery or retry period.
- Very short gaps may not provide sufficient time for another process to execute.
- Extended idle intervals may represent overnight periods or scheduled maintenance windows.

The objective is to distinguish genuinely available robot capacity from intervals that should remain reserved or excluded from scheduling.

## Data Sources

The analysis uses the following data sources:

| Source | Description |
|---|---|
| `Jobs.csv` | Historical UiPath Orchestrator job execution records containing 248,057 rows and 21 columns. |
| `Config.xlsx` | Configuration file containing analysis thresholds and operational parameters. |
| `data/Free_Slots_Analysis.csv` | Processed and anonymized output containing 4,077 classified time slots. |

The raw job history and configuration files contain operationally sensitive information and are excluded from version control.

Only the aggregated analysis output is included in the repository.

## Data Processing Pipeline

The primary analysis is implemented in `free_slots_v2.py` using pandas and NumPy.

The pipeline consists of five main stages:

### 1. Configuration Loading

`load_config()`

Loads analysis parameters from the `Settings` sheet in `Config.xlsx` and maps them to a typed `GapConfig` dataclass.

If configuration values are missing or invalid, documented default values are applied and warnings are logged.

### 2. Data Cleaning

`clean_jobs()`

Prepares the raw job execution records for analysis by:

- Converting `jobStart` and `jobEnd` into datetime objects.
- Handling invalid timestamps using `errors="coerce"`.
- Removing records with missing robot identifiers or execution timestamps.
- Preserving duplicate records because they may represent distinct job executions.

### 3. Gap Detection

`compute_gaps()`

Identifies idle intervals between consecutive jobs executed by the same robot.

The process involves:

1. Sorting records by `robotName` and `jobStart`.
2. Restricting the analysis to the configured lookback period.
3. Using grouped operations and `shift(-1)` to identify each job's successor.
4. Calculating the interval between the current job's end time and the next job's start time.
5. Filtering intervals according to the minimum and maximum duration thresholds.
6. Excluding qualifying retry windows following failed executions.

The final job for each robot is excluded because no subsequent execution exists to establish a measurable gap.

### 4. Gap Classification

`classify_gaps()`

Each detected interval is assigned to one of three categories:

| Classification | Description |
|---|---|
| `Available` | An idle interval that can potentially accommodate a new process. |
| `Reserved_Same_Process` | An interval followed by another execution of the same process. |
| `Post_Error_Window` | An interval associated with a failed execution and its potential recovery period. |

Only intervals classified as `Available` are considered safe for scheduling new processes.

### 5. Output Formatting

`format_outputs()`

Transforms the processed records into a standardized 12-column dataset containing information such as:

- Robot identifier
- Date and weekday
- Previous job duration
- Gap start and end times
- Gap duration
- Gap classification
- Scheduling eligibility

The resulting dataset is exported to `data/Free_Slots_Analysis.csv` and consumed by both reporting solutions.

## Assumptions & Methodology

The analysis uses predefined operational thresholds to distinguish meaningful scheduling opportunities from insignificant or potentially unsafe intervals.

| Parameter | Value | Rationale |
|---|---:|---|
| Minimum gap duration | 30 minutes | Excludes short intervals that are generally unsuitable for additional workloads. |
| Maximum gap duration | 12 hours | Excludes extended idle periods that may represent overnight or maintenance windows. |
| Retry window | 35 minutes | Prevents recently failed executions from being treated as available capacity. |
| Lookback period | 1 month | Focuses the analysis on recent robot utilization. |
| Date attribution | Previous job end time | Provides consistent date and weekday attribution for detected gaps. |

Additional considerations:

- Jobs with missing essential identifiers or timestamps are excluded.
- The last job for each robot is excluded because its subsequent idle interval cannot be determined.
- Gaps crossing midnight are retained if they satisfy the configured duration constraints.
- Post-error windows are never classified as safe for new processes.
- Changes to analysis thresholds require regenerating the processed dataset before refreshing the dashboards.

The pipeline also generates `output/ASSUMPTIONS.md`, documenting the assumptions and execution statistics for each analysis run.

## Dashboards & Visualizations

The project provides two complementary reporting solutions.

### Excel Dashboard

Implemented in `build_excel_dashboard.py`.

The generated workbook includes:

| Worksheet | Description |
|---|---|
| `KPI_Summary` | Key performance indicators using Excel formulas. |
| `Pivots` | Robot-level and weekday-level breakdowns by gap classification. |
| `Raw_Data` | Processed records with filters, formatting, and conditional highlighting. |
| `Charts` | Native Excel charts for visual analysis. |

The workbook uses Excel functions such as `COUNTIFS` and `XLOOKUP`, alongside pivot tables and conditional formatting.

### Interactive Dashboard

Implemented in `app.py` using Streamlit and Plotly.

The dashboard provides:

**Key Performance Indicators**
- Total identified gaps.
- Available scheduling windows.
- Reserved intervals.
- Post-error windows.

**Interactive Filters**
- Robot selection.
- Gap classification.
- Weekday selection.

**Visualizations**
- Stacked bar chart showing capacity distribution across robots.
- Heatmap displaying idle time distribution by hour and robot.
- Histogram illustrating the distribution of gap durations, including the configured minimum and maximum thresholds.

The visualizations support interactive exploration through filtering, hovering, zooming, and panning.

Both dashboards consume the same processed CSV dataset, ensuring consistent reporting across the two interfaces.

## Alternative SQLite Implementation

The repository also includes `free_slots_from_sqlite.py`, an earlier implementation of the analysis using SQLite.

This version is retained as a reference implementation and provides an alternative approach to identifying idle intervals.

Unlike the primary pandas implementation, the SQLite version includes additional handling for retry chains and overlapping gaps.

## Project Structure

```text
UiPath-Gap-Analysis/
│
├── data/
│   └── Free_Slots_Analysis.csv
│
├── output/
│   ├── ASSUMPTIONS.md
│   ├── analysis.log
│   └── generated reports and visualizations
│
├── app.py
├── free_slots_v2.py
├── free_slots_from_sqlite.py
├── build_excel_dashboard.py
├── requirements.txt
├── .gitignore
└── README.md
```

Raw input files such as `Jobs.csv` and `Config.xlsx` are maintained locally and excluded from version control.

## Technologies Used

| Technology | Purpose |
|---|---|
| Python | Core implementation and data processing. |
| pandas | Data cleaning, transformation, grouping, and gap detection. |
| NumPy | Vectorized data classification and numerical operations. |
| SQLite | Alternative implementation for querying and analyzing job records. |
| Excel / XlsxWriter | Automated report generation, formulas, pivot tables, and charts. |
| Streamlit | Interactive dashboard development. |
| Plotly | Interactive data visualizations. |
| Matplotlib | Static visualizations and reporting artifacts. |

## Installation & Execution

### Prerequisites

- Python 3.10 or later.
- Access to the required input files (`Jobs.csv` and `Config.xlsx`) for regenerating the analysis.

### Setup

Clone the repository:

```bash
git clone https://github.com/TisTis0004/UiPath-Gap-Analysis.git
cd UiPath-Gap-Analysis
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### Run the Analysis

```bash
python free_slots_v2.py
```

This processes the raw job history and generates the classified gap dataset and analysis artifacts.

### Launch the Interactive Dashboard

```bash
streamlit run app.py
```

### Generate the Excel Dashboard

```bash
python build_excel_dashboard.py
```

## Output

The primary output is `data/Free_Slots_Analysis.csv`, which contains the classified robot idle intervals and their scheduling eligibility.

This dataset serves as the shared input for the Excel and Streamlit reporting solutions.

The analysis provides an auditable approach to understanding robot utilization and identifying potential opportunities for improving RPA scheduling efficiency.