from scraping_src.driver_manager import get_driver, driver_lock
import yaml
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
import pyodbc
import time
from pathlib import Path
import json
import re
import logging
from logging.handlers import RotatingFileHandler
from npl_src.db import DatabaseConnection
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "interbank_rates.log"

logger = logging.getLogger("InterbankRates")
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


class InterbankWeeklyInterestRates:
    last_run_date = None

    def __init__(self):
        with open("config.json", "r") as f:
            config = json.load(f)
        self.url = config["INT_URL"]
        self.headers = config["HEADERS"]
        self.filename = config.get("FILENAME", "interbank_weekly_rates.csv")
        self.db_config = CFG["database"]
        self.last_run_date = None
        self.db = DatabaseConnection()

        logger.info(f"InterbankWeeklyInterestRates initialized. Source URL: {self.url}, Log file: {LOG_FILE}")

    def get_last_db_date(self):
        query = text("""
            SELECT MAX(CAST([Week Ending] AS date)) AS max_date
            FROM t_macro_econs_WeeklyInterestRates
        """)
        engine = self.db.get_db_connection()
        with engine.connect() as conn:
            result = conn.execute(query)
            return result.scalar()

    def check_last_date(self):
        last_date = self.get_last_db_date()
        if not last_date:
            return True
        return last_date != datetime.now().date()

    def write_weekly_to_db(self, rows):
        """rows: list of [week_ending_str, average_rate_str_or_float]"""
        if not rows:
            return

        engine = self.db.get_db_connection()

        typed = []
        for dt_str, rate in rows:
            weekly_date = self._parse_date(dt_str)
            rate_val = self._parse_float(rate)
            typed.append([weekly_date, rate_val])

        with engine.begin() as conn:
            for row in typed:
                conn.execute(
                    text("""
                        INSERT INTO t_macro_econs_WeeklyInterestRates (
                            [Week Ending],
                            [Average Rate (%)]
                        ) VALUES (:week_ending, :avg_rate)
                    """),
                    {"week_ending": row[0], "avg_rate": row[1]}
                )

        logger.info(f"Wrote {len(typed)} rows to t_macro_econs_WeeklyInterestRates.")

    def _parse_date(self, s):
        s = s.strip().replace("\xa0", " ")
        s = re.sub(r'(\d{1,2})(st|nd|rd|th)', r'\1', s)

        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue

        raise ValueError(f"Unrecognized date format: {s}")

    def _parse_float(self, s):
        if isinstance(s, (int, float)):
            return float(s)
        return float(str(s).replace(",", "").replace("%", "").strip())

    def _header_index_map(self, headers):
        """Map 'Week Ending' and 'Average Rate (%)' columns, robust to minor text variations."""
        norm = [h.lower().strip() for h in headers]
        idx = {}

        for i, h in enumerate(norm):
            if ("week" in h and "ending" in h) or h == "weekly ending":
                idx["week_ending"] = i
                break

        for i, h in enumerate(norm):
            if "average" in h and "rate" in h:
                idx["avg_rate"] = i
                break

        if "week_ending" not in idx or "avg_rate" not in idx:
            logger.error(f"Could not locate required columns in headers: {headers}")
            raise ValueError(f"Could not locate required columns in headers: {headers}")
        return idx

    def _extract_weekly_rows(self, soup):
        label = soup.find("div", class_="jet-tabs__label-text",
                          string=re.compile("Weekly Interest Rates", re.I))
        if not label:
            logger.error("Could not find 'Weekly Interest Rates' tab label on the page. "
                         "Site layout may have changed.")
            raise ValueError("Could not find 'Weekly Interest Rates' tab label")

        tab = label.find_parent("div", class_="jet-tabs__control")
        panel_id = tab.get("aria-controls")
        weekly_panel = soup.find("div", id=panel_id)

        tables = weekly_panel.find_all("table")

        for idx, table in enumerate(tables, start=1):
            thead = table.find("thead")
            if thead:
                header_cells = [c.get_text(strip=True) for c in thead.find_all(["th", "td"])]
            else:
                first_tr = table.find("tr")
                header_cells = [c.get_text(strip=True) for c in first_tr.find_all(["th", "td"])] if first_tr else []

            if "Week Ending" in header_cells and "Average Rate (%)" in header_cells:
                col_idx = self._header_index_map(header_cells)

                body_rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]
                out = []
                for tr in body_rows:
                    cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                    if len(cells) < max(col_idx.values()) + 1:
                        continue
                    week_str = cells[col_idx["week_ending"]]
                    avg_str = cells[col_idx["avg_rate"]]
                    if not week_str or week_str.lower().startswith("week"):
                        continue
                    out.append([week_str, avg_str])

                logger.info(f"Extracted {len(out)} rows from chosen Weekly table.")
                return out

        logger.error("No suitable Weekly Interest Rates table found inside panel.")
        raise ValueError("No suitable Weekly Interest Rates table found inside panel")

    def get_weekly_rates(self):
        logger.info("Checking Weekly Interbank Rates...")

        # if not self.check_last_date():
        #     logger.info("No update needed: today's date already in DB.")
        #     return
        #
        # if datetime.now().weekday() != 4:
        #     logger.info("Not Friday, skipping.")
        #     return
        #
        # logger.info(f"Fetching HTML with Selenium from {self.url}")

        try:
            driver = get_driver()
            with driver_lock:
                driver.get(self.url)
                time.sleep(5)
                html = driver.page_source
        except Exception:
            logger.error("Failed to load page with Selenium.", exc_info=True)
            raise

        soup = BeautifulSoup(html, "html.parser")
        rows = self._extract_weekly_rows(soup)
        last_db = self.get_last_db_date()

        if last_db:
            if isinstance(last_db, str):
                last_db = self._parse_date(last_db)
            elif isinstance(last_db, datetime):
                last_db = last_db.date()

            filtered = []
            for dt_str, rate in rows:
                try:
                    d = self._parse_date(dt_str)
                except ValueError:
                    logger.warning(f"Skipping row with unparseable date: {dt_str!r}")
                    continue
                if d > last_db:
                    filtered.append([dt_str, rate])
            rows = filtered

        if rows:
            self.write_weekly_to_db(rows)
            self.last_run_date = datetime.now().date()
            logger.info(f"Inserted {len(rows)} new weekly rows. Success.")
        else:
            logger.info("No new weekly rows to insert (DB up-to-date).")

    def run(self):
        """
        Scheduler loop wrapped in try/except so an unexpected error (Chrome
        driver issue, changed page layout, DB drop, etc.) gets logged with a
        full traceback instead of silently killing this thread.
        """
        logger.info("Interbank rates scheduler thread started.")
        # while True:
        try:
            # now = datetime.now()
            # logger.info(f"Scheduler waking up (last_run_date={self.last_run_date})...")
            #
            # if self.last_run_date != now.date() and now.weekday() == 4 and now.hour >= 13:
            #     logger.info("Friday after trigger hour → Running scrape.")
            self.get_weekly_rates()
            # time.sleep(7 * 24 * 3600)

            # elif now.weekday() == 4 and now.hour < 13:
            #     logger.info("Friday but before trigger hour → Sleeping 1 hour.")
            #     time.sleep(3600)
            #
            # else:
            #     logger.info("Not Friday → Sleeping 1 day.")
            #     time.sleep(86400)

        except Exception as e:
            logger.critical(
                f"Interbank rates scheduler hit an unhandled error and would have crashed "
                f"the thread. Sleeping 1 hour before retrying. {type(e)}",
                exc_info=True,
            )
            # time.sleep(3600)

