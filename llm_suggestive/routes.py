# routes.py
from flask import Blueprint, request, jsonify
from datetime import datetime, timezone
from pydantic import ValidationError
from sqlalchemy import text
from Schema import Recommendation
from helper import ENGINE, get_published_address, normalize_record, run_query, insert_recommendations, \
    opex_summary_prompt, financial_summary_prompt,revenue_summary_prompt, call_gpt, insert_summary, send_summary_email,\
    summary_prompt, insert_duration_gap_analysis,DurationGap_PROMPT, get_llm_suggestive_recipients, \
    get_llm_finance_recipients, get_llm_revenue_recipients, get_llm_opex_recipients
import json
from dotenv import load_dotenv
load_dotenv()


bp = Blueprint("api", __name__)


@bp.get("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


@bp.get("/data/sql")
def data_sql():
  sql = """
      ;WITH maxd AS (
    SELECT MAX(TRY_CONVERT(date, [As of Date])) AS max_date
    FROM [DBG_DATA].[dbo].[t_KRI_DATA]
)
SELECT
    t.[KRI ID]              AS relatedEntityId,
    t.[Key Risk Indidcator]            AS metricName,
    t.[CurrentMth_Numeric] AS metricValue,
    t.[As of Date]          AS observedAt,
    t.[KRI Standard]        AS kriStandard,
    t.[Risk Type]           AS riskType,
    t.[Warning_Numeric]      AS warningLimit,
    t.[Warning_Symbol] AS warningLimitOperator,
    t.[Escalation_Numeric] AS escalationLimit,
    t.[Escalation_Symbol] AS escalationLimitOperator,
    t.[Threshold Limits]     AS thresholdLimit,
    t.[Breached KRIs]          AS statusBand,
    CASE
        WHEN t.[Breached KRIs] = '2' THEN 2
        WHEN t.[Breached KRIs] = '1'  THEN 1
        ELSE 0
    END AS breachLevel
FROM [DBG_DATA].[dbo].[t_KRI_DATA] t
WHERE
    t.[TOP_KRIs] = 1
    AND t.[Breached KRIs] > 0
    AND TRY_CONVERT(date, t.[As of Date]) 
        BETWEEN DATEADD(MONTH, -2, (SELECT max_date FROM maxd)) 
            AND (SELECT max_date FROM maxd)
ORDER BY
    breachLevel DESC,
    TRY_CONVERT(date, t.[As of Date]) DESC;

      """
  rows = run_query(sql)
  for r in rows:
    print(f"{r['relatedEntityId']} — {r['metricName']}: {r['metricValue']} | "
          f"status={r['statusBand']} | breachLevel={r['breachLevel']} | "
          f"asOf={r['observedAt']}")
  return jsonify({
        "meta": {"rows": len(rows), "window": "last_12_months_from_max_AsOfDate"},
        "data": {"source": "KRI", "rows": rows}
    })


@bp.post("/recommendations")
def post_recommendations():
    payload = request.get_json(force=True)
    items = payload if isinstance(payload, list) else [payload]
    validated = []
    for it in items:
        try:
            r = Recommendation(**it)
            rec = {
                "source": r.source,
                "relatedEntityId": r.relatedEntityId,
                "metricName": r.metricName,
                "metricValue": r.metricValue,
                "recommendationText": r.recommendationText,
                "actionType": r.actionType,
                "confidence": r.confidence,
                "riskType": r.riskType,
                "referenceTimestamp": r.referenceTimestamp,
                "observedAt": getattr(r, "observedAt", None) or datetime.today().date().isoformat(),
                "metadata": r.metadata or {},
                "postMitigationValue": getattr(r, "postMitigationValue", None)
            }
            validated.append(normalize_record(rec))

        except ValidationError as ve:
            return jsonify({"error": "validation_failed", "detail": ve.errors()}), 400
    inserted = insert_recommendations(validated)
    return jsonify({"inserted": inserted}), 201


@bp.post("/gpt/run")
def gpt_run():
    sql = """
WITH maxd AS (
    SELECT MAX(CONVERT(date, [As of Date])) AS max_date
    FROM v_KRI_Merge_values
    WHERE TOP_KRIs = 1
)
SELECT
    v.[KRI ID]          AS relatedEntityId,
    v.KRI_Name          AS metricName,

    -- 🔹 Add PFI name only for SOL (KRI-05112)
    CASE
        WHEN v.[KRI ID] = 'KRI-05112' THEN v.PFI
        ELSE NULL
    END AS pfiName,

    -- 🔹 Main numeric value the LLM should reason on
    CASE
    WHEN v.[KRI ID] = 'KRI-05112' THEN ROUND(v.Sol_Limit * 100, 2)
    WHEN v.[KRI ID] = 'KRI-05203' THEN ROUND(v.Cap_ratio * 100, 2)
    WHEN v.[KRI ID] = 'KRI-05204' THEN ROUND(v.Leverage_Ratio * 100, 2)
    WHEN v.[KRI ID] = 'KRI-05207' AND v.KRI_Name LIKE '%EUR%' THEN ROUND(v.NOP_EUR_Perc * 100, 2)
    WHEN v.[KRI ID] = 'KRI-05207' AND v.KRI_Name LIKE '%USD%' THEN ROUND(v.NOP_USD_Perc * 100, 2)
    WHEN v.[KRI ID] = 'KRI-05309' THEN ROUND(v.CurrentMth_Val * 100, 2)
    ELSE v.CurrentMth_Val
END AS metricValue,

    v.[As of Date]      AS observedAt,
    v.[Risk Type]       AS riskType,

    -- Thresholds (raw values, as required)
    v.Warning_Numeric       AS warningLimit,
    v.[Warning_Symbol]      AS warningLimitOperator,
    v.Escalation_Numeric    AS escalationLimit,
    v.[Escalation_Symbol]   AS escalationLimitOperator,

    -- Final merged status band
    v.KRI_Status        AS statusBand,

    CASE
    
        WHEN v.KRI_Status = 'Breached' THEN 2
        WHEN v.KRI_Status = 'Warning'  THEN 1
        ELSE 0
    END AS breachLevel

FROM v_KRI_Merge_values AS v
CROSS JOIN maxd
WHERE
    v.TOP_KRIs = 1
    AND v.KRI_Status IN ('Breached', 'Warning')   -- ignore Safe
    AND YEAR(v.[As of Date]) = 2026
    AND MONTH(v.[As of Date]) = 3          -- you filtered month 1 (January)
ORDER BY
    breachLevel DESC,
    CONVERT(date, v.[As of Date]) DESC;
       """

    sql_procedure = """Exec dbo.prcLLMSuggestive_gpt_run"""

    rows = run_query(sql_procedure)
    compact = {"source": "KRI", "window": f"year_{datetime.now().year}", "rows": rows}
    recs = call_gpt(compact)

    validated, errors = [], []

    for it in recs:
      try:
        r = Recommendation(**it)
        rec = {
            "source": r.source,
            "relatedEntityId": r.relatedEntityId,
            "metricName": r.metricName,
            "metricValue": r.metricValue,
            "recommendationText": r.recommendationText,
            "rootCause": r.rootCause,
            "actionType": r.actionType,
            "confidence": r.confidence,
            "riskType": r.riskType,
            "referenceTimestamp": r.referenceTimestamp,
            "observedAt": getattr(r, "observedAt", None)
                or next((row["observedAt"] for row in rows if row["relatedEntityId"] == r.relatedEntityId), None),
            "metadata": r.metadata or {},
            "postMitigationValue": getattr(r, "postMitigationValue", None)
        }
        validated.append(normalize_record(rec))
      except Exception as e:
        errors.append({"item": it, "error": str(e)})
    count = insert_recommendations(validated) if validated else 0
    return jsonify({"generated": len(recs), "inserted":count,  "errors": errors,  "recommendations": validated })


@bp.post("/gpt/durationanalysis")
def gpt_duration_analysis():
    sql = """
  -- Set reference date for April 2026
    WITH ref_date AS (
        SELECT CAST('2026-03-31' AS date) AS TargetDate
    ),
    -- 1. Governance & Outlier Data
    gov_data AS (
        SELECT 
            ReportingDate, 
            'Governance' AS Category, 
            MethodCode, 
            ScenarioCode,
            ScenarioName,
            DeltaEVE_GHS, 
            Tier1Capital_GHS,
            OutlierPctTier1, 
            LimitPctTier1,
            BreachFlag,
            NULL AS EVE_Base_GHS, NULL AS EVE_Shocked_GHS,
            NULL AS PV_Assets_Base_GHS, NULL AS PV_Liabs_Base_GHS,
            NULL AS PV_Assets_Shocked_GHS, NULL AS PV_Liabs_Shocked_GHS,
            NULL AS Duration_Assets_Yrs, NULL AS Duration_Liabs_Yrs,
            NULL AS LeverageRatio, NULL AS DGAP_Yrs,
            Notes
        FROM [DBG_DATA].[dbo].[t_IRRBB_GovernanceBreachLog]
        WHERE CAST(ReportingDate AS date) = (SELECT TargetDate FROM ref_date)
    ),
    -- 2. Detailed Scenario Results
    scenario_data AS (
        SELECT 
            ReportingDate, 
            'Scenario_Analysis' AS Category, 
            MethodCode, 
            ScenarioCode,
            ScenarioName,
            DeltaEVE_GHS, 
            NULL AS Tier1Capital_GHS,
            OutlierPctTier1, 
            LimitPctTier1,
            NULL AS BreachFlag,
            EVE_Base_GHS, 
            EVE_Shocked_GHS,
            PV_Assets_Base_GHS, 
            PV_Liabs_Base_GHS,
            PV_Assets_Shocked_GHS, 
            PV_Liabs_Shocked_GHS,
            Duration_Assets_Yrs, 
            Duration_Liabs_Yrs,
            LeverageRatio, 
            DGAP_Yrs,
            Notes
        FROM [DBG_DATA].[dbo].[t_IRRBB_EVE_MethodScenarioResult]
        WHERE CAST(ReportingDate AS date) = (SELECT TargetDate FROM ref_date)
          AND (IsMaximumDisclosure = 1 OR IsMaximumAll = 1 OR ScenarioClass = 'ENHANCED' OR ScenarioCode = 'FLAT')
    ),
    -- 3. Tier 1 Capital (April Specific)
    tier1_data AS (
        SELECT 
            (SELECT TargetDate FROM ref_date) AS ReportingDate,
            'Capital_Base' AS Category,
            'N/A' AS MethodCode,
            'T1' AS ScenarioCode,
            'Tier 1 Capital Balance' AS ScenarioName,
            NULL AS DeltaEVE_GHS,
            CAST(MAX(Tier1Capital) AS float) AS Tier1Capital_GHS,
            NULL AS OutlierPctTier1,
            NULL AS LimitPctTier1,
            NULL AS BreachFlag,
            NULL AS EVE_Base_GHS, NULL AS EVE_Shocked_GHS,
            NULL AS PV_Assets_Base_GHS, NULL AS PV_Liabs_Base_GHS,
            NULL AS PV_Assets_Shocked_GHS, NULL AS PV_Liabs_Shocked_GHS,
            NULL AS Duration_Assets_Yrs, NULL AS Duration_Liabs_Yrs,
            NULL AS LeverageRatio, NULL AS DGAP_Yrs,
            'April 2026 Tier 1' AS Notes
        FROM [DBG_DATA].[dbo].[t_CalculateCapitalAdequacyRatios]
        WHERE CAST(MonthEndDate AS date) = (SELECT TargetDate FROM ref_date)
    )
    
    -- Final Union: Results for April 2026
    SELECT * FROM gov_data
    UNION ALL
    SELECT * FROM scenario_data
    UNION ALL
    SELECT * FROM tier1_data
    ORDER BY Category DESC, ScenarioName ASC;
    """

    sql_procedure = """Exec dbo.prcLLMSuggestive_DurationAnalysis"""
    rows = run_query(sql_procedure)
    if not rows:
        return jsonify({"saved": False, "error": "No duration analysis rows returned"}), 400

    # latest reporting date from returned dataset
    observed_at = max(r["ReportingDate"] for r in rows)
    compact = {
        "source": "DURATION_GAP",
        "asOfDate": str(observed_at),
        "rows": rows
    }

    payload = {
        "messages": [
            {"role": "system", "content": DurationGap_PROMPT},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}
        ]
    }

    analysis_text = call_gpt(payload)

    inserted = insert_duration_gap_analysis([{
        "observedAt": str(observed_at),
        "AnalysisText": analysis_text
    }])

    return jsonify({
        "saved": inserted == 1,
        "inserted": inserted,
        "asOfDate": str(observed_at),
        "analysis": analysis_text
    })


@bp.post("/gpt/summary")
def gpt_summary():
    return
    sql = """
            ;WITH maxd AS (
            SELECT MAX(TRY_CONVERT(date, [As of Date])) AS max_date
            FROM [DBG_DATA].[dbo].[t_KRI_DATA]
        )
        SELECT
            t.[KRI ID]                AS kriId,
            t.[Key Risk Indidcator]              AS kriName,
            t.[Adjusted Current Mth]  AS adjustedCurrentMth,
            t.[Risk Type]             AS riskType,
            t.[Warning_Numeric]      AS warningLimit,
            t.[Warning_Symbol] AS warningLimitOperator,
            t.[Escalation_Numeric] AS escalationLimit,
            t.[Escalation_Symbol] AS escalationLimitOperator,
            t.[Threshold Limits]     AS thresholdLimit,
            t.[Breached KRIs]          AS statusBand,
            TRY_CONVERT(date, t.[As of Date]) AS asOfDate,
            CASE WHEN t.[Breached KRIs] = 'Breached' THEN 1 ELSE 0 END AS isBreached,
            CASE WHEN t.[Breached KRIs] = 'Warning'  THEN 1 ELSE 0 END AS isWarning,
            CASE WHEN t.[ImpactBin_Col] = 3 AND t.[LikelihoodBin_Col] = 5 THEN 1 ELSE 0 END AS highImpactHighLikelihood
                FROM [DBG_DATA].[dbo].[t_KRI_DATA] t
        CROSS JOIN maxd
        WHERE TRY_CONVERT(date, t.[As of Date]) = maxd.max_date
        AND t.[TOP_KRIs] = 1
        ORDER BY t.[Risk Type], t.[Key Risk Indidcator];
    """
    # sql_procedure = """Exec dbo.prcLLMSuggestive_gpt_summary"""

    rows = run_query(sql)
    compact = {"source": "KRI", "window": "current_month", "rows": rows}
    summary_payload = {
        "messages": [
            {"role": "system", "content": summary_prompt},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}
        ]
    }

    summary_text = call_gpt(summary_payload)
    as_of_date = rows[0]["asOfDate"]
    insert_summary(summary_text, "KRI", as_of_date)

    dashboard_name = None
    dashboard_link = None
    link_info = get_published_address("KRI")
    print("📊 Dashboard link info:", link_info)

    summary_html = summary_text.replace("\n", "<br>") 
    email_body = f"""
            <div style="font-family:Verdana, sans-serif; font-size:12px; color:#222; line-height:1.6;">
                {summary_html}
            </div>
            """
    if link_info:
            dashboard_name, dashboard_link = link_info
            email_body += f"""
                <br><br>
                <p style="font-size:14px; color:#333; line-height:1.6;">
                    For more information, click the link to view the dashboard:
                    <a href="{dashboard_link}" style="color:#0078D4; text-decoration:underline;">
                        {dashboard_name}
                    </a>
                </p>
            """
    else:
        email_body += """
            <br><br>
            <p style="font-size:14px; color:#999; line-height:1.6;">
                Dashboard link currently unavailable.
        </p>
    """
    email_body += """
        <br><br>
        <p style="font-size:14px; color:#333; line-height:1.6;">
            Best regards,<br>
            <strong>InsightView Monitoring System</strong>
        </p>
        <p style="font-size:12px; color:#777; line-height:1.4;">
            This is a system-generated message from InsightView.<br>
            Please do not reply to this email.
        </p>
    """

    recipients = get_llm_suggestive_recipients()
    subject = f"DBG KRI Summary Report – {as_of_date}"

    success = send_summary_email(subject, email_body, recipients, is_html=True)

    if success:
        # Mark as emailed
        update_sql = """
            UPDATE [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
            SET IsEmailed = 1
            WHERE SummaryType = 'KRI' AND AsOfDate = :asOfDate;
        """
        with ENGINE.begin() as conn:
            conn.execute(text(update_sql), {"asOfDate": as_of_date})

        print(f" Summary for {as_of_date} emailed successfully.")
    else:
        print(f" Email failed for summary {as_of_date}. Will remain IsEmailed = 0.")

    return jsonify({
        "summary_saved": True,
        "emailed": success,
        "asOfDate": as_of_date,
        "summary": summary_text
    })
   

@bp.post("/gpt/summary/finance")
def gpt_capital_summary():
    sql = """
    WITH maxd AS (
        SELECT MAX(CAST(MonthEndDate AS date)) AS max_date
                FROM [DBG_DATA].[dbo].t_CalculateCapitalAdequacyRatios
    ),
    actuals AS (
        SELECT
            CAST(MonthEndDate AS date) AS asOfDate,
            Operating_Income,
            Operating_Expense_ADJ AS Operating_Expense,
            (Operating_Income - Operating_Expense_ADJ) AS Profit_Before_Tax,
            (Interest_Income + Other_Income) AS Gross_Revenue,
            Cap_ratio AS Capital_Adequacy_Ratio,
            Return_On_Equity AS ROE,
            (Profit_After_Tax/Total_assets_dbg) AS ROA,
            CTI AS Cost_to_Income_Ratio,
            Leverage_Ratio,
            Liquid_Ratio,
            ABS(Operating_Expense_ADJ + Interest_Expense + Impairments_on_Finance) AS total_expense,
            (Interest_Income + Other_Income) AS total_income
        FROM [DBG_DATA].[dbo].t_CalculateCapitalAdequacyRatios
        CROSS JOIN maxd
        WHERE CAST(MonthEndDate AS date) = maxd.max_date
    ),
    budget_expense AS (
        SELECT ABS(SUM(b.Value)) AS total_expense_bud
        FROM [DBG_DATA].[dbo].Budget b
        JOIN [DBG_DATA].[dbo].t_CHARTOFACCOUNTS c ON b.Line_ID = c.[Line ID]
        CROSS JOIN maxd
        WHERE YEAR(b.Date) = YEAR(maxd.max_date)
        AND MONTH(b.Date) <= MONTH(maxd.max_date)
        AND (
            c.[MAIN CLASS] = 'Interest Expense'
            OR c.[HEADER] = 'OPERATING EXPENSE'
            OR c.[MAIN CLASS] IN ('Impairment On Financial Assets','Exchange Loss on Borrowings')
        )
    ),
    budget_income AS (
        SELECT SUM(b.Value) AS total_income_bud
        FROM [DBG_DATA].[dbo].Budget b
        JOIN [DBG_DATA].[dbo].t_CHARTOFACCOUNTS c ON b.Line_ID = c.[Line ID]
        CROSS JOIN maxd
        WHERE YEAR(b.Date) = YEAR(maxd.max_date)
        AND MONTH(b.Date) <= MONTH(maxd.max_date)
        AND c.[MAIN CLASS] IN ('Interest Income', 'Other Income')
    )
    SELECT
        a.asOfDate,
        a.Operating_Income,
        a.Operating_Expense,
        a.Profit_Before_Tax,
        a.Gross_Revenue,
        a.Capital_Adequacy_Ratio,
        a.ROE,
        a.ROA,
        a.Cost_to_Income_Ratio,
        a.Leverage_Ratio,
        a.Liquid_Ratio,
        CASE WHEN be.total_expense_bud = 0 THEN 0 ELSE a.total_expense / be.total_expense_bud END AS Budget_Utilization_Expense,
        CASE WHEN bi.total_income_bud = 0 THEN 0 ELSE a.total_income / bi.total_income_bud END AS Budget_Utilization_Income
    FROM actuals a
    CROSS JOIN budget_expense be
    CROSS JOIN budget_income bi;
    """

    sql_procedure = """Exec  dbo.prcLLMSuggestive_gpt_summary_finance"""
    rows = run_query(sql_procedure)

    # Pack for GPT
    compact = {"source": "Capital Adequacy", "window": "current_month", "rows": rows}
    summary_payload = {
        "messages": [
            {"role": "system", "content": financial_summary_prompt},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}
        ]
    }

    summary_text = call_gpt(summary_payload)
    as_of_date = rows[0]["asOfDate"]

    with ENGINE.begin() as conn:
        existing = conn.execute(
            text("SELECT COUNT(1) FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries] "
                "WHERE SummaryType = :stype AND AsOfDate = :aDate"),
            {"stype": "Finance", "aDate": as_of_date}
        ).scalar()

    if existing == 0:
        insert_summary(summary_text, "Finance", as_of_date)

    # Get dashboard link if available
    dashboard_name, dashboard_link = None, None
    link_info = get_published_address("Finance")
    print("Dashboard link info:", link_info)
    if link_info:
        dashboard_name, dashboard_link = link_info

    # Build email body
    summary_html = summary_text.replace("\n", "<br>")
    email_body = f"""
    <div style="font-family:Verdana, sans-serif; font-size:12px; color:#222; line-height:1.6;">
        {summary_html}
    </div>
    """
    if dashboard_link:
        email_body += f"""
        <br><br>
        <p style="font-size:14px; color:#333; line-height:1.6;">
            For more information, click the link to view the dashboard:
            <a href="{dashboard_link}" style="color:#0078D4; text-decoration:underline;">
                {dashboard_name}
            </a>
        </p>
        """
    else:
        email_body += """
        <br><br>
        <p style="font-size:14px; color:#999; line-height:1.6;">
            Dashboard link currently unavailable.
        </p>
        """

    email_body += """
    <br><br>
    <p style="font-size:14px; color:#333; line-height:1.6;">
        Best regards,<br>
        <strong>InsightView Monitoring System</strong>
    </p>
    <p style="font-size:12px; color:#777; line-height:1.4;">
        This is a system-generated message from InsightView.<br>
        Please do not reply to this email.
    </p>
    """

    recipients = get_llm_finance_recipients()
    subject = f"DBG's Financial Summary Report – {as_of_date}"

    success = send_summary_email(subject, email_body, recipients, is_html=True)

    if success:
        # Mark as emailed
        update_sql = """
            UPDATE [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
            SET IsEmailed = 1
            WHERE SummaryType = 'Finance' AND AsOfDate = :asOfDate;
        """
        with ENGINE.begin() as conn:
            conn.execute(text(update_sql), {"asOfDate": as_of_date})
        print(f" Summary for {as_of_date} emailed successfully.")
    else:
        print(f" Email failed for summary {as_of_date}. Will remain IsEmailed = 0.")

    return jsonify({
        "summary_saved": True,
        "emailed": success,
        "asOfDate": as_of_date,
        "summary": summary_text
    })

@bp.post("/gpt/summary/revenue_alert")
def gpt_actual_vs_budget_summary():
    sql = """
    DECLARE @MaxDate DATE;
    SELECT @MaxDate = MAX(Date_of_Extraction)
    FROM [DBG_DATA].[dbo].fn_Merged_TBS_Adjustments();

    WITH 
    LatestMerged AS (
        SELECT *
        FROM [dbg_data].[dbo].fn_Merged_TBS_Adjustments()
        WHERE Date_of_Extraction = @MaxDate
    ),
    FilteredAccounts AS (
        SELECT
            [LINE ID],
            HEADER,
            [MAIN CLASS]
        FROM [dbg_data].[dbo].t_CHARTOFACCOUNTS 
        WHERE [MAIN CLASS] IN (
            'Interest Income',
            'Interest Expense',
            'Impairment on Financial Assets',
            'Exchange loss on borrowings',
            'Other Income',
            'Personnel Expense',
            'General Expense'
        )
        AND HEADER IN (
            'OPERATING INCOME',
            'OPERATING EXPENSE'
        )
    ),
    Actuals AS (
        SELECT
            fa.HEADER,
            fa.[MAIN CLASS],
            SUM(md.LCL_CCY_AMT) AS Actual
        FROM LatestMerged md
        JOIN FilteredAccounts fa
            ON md.LINE_ID = fa.[LINE ID]
        GROUP BY fa.HEADER, fa.[MAIN CLASS]
    ),
    LatestBudgetDates AS (
        SELECT 
            Line_ID,
            MAX(Date) AS LatestBudgetDate
        FROM [DBG_DATA].[dbo].Budget
        WHERE Date <= @MaxDate
        GROUP BY Line_ID
    ),
    BudgetWithLatest AS (
        SELECT b.*
        FROM [DBG_DATA].[dbo].Budget b
        INNER JOIN LatestBudgetDates lbd 
            ON b.Line_ID = lbd.Line_ID 
           AND b.Date = lbd.LatestBudgetDate
    ),
    Budgets AS (
        SELECT
            fa.HEADER,
            fa.[MAIN CLASS],
            SUM(b.Cumulative_Value) AS Budget
        FROM BudgetWithLatest b
        JOIN FilteredAccounts fa
            ON b.Line_ID = fa.[LINE ID]
        GROUP BY fa.HEADER, fa.[MAIN CLASS]
    ),
    Combined AS (
        SELECT
            COALESCE(a.HEADER, b.HEADER) AS Header,
            COALESCE(a.[MAIN CLASS], b.[MAIN CLASS]) AS Category,
            COALESCE(a.Actual, 0) AS Actual,
            COALESCE(b.Budget, 0) AS Budget
        FROM Actuals a
        FULL OUTER JOIN Budgets b
            ON a.[MAIN CLASS] = b.[MAIN CLASS]
    )
    SELECT @MaxDate AS asOfDate, Header, Category, Actual, Budget
    FROM (
        SELECT 
            Header, 
            Category, 
            Actual, 
            Budget,
            CASE WHEN Category = 'TOTAL' THEN 1 ELSE 0 END AS SortOrder
        FROM Combined
        UNION ALL
        SELECT 
            Header, 
            'TOTAL' AS Category,
            SUM(Actual) AS Actual,
            SUM(Budget) AS Budget,
            1 AS SortOrder
        FROM Combined
        GROUP BY Header
    ) AS t
    ORDER BY Header, SortOrder, Category;
    """

    sql_procedure = """Exec dbo.prcLLMSuggestive_gpt_summary_revenue_alert"""
    rows = run_query(sql_procedure)

    compact = {"source": "Actual vs Budget", "window": "current_month", "rows": rows}
    summary_payload = {
        "messages": [
            {"role": "system", "content": revenue_summary_prompt},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}
        ]
    }

    summary_text = call_gpt(summary_payload)
    as_of_date = rows[0]["asOfDate"]

    with ENGINE.begin() as conn:
        existing = conn.execute(
            text("SELECT COUNT(1) FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries] "
                "WHERE SummaryType = :stype AND AsOfDate = :aDate"),
            {"stype": "Revenue", "aDate": as_of_date}
        ).scalar()

    if existing == 0:
        insert_summary(summary_text, "Revenue", as_of_date)

    # === Convert GPT pipe table into HTML ===
    def pipe_to_html_table(text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return text  # fallback

        # first line might be summary sentence
        first_line = lines[0]
        data_lines = lines[1:]  # actual pipe rows

        html = "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:Verdana;font-size:12px;'>"
        
        # header from first data line if it contains '|'
        header_line = data_lines[0] if "|" in data_lines[0] else None
        if header_line:
            headers = [h.strip() for h in header_line.split("|")]
            html += "<tr style='background-color:#f2f2f2;font-weight:bold;'>"
            for h in headers:
                html += f"<th>{h}</th>"
            html += "</tr>"
            data_lines = data_lines[1:]  # remove header from rows

        # table rows
        for line in data_lines:
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            html += "<tr>"
            for i, c in enumerate(cols):
                # optional: color Alert column
                if headers and i == len(headers) - 1:  # last column = Alert
                    color = "#28a745" if c.lower() == "positive" else "#dc3545"
                    html += f"<td style='color:{color};font-weight:bold'>{c}</td>"
                else:
                    html += f"<td>{c}</td>"
            html += "</tr>"

        html += "</table>"
        return f"<p>{first_line}</p>{html}"

    summary_html = pipe_to_html_table(summary_text)

    # Email body
    dashboard_name, dashboard_link = None, None
    link_info = get_published_address("Revenue")
    if link_info:
        dashboard_name, dashboard_link = link_info

    email_body = f"<div style='font-family:Verdana; font-size:12px;'>{summary_html}</div>"
    if dashboard_link:
        email_body += f"<p>View dashboard: <a href='{dashboard_link}'>{dashboard_name}</a></p>"
    else:
        email_body += "<p>Dashboard link currently unavailable.</p>"

    email_body += """
    <br><br>
    <p style="font-size:14px; color:#333; line-height:1.6;">
        Best regards,<br>
        <strong>InsightView Monitoring System</strong>
    </p>
    <p style="font-size:12px; color:#777; line-height:1.4;">
        This is a system-generated message from InsightView.<br>
        Please do not reply to this email.
    </p>
    """

    recipients = get_llm_revenue_recipients()
    subject = f"DBG's Revenue Analysis Actual vs Budget Summary – {as_of_date}"

    success = send_summary_email(subject, email_body, recipients, is_html=True)

    if success:
        update_sql = """
        UPDATE [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
        SET IsEmailed = 1
        WHERE SummaryType = 'Revenue' AND AsOfDate = :asOfDate;
        """
        with ENGINE.begin() as conn:
            conn.execute(text(update_sql), {"asOfDate": as_of_date})

    return jsonify({"summary_saved": True, "emailed": success, "asOfDate": as_of_date, "summary": summary_text})

@bp.post("/gpt/summary/opex_alert")
def gpt_opex_act_bdgt_summary():
    sql = """
    DECLARE @MaxDate DATE;

-- Get latest extraction date for actuals
SELECT @MaxDate = MAX(Date_of_Extraction)
FROM [DBG_DATA].[dbo].fn_Merged_TBS_Adjustments();

-- Pull latest adjustments
WITH LatestMerged AS (
    SELECT *
    FROM [DBG_DATA].[dbo].fn_Merged_TBS_Adjustments()
    WHERE Date_of_Extraction = @MaxDate
),

-- Filter chart of accounts for relevant operating expenses
FilteredAccounts AS (
    SELECT
        [LINE ID],
        [SUB CLASS] AS Header,
        [item description] AS Category
    FROM [DBG_DATA].[dbo].t_CHARTOFACCOUNTS
    WHERE [MAIN CLASS] IN ('General Expense', 'Personnel Expense')
      AND HEADER = 'OPERATING EXPENSE'
),

-- Aggregate cumulative actuals per Header/Category
Actuals AS (
    SELECT
        fa.Header,
        fa.Category,
        SUM(md.LCL_CCY_AMT) AS Actual
    FROM LatestMerged md
    JOIN FilteredAccounts fa
        ON md.LINE_ID = fa.[LINE ID]
    GROUP BY fa.Header, fa.Category
),

-- Get latest budget per Line_ID
LatestBudgetDates AS (
    SELECT 
        Line_ID,
        MAX(Date) AS LatestBudgetDate
    FROM [DBG_DATA].[dbo].Budget
    WHERE Date <= @MaxDate
    GROUP BY Line_ID
),

-- Join latest budget values
BudgetWithLatest AS (
    SELECT b.*
    FROM [DBG_DATA].[dbo].Budget b
    INNER JOIN LatestBudgetDates lbd 
        ON b.Line_ID = lbd.Line_ID
       AND b.Date = lbd.LatestBudgetDate
),

-- Aggregate budget per Header/Category
Budgets AS (
    SELECT
        fa.[SUB CLASS] AS Header,
        fa.[item description] AS Category,
        SUM(b.Cumulative_Value) AS Budget
    FROM BudgetWithLatest b
    JOIN [DBG_DATA].[dbo].t_CHARTOFACCOUNTS fa
        ON b.Line_ID = fa.[LINE ID]
    WHERE fa.[MAIN CLASS] IN ('General Expense', 'Personnel Expense')
      AND fa.HEADER = 'OPERATING EXPENSE'
        GROUP BY fa.[SUB CLASS], fa.[item description]
),

-- Combine actuals and budgets, coalesce NULLs
Combined AS (
    SELECT
        COALESCE(a.Header, b.Header) AS Header,
        COALESCE(a.Category, b.Category, a.Header, b.Header) AS Category,
        COALESCE(a.Actual, 0) AS Actual,
        COALESCE(b.Budget, 0) AS Budget
    FROM Actuals a
    FULL OUTER JOIN Budgets b
        ON a.Header = b.Header 
       AND COALESCE(a.Category, a.Header) = COALESCE(b.Category, b.Header)
),

-- Aggregate duplicates after coalescing NULLs
Aggregated AS (
    SELECT 
        Header, 
        Category, 
        SUM(Actual) AS Actual, 
        SUM(Budget) AS Budget
    FROM Combined
    GROUP BY Header, Category
),

-- Add TOTAL per SUB CLASS
WithTotals AS (
    SELECT Header, Category, Actual, Budget, 0 AS SortOrder
    FROM Aggregated

    UNION ALL

    SELECT 
        Header,
        'TOTAL' AS Category,
        SUM(Actual) AS Actual,
        SUM(Budget) AS Budget,
        1 AS SortOrder
    FROM Aggregated
    GROUP BY Header
)

-- Final select ordered by SUB CLASS and Category
SELECT  @MaxDate AS asOfDate, Header AS [Operating Expenses], Category, Actual, Budget
FROM WithTotals
ORDER BY Header, SortOrder, Category;
    """

    sql_procedure = """Exec dbo.prcLLMSuggestive_gpt_summary_opex_alert"""
    rows = run_query(sql_procedure)
    as_of_date = rows[0]["asOfDate"]

    # === Prevent duplicate insertion ===
    with ENGINE.begin() as conn:
        existing = conn.execute(
            text("SELECT COUNT(1) FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries] "
                 "WHERE SummaryType = :stype AND AsOfDate = :aDate"),
            {"stype": "Opex", "aDate": as_of_date}
        ).scalar()

    if existing == 0:
            summary_text = call_gpt({
                "messages": [
                    {"role": "system", "content": opex_summary_prompt},
                    {"role": "user", "content": json.dumps({"source": "Opex Alert", "window": "current_month", "rows": rows}, ensure_ascii=False)}
                ]
            })
            insert_summary(summary_text, "Opex", as_of_date)
    else:
         summary_text = f"Opex summary for {as_of_date} already exists in DB."

    
    def pipe_to_html_table(text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines or "already exists" in text:
            return f"<p>{text}</p>"

        first_line = lines[0]
        data_lines = lines[1:]
        html = "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:Verdana;font-size:12px;'>"

        header_line = data_lines[0] if "|" in data_lines[0] else None
        if header_line:
            headers = [h.strip() for h in header_line.split("|")]
            html += "<tr style='background-color:#f2f2f2;font-weight:bold;'>"
            for h in headers:
                html += f"<th>{h}</th>"
            html += "</tr>"
            data_lines = data_lines[1:]

        for line in data_lines:
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            html += "<tr>"
            for i, c in enumerate(cols):
                if headers and i == len(headers) - 1:
                    color = "#28a745" if c.lower() == "positive" else "#dc3545"
                    html += f"<td style='color:{color};font-weight:bold'>{c}</td>"
                else:
                    html += f"<td>{c}</td>"
            html += "</tr>"

        html += "</table>"
        return f"<p>{first_line}</p>{html}"

    summary_html = pipe_to_html_table(summary_text)

    # === Email ===
    dashboard_name, dashboard_link = None, None
    link_info = get_published_address("Opex")
    if link_info:
        dashboard_name, dashboard_link = link_info

    email_body = f"<div style='font-family:Verdana; font-size:12px;'>{summary_html}</div>"
    if dashboard_link:
        email_body += f"<p>View dashboard: <a href='{dashboard_link}'>{dashboard_name}</a></p>"
    else:
        email_body += "<p>Dashboard link currently unavailable.</p>"

    email_body += """
    <br><br>
    <p style="font-size:14px; color:#333; line-height:1.6;">
        Best regards,<br>
        <strong>InsightView Monitoring System</strong>
    </p>
    <p style="font-size:12px; color:#777; line-height:1.4;">
        This is a system-generated message from InsightView.<br>
        Please do not reply to this email.
    </p>
    """

    recipients = get_llm_opex_recipients()
    subject = f"DBG's Opex Analysis Actual vs Budget Alert System – {as_of_date}"

    success = send_summary_email(subject, email_body, recipients, is_html=True)

    # === Mark emailed ===
    if success:
        with ENGINE.begin() as conn:
            conn.execute(
                text("""
                    UPDATE [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
                    SET IsEmailed = 1
                    WHERE SummaryType = 'Opex' AND AsOfDate = :asOfDate;
                """),
                {"asOfDate": as_of_date}
            )

    return jsonify({"summary_saved": existing == 0, "emailed": success, "asOfDate": as_of_date, "summary": summary_text})

