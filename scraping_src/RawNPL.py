import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
from pathlib import Path
import yaml, json
import pdfplumber
import pandas as pd
import re
import time
import logging
from logging.handlers import RotatingFileHandler
from npl_src.db import DatabaseConnection
from sqlalchemy import text
import urllib3

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
with open(ROOT / "config.json", "r") as f:
    config = json.load(f)
SURL = config["SURL"]
HEADERS = config["HEADERS"]
DATA_FOLDER = Path(ROOT / CFG["data_folder"])
DATA_FOLDER.mkdir(parents=True, exist_ok=True)

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "summary_pdf.log"

logger = logging.getLogger("SummaryPDF")
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


def download_latest_summary_pdf():
    logger.info(f"Fetching summary page: {SURL}")

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Use a Session object to force ignore environment SSL variables
    session = requests.Session()
    session.verify = False
    session.trust_env = False  # This stops Python from forcing system certificates

    # changed requests.get(self.url, headers=self.headers, timeout=30).text
    # To session.get(self.url, headers=self.headers, timeout=30).text
    resp = session.get(SURL, headers=HEADERS, timeout=30)

    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_link = None
    pdf_title = None
    for a in soup.find_all("a", href=True):
        link_text = a.get_text(strip=True)
        if "Summary of Economic and Financial Data" in link_text and "Charts" not in link_text:
            pdf_link = urljoin(SURL, a["href"])
            pdf_title = link_text
            break

    if not pdf_link:
        logger.error("No valid 'Summary of Economic and Financial Data' PDF link found on the page.")
        raise ValueError("No valid 'Summary of Economic and Financial Data' PDF link found.")

    safe_title = pdf_title.replace(" ", "_").replace("–", "-")
    existing_files = list(DATA_FOLDER.glob(f"{safe_title}*.pdf"))
    if existing_files:
        logger.info(f"Already downloaded: {pdf_title}")
        return existing_files[0]

    # changed from  resp_file = requests.get(pdf_link, headers=HEADERS, timeout=60)
    # To  resp_file = session.get(pdf_link, headers=HEADERS, timeout=60)
    resp_file = session.get(pdf_link, headers=HEADERS, timeout=60)
    resp_file.raise_for_status()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = DATA_FOLDER / f"{safe_title}_{timestamp}.pdf"
    with open(filename, "wb") as f:
        f.write(resp_file.content)

    logger.info(f"Downloaded: {pdf_title}")
    logger.info(f"Saved to: {filename}")
    return filename


def extract_npl_and_mpr_from_pdf(pdf_path: Path):
    """
    Extracts MPR, NPL, Inflation, Lending Rate, GLA, Loan Advances, and GRR
    from fixed page positions in the Summary PDF.

    Each indicator is wrapped in its own try/except so a single page-layout
    change (e.g. a page number shift) doesn't stop the other indicators from
    being extracted.
    """
    results = {}
    with pdfplumber.open(pdf_path) as pdf:

        # --- MPR (page 5) ---
        try:
            page = pdf.pages[4]
            text_lines = page.extract_text().splitlines()
            mpr_line = next((line for line in text_lines if "Monetary Policy Rate" in line), None)

            if mpr_line:
                tokens = mpr_line.replace("Monetary Policy Rate", "").split()
                values = [t for t in tokens if t.replace(".", "", 1).isdigit()]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if not month_line:
                    raise ValueError("Could not locate month header on page 5.")
                month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                if len(months) > len(values):
                    months = months[-len(values):]
                elif len(values) > len(months):
                    values = values[-len(months):]

                mpr_df = pd.DataFrame({"Date": months, "MPR": pd.to_numeric(values, errors="coerce")})
                results["MPR"] = mpr_df
                logger.info(f"MPR extracted: {len(mpr_df)} rows.")
            else:
                logger.warning("Could not find 'Monetary Policy Rate' line on page 5.")
        except Exception:
            logger.error("Failed to extract MPR from PDF.", exc_info=True)

        # --- NPL (page 13) ---
        try:
            page = pdf.pages[12]
            text_lines = page.extract_text().splitlines()
            npl_line = next((line for line in text_lines if "Non-Performing Loans" in line), None)

            if npl_line:
                tokens = npl_line.replace("Non-Performing Loans", "").split()
                values = [t for t in tokens if t.replace(".", "", 1).isdigit()]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if not month_line:
                    raise ValueError("Could not locate month header on page 13 for NPL.")
                month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                if len(months) > len(values):
                    months = months[-len(values):]
                elif len(values) > len(months):
                    values = values[-len(months):]

                npl_df = pd.DataFrame({"Date": months, "NPL": pd.to_numeric(values, errors="coerce")})
                results["NPL"] = npl_df
                logger.info(f"NPL extracted: {len(npl_df)} rows.")
            else:
                logger.warning("Could not find 'Non-Performing Loans' line on page 13.")
        except Exception:
            logger.error("Failed to extract NPL from PDF.", exc_info=True)

        # --- Inflation (page 3) ---
        try:
            page = pdf.pages[2]
            text_lines = page.extract_text().splitlines()
            inflation_line = None

            for i, line in enumerate(text_lines):
                if "Year-on-Year" in line:
                    for j in range(i + 1, len(text_lines)):
                        if "All Consumer Prices" in text_lines[j]:
                            inflation_line = text_lines[j]
                            break
                    break

            if inflation_line:
                tokens = inflation_line.replace("All Consumer Prices", "").split()
                values = [t.strip("%") for t in tokens if t.replace(".", "", 1).isdigit()]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if not month_line:
                    raise ValueError("Could not locate month header on page 3 for Inflation.")
                month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                if len(months) > len(values):
                    months = months[-len(values):]
                elif len(values) > len(months):
                    values = values[-len(months):]

                inflation_df = pd.DataFrame({"Date": months, "Inflation": pd.to_numeric(values, errors="coerce")})
                results["Inflation"] = inflation_df
                logger.info(f"Inflation extracted: {len(inflation_df)} rows.")
            else:
                logger.warning("Could not find 'All Consumer Prices' line under 'Year-on-Year' on page 3.")
        except Exception:
            logger.error("Failed to extract Inflation from PDF.", exc_info=True)

        # --- CBLR / Lending Rate (page 5) ---
        try:
            page = pdf.pages[4]
            text_lines = page.extract_text().splitlines()
            lending_line = None

            for i, line in enumerate(text_lines):
                if "Credit Market" in line:
                    for j in range(i + 1, len(text_lines)):
                        if "Average Lending Rate" in text_lines[j]:
                            lending_line = text_lines[j]
                            break
                    break

            if lending_line:
                tokens = lending_line.replace("Average Lending Rate", "").split()
                values = [t.strip("%") for t in tokens if t.replace(".", "", 1).isdigit()]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if not month_line:
                    raise ValueError("Could not locate month header on page 5 for Lending Rate.")
                month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                if len(months) > len(values):
                    months = months[-len(values):]
                elif len(values) > len(months):
                    values = values[-len(months):]

                lending_df = pd.DataFrame({"Date": months, "LendingRate": pd.to_numeric(values, errors="coerce")})
                results["LendingRate"] = lending_df
                logger.info(f"Lending Rate extracted: {len(lending_df)} rows.")
            else:
                logger.warning("Could not find 'Average Lending Rate' line under 'Credit Market' on page 5.")
        except Exception:
            logger.error("Failed to extract Lending Rate from PDF.", exc_info=True)

        # --- GLA (page 13) ---
        try:
            page = pdf.pages[12]
            text_lines = page.extract_text().splitlines()
            gla_line = None

            for i, line in enumerate(text_lines):
                if "Total Advances" in line:
                    for j in range(i + 1, len(text_lines)):
                        if "Annual Growth (%)" in text_lines[j]:
                            gla_line = text_lines[j]
                            break
                    break

            if gla_line:
                tokens = gla_line.replace("Annual Growth (%)", "").split()
                values = [t.strip("%") for t in tokens if t.replace(".", "", 1).isdigit()]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if not month_line:
                    raise ValueError("Could not locate month header on page 13 for GLA Growth.")
                month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                if len(months) > len(values):
                    months = months[-len(values):]
                elif len(values) > len(months):
                    values = values[-len(months):]

                gla_df = pd.DataFrame({"Date": months, "GLA": pd.to_numeric(values, errors="coerce")})
                results["GLA"] = gla_df
                logger.info(f"GLA Growth extracted: {len(gla_df)} rows.")
            else:
                logger.warning("Could not find 'Annual Growth (%)' line under 'Total Advances' on page 13.")
        except Exception:
            logger.error("Failed to extract GLA Growth from PDF.", exc_info=True)

        # --- Loan Advances (page 13) ---
        try:
            page = pdf.pages[12]
            text_lines = page.extract_text().splitlines()

            month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
            if not month_line:
                raise ValueError("Could not locate month header on page 13.")
            month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
            months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

            loan_adv_line = next((line for line in text_lines if "Total Advances (billion GHC)" in line), None)
            if not loan_adv_line:
                raise ValueError("Could not locate 'Total Advances (billion GHC)' line on page 13.")

            tokens = loan_adv_line.replace("Total Advances (billion GHC)", "").split()
            values = [t for t in tokens if re.match(r"^\d+(\.\d+)?$", t)]

            if len(values) != len(months):
                logger.warning(f"Loan Advances length mismatch: {len(values)} values vs {len(months)} months — trimming to match.")
                min_len = min(len(values), len(months))
                values = values[-min_len:]
                months = months[-min_len:]

            loanadv_df = pd.DataFrame({"Date": months, "LoanAdvances": pd.to_numeric(values, errors="coerce")})
            results["LoanAdvances"] = loanadv_df
            logger.info(f"Loan Advances extracted: {len(loanadv_df)} rows.")
        except Exception:
            logger.error("Failed to extract Loan Advances from PDF.", exc_info=True)

        # --- GRR (page 5) ---
        try:
            page = pdf.pages[4]
            text_lines = page.extract_text().splitlines()
            grr_line = next((line for line in text_lines if "Ghana Reference Rate" in line), None)

            if grr_line:
                tokens = grr_line.replace("Ghana Reference Rate", "").split()
                values = [t.strip("%") for t in tokens if re.match(r"^\d+(\.\d+)?$", t.strip("%"))]

                month_line = next((line for line in text_lines if re.search(r"\d{4}:\d{2}", line)), None)
                if month_line:
                    month_tokens = re.findall(r"\d{4}:\d{2}", month_line)
                    months = [pd.to_datetime(m.replace(":", "-") + "-01") + pd.offsets.MonthEnd(0) for m in month_tokens]

                    min_len = min(len(values), len(months))
                    grr_df = pd.DataFrame({
                        "DATE": months[-min_len:],
                        "GRR": pd.to_numeric(values[-min_len:], errors="coerce")
                    })
                    results["GRR"] = grr_df
                    logger.info(f"GRR extracted: {len(grr_df)} rows.")
                else:
                    logger.warning("Could not locate month header on page 5 for GRR.")
            else:
                logger.warning("Could not find 'Ghana Reference Rate' line on page 5.")
        except Exception:
            logger.error("Failed to extract GRR from PDF.", exc_info=True)

    logger.info(f"PDF extraction complete. Indicators found: {list(results.keys())}")
    return results


def clean_npl(npl_df: pd.DataFrame):
    npl_df["DATE"] = pd.to_datetime(npl_df["Date"], format="%Y:%m", errors="coerce") + pd.offsets.MonthEnd(0)
    npl_df["NPL_diff"] = pd.to_numeric(npl_df["NPL"], errors="coerce")
    cleaned = npl_df[["DATE", "NPL_diff"]].dropna().reset_index(drop=True)
    logger.info(f"Cleaned NPL: {len(cleaned)} rows.")
    return cleaned


def clean_grr(grr_df: pd.DataFrame):
    """Clean raw GRR table for t_GRR_raw. Output columns: DATE, GRR"""
    grr_df["DATE"] = pd.to_datetime(grr_df["DATE"], errors="coerce") + pd.offsets.MonthEnd(0)
    cleaned = grr_df[["DATE", "GRR"]].dropna().reset_index(drop=True)
    cleaned["GRR"] = pd.to_numeric(cleaned["GRR"], errors="coerce")
    logger.info(f"Cleaned GRR: {len(cleaned)} rows.")
    return cleaned


def clean_mpr(mpr_df: pd.DataFrame):
    """Clean raw MPR table extracted from PDF. Output columns: Year, Month, DATE, MPR"""
    dates = pd.to_datetime(mpr_df["Date"], format="%Y:%m", errors="coerce") + pd.offsets.MonthEnd(0)
    cleaned = pd.DataFrame({
        "Year": dates.dt.year,
        "Month": dates.dt.month,
        "DATE": dates,
        "MPR": pd.to_numeric(mpr_df["MPR"], errors="coerce")
    })
    logger.info(f"Cleaned MPR: {len(cleaned)} rows.")
    return cleaned


def clean_inflation(inflation_df: pd.DataFrame):
    dates = pd.to_datetime(inflation_df["Date"], format="%Y:%m", errors="coerce") + pd.offsets.MonthEnd(0)
    cleaned = pd.DataFrame({
        "Year": dates.dt.year,
        "Month": dates.dt.month,
        "DATE": dates,
        "HeadlineInflation": pd.to_numeric(inflation_df["Inflation"], errors="coerce")
    })
    logger.info(f"Cleaned Inflation: {len(cleaned)} rows.")
    return cleaned


def clean_lending(lending_df: pd.DataFrame):
    lending_df["DATE"] = pd.to_datetime(lending_df["Date"], errors="coerce") + pd.offsets.MonthEnd(0)
    lending_df["CBLR_diff"] = pd.to_numeric(lending_df["LendingRate"], errors="coerce")
    cleaned = lending_df[["DATE", "CBLR_diff"]].dropna().reset_index(drop=True)
    logger.info(f"Cleaned Lending Rate: {len(cleaned)} rows.")
    return cleaned


def clean_gla(gla_df: pd.DataFrame):
    gla_df["DATE"] = pd.to_datetime(gla_df["Date"], errors="coerce") + pd.offsets.MonthEnd(0)
    gla_df["GLA_diff"] = pd.to_numeric(gla_df["GLA"], errors="coerce")
    cleaned = gla_df[["DATE", "GLA_diff"]].dropna().reset_index(drop=True)
    logger.info(f"Cleaned GLA Growth: {len(cleaned)} rows.")
    return cleaned


def clean_loan_advances(loanadv_df: pd.DataFrame):
    loanadv_df["DATE"] = pd.to_datetime(loanadv_df["Date"], errors="coerce") + pd.offsets.MonthEnd(0)
    loanadv_df["Loan_value"] = pd.to_numeric(loanadv_df["LoanAdvances"], errors="coerce")
    cleaned = loanadv_df[["DATE", "Loan_value"]].dropna().reset_index(drop=True)
    logger.info(f"Cleaned Loan Advances: {len(cleaned)} rows.")
    return cleaned


class DatabaseWriter:
    def __init__(self, db_config, table_name, columns):
        """
        db_config : dict with driver/server/db/user/pass
        table_name : str (e.g., 't_npl_raw_NPL')
        columns : list of str (column names in DB in insert order)
        """
        self.db_config = db_config
        self.table_name = table_name
        self.columns = columns
        self.last_run_date = None
        self.db = DatabaseConnection()

    def get_last_date(self):
        """Get the most recent DATE in this table using SQLAlchemy engine."""
        query = text(f"SELECT MAX(DATE) FROM {self.table_name}")
        engine = self.db.get_db_connection()
        with engine.connect() as conn:
            row = conn.execute(query).fetchone()
            return row[0] if row and row[0] else None

    def insert(self, df: pd.DataFrame):
        try:
            """Insert only new rows into the configured table using SQLAlchemy."""
            last_db_date = self.get_last_date()
            logger.info(f"Last DATE in {self.table_name}: {last_db_date}")

            df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")

            if last_db_date:
                last_db_date = pd.to_datetime(last_db_date)
                df = df[df["DATE"] > last_db_date]

            if df.empty:
                logger.info(f"No new rows to insert into {self.table_name}.")
                return

            columns_str = ", ".join(self.columns)
            placeholders_str = ", ".join([f":{col}" for col in self.columns])
            insert_sql = text(f"""
                INSERT INTO {self.table_name} ({columns_str})
                VALUES ({placeholders_str})
            """)

            rows_to_insert = df[self.columns].to_dict(orient="records")

            engine = self.db.get_db_connection()
            with engine.begin() as conn:
                conn.execute(insert_sql, rows_to_insert)

            logger.info(f"Inserted {len(df)} new rows into {self.table_name}.")
        except Exception as e:
            logger.info(f"Error Inserting data. DatabaseWriter.Insert: {type(e)}")

    def run(self):
        """
        Scheduler: every Monday at trigger hour → run once, then sleep 7 days.
        The whole workflow is wrapped in try/except so an unexpected error
        (download failure, PDF layout change, DB drop, etc.) gets logged with
        a full traceback instead of silently killing this thread.
        """
        logger.info("Summary PDF scheduler thread started.")
        # while True:
        try:
            now = datetime.now()
            # logger.info(f"Summary Scheduler waking up (last_run_date={self.last_run_date})...")

            # if self.last_run_date != now.date() and now.weekday() == 4 and now.hour >= 14:
            #     logger.info("Monday trigger hour reached → Running Summary PDF extraction")

            pdf_path = download_latest_summary_pdf()
            extracted = extract_npl_and_mpr_from_pdf(pdf_path)
            db_config = CFG["database"]

            if "NPL" in extracted:
                try:
                    cleaned_npl = clean_npl(extracted["NPL"])
                    DatabaseWriter(db_config, "NPL_raw", ["DATE", "NPL_diff"]).insert(cleaned_npl)
                except Exception as e:
                    logger.error(f"Failed to clean/insert NPL. Exception {type(e)}", exc_info=True)

            if "MPR" in extracted:
                try:
                    cleaned_mpr = clean_mpr(extracted["MPR"])
                    DatabaseWriter(db_config, "t_macro_econs_MPR",
                                   ["Year", "Month", "DATE", "MPR"]).insert(cleaned_mpr)
                except Exception as e:
                    logger.error(f"Failed to clean/insert MPR. Exception {type(e)}", exc_info=True)

            if "Inflation" in extracted:
                try:
                    cleaned_infl = clean_inflation(extracted["Inflation"])
                    DatabaseWriter(db_config, "t_macro_econs_Inflation",
                                   ["Year", "Month", "DATE", "HeadlineInflation"]).insert(cleaned_infl)
                except Exception as e:
                    logger.error(f"Failed to clean/insert Inflation. Exception {type(e)}", exc_info=True)

            if "LendingRate" in extracted:
                try:
                    cleaned_lending = clean_lending(extracted["LendingRate"])
                    DatabaseWriter(db_config, "CBLR_raw", ["DATE", "CBLR_diff"]).insert(cleaned_lending)
                except Exception as e:
                    logger.error(f"Failed to clean/insert Lending Rate. Exception {type(e)}", exc_info=True)

            if "GLA" in extracted:
                try:
                    cleaned_gla = clean_gla(extracted["GLA"])
                    DatabaseWriter(db_config, "GLA_raw", ["DATE", "GLA_diff"]).insert(cleaned_gla)
                except Exception as e:
                    logger.error(f"Failed to clean/insert GLA Growth. Exception {type(e)}", exc_info=True)

            if "LoanAdvances" in extracted:
                try:
                    cleaned_loanadv = clean_loan_advances(extracted["LoanAdvances"])
                    DatabaseWriter(db_config, "t_insightView_LoanPortfolio",
                                   ["DATE", "Loan_value"]).insert(cleaned_loanadv)
                except Exception as e:
                    logger.error(f"Failed to clean/insert Loan Advances. Exception {type(e)}", exc_info=True)

            if "GRR" in extracted:
                try:
                    cleaned_grr = clean_grr(extracted["GRR"])
                    DatabaseWriter(db_config, "t_GRR_raw", ["DATE", "GRR"]).insert(cleaned_grr)
                except Exception as e:
                    logger.error(f"Failed to clean/insert GRR. Exception {type(e)}", exc_info=True)

            self.last_run_date = now.date()
            # logger.info("Summary PDF workflow completed for today. Sleeping 7 days...")
            # time.sleep(7 * 24 * 3600)

            # elif now.weekday() == 4 and now.hour < 14:
            #     logger.info("Monday but before trigger hour → Sleeping 1 hour")
            #     time.sleep(3600)
            # else:
            #     logger.info("Not Monday → Sleeping 1 day")
            #     time.sleep(86400)

        except Exception as e:
            logger.critical(
                f"Summary PDF scheduler hit an unhandled error and would have crashed the "
                f"thread. Sleeping 1 hour before retrying. Exception {type(e)}",
                exc_info=True,
            )
            # time.sleep(3600)

