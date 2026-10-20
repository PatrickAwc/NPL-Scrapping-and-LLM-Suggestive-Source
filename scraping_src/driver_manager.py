import atexit
import logging
import os
import signal
import threading

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger("DriverManager")

_driver = None

_creation_lock = threading.Lock()
driver_lock = threading.RLock()

_shutdown_started = False

def _build_driver(download_dir=None, headless=True):
    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    if download_dir:
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(download_dir)
            }
        )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    logger.info("Shared Chrome WebDriver instance created.")

    return driver

def get_driver(download_dir=None, headless=True):
    """
    Return the one shared driver, creating it only the first time it's called.
    """

    global _driver

    if _driver is None:

        with _creation_lock:

            if _driver is None:

                _driver = _build_driver(
                    download_dir=download_dir,
                    headless=headless
                )

    return _driver

def quit_driver():
    """
    Safely close the shared Selenium WebDriver.

    This function is safe to call multiple times.
    """

    global _driver
    global _shutdown_started

    with _creation_lock:

        if _shutdown_started:
            return

        _shutdown_started = True

        if _driver is not None:

            try:
                logger.info("Closing shared Chrome WebDriver...")

                _driver.quit()

                logger.info(
                    "Shared Chrome WebDriver instance closed."
                )

            except Exception:

                logger.exception(
                    "Error while quitting shared WebDriver."
                )

            finally:

                _driver = None

def _handle_shutdown_signal(signum, frame):
    """
    Handle operating-system termination signals.

    This gives the WebDriver a chance to close before the Python process exits.
    """

    logger.warning(
        "Shutdown signal received: %s",
        signum
    )

    quit_driver()

    # Exit after cleanup
    raise SystemExit(0)
