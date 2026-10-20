import requests
import yaml
from bs4 import BeautifulSoup
from datetime import datetime, date
import time
from pathlib import Path
import json
import logging
from logging.handlers import RotatingFileHandler
from npl_src.db import DatabaseConnection
from sqlalchemy import text
import urllib3

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
INT = ROOT / CFG["data_folder"] / "interim"
INT.mkdir(parents=True, exist_ok=True)

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "treasury_bills.log"

logger = logging.getLogger("TreasuryBillRates")
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


class TreasuryBillRates:
    last_run_date = None

    def __init__(self):
        with open("config.json", "r") as f:
            config = json.load(f)
        self.url = config["TBILL_URL"]
        self.headers = config["HEADERS"]
        self.filename = config["FILENAME"]
        self.db_config = CFG["database"]
        self.last_run_date = None
        self.db = DatabaseConnection()

        logger.info(f"TreasuryBillRates initialized. Source URL: {self.url}, Log file: {LOG_FILE}")

    def write_tb_to_db(self, rows):
        """
        Insert Treasury Bills rows into t_TreasuryBills using SQLAlchemy.
        Each row must have 5 columns:
        [Issue Date, Tender, Security Type, Discount Rate, Interest Rate]
        """
        if not rows:
            return

        if not all(isinstance(r, (list, tuple)) and len(r) == 5 for r in rows):
            logger.error(f"Each row must have 5 columns; sample: {rows[0] if rows else rows}")
            raise ValueError(f"Each row must have 5 columns; sample: {rows[0] if rows else rows}")

        insert_sql = text("""
            INSERT INTO t_TreasuryBills (
                [Issue Date],
                Tender,
                [Security Type],
                [Discount Rate],
                [Interest Rate]
            )
            VALUES (
                :issue_date,
                :tender,
                :security_type,
                :discount_rate,
                :interest_rate
            )
        """)

        new_rows = []
        for row in rows:
            date_value = row[0]

            if isinstance(date_value, (datetime, date)):
                iso_date = date_value.isoformat()
            else:
                iso_date = datetime.strptime(date_value, "%d %b %Y").date().isoformat()

            new_rows.append({
                "issue_date": iso_date,
                "tender": row[1],
                "security_type": row[2],
                "discount_rate": row[3],
                "interest_rate": row[4],
            })

        engine = self.db.get_db_connection()
        with engine.begin() as conn:
            conn.execute(insert_sql, new_rows)

        logger.info(f"Inserted {len(new_rows)} Treasury Bill rows:")
        for r in new_rows:
            logger.info(f"   {r['issue_date']} -> {r['security_type']}")

    def get_tbill_rates(self):
        logger.info("Checking for new T-bill rates...")

        # if datetime.now().weekday() != 1:
        #     logger.info("Not the scheduled weekday, skipping.")
        #     return

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
            logger.error(f"Failed to fetch page from {self.url}", exc_info=True)
            raise

        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table', id='table_1')
        if not table:
            logger.error("No table with id='table_1' found in the fetched page. "
                         "The site's HTML structure may have changed.")
            raise ValueError("No table_1 found in response")

        rows = []
        tbody = table.find('tbody')
        trs = tbody.find_all('tr') if tbody else table.find_all('tr')

        for tr in trs:
            cells = [c.get_text(strip=True) for c in tr.find_all(['td', 'th'])]
            if not cells:
                continue
            if cells[0].lower().startswith('issue') or cells[0].lower() == 'issue date':
                continue
            cells = (cells + [''] * 5)[:5]
            rows.append(cells)

        unique_rows = {(r[0], r[2]): r for r in rows}
        rows = list(unique_rows.values())

        final_rows = []
        engine = self.db.get_db_connection()
        with engine.connect() as conn:
            for r in rows:
                try:
                    issue_date = datetime.strptime(r[0], "%d %b %Y").date()
                except ValueError:
                    logger.warning(f"Skipping row with unparseable Issue Date: {r[0]!r}")
                    continue
                sec_type = r[2]

                check_sql = text("""
                    SELECT COUNT(*)
                    FROM t_TreasuryBills
                    WHERE [Issue Date] = :issue_date
                      AND [Security Type] = :security_type
                """)
                count = conn.execute(check_sql,
                                     {"issue_date": issue_date.isoformat(), "security_type": sec_type}).scalar()

                if count == 0:
                    final_rows.append([issue_date, *r[1:]])

        if final_rows:
            logger.info(f"New rows to insert: {len(final_rows)}")
            for r in final_rows:
                logger.info(f"   {r[0]} -> {r[2]}")
            self.write_tb_to_db(final_rows)
            self.last_run_date = datetime.now().date()
            logger.info(f"Success: Inserted {len(final_rows)} new unique Treasury Bill rows.")
        else:
            logger.info("No new rows to insert (DB up to date).")

    def run(self):
        """
        Scheduler loop wrapped in try/except so an unexpected error (site down,
        changed table structure, DB drop, etc.) gets logged with a full
        traceback instead of silently killing this thread.
        """
        logger.info("Treasury Bill Rates scheduler thread started.")
        # while True:
        try:
            # now = datetime.now()
            # logger.info(f"Scheduler waking up (last_run_date={self.last_run_date})...")
            #
            # if self.last_run_date != now.date() and now.weekday() == 1 and now.hour >= 17:
            #     logger.info("Trigger day/hour reached → Running scrape.")
            self.get_tbill_rates()
            # time.sleep(7 * 24 * 3600)

            # elif now.weekday() == 4 and now.hour < 13:
            #     logger.info("Trigger day but before trigger hour → Sleeping 1 hour.")
            #     time.sleep(3600)
            #
            # else:
            #     logger.info("Not the trigger day → Sleeping 1 day.")
            #     time.sleep(86400)

        except Exception as e:
            logger.critical(
                f"Treasury Bill scheduler hit an unhandled error and would have crashed "
                f"the thread. Sleeping 1 hour before retrying. {type(e)}",
                exc_info=True,
            )
            # time.sleep(3600)
