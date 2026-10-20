import requests
from selenium.webdriver.common.by import By
from scraping_src.driver_manager import get_driver, driver_lock
import yaml
from bs4 import BeautifulSoup
from datetime import datetime
import pyodbc
import time
from pathlib import Path
import json
import re
import calendar
import logging
from logging.handlers import RotatingFileHandler
from npl_src.db import DatabaseConnection
from sqlalchemy import text
import urllib3

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "monthly_exchange_rates.log"

logger = logging.getLogger("MonthlyExchangeRates")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class MonthlyUSExchangeRate:
    last_run_date = None

    USD_ROW_LABEL = "US$"
    EURO_ROW_LABEL = "GHC/EURO"

    def __init__(self):
        with open("config.json", "r") as f:
            config = json.load(f)
        self.url = config["EXURL"]
        self.headers = config["HEADERS"]
        self.filename = config["FILENAME"]
        self.db_config = CFG["database"]
        self.db = DatabaseConnection()

        logger.info(f"MonthlyUSExchangeRate initialized. Source URL: {self.url}, Log file: {LOG_FILE}")

    # ---------------------- DB helpers ----------------------
    def get_last_db_date(self, table: str, date_col: str = "Date"):
        query = text(f"SELECT MAX([{date_col}]) AS max_date FROM {table}")
        engine = self.db.get_db_connection()
        with engine.connect() as conn:
            row = conn.execute(query).fetchone()
            last_date = row[0] if row and row[0] else None
            logger.info(f"Last date in {table}.[{date_col}]: {last_date}")

            if last_date is None:
                return None
            if isinstance(last_date, datetime):
                return last_date.date()
            elif isinstance(last_date, str):
                for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
                    try:
                        return datetime.strptime(last_date, fmt).date()
                    except ValueError:
                        continue
                logger.warning(f"Could not parse last_date string '{last_date}' from {table}.")
                return None
            else:
                return last_date

    def check_last_date(self, table: str, date_col: str = "Date"):
        last_date = self.get_last_db_date(table, date_col)
        today = datetime.now().date().replace(day=1)
        return (last_date is None) or (last_date < today)

    def _month_str_to_num(self, month_str):
        mapping = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
            "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
            "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        return mapping.get(month_str.strip()[:3], None)

    def _rows_to_series(self, target_row, label):
        year = int(target_row[0])
        values = target_row[2:]

        out = []
        for idx, val in enumerate(values, start=1):
            if not val or re.match(r"^[\.\-]+$", val):
                continue
            try:
                rate = float(val)
            except ValueError:
                continue
            last_day = calendar.monthrange(year, idx)[1]
            date_val = datetime(year, idx, last_day).date()
            out.append((date_val, rate))

        logger.info(f"Parsed {len(out)} monthly rows for '{label}'.")
        return out

    # ---------------------- USD: unchanged, plain requests ----------------------
    def get_table_data(self):
        """
        USD Month Average lives on page 1 of table_2 — plain requests works
        fine here, no browser needed. Left exactly as originally written.
        """
        try:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            # Use a Session object to force ignore environment SSL variables
            session = requests.Session()
            session.verify = False
            session.trust_env = False  # This stops Python from forcing system certificates

            # changed requests.get(self.url, headers=self.headers, timeout=30).text
            # To session.get(self.url, headers=self.headers, timeout=30).text
            html = session.get(self.url, headers=self.headers, timeout=30).text
            soup = BeautifulSoup(html, "html.parser")

            table = soup.find("table", {"id": "table_2"})
            if not table:
                logger.error("Could not find table with id=table_2")
                raise ValueError("Could not find table with id=table_2")

            logger.info("USD table located successfully.")
            target_row = None
            for tr in table.find("tbody").find_all("tr"):
                cols = [c.get_text(strip=True).replace("\xa0", " ") for c in tr.find_all("td")]
                if len(cols) > 1 and "Month Average" in cols[1] and self.USD_ROW_LABEL in cols[1]:
                    target_row = cols
                    break

            if not target_row:
                logger.error("Could not find 'Inter-Bank Exchange Rate - Month Average (GHC/US$)' row")
                raise ValueError("Could not find 'Inter-Bank Exchange Rate - Month Average (GHC/US$)' row")

            return self._rows_to_series(target_row, "US$ Month Average")
        except Exception as e:
            logger.error(f"Error get_Table_data: {type(e)}")

    def write_to_db(self, rows):
        if not rows:
            return

        insert_sql = text("""
            INSERT INTO DEGU_raw ([Date], [DEGU_diff])
            VALUES (:date, :degu_diff)
        """)

        safe_rows = []
        for d, val in rows:
            if isinstance(d, datetime):
                d = d.date()
            if hasattr(d, "isoformat"):
                d = d.isoformat()
            safe_rows.append({"date": d, "degu_diff": float(val)})

        engine = self.db.get_db_connection()
        with engine.begin() as conn:
            conn.execute(insert_sql, safe_rows)

        logger.info(f"Inserted {len(safe_rows)} USD rows into DEGU_raw.")

    def run_scraper(self):
        try:
            """USD only — original method name/behavior kept for backward compatibility."""
            if not self.check_last_date("DEGU_raw", "Date"):
                logger.info("DB already up to date -> skipping USD scrape.")
                return

            rows = self.get_table_data()
            last_db = self.get_last_db_date("DEGU_raw", "Date")
            if last_db:
                rows = [(d, r) for d, r in rows if d > last_db]

            if rows:
                self.write_to_db(rows)
                self.last_run_date = datetime.now().date()
                logger.info(f"Inserted {len(rows)} new USD rows up to {rows[-1][0]}")
            else:
                logger.info("No new USD rows to insert.")

        except Exception as e:
            logger.error(f"Error running Scrapper: {type(e)}")

    # ---------------------- EUR: needs pagination, so needs a browser ----------------------
    def _get_all_rows_paginated(self, table_id: str = "table_2", max_pages: int = 50):
        """
        EUR's 'End Period' row sits on a later pagination page than page 1,
        and those rows aren't in the raw HTML until that page is clicked —
        so unlike USD, this one genuinely needs a browser to click through
        pagination first. Kept as small as possible: one headless session,
        a few clicks, then quit.
        """
        driver = get_driver()
        all_rows = []
        seen = set()

        try:
            with driver_lock:
                driver.get(self.url)
                time.sleep(3)

                for page_num in range(1, max_pages + 1):
                    html = driver.page_source
                    soup = BeautifulSoup(html, "html.parser")
                    table = soup.find("table", {"id": table_id})
                    if not table:
                        logger.error(f"Could not find table with id={table_id} on page {page_num}.")
                        break

                    tbody = table.find("tbody")
                    if tbody:
                        for tr in tbody.find_all("tr"):
                            cols = [c.get_text(strip=True).replace("\xa0", " ") for c in tr.find_all("td")]
                            if len(cols) > 1:
                                key = tuple(cols)
                                if key not in seen:
                                    seen.add(key)
                                    all_rows.append(cols)

                    logger.info(f"Collected rows from page {page_num} of {table_id} (total so far: {len(all_rows)}).")

                    try:
                        next_btn = driver.find_element(By.ID, f"{table_id}_next")
                    except Exception:
                        logger.info(f"No pagination 'next' control found for {table_id} — assuming done.")
                        break

                    classes = next_btn.get_attribute("class") or ""
                    if "disabled" in classes:
                        logger.info(f"Reached last pagination page for {table_id} (page {page_num}).")
                        break

                    driver.execute_script("arguments[0].click();", next_btn)
                    time.sleep(1.5)

        except Exception:
            logger.error(f"Error while paginating through {table_id}.", exc_info=True)
            raise

        return all_rows

    def get_euro_table_data(self):
        all_rows = self._get_all_rows_paginated(table_id="table_2")

        target_row = None
        end_period_rows_found = []
        for cols in all_rows:
            if "End Period" in cols[1]:
                end_period_rows_found.append(cols[1])
                if self.EURO_ROW_LABEL in cols[1]:
                    target_row = cols
                    break

        if not target_row:
            logger.error(
                f"Could not find 'End Period' row for '{self.EURO_ROW_LABEL}' across all pagination pages. "
                f"'End Period' rows found: {end_period_rows_found}"
            )
            raise ValueError(f"Could not find 'End Period' row for '{self.EURO_ROW_LABEL}'")

        return self._rows_to_series(target_row, "EUR End Period")

    def write_euro_to_db(self, rows):
        """
        Requires DEGE_diff to be DECIMAL in the DB — see:
            ALTER TABLE [dbo].[t_insightView_EUROex]
            ALTER COLUMN [DEGE_diff] DECIMAL(18, 6);
        """
        if not rows:
            return

        insert_sql = text("""
            INSERT INTO t_insightView_EUROex ([DATE], [DEGE_diff])
            VALUES (:date, :dege_diff)
        """)

        safe_rows = []
        for d, val in rows:
            if isinstance(d, datetime):
                d = d.date()
            if hasattr(d, "isoformat"):
                d = d.isoformat()
            safe_rows.append({"date": d, "dege_diff": float(val)})

        engine = self.db.get_db_connection()
        with engine.begin() as conn:
            conn.execute(insert_sql, safe_rows)

        logger.info(f"Inserted {len(safe_rows)} EUR rows into t_insightView_EUROex.")

    def run_euro_scraper(self):
        if not self.check_last_date("t_insightView_EUROex", "DATE"):
            logger.info("t_insightView_EUROex already up to date -> skipping EUR scrape.")
            return

        rows = self.get_euro_table_data()
        last_db = self.get_last_db_date("t_insightView_EUROex", "DATE")
        if last_db:
            rows = [(d, r) for d, r in rows if d > last_db]

        if rows:
            self.write_euro_to_db(rows)
            logger.info(f"Inserted {len(rows)} new EUR rows up to {rows[-1][0]}")
        else:
            logger.info("No new EUR rows to insert.")

    # ---------------------- Scheduler ----------------------
    def run(self):
        """
        Runs USD (unchanged, original run_scraper) then EUR (new
        run_euro_scraper). Each wrapped so one failing doesn't block the
        other. Loop itself wrapped so an unexpected error gets logged with
        a full traceback instead of silently killing this thread.
        """
        logger.info("Monthly Exchange Rate scheduler thread started.")
        # while True:
        try:
            # now = datetime.now()
            # logger.info(f"Scheduler waking up (last_run_date={self.last_run_date})...")

            # if now.weekday() == 4 and now.hour >= 13 and self.last_run_date != now.date():
            #     logger.info("Trigger day/hour reached -> Running scrape.")

            try:
                self.run_scraper()
            except Exception as e:
                logger.error(f"Failed to run USD scrape: {type(e)} ", exc_info=True)

            try:
                self.run_euro_scraper()
            except Exception as e:
                logger.error(f"Failed to run EUR scrape: {type(e)} ", exc_info=True)
                # self.last_run_date = now.date()
                # time.sleep(24 * 3600)

            # elif now.weekday() == 4 and now.hour < 13:
            #     logger.info("Trigger day but before trigger hour -> Sleeping 1 hour.")
            #     time.sleep(3600)
            #
            # else:
            #     logger.info("Not the trigger day -> Sleeping 1 day.")
            #     time.sleep(86400)

        except Exception as e:
            logger.critical(
                f"Monthly Exchange Rate scheduler hit an unhandled error and would have "
                f"crashed the thread. Sleeping 1 hour before retrying.: Exception: {type(e)}",
                exc_info=True,
            )
            # time.sleep(3600)

