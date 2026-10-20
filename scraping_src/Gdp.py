import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
from pathlib import Path
import yaml, json
import pandas as pd
import pyodbc
import sqlalchemy
from sqlalchemy import create_engine, text
import urllib
import re
import os
import logging
from logging.handlers import RotatingFileHandler
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from scraping_src.driver_manager import get_driver, driver_lock
from sqlalchemy import text
import time
from npl_src.db import DatabaseConnection

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
with open("config.json", "r") as f:
    config = json.load(f)
GURL = config["GURL"]
GHURL = config["GHURL"]
HEADERS = config["HEADERS"]
DATA_FOLDER = Path(ROOT / CFG["data_folder"])
DATA_FOLDER.mkdir(parents=True, exist_ok=True)

LOG_FOLDER = Path(ROOT / "logs")
LOG_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_FOLDER / "gdp_scraper.log"

logger = logging.getLogger("GDPScraper")
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


def scrape_gss_home_expenditure(driver):
    try:
        driver.get(GHURL)

        section_xpath = "//p[contains(normalize-space(), 'Growth by Expenditure')]"
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, section_xpath))
        )

        # Target the Quarterly card specifically (not Annual).
        rate_xpath = (
            section_xpath
            + "/following-sibling::div//p[normalize-space()='Quarterly']"
            + "/following-sibling::p[contains(@class, 'font-extrabold')]"
        )
        period_xpath = (
            section_xpath
            + "/following-sibling::div//p[normalize-space()='Quarterly']"
            + "/following-sibling::p[contains(@class, 'text-slate-400')]"
        )

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, rate_xpath))
        )
        WebDriverWait(driver, 20).until(
            lambda d: re.search(
                r"\d",
                d.find_element(By.XPATH, rate_xpath).text or "",
            )
        )

        rate_text = driver.find_element(By.XPATH, rate_xpath).text.strip()
        period_text = driver.find_element(By.XPATH, period_xpath).text.strip()

        logger.info(f"Scraped from Home Page: {rate_text} for {period_text}")

        rate_str = rate_text.replace("%", "").strip()
        if not rate_str:
            logger.warning("Home page rate text was empty after load; skipping.")
            return None, None

        rate = float(rate_str)
        match = re.search(r"(\d{4})\s+(Q\d)", period_text)
        if match:
            year = match.group(1)
            q = match.group(2)
            q_map = {"Q1": "-03-31", "Q2": "-06-30", "Q3": "-09-30", "Q4": "-12-31"}
            target_date = f"{year}{q_map[q]}"
            return target_date, rate

        logger.warning(f"Could not parse period from home page text: {period_text!r}")
        return None, None

    except Exception:
        logger.error("Home page scrape failed", exc_info=True)
        return None, None


def sync_latest_gdp_report(driver, category_name, log_file="download_history.txt"):
    try:
        driver.get(GURL)

        category_xpath = f"//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{category_name.lower()}')]"
        filter_btn = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, category_xpath)))
        filter_btn.click()
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))
        time.sleep(2)

        rows = driver.find_elements(By.CSS_SELECTOR, "tbody tr")
        downloaded_list = []
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                downloaded_list = f.read().splitlines()

        for row in rows:
            edition_name = row.find_element(By.CSS_SELECTOR, "td.font-medium").text.strip()
            if "december 2025" in edition_name.lower():

                expected_file = Path(DATA_FOLDER) / f"{edition_name}.xlsx"

                if edition_name in downloaded_list and expected_file.exists():
                    logger.info(f"Found {edition_name}, already in folder. Skipping download.")
                    return edition_name

                logger.info(f"Target found: {edition_name}. Downloading now...")
                download_btn = row.find_element(By.CSS_SELECTOR, "button")
                driver.execute_script("arguments[0].scrollIntoView();", download_btn)
                download_btn.click()

                logger.info("Waiting for file to save to disk...")
                max_wait = 30
                for _ in range(max_wait):
                    if expected_file.exists():
                        logger.info("File detected on disk.")
                        break
                    time.sleep(1)
                else:
                    logger.error(f"Timeout: {expected_file.name} never appeared in DATA_FOLDER after {max_wait}s.")
                    return None

                if edition_name not in downloaded_list:
                    with open(log_file, "a") as f:
                        f.write(edition_name + "\n")
                return edition_name
        return None

    except Exception:
        logger.error("Error during GDP report sync", exc_info=True)
        return None


def worker():
    driver = get_driver(download_dir=DATA_FOLDER)

    with driver_lock:
        file = sync_latest_gdp_report(driver, "GDP by expenditure")
    if file:
        logger.info(f"File saved in: {DATA_FOLDER}")


def clean_quarter_string(q_str):
    if not isinstance(q_str, str):
        return None

    q_str = q_str.replace("*", "").strip()
    q_str = re.sub(r"(\d{4})(Q\d)", r"\1 \2", q_str)
    parts = q_str.split()
    if len(parts) != 2:
        return None
    year, q = parts
    if q not in ["Q1", "Q2", "Q3", "Q4"]:
        return None
    return f"{year} {q}"


q_map = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}


def quarter_to_date_safe(q_str):
    year, q = q_str.split()
    return pd.to_datetime(f"{year}-{q_map[q]}")


def clean_qgdpe(file_path: str, last_n: int = 3, include_update: bool = True):
    xl = pd.ExcelFile(file_path)

    target_sheets = [s for s in xl.sheet_names if "qgdp" in s.lower() and "growth" in s.lower()]
    if not target_sheets:
        logger.error(f"Could not find a sheet with 'QGDP' and 'Growth' in its name. Available: {xl.sheet_names}")
        raise ValueError(f"Could not find a sheet with 'QGDP' and 'Growth' in its name. Found: {xl.sheet_names}")

    target_sheet = target_sheets[0]
    df = pd.read_excel(file_path, sheet_name=target_sheet, skiprows=3, engine="openpyxl")

    quarter_col = df.columns[1]

    gdp_col = "Gross Domestic Expenditure - Open Economy"
    if gdp_col not in df.columns:
        logger.error(f"Could not find '{gdp_col}' column in sheet. Available: {df.columns.tolist()}")
        raise ValueError(f"Could not find '{gdp_col}' column in sheet. Available: {df.columns.tolist()}")

    df["Quarter_cleaned"] = df[quarter_col].apply(clean_quarter_string)

    cleaned = pd.DataFrame({
        "DATE": df["Quarter_cleaned"].dropna().apply(quarter_to_date_safe).dt.date,
        "GDP": pd.to_numeric(df[gdp_col], errors="coerce")
    }).dropna()

    if include_update:
        cleaned = cleaned.tail(last_n + 1)
    else:
        cleaned = cleaned.tail(last_n)

    cleaned = cleaned.reset_index(drop=True)

    logger.info(f"Cleaned QGDPE data:\n{cleaned}")

    return cleaned


class GDPDatabaseWriter:
    def __init__(self, db_config, table_name="GDP_raw"):
        self.db_config = db_config
        self.table_name = table_name
        self.last_run_date = None
        self.db = DatabaseConnection()
        logger.info(f"GDPDatabaseWriter initialized. Table: {self.table_name}, Log file: {LOG_FILE}")

    def get_last_db_date(self):
        query = text(f"SELECT MAX(DATE) FROM [{self.table_name}]")
        engine = self.db.get_db_connection()
        with engine.connect() as con:
            result = con.execute(query)
            return result.scalar()

    def get_gdp_by_date(self, date_value):
        query = text(f"SELECT GDP FROM [{self.table_name}] WHERE DATE = :date_value")
        engine = self.db.get_db_connection()
        with engine.connect() as con:
            result = con.execute(query, {"date_value": date_value})
            row = result.fetchone()
            return float(row[0]) if row else None

    def write_gdp_to_db(self, df):
        if df.empty:
            logger.warning("No GDP data to insert or update.")
            return

        df["DATE"] = pd.to_datetime(df["DATE"]).dt.date
        engine = self.db.get_db_connection()

        with engine.begin() as con:

            last_two = df.tail(2)
            for _, row in last_two.iterrows():
                date_val = row["DATE"]
                gdp_value = float(row["GDP"])

                select_query = text(f"SELECT GDP FROM [{self.table_name}] WHERE DATE = :date_val")
                result = con.execute(select_query, {"date_val": date_val}).fetchone()
                db_value = float(result[0]) if result else None

                if db_value is not None and round(db_value, 2) != round(gdp_value, 2):
                    update_query = text(f"UPDATE [{self.table_name}] SET GDP = :gdp_value WHERE DATE = :date_val")
                    con.execute(update_query, {"gdp_value": gdp_value, "date_val": date_val})
                    logger.info(f"Revision found for {date_val}: {db_value} -> {gdp_value}")

            last_date_query = text(f"SELECT MAX(DATE) FROM [{self.table_name}]")
            last_row = con.execute(last_date_query).fetchone()
            last_db_date = last_row[0] if last_row and last_row[0] else None

            if last_db_date:
                last_db_date = pd.to_datetime(last_db_date).date()

            df_to_insert = df
            if last_db_date:
                df_to_insert = df[df["DATE"] > last_db_date]

            if df_to_insert.empty:
                logger.info("No new GDP dates to insert.")
                return

            insert_query = text(f"INSERT INTO [{self.table_name}] (DATE, GDP) VALUES (:date_val, :gdp_value)")
            for _, row in df_to_insert.iterrows():
                con.execute(insert_query, {"date_val": row["DATE"], "gdp_value": float(row["GDP"])})

            logger.info(f"Successfully added {len(df_to_insert)} new row(s) to {self.table_name}.")

    def run(self, clean_gdp_func, driver):
        """Scheduler: coordinates Home Page scout and Excel sync."""
        logger.info("GDP scheduler thread started.")
        # while True:
        now = datetime.now()

        # if self.last_run_date != now.date() and now.weekday() == 4 and now.hour >= 13:
        try:
            with driver_lock:
                hp_date, hp_rate = scrape_gss_home_expenditure(driver)
                db_latest = self.get_last_db_date()
                db_latest_str = str(db_latest) if db_latest else "0000-00-00"

                if hp_date and hp_date > db_latest_str:
                    file_name = sync_latest_gdp_report(driver, "GDP by expenditure")
                else:
                    file_name = None

            if hp_date and hp_date > db_latest_str:
                if file_name:
                    full_path = Path(DATA_FOLDER) / f"{file_name}.xlsx"
                    df = clean_gdp_func(str(full_path))

                    excel_max_date = pd.to_datetime(df["DATE"]).max().date()
                    hp_date_obj = pd.to_datetime(hp_date).date()

                    if excel_max_date < hp_date_obj:
                        logger.warning(f"Excel file lagging ({excel_max_date}). "
                                       f"Adding Home Page headline ({hp_date}).")
                        hp_row = pd.DataFrame([{"DATE": hp_date, "GDP": hp_rate}])
                        df = pd.concat([df, hp_row], ignore_index=True)
                    self.write_gdp_to_db(df)
                else:
                    provisional_df = pd.DataFrame([{"DATE": hp_date, "GDP": hp_rate}])
                    self.write_gdp_to_db(provisional_df)

                self.last_run_date = now.date()

        except Exception as e:
            logger.critical(f"Unhandled error during GDP scheduler run. Exception: {type(e)}", exc_info=True)

            #     time.sleep(3600)
            # else:
            #     time.sleep(1800)

