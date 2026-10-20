# npl_src/extend_panel.py
from __future__ import annotations

from pathlib import Path
import calendar
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from npl_src.db import DatabaseConnection
from npl_src.data_transformation import _read_series, _candidate_stems

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yml").read_text()) or {}

class ExtendPanel:

    def __init__(self):
        self.db_con = DatabaseConnection()

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────
    def _get_anchor_date(self, date: pd.Timestamp) -> pd.Timestamp:
        """
        Dynamically calculates the anchor month for any given date.
        - Jun, Jul, Aug -> June
        - Sep, Oct, Nov -> Sept
        - Dec, Jan, Feb -> Dec
        - Mar, Apr, May -> March
        """
        month = date.month
        year = date.year

        if month in [1, 2]:        # Jan, Feb belong to Dec of previous year
            anchor_month = 12
            anchor_year = year - 1
        elif month in [3, 4, 5]:
            anchor_month = 3
            anchor_year = year
        elif month in [6, 7, 8]:
            anchor_month = 6
            anchor_year = year
        elif month in [9, 10, 11]:
            anchor_month = 9
            anchor_year = year
        else:                      # 12 -> December of current year
            anchor_month = 12
            anchor_year = year

        last_day = calendar.monthrange(anchor_year, anchor_month)[1]
        return pd.Timestamp(year=anchor_year, month=anchor_month, day=last_day)

    # ─────────────────────────────────────────────────────────────────────
    # Transformations
    # ─────────────────────────────────────────────────────────────────────
    def _percent_to_decimal(self, s: pd.Series) -> pd.Series:
        q95 = s.quantile(0.95)
        return s / 100.0 if q95 > 1.5 else s

    def _safe_log(self, s: pd.Series) -> pd.Series:
        return np.log(s.clip(lower=1e-6).astype(float))

    def _logdiff(self, s: pd.Series) -> pd.Series:
        return np.log(s.clip(lower=1e-6).astype(float)).diff()

    def _diff(self, s: pd.Series) -> pd.Series:
        return s.astype(float).diff()

    def _fetch_and_transform_monthly(self) -> pd.DataFrame:
        """Fetches and transforms monthly data based on current DB state."""
        npl_raw  = self._percent_to_decimal(_read_series(_candidate_stems("NPL",  ["NPL_raw",  "NPL"]),                   prefer_quarterly=False))
        degu_raw = self._percent_to_decimal(_read_series(_candidate_stems("DEGU", ["DEGU_raw", "USDGHS", "DEGU"]),        prefer_quarterly=False))
        cblr_raw = self._percent_to_decimal(_read_series(_candidate_stems("CBLR", ["CBLR_raw", "CBLR"]),                  prefer_quarterly=False))
        gla_raw  = self._percent_to_decimal(_read_series(_candidate_stems("GLA",  ["GLA_raw",  "GLA"]),                   prefer_quarterly=False))

        npl_t  = self._safe_log(npl_raw);  npl_t.name  = "NPL"
        degu_t = self._logdiff(degu_raw);  degu_t.name = "DEGU"
        cblr_t = self._diff(cblr_raw);     cblr_t.name = "CBLR"
        gla_t  = self._diff(gla_raw);      gla_t.name  = "GLA"

        return pd.concat([npl_t, degu_t, cblr_t, gla_t], axis=1).sort_index()

    # ─────────────────────────────────────────────────────────────────────
    # Main Method
    # ─────────────────────────────────────────────────────────────────────
    def extend_panel_with_gdp_estimate(self, panel: pd.DataFrame) -> tuple[pd.DataFrame,dict]:
        """
        Extends the panel automatically based on available data.
        """

        is_rolling_mean = False
        # 1. Identify current data state dynamically
        npl_raw_series = _read_series(_candidate_stems("NPL", ["NPL_raw", "NPL"]), prefer_quarterly=False)
        npl_last = npl_raw_series.index.max()
        
        gdp_series = panel["GDP"].dropna()
        gdp_last = gdp_series.index.max()

        # If GDP already covers the period of monthly data, exit
        if npl_last <= gdp_last:
            return panel, {"status": "current", "message": f"All data current. GDP ends: {gdp_last.strftime('%Y-%m')}"}

        # 2. Identify the gap to fill
        monthly_df = self._fetch_and_transform_monthly()
        new_monthly = monthly_df[monthly_df.index > panel.index.max()].copy()
        
        if new_monthly.empty:
            return panel, {"status": "current", "message": "No new monthly data to extend."}

        # 3. Setup dynamic imputation
        new_monthly["GDP"] = np.nan
        # Filter existing history to only include anchor months (Mar, Jun, Sep, Dec)
        gdp_anchors = gdp_series[gdp_series.index.month.isin([3, 6, 9, 12])].copy()
        
        q_value_cache = {} 
        imputed_log = {}

        # 4. Process the gap month-by-month
        for m_date in sorted(new_monthly.index):
            anchor = self._get_anchor_date(m_date)
            
            # Scenario A: Use real value from the data frame if the anchor exists
            if anchor in gdp_series.index:
                val = gdp_series.loc[anchor]
            # Scenario B: Use previously imputed value for this anchor
            elif anchor in q_value_cache:
                val = q_value_cache[anchor]
            # Scenario C: Calculate a new rolling mean (The Imputation)
            else:
                is_rolling_mean = True
                if gdp_anchors.empty: 
                    continue
                
                # Rolling mean of the 8 most recent anchors
                val = float(gdp_anchors.iloc[-8:].mean())
                q_value_cache[anchor] = val
                
                # Update the history so the next quarter (e.g. Dec) sees the previous (e.g. Sep)
                gdp_anchors.loc[anchor] = val
                gdp_anchors = gdp_anchors.sort_index()
                
                imputed_log[anchor.strftime('%Y-%m')] = val
            
            new_monthly.loc[m_date, "GDP"] = val

        # 5. Clean and Merge
        new_monthly = new_monthly.dropna(subset=["GDP", "NPL"])
        new_monthly["ln_npl"] = new_monthly["NPL"]
        new_monthly["gdp"]    = new_monthly["GDP"]

        for col in panel.columns:
            if col not in new_monthly.columns:
                new_monthly[col] = np.nan
        new_monthly = new_monthly[panel.columns]

        # 6. Automatic reporting

        final_imputed_log = {
            date_str: val for date_str, val in imputed_log.items()
            if pd.to_datetime(date_str) <= npl_last
        }

        print("\n" + "=" * 60)
        print("⚠️ GDP IMPUTATION SUMMARY")
        print("=" * 60)
        print(f"   Monthly Data End : {npl_last.strftime('%Y-%m')}")
        print(f"   Real GDP Data End           : {gdp_last.strftime('%Y-%m')}")
        
        # Use the filtered log for printing
        if final_imputed_log and is_rolling_mean:
            print("   Calculated Estimations (Rolling 8-Q Mean):")
            for date_str, value in final_imputed_log.items():
                print(f"    - Anchor {date_str}: {value:.4f}")
        else:
            print("   Status: Forward-filled within existing data range.")
        print("=" * 60 + "\n")

        # Create the report dictionary using the filtered log
        report = {
            "monthly_data_end": npl_last.strftime('%Y-%m'),
            "real_gdp_end": gdp_last.strftime('%Y-%m'),
            "method": "rolling_8q_mean" if (is_rolling_mean and final_imputed_log) else "forward_fill",
            **({
                "GDP imputed_anchors": [
                    {"anchor": date_str, "value": round(value, 4)}
                    for date_str, value in final_imputed_log.items()
                ]
            } if (is_rolling_mean and final_imputed_log) else {})
        }

        print(pd.concat([panel, new_monthly]).sort_index().tail(20).to_string())

        return pd.concat([panel, new_monthly]).sort_index(), report

        
        
        #return pd.concat([panel, new_monthly]).sort_index()