import requests
import yaml
from bs4 import BeautifulSoup
from datetime import datetime
import pyodbc

import time
from pathlib import Path
import json
import threading
import logging
from logging.handlers import RotatingFileHandler
from sqlalchemy import text
from npl_src.db import DatabaseConnection
import urllib3

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "exchange_rates.log"

logger = logging.getLogger("ExchangeRates")
logger.setLevel(logging.INFO)
TARGET_CODES = {"USD", "GBP", "EUR"}
if not logger.handlers:  # avoid duplicate handlers if module gets re-imported
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


class ExchangeRates:
    last_run_date = None

    def __init__(self):
        with open("config.json", "r") as f:
            config = json.load(f)
        self.url = config["URL"]
        self.headers = config["HEADERS"]
        self.filename = config["FILENAME"]
        self.db_config = CFG["database"]

        self.db = DatabaseConnection()

        logger.info(f"ExchangeRates initialized. Source URL: {self.url}, Log file: {LOG_FILE}")

    def get_last_db_date(self):
        query = text("SELECT MAX(CurrentDate) FROM T_EXCHANGERATES")
        engine = self.db.get_db_connection()
        with engine.connect() as con:
            result = con.execute(query)
            return result.scalar()

    def check_last_date(self):
        """Return True if today's date is not yet in DB."""
        last_date = self.get_last_db_date()
        today = datetime.now().date()
        return (last_date is None) or (last_date != today)

    def write_to_db(self, rows):
        query = text("""
                INSERT INTO T_EXCHANGERATES (
                    CurrentDate, Currency, CurrencyPair, BuyingRate, SellingRate, MidRate
                ) VALUES (:CurrentDate, :Currency, :CurrencyPair, :BuyingRate, :SellingRate, :MidRate)
            """)
        data = [
            {
                "CurrentDate": row[0],
                "Currency": row[1],
                "CurrencyPair": row[2],
                "BuyingRate": row[3],
                "SellingRate": row[4],
                "MidRate": row[5]
            } for row in rows
        ]

        engine = self.db.get_db_connection()
        with engine.begin() as con:  # begin() automatically commits
            con.execute(query, data)

        logger.info(f"Wrote {len(data)} exchange rate rows to T_EXCHANGERATES.")

    def get_exchange_rates(self):
        logger.info(f"Fetching exchange rates from {self.url}")

        try:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            # Use a Session object to force ignore environment SSL variables
            session = requests.Session()
            session.verify = False
            session.trust_env = False  # This stops Python from forcing system certificates

            # changed requests.get(self.url, headers=self.headers, timeout=30).text
            # To session.get(self.url, headers=self.headers, timeout=30).text
            html = session.get(self.url, headers=self.headers, timeout=30).text
        except Exception:
            logger.error(f"Failed to fetch exchange rate page from {self.url}", exc_info=True)
            raise

        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find(name='table', id='table_2')
        if not table:
            logger.error("No table with id='table_2' found in the fetched page. "
                         "The site's HTML structure may have changed.")
            raise ValueError("No table found in response")

        rows = []
        for tr in table.find('tbody').find_all('tr'):
            row = [td.get_text(strip=True) for td in tr.find_all('td')]
            if len(row) < 3:
                logger.warning(f"Skipping malformed row (too few columns): {row}")
                continue

            currency_pair = row[2].strip().upper()
            leading_code = currency_pair[:3]  # e.g. "USDGHS" -> "USD", "GHSXOF" -> "GHS"

            if leading_code in TARGET_CODES:
                row[1] = leading_code  # overwrite name only for USD/GBP/EUR rows
                row[2] = currency_pair  # normalize casing on the pair too

            # any other row (GHSXOF, CHFGHS, etc.) is appended completely as-is
            rows.append(row)

        if not rows:
            logger.warning("Table found but no rows extracted — check page structure.")

        self.write_to_db(rows)
        self.last_run_date = datetime.now().date()
        logger.info(f"Inserted {len(rows)} rows for {self.last_run_date}")

    def run(self):
        """
        Scheduler loop: checks roughly hourly before the trigger hour, then
        once past it, scrapes and writes to DB if today isn't already there.

        The whole loop body is wrapped in try/except so an unexpected error
        (site down, DB connection drop, changed table structure, etc.) gets
        logged with a full traceback instead of silently killing this thread.
        On failure it backs off for an hour and retries rather than looping
        tightly or dying outright.
        """
        logger.info("ExchangeRates scheduler thread started.")
        # while True:
        try:
            # now = datetime.now()
            # logger.info(f"Scheduler waking up (last_run_date={self.last_run_date})...")

            # if now.hour == 17 and self.check_last_date() and self.last_run_date != now.date():
            if self.check_last_date():
                logger.info("Conditions met → Running scrape.")
                self.get_exchange_rates()
                # logger.info("Scrape completed. Sleeping 24 hours...")
                # time.sleep(24 * 3600)  # sleep a day after successful run
                # elif now.hour < 17:
                #     logger.info("Before trigger hour → Sleeping 1 hour.")
                #     time.sleep(3600)
                # else:
                #     logger.info("Already updated today → Sleeping 6 hours.")
                #     time.sleep(6 * 3600)

        except Exception as e:

            logger.critical(
                f"Exchange rate scheduler hit an unhandled error and would have crashed "
                f"the thread. Sleeping 1 hour before retrying. Exception: {type(e)}",
                exc_info=True,
            )
            # time.sleep(3600)
