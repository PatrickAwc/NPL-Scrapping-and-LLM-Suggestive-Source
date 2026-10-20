import time
from pathlib import Path
import yaml
import traceback
import threading
from scraping_src.ExchangeRates import ExchangeRates
from scraping_src.TBills import TreasuryBillRates
from scraping_src.RawNPL import DatabaseWriter, CFG
from scraping_src.InterestRate import InterbankWeeklyInterestRates
from scraping_src.Gdp import clean_qgdpe, GDPDatabaseWriter, DATA_FOLDER
from scraping_src.MonthlExchangeRate import MonthlyUSExchangeRate
from scraping_src.GIFIM import GFIMManager
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# Load configuration
ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
INT = ROOT / CFG["data_folder"] / "interim"
DB_CFG = CFG["database"]


def start_exchange_rates():
    tb = ExchangeRates()
    tb.run()
    # t = threading.Thread(target=ExchangeRates().run, name="ExchangeRatesWorker", daemon=True)
    # t.start()
    # return t


def start_tbill_rates(start_scheduler=True, run_once=False):
    tb = TreasuryBillRates()
    tb.run()
    # if run_once:
    #     threading.Thread(target=tb.get_tbill_rates, name="TBillRatesOnce", daemon=True).start()
    # if start_scheduler:
    #     threading.Thread(target=tb.run, name="TBillRatesWorker", daemon=True).start()


def start_interest_rate(start_scheduler=True, run_once=False):
    tb = InterbankWeeklyInterestRates()
    tb.run()
    # if run_once:
    #     threading.Thread(target=tb.get_weekly_rates, name="interestatesOnce", daemon=True).start()
    # if start_scheduler:
    #     threading.Thread(target=tb.run, name="interestRateWorker", daemon=True).start()


def start_summary_scheduler():
    # def worker():
    try:
        db_writer = DatabaseWriter(CFG["database"], None, None)
        db_writer.run()
    except Exception as e:
        print(f"❌ Summary scheduler failed: {e}")
        import traceback;
        traceback.print_exc()

    # threading.Thread(target=worker, name="summaryScheduler", daemon=True).start()


def start_qgdpe_extraction():
    # def worker():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    prefs = {
        "download.default_directory": str(DATA_FOLDER),
        "download.prompt_for_download": False,
        "directory_upgrade": True
    }
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        db_config = CFG["database"]
        gdp_writer = GDPDatabaseWriter(db_config, "GDP_raw")
        gdp_writer.run(clean_qgdpe, driver)
    except Exception as e:
        print(f"❌ QGDPE extraction failed: {e}")
        traceback.print_exc()
    finally:
        driver.quit()

    # Start the thread
    # threading.Thread(target=worker, name="qgdpeWorker", daemon=True).start()


def start_gfim():
    gfim = GFIMManager()
    gfim.start_gfim_scheduler()
    # t = threading.Thread(target=gfim.start_gfim_scheduler, name="GFIMScheduler", daemon=True)
    # t.start()


def start_monthly_usd_scraper():
    MonthlyUSExchangeRate().run
    # t = threading.Thread(target=c, name="MonthlyUSDScraper", daemon=True)
    # t.start()
    # return t


def run_scrapping():
    print('running scrapping functions')
    start_monthly_usd_scraper()  # Done
    start_gfim()  # Done
    start_exchange_rates()  # Done
    start_tbill_rates()  # Done
    start_interest_rate()  # Done
    start_summary_scheduler()  # Done
    start_qgdpe_extraction()  # Done


if __name__ == "__main__":
    run_scrapping()

# while True:
#     # execute processes
#     run_scrapping()
#     time.sleep(72000)
