import requests
from pathlib import Path
from datetime import datetime, date, timedelta
import yaml
import json
import pandas as pd
import pyodbc
import re
import time
import logging
from logging.handlers import RotatingFileHandler
from npl_src.db import DatabaseConnection
from sqlalchemy import text

# ----------------- GLOBAL CONFIG LOADING -----------------
ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())

with open("config.json", "r") as f:
    config = json.load(f)

GIFURL = config["GIFURL"]          # not strictly needed now, but kept
HEADERS = config["HEADERS"]
DATA_FOLDER = Path(ROOT / CFG["data_folder"])
DATA_FOLDER.mkdir(parents=True, exist_ok=True)

# ----------------- LOGGING SETUP -----------------
# Everything the scheduler does — successes, warnings, and full error
# tracebacks — gets written to this file. If the scheduler stops working,
# check logs/gfim_manager.log first; it will show the last thing that ran
# and, if something crashed, the full Python traceback for it.
LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "gfim_manager.log"

logger = logging.getLogger("GFIMManager")
logger.setLevel(logging.INFO)

if not logger.handlers:  # avoid duplicate handlers if module is re-imported (e.g. Flask reloader)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: keeps log file from growing forever.
    # Rolls over at 5MB, keeps 5 backups (gfim_manager.log.1, .2, etc.)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Console handler: still see it live if running interactively
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class GFIMManager:
    def __init__(self):
        # Config
        self.gif_url = GIFURL
        self.headers = HEADERS
        self.data_folder = DATA_FOLDER
        self.db_config = CFG["database"]

        # State for scheduler
        self.last_run_date: date | None = None

        # --------------- DB CONNECTION -----------------
        # NOTE: db.py's get_db_connection() creates a brand new SQLAlchemy
        # Engine (and connection pool) every time it's called. We can't edit
        # db.py, so instead we cache the engine here and only refresh it
        # periodically (see _get_engine below) — this avoids spinning up a
        # fresh engine/connection on every single DB call in this class.
        self.db = DatabaseConnection()
        self._engine = None
        self._engine_created_at = None

        logger.info(f"GFIMManager initialized. Data folder: {self.data_folder}, Log file: {LOG_FILE}")

    # --------------- CACHED DB ENGINE -----------------
    def _get_engine(self):
        """
        Return a cached SQLAlchemy engine, refreshing it at most once every
        12 hours. This avoids calling self.db.get_db_connection() (which
        creates a brand new engine/connection pool every time) on every
        single DB touch within a run, while still refreshing periodically
        so we don't hold a single connection open across the scheduler's
        24h sleep, where it could otherwise go stale.
        """
        if self._engine is None or (datetime.now() - self._engine_created_at) > timedelta(hours=12):
            self._engine = self.db.get_db_connection()
            self._engine_created_at = datetime.now()
        return self._engine

    # --------------- LISTING & DOWNLOADING REPORTS -----------------

    def list_gfim_reports(self):
        """
        Return all GFIM trading reports as a list of (report_date, title, url),
        sorted by report_date ascending.
        Uses FileBird JSON API.
        """

        api_url = "https://gfim.com.gh/wp-json/filebird/v1/get-attachments"

        payload = {
            "pagination": {"current": 1, "limit": 200},
            "search": "",
            "orderBy": "post_modified",
            "orderType": "DESC",
            # Use the latest folder ID
            "selectedFolder": ["uk+N1Df7mDmi3uS44m8kXQ=="]
        }

        logger.info(f"Sending request to GFIM API with payload: {payload}")

        try:
            resp = requests.post(api_url, json=payload, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Error during GFIM API request: {e}", exc_info=True)
            return []

        logger.info(f"GFIM API response status code: {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"Failed to parse GFIM API JSON response: {e}", exc_info=True)
            logger.debug(f"Raw response text: {resp.text}")
            return []

        logger.info(f"Response JSON keys: {list(data.keys())}")
        files = data.get("files", [])
        logger.info(f"Number of files returned: {len(files)}")

        reports = []

        try:
            for file in files:
                title = file.get("title", "")
                url = file.get("url", "")

                if not title or not url:
                    continue

                if "TRADING REPORT FOR GFIM" not in title.upper():
                    continue

                m = re.search(r"(\d{2})(\d{2})(\d{4})$", title)
                if not m:
                    continue

                dd, mm, yyyy = m.groups()
                report_date = datetime.strptime(f"{dd}{mm}{yyyy}", "%d%m%Y").date()

                reports.append((report_date, title, url))

            reports.sort(key=lambda x: x[0])
            logger.info(f"Total GFIM trading reports found: {len(reports)}")
            return reports
        except Exception as e:
            logger.warning(f"The required excel sheet not found. {type(e)}")

    def get_existing_gfim_dates(self) -> list[date]:
        """
        Scan DATA_FOLDER for already-downloaded GFIM reports and
        extract their dates from the filename pattern:
        gfim_trading_report_YYYY-MM-DD_*.xlsx
        """
        dates = []
        pattern = re.compile(r"gfim_trading_report_(\d{4}-\d{2}-\d{2})_")

        for path in self.data_folder.glob("gfim_trading_report_*.xlsx"):
            m = pattern.search(path.name)
            if not m:
                continue
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            dates.append(d)

        return sorted(set(dates))

    def download_next_weekly_gfim_report(self) -> Path | None:
        """
        Download the next weekly GFIM report, enforcing a >= 7-day gap
        between report dates.

        Logic:
          - Look at all available GFIM reports.
          - Look at the latest date we already have locally.
          - Pick the earliest report with date >= last_date + 7 days.
          - If none found, do nothing.
        """
        reports = self.list_gfim_reports()
        if not reports:
            logger.warning("No GFIM reports available from API.")
            return None

        existing_dates = self.get_existing_gfim_dates()
        last_downloaded = max(existing_dates) if existing_dates else None

        if last_downloaded is None:
            # No previous weekly — pick the latest available as starting point
            report_date, title, url = reports[-1]
        else:
            min_target_date = last_downloaded + timedelta(days=7)
            candidate = None
            for d, title, url in reports:
                if d >= min_target_date:
                    candidate = (d, title, url)
                    break

            if candidate is None:
                logger.info(f"No new weekly GFIM report (need >= {min_target_date}).")
                return None

            report_date, title, url = candidate

        date_slug = report_date.strftime("%Y-%m-%d")
        base_name = f"gfim_trading_report_{date_slug}"

        # avoid duplicates in folder
        existing = list(self.data_folder.glob(f"{base_name}*.xlsx"))
        if existing:
            logger.info(f"Already downloaded weekly report for {date_slug}: {existing[0]}")
            return existing[0]

        try:
            resp_file = requests.get(url, timeout=60)
            resp_file.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to download weekly report from {url}: {e}", exc_info=True)
            return None

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = self.data_folder / f"{base_name}_{timestamp}.xlsx"

        with open(filename, "wb") as f:
            f.write(resp_file.content)

        logger.info(f"Downloaded WEEKLY GFIM report: {title}")
        logger.info(f"Saved to: {filename}")
        return filename

    def _get_last_completed_month(self, today: date | None = None) -> tuple[int, int]:
        """
        Return (year, month) for the last fully completed month.
        E.g., if today is 2025-11-13, we return (2025, 10).
        """
        if today is None:
            today = date.today()

        if today.month == 1:
            return today.year - 1, 12
        else:
            return today.year, today.month - 1

    def download_eom_gfim_report(
        self,
        year: int | None = None,
        month: int | None = None,
        search_window_days: int = 5,
    ) -> Path | None:
        """
        Download the end-of-month GFIM report for a given year-month.

        Rules:
          - Primary: last trading day in that month
            -> max(report_date) with same year & month.
          - Fallback: if no reports in that month, choose the first report
            with date > month_end and <= month_end + search_window_days.
        """
        if year is None or month is None:
            year, month = self._get_last_completed_month()

        reports = self.list_gfim_reports()
        if not reports:
            logger.warning("No GFIM reports available from API.")
            return None

        # Compute calendar month-end
        if month == 12:
            month_end = date(year, 12, 31)
        else:
            next_month = date(year if month < 12 else year + 1, (month % 12) + 1, 1)
            month_end = next_month - timedelta(days=1)

        # 1) Try to find last trading day inside the month
        in_month = [(d, t, u) for (d, t, u) in reports if d.year == year and d.month == month]

        target_report = None

        if in_month:
            # last trading day in that month
            in_month.sort(key=lambda x: x[0])
            target_report = in_month[-1]
        else:
            # 2) Fallback: first report after month_end, within window
            upper_bound = month_end + timedelta(days=search_window_days)
            after = [(d, t, u) for (d, t, u) in reports if month_end < d <= upper_bound]
            if after:
                after.sort(key=lambda x: x[0])
                target_report = after[0]

        if not target_report:
            logger.warning(f"No EOM GFIM report found for {year}-{month:02d}.")
            return None

        report_date, title, url = target_report
        date_slug = report_date.strftime("%Y-%m-%d")
        base_name = f"gfim_trading_report_{date_slug}"

        existing = list(self.data_folder.glob(f"{base_name}*.xlsx"))
        if existing:
            logger.info(f"Already downloaded EOM report for {date_slug}: {existing[0]}")
            return existing[0]

        try:
            resp_file = requests.get(url, timeout=60)
            resp_file.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to download EOM report from {url}: {e}", exc_info=True)
            return None

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = self.data_folder / f"{base_name}_{timestamp}.xlsx"

        with open(filename, "wb") as f:
            f.write(resp_file.content)

        logger.info(f"Downloaded EOM GFIM report for {year}-{month:02d}: {title}")
        logger.info(f"Saved to: {filename}")
        return filename

    # --------------- UTIL: EXTRACT DATE FROM FILENAME -----------------
    def extract_report_date_from_filename(self, path: Path) -> date:
        """
        Extracts the report date from GFIM filenames.

        Supports:
          - gfim_trading_report_2025-11-12_*.xlsx
          - TRADING-REPORT-FOR-GFIM-12112025-.xlsx
        """
        name = path.stem  # filename without .xlsx

        # Pattern A: gfim_trading_report_YYYY-MM-DD_*
        m1 = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        if m1:
            return datetime.strptime(m1.group(1), "%Y-%m-%d").date()

        # Pattern B: ...-ddmmyyyy
        m2 = re.search(r"(\d{2})(\d{2})(\d{4})", name)
        if m2:
            dd, mm, yyyy = m2.groups()
            return datetime.strptime(f"{dd}{mm}{yyyy}", "%d%m%Y").date()

        raise ValueError(f"Could not extract a date from filename: {path}")

    # --------------- CLEANERS -----------------
    def clean_new_gog_notes_and_bonds(self, excel_path: Path) -> pd.DataFrame:
        """
        Clean the 'NEW GOG NOTES AND BONDS' sheet from a GFIM trading report.
        Handles swapped columns, messy headers, and numeric/text cleaning.
        """
        xl = pd.ExcelFile(excel_path)
        sheet_name = next(
            (s for s in xl.sheet_names if "NEW GOG NOTES AND BONDS" in s.upper().strip()),
            None
        )
        if sheet_name is None:
            logger.error(
                f"Could not find GOG Notes and Bonds sheet in {excel_path.name}. "
                f"Available sheets: {xl.sheet_names}"
            )
            raise ValueError(
                f"Could not find GOG Notes and Bonds sheet in {excel_path.name}. "
                f"Available sheets: {xl.sheet_names}"
            )
        df = pd.read_excel(excel_path, sheet_name=sheet_name, header=3)
        report_date = self.extract_report_date_from_filename(excel_path)

        # -------------------------
        # 1️⃣ Normalize headers robustly
        day_low_col = None
        day_high_col = None
        for col in df.columns:
            col_upper = str(col).upper()
            if "DAY LOW" in col_upper:
                day_low_col = col
            elif "DAY HIGH" in col_upper:
                day_high_col = col

        if day_low_col is None or day_high_col is None:
            logger.error(f"Could not find Day Low or Day High column in sheet '{sheet_name}' of {excel_path.name}")
            raise ValueError("Could not find Day Low or Day High column in the sheet!")

        keep_cols_map = {
            "TENOR": "Tenors",
            "SECURITY DESCRIPTION": "SecurityDescription",
            "ISIN": "ISIN",
            "OPENING\nYIELD": "OpeningYield",
            "CLOSING\n YIELD": "ClosingYield",
            "END OF DAY CLOSING\n PRICE": "EndOfDayClosingPrice",
            "VOLUME": "Volume",
            "NUMBER \nTRADED": "NumberTraded",
            day_high_col: "DayHighYield",
            day_low_col: "DayLowYield",
            "DAYS TO \nMATURITY": "DaysToMaturity",
            "MATURITY\nDATE": "MaturityDate",
        }

        # Keep only the columns we need
        df = df[list(keep_cols_map.keys())].copy()
        df.columns = list(keep_cols_map.values())

        # -------------------------
        # 6️⃣ Remove summary rows / empty rows
        # -------------------------
        df = df[df["Tenors"].astype(str).str.upper() != "TOTAL"]
        df = df[df["ISIN"].notna()]
        df = df.dropna(how="all")

        # -------------------------
        # 7️⃣ Convert dates
        # -------------------------
        df["MaturityDate"] = pd.to_datetime(df["MaturityDate"], errors="coerce").dt.date

        # -------------------------
        # 8️⃣ Numeric columns
        # -------------------------
        numeric_cols = [
            "OpeningYield",
            "ClosingYield",
            "EndOfDayClosingPrice",
            "Volume",
            "NumberTraded",
            "DayHighYield",
            "DayLowYield",
            "DaysToMaturity",
        ]

        # -------------------------
        # 9️⃣ Clean numeric columns safely
        # -------------------------
        for col in numeric_cols:
            if col not in df.columns:
                logger.warning(f"Column {col} not found in GOG sheet — skipping numeric cleaning.")
                continue
            series = df[col].fillna("").astype(str)

            series = series.str.replace(",", "", regex=False)
            series = series.str.replace(r"[^0-9\.\-]", "", regex=True)
            series = series.str.replace(r"\.(?=.*\.)", "", regex=True)
            series = series.str.strip()
            series = series.replace(["", ".", "-", " ", "N/A", "n/a", "#N/A", "NA"], pd.NA)
            df[col] = pd.to_numeric(series, errors="coerce")

        existing_numeric_cols = [c for c in numeric_cols if c in df.columns]
        df[existing_numeric_cols] = df[existing_numeric_cols].where(df[existing_numeric_cols].notna(), 0.0)
        df[existing_numeric_cols] = df[existing_numeric_cols].apply(
            lambda col: col.apply(lambda x: round(x, 6) if isinstance(x, float) else x)
        )

        # -------------------------
        # 10️⃣ Clean text columns
        # -------------------------
        text_cols = ["Tenors", "SecurityDescription", "ISIN"]
        for col in text_cols:
            df[col] = df[col].astype(object).fillna("").astype(str).str.strip()
            df[col] = df[col].replace({"": None, "nan": None, "None": None, "<NA>": None})

        # -------------------------
        # 11️⃣ Add report date
        # -------------------------
        df["ReportDate"] = report_date

        # -------------------------
        # 12️⃣ Warn if strings remain in numeric columns
        # -------------------------
        for col in existing_numeric_cols:
            mask = df[col].apply(lambda x: isinstance(x, str))
            if mask.any():
                logger.warning(f"Column {col} still contains string values after cleaning: {df.loc[mask, col].tolist()}")

        logger.info(f"Cleaned NEW GOG NOTES AND BONDS sheet for report date {report_date}: {len(df)} rows.")
        logger.debug(f"CLEANED NEW GOG NOTES AND BONDS (FINAL):\n{df.to_string(index=False)}")

        return df

    def clean_treasury_bills(self, excel_path: Path) -> pd.DataFrame:
        """
        Clean the 'TREASURY BILLS' sheet from a GFIM trading report.
        """
        xl = pd.ExcelFile(excel_path)
        sheet_name = next(
            (s for s in xl.sheet_names if "TREASURY BILLS" in s.upper().strip()),
            None
        )
        if sheet_name is None:
            logger.error(
                f"Could not find Treasury Bills sheet in {excel_path.name}. "
                f"Available sheets: {xl.sheet_names}"
            )
            raise ValueError(
                f"Could not find Treasury Bills sheet in {excel_path.name}. "
                f"Available sheets: {xl.sheet_names}"
            )

        df = pd.read_excel(excel_path, sheet_name=sheet_name, header=3)
        report_date = self.extract_report_date_from_filename(excel_path)
        day_low_col = None
        day_high_col = None
        for col in df.columns:
            col_upper = str(col).upper()
            if "DAY LOW" in col_upper:
                day_low_col = col
            elif "DAY HIGH" in col_upper:
                day_high_col = col

        if day_low_col is None or day_high_col is None:
            logger.error(f"Could not find Day Low or Day High column in sheet '{sheet_name}' of {excel_path.name}")
            raise ValueError("Could not find Day Low or Day High column in the sheet!")

        keep_cols_map = {
            "TENOR": "Tenor",
            "SECURITY DESCRIPTION": "SecurityDescription",
            "ISIN": "ISIN",
            "OPENING\nPRICE": "OpeningPrice",
            "CLOSING\nPRICE": "ClosingPrice",
            "VOLUME TRADED": "VolumeTraded",
            "NUMBER \nTRADED": "NumberTraded",
            day_high_col: "DayHighPrice",
            day_low_col: "DayLowPrice",
            "DAYS TO \nMATURITY": "DaysToMaturity",
            "MATURITY\nDATE": "MaturityDate",
        }

        df = df[list(keep_cols_map.keys())].copy()
        df.columns = list(keep_cols_map.values())

        df["Tenor"] = df["Tenor"].ffill()

        df = df[df["ISIN"].notna()]
        df = df.dropna(how="all")

        df["MaturityDate"] = pd.to_datetime(df["MaturityDate"], errors="coerce").dt.date

        numeric_cols = [
            "OpeningPrice",
            "ClosingPrice",
            "VolumeTraded",
            "NumberTraded",
            "DayHighPrice",
            "DayLowPrice",
            "DaysToMaturity",
        ]

        for col in numeric_cols:
            series = df[col].fillna("").astype(str)
            series = series.infer_objects(copy=False)
            series = series.str.replace(",", "", regex=False)
            series = series.str.replace(r"[^0-9\.\-]", "", regex=True)
            series = series.str.replace(r"\.(?=.*\.)", "", regex=True)
            series = series.str.strip()
            series = series.replace(["", ".", "-", " ", "N/A", "n/a", "#N/A", "NA"], pd.NA)
            df[col] = pd.to_numeric(series, errors="coerce")

        df[numeric_cols] = df[numeric_cols].where(df[numeric_cols].notna(), 0.0)

        text_cols = ["Tenor", "SecurityDescription", "ISIN"]
        for col in text_cols:
            df[col] = df[col].astype(object).fillna("").astype(str).str.strip()
            df[col] = df[col].replace({"": None, "nan": None, "None": None, "<NA>": None})
        df[numeric_cols] = df[numeric_cols].apply(
            lambda col: col.apply(lambda x: round(x, 6) if isinstance(x, float) else x)
        )
        df["ReportDate"] = report_date

        for col in numeric_cols:
            if df[col].apply(lambda x: isinstance(x, str)).any():
                logger.warning(f"Treasury Bills column {col} contains string values after cleaning: "
                                f"{df[col][df[col].apply(lambda x: isinstance(x, str))].tolist()}")

        logger.info(f"Cleaned TREASURY BILLS sheet for report date {report_date}: {len(df)} rows.")
        logger.debug(f"CLEANED TREASURY BILLS (FINAL):\n{df.to_string(index=False)}")

        return df

    def clean_bog_bills(self, excel_path: Path) -> pd.DataFrame:
        try:
            """
            Clean the 'BOG BILLS' sheet from a GFIM trading report.
            """
            xl = pd.ExcelFile(excel_path)
            sheet_name = next(
                (s for s in xl.sheet_names if "BOG BILLS" in s.upper().strip()),
                None
            )
            if sheet_name is None:
                logger.error(
                    f"Could not find BOG Bills sheet in {excel_path.name}. "
                    f"Available sheets: {xl.sheet_names}"
                )
                raise ValueError(
                    f"Could not find BOG Bills sheet in {excel_path.name}. "
                    f"Available sheets: {xl.sheet_names}"
                )
            df = pd.read_excel(excel_path, sheet_name=sheet_name, header=3)
            report_date = self.extract_report_date_from_filename(excel_path)

            keep_cols_map = {
                "TENOR.1": "Tenor",
                "SECURITY DESCRIPTION": "SecurityDescription",
                "ISIN": "ISIN",
                "OPENING\nPRICE": "OpeningPrice",
                "CLOSING\nPRICE": "ClosingPrice",
                "VOLUME TRADED": "VolumeTraded",
                "NUMBER \nTRADED": "NumberTraded",
                "DAY HIGH\nPRICE": "DayHighPrice",
                "DAY LOW \n PRICE": "DayLowPrice",
                "DAYS TO \nMATURITY": "DaysToMaturity",
                "MATURITY\nDATE": "MaturityDate",
            }

            df = df[list(keep_cols_map.keys())].copy()
            df.columns = list(keep_cols_map.values())

            df["Tenor"] = df["Tenor"].ffill()
            df = df[df["ISIN"].notna()]
            df = df.dropna(how="all")

            df["MaturityDate"] = pd.to_datetime(df["MaturityDate"], errors="coerce").dt.date

            numeric_cols = [
                "OpeningPrice",
                "ClosingPrice",
                "VolumeTraded",
                "NumberTraded",
                "DayHighPrice",
                "DayLowPrice",
                "DaysToMaturity",
            ]

            for col in numeric_cols:
                series = df[col].fillna("").astype(str)
                series = series.infer_objects(copy=False)
                series = series.str.replace(",", "", regex=False)
                series = series.str.replace(r"[^0-9\.\-]", "", regex=True)
                series = series.str.replace(r"\.(?=.*\.)", "", regex=True)
                series = series.str.strip()
                series = series.replace(["", ".", "-", " ", "N/A", "n/a", "#N/A", "NA"], pd.NA)
                df[col] = pd.to_numeric(series, errors="coerce")

            df[numeric_cols] = df[numeric_cols].where(df[numeric_cols].notna(), 0.0)

            text_cols = ["Tenor", "SecurityDescription", "ISIN"]
            for col in text_cols:
                df[col] = df[col].astype(object).fillna("").astype(str).str.strip()
                df[col] = df[col].replace({"": None, "nan": None, "None": None, "<NA>": None})
            df[numeric_cols] = df[numeric_cols].apply(
                lambda col: col.apply(lambda x: round(x, 6) if isinstance(x, float) else x)
            )
            df["ReportDate"] = report_date

            for col in numeric_cols:
                if df[col].apply(lambda x: isinstance(x, str)).any():
                    logger.warning(f"BOG Bills column {col} contains string values after cleaning: "
                                    f"{df[col][df[col].apply(lambda x: isinstance(x, str))].tolist()}")

            logger.info(f"Cleaned BOG BILLS sheet for report date {report_date}: {len(df)} rows.")
            logger.debug(f"CLEANED BOG BILLS (FINAL):\n{df.to_string(index=False)}")

            return df
        except Exception as e:
            logger.warning(f"The Expected Excel Sheet BOG BILLS does not exists: {type(e)}")
            return None

    # --------------- LAST DATE CHECK -----------------
    def last_date_check(self, table: str, report_date: date) -> bool:
        """
        Returns True if the given report_date already exists in the table.
        Prevents inserting duplicates.
        """
        engine = self._get_engine()
        with engine.connect() as con:
            query = text(f"SELECT MAX(ReportDate) FROM {table}")
            result = con.execute(query).fetchone()[0]

        if result is None:
            return False

        return report_date <= result

    # --------------- WRITE TO DB -----------------
    def write_new_gog(self, df: pd.DataFrame):
        if df.empty:
            logger.warning("GOG Bonds DF empty, skipping DB insert.")
            return

        engine = self._get_engine()
        insert_sql = text("""
            INSERT INTO t_insightView_New_GOG_Bonds (
                Tenors, SecurityDescription, ISIN,
                OpeningYield, ClosingYield, EndOfDayClosingPrice,
                Volume, NumberTraded, DayHighYield, DayLowYield,
                DaysToMaturity, MaturityDate, ReportDate
            )
            VALUES (:Tenors, :SecurityDescription, :ISIN,
                    :OpeningYield, :ClosingYield, :EndOfDayClosingPrice,
                    :Volume, :NumberTraded, :DayHighYield, :DayLowYield,
                    :DaysToMaturity, :MaturityDate, :ReportDate)
        """)

        with engine.begin() as con:  # handles commit
            for idx, row in df.iterrows():
                values = {
                    "Tenors": row["Tenors"],
                    "SecurityDescription": row["SecurityDescription"],
                    "ISIN": row["ISIN"],
                    "OpeningYield": row["OpeningYield"],
                    "ClosingYield": row["ClosingYield"],
                    "EndOfDayClosingPrice": row["EndOfDayClosingPrice"],
                    "Volume": row["Volume"],
                    "NumberTraded": row["NumberTraded"],
                    "DayHighYield": row["DayHighYield"],
                    "DayLowYield": row["DayLowYield"],
                    "DaysToMaturity": row["DaysToMaturity"],
                    "MaturityDate": row["MaturityDate"],
                    "ReportDate": row["ReportDate"],
                }

                if not (values["DayLowYield"] is None or isinstance(values["DayLowYield"], (int, float))):
                    logger.error(
                        f"Invalid DayLowYield at row {idx}: {values['DayLowYield']} "
                        f"(type: {type(values['DayLowYield'])})"
                    )
                    raise ValueError(
                        f"Invalid DayLowYield: {values['DayLowYield']} (type: {type(values['DayLowYield'])})")

                con.execute(insert_sql, values)

        logger.info(f"Inserted {len(df)} GOG Bonds rows into database.")

    def write_treasury_bills(self, df: pd.DataFrame):
        if df.empty:
            logger.warning("Treasury Bills DF empty, skipping DB insert.")
            return

        engine = self._get_engine()
        insert_sql = text("""
               INSERT INTO t_insightViewTreasuryBill (
                   Tenor, SecurityDescription, ISIN,
                   OpeningPrice, ClosingPrice, VolumeTraded,
                   NumberTraded, DayHighPrice, DayLowPrice,
                   DaysToMaturity, MaturityDate, ReportDate
               )
               VALUES (:Tenor, :SecurityDescription, :ISIN,
                       :OpeningPrice, :ClosingPrice, :VolumeTraded,
                       :NumberTraded, :DayHighPrice, :DayLowPrice,
                       :DaysToMaturity, :MaturityDate, :ReportDate)
           """)

        with engine.begin() as con:
            for _, row in df.iterrows():
                con.execute(insert_sql, {col: row[col] for col in row.index})

        logger.info(f"Inserted {len(df)} Treasury Bills rows into database.")

    def write_bog_bills(self, df: pd.DataFrame):
        if df.empty:
            logger.warning("BOG Bills DF empty, skipping DB insert.")
            return

        engine = self._get_engine()
        insert_sql = text("""
            INSERT INTO t_insightView_BOG_Bills (
                Tenor, SecurityDescription, ISIN,
                OpeningPrice, ClosingPrice, VolumeTraded,
                NumberTraded, DayHighPrice, DayLowPrice,
                DaysToMaturity, MaturityDate, ReportDate
            )
            VALUES (:Tenor, :SecurityDescription, :ISIN,
                    :OpeningPrice, :ClosingPrice, :VolumeTraded,
                    :NumberTraded, :DayHighPrice, :DayLowPrice,
                    :DaysToMaturity, :MaturityDate, :ReportDate)
        """)

        with engine.begin() as con:
            for _, row in df.iterrows():
                con.execute(insert_sql, {col: row[col] for col in row.index})

        logger.info(f"Inserted {len(df)} BOG Bills rows into database.")

    # --------------- PROCESS ONE FILE (ALL 3 TABLES + DUP CHECK) -----------------
    def process_gfim_file(self, excel_path: Path):
        """
        For a single GFIM Excel file:
          - extract report_date
          - run last_date_check on each table
          - if new → clean sheet → insert into DB

        Each of the three tables is wrapped in its own try/except so that,
        for example, a broken GOG Bonds sheet doesn't prevent Treasury Bills
        or BOG Bills from still being processed and logged.
        """
        report_date = self.extract_report_date_from_filename(excel_path)
        logger.info(f"Processing GFIM file {excel_path.name} for report date {report_date}")

        # NEW GOG
        try:
            if not self.last_date_check("t_insightView_New_GOG_Bonds", report_date):
                df_gog = self.clean_new_gog_notes_and_bonds(excel_path)
                self.write_new_gog(df_gog)
            else:
                logger.info(f"GOG Bonds for {report_date} already in DB, skipping.")
        except Exception as e:
            logger.error(f"Failed processing GOG Bonds for {excel_path.name} (report date {report_date}) "
                         f"Exception: {type(e)}",
                         exc_info=True)

        # TREASURY BILLS
        try:
            if not self.last_date_check("t_insightViewTreasuryBill", report_date):
                df_tb = self.clean_treasury_bills(excel_path)
                self.write_treasury_bills(df_tb)
            else:
                logger.info(f"Treasury Bills for {report_date} already in DB, skipping.")
        except Exception as e:
            logger.error(f"Failed processing Treasury Bills for {excel_path.name} "
                         f"(report date {report_date}) Exception: {type(e)}",
                         exc_info=True)

        # BOG BILLS
        try:
            if not self.last_date_check("t_insightView_BOG_Bills", report_date):
                df_bog = self.clean_bog_bills(excel_path)
                if df_bog is not None:
                    self.write_bog_bills(df_bog)
            else:
                logger.info(f"BOG Bills for {report_date} already in DB, skipping.")
        except Exception as e:
            logger.error(f"Failed processing BOG Bills for {excel_path.name} (report date {report_date}) "
                         f"Exception: {type(e)}",
                         exc_info=True)

    # --------------- SCHEDULER (RUNS EVERY DAY AT 17:00) -----------------
    def start_gfim_scheduler(self):
        """
        Scheduler: Every day at 17:00 → run GFIM workflow once.
        - Weekly: download_next_weekly_gfim_report() and process
        - EOM: on day 2 → download_eom_gfim_report() and process
        - Uses last_date_check() to avoid DB duplicates

        The whole daily workflow is wrapped in a try/except so that an
        unexpected error (a bad download, a DB connection drop, etc.)
        gets logged with a full traceback instead of silently killing
        this background thread. If that happens, this method still marks
        the day as "run" and sleeps 24h rather than retrying in a tight
        loop.
        """
        logger.info("GFIM scheduler thread started.")
        # while True:
        try:
            # now = datetime.now()
            # logger.info(f"GFIM Scheduler waking up (last_run_date={self.last_run_date})...")

            # If we haven't run today AND it's past the trigger hour
            # if self.last_run_date != now.date() and now.hour == 14:
            #     logger.info("Trigger hour reached → Running GFIM workflow")

            # 1. WEEKLY
            weekly_path = self.download_next_weekly_gfim_report()
            if weekly_path:
                self.process_gfim_file(weekly_path)
            else:
                logger.info("No weekly file downloaded — skipping weekly processing.")

            # 2. EOM (only on day 1)
            # if now.day == 1:
            eom_path = self.download_eom_gfim_report()
            if eom_path:
                self.process_gfim_file(eom_path)
            else:
                logger.info("No EOM file downloaded — skipping EOM processing.")

                # self.last_run_date = now.date()
                # logger.info("GFIM workflow completed for today. Sleeping 24 hours...")
                # time.sleep(24 * 3600)

            # elif now.hour < 14:
            #     logger.info("Before trigger hour → Sleeping 1 hour")
            #     time.sleep(3600)
            #
            # else:
            #     logger.info("Already executed today → Sleeping 24 hours")
            #     time.sleep(24 * 3600)

        except Exception as e:

            logger.critical(
                f"GFIM scheduler hit an unhandled error and would have crashed the thread. "
                f"Marking today as run and sleeping 1 hour before retrying. Exception: {type(e)}",
                exc_info=True,
            )
            # self.last_run_date = datetime.now().date()
            # time.sleep(3600)


# obj = GFIMManager()
# obj.list_gfim_reports()
