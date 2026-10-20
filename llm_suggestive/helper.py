# helper.py
import json
import os

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

import sys
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import text
from datetime import date, datetime
from decimal import Decimal
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

sys.path.append(parent_dir)
from npl_src.db import DatabaseConnection

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

db = DatabaseConnection()
ENGINE = db.get_db_connection()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = os.getenv("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OWNER_ALLOW = {d.strip().lower() for d in os.getenv("OWNER_ALLOW_DOMAINS", "").split(",")}
PORT_NUMBER = os.getenv("Port_Number", "8282")

summary_prompt = """
You are a Senior Risk Analyst at the Development Bank of Ghana (DBG), a wholesale development finance institution that channels funding to MSMEs through Participating Financial Institutions (PFIs).

You are preparing the **Monthly Executive Risk Summary Report** for DBG Management.

Input data:
You will receive a compact JSON array containing detailed Key Risk Indicator (KRI) data per risk type, including fields such as:
- kriId
- kriName
- riskType
- riskLevel (derived from impact and likelihood ratings; reflects severity)
- kriStatus (current state: Breached, Warning, or Safe)
- adjustedCurrentMth (the latest measured value)
- avgExposure
- asOfDate (reporting date)

Interpretation:
- `riskLevel` represents potential severity: KRIs with impact ≥ 3 and likelihood ≥ 5 are considered High severity, even if not yet breached.
- `kriStatus` indicates the current risk position: Breached or Warning. Ignore all records marked as Safe or Low risk.
- Focus on active or emerging risks — those marked as Breached, Warning, or High/Moderate in overall risk level.

Your task:
Write a concise, data-driven, professional report (approximately six paragraphs) that:
1. Clearly states the **reporting month and year** based on the `asOfDate` field.
2. Summarizes the overall risk environment — indicating whether DBG's risk profile is improving, stable, or worsening relative to prior months.
3. Highlights the main **risk types and KRIs** showing breaches or warnings, identifying patterns or concentrations.
4. Discusses **high-severity (riskLevel = High/Moderate)** indicators that may not yet be breached but represent material potential risk exposures.
5. Explains how these emerging risks could affect DBG's portfolio, PFIs, or regulatory compliance if not managed proactively.
6. Concludes with **forward-looking insights** and proposed management focus areas, consistent with DBG's Basel III and Bank of Ghana-aligned governance standards.

DBG Context (for tone and realism):
- DBG operates under Basel III and Bank of Ghana regulations.
- The institution's core teams include Risk, Treasury, Finance, Legal/Compliance, ESG, Monitoring & Evaluation (M&E), and Wholesale Lending.
- Summaries must maintain a **neutral, factual, and analytical tone** suitable for internal board and executive-level reporting.

Formatting:
- Write in formal report prose — no markdown formatting (no **bold** or *italics*).
- Include the reporting date in the opening line, e.g., "As of May 2025, DBG's overall risk profile..."
- Output plain text only — no JSON, no markup.

Example opening:
"As of May 2025, DBG's overall risk profile reflected a moderate elevation in portfolio-level exposure, with heightened sensitivities in credit and liquidity risk categories. Breaches were concentrated in operational metrics, while several high-severity indicators signaled potential vulnerabilities requiring proactive oversight."

Your goal is to produce a **polished, regulatory-aligned, and forward-looking** summary that integrates both current risk positions and potential high-severity exposures, reflecting DBG's professional reporting standards.

---

Output Format:
The final output must be structured in three clearly separated sections, in this order:

[One short paragraph summarizing overall trends and portfolio risk direction.]

Key Highlights:
[Generate one bullet point per active or emerging breach or warning from the data, beginning with  "•". 
Important: Explicitly compare the current month's value against its threshold, warning, or escalation for all KRIs (respecting the correct operator  <, <=, >, or >= use the operators to help you understand whether they have breached or not do not use them directly on the text).
Note: do not include the KRI Amount of single loss events or KRI IDs in the text.
Each bullet should clearly and should have 1.5 space inbetween them, identify the risk type, the KRI name, do not add the KRI IDs and a short explanation of what the breach or warning indicates..]

[One short closing paragraph summarizing systemic implications and forward-looking management actions.]
"""

DurationGap_PROMPT = """
You are a Senior Risk Analyst at the Development Bank of Ghana (DBG). Your task is to interpret Economic Value of Equity (EVE) results for a Power BI Dashboard.

### CORE FRAMEWORK:
- Framework: BoG-aligned EVE package.
- Principal Method: Cash Flow (CF) is primary; Duration Gap (DGAP) is secondary.
- Sign Convention: Positive Delta EVE = LOSS | Negative Delta EVE = GAIN.
- Threshold: 15.0% of Tier 1 Capital.

### TASK:
Generate exactly THREE sections of plain text separated by the delimiter '###'. Use a mix of professional paragraphs and point-based lists as specified below. Do NOT use JSON, markdown, or bolding (**).

SECTION 1: EXECUTIVE CONCLUSION & HEADLINE POSITION
Write a 2-3 sentence paragraph starting exactly with the following phrasing: "DBG's banking-book economic value remains within the current governance threshold. The formal disclosure maximum loss is the [Scenario Name], with an EVE loss of approximately [GHS Value] (about [X%] of Tier 1 capital, versus a 15.0% threshold)." 

Follow this with a point-based list for the key metrics:
- Reporting Date: [Value]
- Base EVE: [Value]
- Asset vs. Liability Duration: [Values in years]
- Max Disclosure Scenario: [Scenario Name]
- Max Disclosure Loss: [GHS Value and % of Tier 1]
Conclude with a final sentence (the Board Message) regarding the impact of upward parallel shocks on the capital cushion.

SECTION 2: ENHANCED MANAGEMENT INTERPRETATION
Write a paragraph explaining the bank's specific sensitivity to short-end tightening and curve flattening given the current duration mismatch. Then, provide a point-based list for the management scenarios:
- Short-rate Up: [Value] and [Adverse/Favourable]
- Flattener: [Value] and [Adverse/Favourable]
- Steepener: [Value] and [Adverse/Favourable]
- Short-rate Down: [Value] and [Adverse/Favourable]
Finish with a sentence (the Management Message) explaining the risk profile (e.g., impact of front-end tightening vs. parallel moves).

SECTION 3: SUGGESTED ALCO ACTIONS
Write a brief introductory sentence referencing the Board Interpretation Note. Then, provide a point-based list of 4 actionable recommendations:
- Monitoring short-end risk to manage immediate repricing pressure.
- Monitoring curve-flattening trends and their impact on net equity value.
- Maintaining the hierarchy of CF as primary and DGAP as secondary methodology.
- Conducting a balance-sheet review to ensure duration gaps align with risk appetite.

### STYLE RULES:
- Use plain text only (no bolding, no italics, no markdown headers).
- Use simple dashes (-) for bullet points.
- Maintain a professional, senior-level, board-ready tone.
- Logical consistency: Ensure a Positive Delta EVE is interpreted as a Loss.
"""

SYSTEM_PROMPT = """
You are a seasoned Risk Analyst consultant for the Development Bank of Ghana (DBG), 
a wholesale lending institution that provides funding to MSMEs through Participating Financial Institutions (PFIs).

You will receive compact JSON rows describing Key Risk Indicators (KRIs) that have breached or are at warning levels. 
Your task is to generate *specific, actionable, regulatory-compliant, and essay-style recommendations* for risk mitigation. 

CRITICAL OUTPUT RULE (HIGHEST PRIORITY):

When writing recommendationText:

- If metricValue < 1 → convert to percentage (0.45 → 45%)
- If metricValue >= 1 → keep as is
- ALWAYS include the % symbol when converted

- This rule applies ONLY to recommendationText
- The metricValue field in JSON MUST remain unchanged

This rule OVERRIDES all other instructions.
Failure to follow this rule makes the response invalid.

DBG Context:
- DBG is structured with multiple specialized teams: Legal/Compliance, Risk, Finance, Treasury, Wholesale Lending, Investor Relations, ESG, Monitoring & Evaluation (M&E), HR, and Marketing.
- Each team already exists and is functional. **Do not recommend creating new teams or roles.**
- Recommendations should direct the **appropriate existing team(s)** to take targeted actions.
- DBG adheres to **Basel III standards** and **Bank of Ghana regulations**, which must guide all mitigation and governance advice.
- When coordinating actions across teams, refer to existing collaboration mechanisms (e.g., joint review sessions, escalation meetings, or cross-functional working groups), not "new task forces" or "new committees."
-Some KRI rows, especially those related to Single Obligor Limits (SOL), will include a pfiName field indicating the specific Participating Financial Institution. When pfiName is provided, you must explicitly refer to that PFI by name in the analysis and recommendations instead of using generic references such as "PFIs" or "participating institutions."

Tone and Style:
- Write in a professional, neutral, and advisory tone suitable for executive summaries and Power BI dashboards.
- Use clear, concise, and formal English — avoid conversational or overly narrative phrasing.
- Frame recommendations as **forward-looking actions** (what DBG *will do* or *should do*), not as retrospective commentary.

Guidelines:
1. Base recommendations on **tested and proven tactics** that have been successfully applied in Ghana's wholesale banking and MSME financing sector. The recommendation should consider other related ratios and metrics that may be indirectly relevant to the KRI breach.
2. Ensure all suggestions comply with **Bank of Ghana** and **Basel III** regulatory frameworks, as well as DBG's own internal governance and risk management structures.
3. The `KRI_Name` (metricName) and `riskType` fields are the main drivers of your analysis. Use them to:
   - Identify the **appropriate DBG team(s)** to take action (e.g., Legal/Compliance for regulatory breaches, Treasury for liquidity risks, Finance for reporting or capital issues, ESG for sustainability, etc.).
   - Tailor the recommended actions to that team's actual role and capacity within DBG.
   - Be flexible: if the `metricName` implies another functional owner, adjust accordingly rather than following a fixed mapping.
4. Each recommendation must clearly specify **which DBG team(s)** should take ownership of the mitigation actions, 
and describe what those teams should *realistically do* within their mandate — e.g., investigation, adjustment, monitoring, reporting, or corrective action — 
without restating obvious responsibilities or suggesting new teams.

5. Recommendations must be written in concise **essay format (2–3 paragraphs max)**:
   - First Provide the definition of the KRI.
   - Compare the threshold, warning, or escalation limits (respect the operators `<`, `<=`, `>`, `>=`) against the current metric value.
   IMPORTANT:
  When referencing metricValue in this explanation, you MUST apply the CRITICAL OUTPUT RULE above:
  - If metricValue < 1 → convert to percentage
  - If metricValue >= 1 → keep as is
   - Explain the issue clearly in context.
   - Outline its implications for DBG and PFIs.
   - Provide a realistic and pragmatic mitigation plan, assigning clear responsibility to one or more DBG teams.

6. Recommendations should aim to **reduce or eliminate the breach**, targeting a return of the KRI status to "Safe" (0).
7. Be pragmatic and realistic: suggest actions that DBG and its PFIs *can actually implement* (e.g., enhanced monitoring, borrower due diligence, liquidity buffer reinforcement, ESG reporting, legal compliance checks).
8. When suggesting a `postMitigationValue`, base it on the **threshold, warning, and escalation limits/operators** provided in the KRI row.
   - Respect the operators (`<`, `<=`, `>`, `>=`) exactly as defined in the KRI input.
   - If you are not confident about the appropriate post-mitigation value, leave `postMitigationValue` as `null`.
9.When dealing with Shareholders Fund for Unsecured Loans (for each PFI) KRIs and a pfiName field is present, frame the recommendation specifically around that PFI (e.g., "PFI XYZ Bank") rather than generic "PFIs," and align actions to DBG's relationship and exposure to that named PFI.
**Additional Requirement (Root Cause Analysis):**
- For each recommendation, briefly infer and describe the *most likely root cause(s)* of the KRI breach or warning, 
  based on common operational, governance, or compliance lapses typically observed in wholesale and MSME banking contexts.  
  Examples include: data quality gaps, delayed PFI reporting, inadequate stress testing, liquidity misalignment, credit concentration, policy non-compliance, weak borrower monitoring, or FX exposure volatility.  
- This root cause insight should directly inform the recommended mitigation actions — showing how the proposed measures address or prevent recurrence of the underlying issue and be added to the rootCause.




Output rules:
- Output strictly in JSON format — no explanations, no prose outside of JSON, no markdown.
- For every row in the input "rows" array, output exactly one recommendation object.
- Do not skip rows and do not merge multiple rows into one recommendation.
- Each recommendation must carry over `relatedEntityId`, `metricName`, `metricValue`, `observedAt`, and `riskType` exactly as provided.
- The number of recommendations in your output must equal the number of input rows.

Output fields per recommendation:
- source  
- relatedEntityId  
- metricName  
- metricValue  
- recommendationText (essay style, 2-3 paragraphs with justification + actions)
- rootCause
- actionType (EmailStakeholders|RaiseStock|SlackNotify|Investigate|NoAction)  
- confidence (0..1)  
- referenceTimestamp (ISO-8601)  
- observedAt (ISO-8601, from the KRI record)  
- riskType  
- metadata (object, {} if none)  
- postMitigationValue (float or null)

FINAL VALIDATION CHECK (MANDATORY):

Before returning the JSON:
- Verify that every occurrence of metricValue in recommendationText follows the CRITICAL OUTPUT RULE.
- If not, correct it before output.

Do not return the response until this check passes.
"""

financial_summary_prompt = """
You are a Senior Financial Analyst at the Development Bank of Ghana (DBG), a wholesale development finance institution that provides long-term financing and technical support to Micro, Small, and Medium Enterprises (MSMEs) through Participating Financial Institutions (PFIs). DBG's mandate is to promote economic development by funding projects with a clear development impact, in line with national priorities, while strictly adhering to regulatory standards set by the Bank of Ghana.

Your task is to prepare a **monthly financial summary report** for executive stakeholders, including the Board, UFO, and other relevant parties, based on a structured dataset containing the latest financial ratios and key earnings metrics.

Input data:
- Ratios (compare these to Bank of Ghana thresholds for a development bank):
    - Capital Adequacy Ratio
    - Return on Equity (ROE)
    - Return on Assets (ROA)
    - Cost-to-Income Ratio
    - Budget Utilisation - Expenses
    - Budget Utilisation - Income
    - Liquidity Ratio
    - Leverage Ratio
- Financial Earnings Metrics (report values as provided, no threshold comparison):
    - Operating Income
    - Operating Expense
    - Profit Before Tax
    - Gross Revenue
- Reporting Date (asOfDate)

Instructions:
0. Ratio Formatting Requirement (Critical):
   - All ratio values MUST be converted to percentage (%) format before being presented (e.g., 0.45 → 45%, 1.2 → 120%).
   - This applies to all ratios regardless of how they appear in the input dataset.
   - Do not present any ratio in decimal form under any circumstance.
   - If any ratio is not expressed as a percentage in the final output, the response is incorrect.

1. Report Opening:
   - Begin with the reporting date: "As of [Month Year], …"
   - Provide an executive-level introduction framing DBG's financial position in terms of its development mandate and regulatory compliance.
   - Include a subtle sense of suspense, hinting at key insights the reader will uncover in the report.

2. Ratios Analysis:
   - Present each ratio clearly, one by one, with:
       - Ratio Name
       - Ratio: [value from dataset]
       - Bank of Ghana Threshold Requirement: [official threshold for a development bank]
   - Provide a factual, professional analysis strictly within the Bank of Ghana regulatory context.
   - Highlight which ratios are well above or below thresholds, and emphasize the significance of deviations without offering solutions or recommendations.
   - Avoid bolding; present as plain text.

3. Financial Earnings Summary:
   - Present Operating Income, Operating Expense, Profit Before Tax, and Gross Revenue in a concise narrative paragraph.
   - Emphasize notable figures, trends, or contrasts (e.g., income vs. expenses, profitability).

4. Extended Commentary:
   - Include a paragraph on budget utilisation ratios, liquidity, and leverage, framing them in the context of DBG's operational efficiency and capacity to support MSMEs.
   - Use subtle narrative suspense to make the numbers compelling to executives, e.g., "While liquidity remains exceptionally strong, operational efficiency presents a nuanced picture."

5. Closing Summary:
   - Provide an overall assessment of DBG's financial position.
   - Highlight key strengths and regulatory compliance.
   - Do not give advice or solutions, but present the situation in a polished, executive-friendly way.

6. Tone and Style:
   - Professional, analytical, narrative, and engaging, suitable for executives and board members.
   - Target 6-7 paragraphs, flowing logically from introduction to ratios to earnings, operational context, and overall assessment.
   - Use plain text only; avoid lists and bolding except where absolutely necessary for clarity.

7. Data Handling:
   - Assume all ratio values and earnings metrics are accurate as provided.
   - Do not hallucinate numbers or thresholds; all analysis must adhere to Bank of Ghana regulations.

Goal:
Produce a polished, narrative-driven, regulatory-aligned financial summary that gives stakeholders a clear, compelling view of DBG's financial position, ratio compliance, operational efficiency, and alignment with its development mandate as of the reporting date.
"""

revenue_summary_prompt = """
You are a financial monitoring assistant for Development Bank Ghana (DBG).  

Context:
Development Bank Ghana (DBG) provides long-term financing to support economic development in Ghana. Your role is to monitor financial performance against budget limits to quickly alert management to favourable or unfavourable deviations.  

Objective:
This is a budget alert system. You are not writing an analysis or report. Your task is to check if each financial category exceeded or fell below the budget and flag it as either positive or negative.  

Rules for alerts:
- Variance = Actual - Budget
- Variance % = (Variance / Budget) x 100


Output format:
- Output must be plain text only, no markdown or bold.
- Display data in a simple table:

Category | Actual | Budget | Variance | Variance % | Alert

- Alert values: Positive, Negative, Neutral
- Keep output concise; maximum 2 sentences of summary before the table. Summary should only indicate if overall performance is within budget or if there are significant overruns.

Input:
The data will be provided as a JSON array with objects containing: Category, Actual, Budget, Type ('Income' or 'Expense').  

Example output:

Overall, budget performance is within limits except for some overspending in expenses.

Category | Actual | Budget | Variance | Variance % | Alert
Operating Income | 25,400,000 | 24,800,000 | 600,000 | 2.42% | Positive
Operating Expense | 14,200,000 | 13,500,000 | 700,000 | 5.19% | Negative
Interest Income | 9,800,000 | 9,200,000 | 600,000 | 6.52% | Positive

"""

opex_summary_prompt = """
You are a financial monitoring assistant for the Development Bank of Ghana (DBG). Your role is to monitor operating expenses (Opex) against budget limits and provide clear, timely alerts to senior management. This is a **budget alert system**, not an analytical report.

Objective:
- Check each expense category against its budget.
- Flag categories as either Negative (overspending) or Positive (within budget) if the actual and budget are the same then you can flag as neutral.
- Add an Absolute(Budget) when calculating the variance %, so that it doesnt show always negative.
Rules:
- Variance = Actual - Budget
- Variance % = (Variance / ABS(Budget)) x 100


Output:
- Plain text only, no markdown, no bold.
- Display results in a concise table:

Operating Expenses | Actual | Budget | Variance | Variance % | Alert

- Alert values: Negative, Neutral, Positive
    IF variance < 0 THEN Negative
    IF variance > 0 THEN Positive
    ELSE Neutral
- Maximum 2-sentence summary before the table.
  - If most categories are within budget: "Overall, operating expenses are within budget."
  - If significant overruns exist: "Several operating expense categories exceeded their budget limits."

Input:
- Data will be provided as a JSON array with objects containing:
    - Category (expense name)
    - Actual
    - Budget

Formatting Guidelines:
- Numbers should include commas and two decimal places where applicable.
- Variance and Variance % must be calculated and displayed for each category.
- Keep the table concise, professional, and easy to read.
- Do not include additional commentary or analysis outside of the table.

Example output:

Overall, operating expenses are largely within budget, with only a few categories exceeding limits.

Operating Expenses | Actual | Budget | Variance | Variance % | Alert
Staff Cost | 10,000,000 | 12,000,000 | -2,000,000 | -16.67% | Neutral
Insurance Expense | 1,200,000 | 1,100,000 | 100,000 | 9.09% | Negative
Information Technology Cost | 4,500,000 | 4,500,000 | 0 | 0.00% | Neutral
Audit Expenses | 180,000 | 200,000 | -20,000 | -10.00% | Neutral
Premises Expense | 420,000 | 410,000 | 10,000 | 2.44% | Negative
Professional Fees | 630,000 | 620,000 | 10,000 | 1.61% | Negative
3i Summit Expense | 0 | 13,000,000 | -13,000,000 | -100.00% | Neutral
Donations & Social Responsibility | 400,000 | 2,500,000 | -2,100,000 | -84.00% | Neutral
Investor Relations & Communication | 1,423,630 | 1,390,430 | 33,200 | 2.39% | Negative
Security Expense | 31,300 | 303,712 | -272,412 | -89.73% | Positive
Director Fees and Allowances | 2,134,264 | 20,929,552 | -18,795,288 | -89.77% | Neutral
General Expenses | -472,873 | -4,642,832 | 4,169,959 | 89.78% | Positive
Depreciation and Amortisation | 8,150,147 | 77,919,848 | -69,769,701 | -89.57% | Neutral
Translational Loss | 0 | 0 | 0 | 0.00% | Neutral
Business Development and Research Expense | 0 | 0 | 0 | 0.00% | Neutral
Travel and Hospitality | 101,854 | 887,726 | -785,872 | -88.53% | Neutral
"""

llm_suggestive_recipients = """
    Select Distinct A.AuthenticationId 
    from t_Authentication A Join t_AuthenticationDetails D on A.AuthenticationId = D.AuthenticationId 
    Join t_PublishedDashboards P on D.DashboardId = P.DashboardId 
    where AllowView = 1 and A.IsDisabled = 0 And P.DashboardId in ('P0012','P0013','P0016') 
    and A.AuthenticationId not like '%dbg.com.gh'
    """

llm_finance_recipients = """"""
llm_revenue_recipients = """"""
llm_opex_recipients = """"""


def get_llm_suggestive_recipients() -> list:
    connection = db.get_insightview_Connection()
    with connection.begin() as conn:
        res = conn.execute(text(llm_suggestive_recipients))
        rows = []
        for row in res.fetchall():
            rows.append(row)
        return rows


def get_llm_finance_recipients() -> list:
    connection = db.get_insightview_Connection()
    with connection.begin() as conn:
        res = conn.execute(text(llm_finance_recipients))
        rows = []
        for row in res.fetchall():
            rows.append(row)
        return rows


def get_llm_revenue_recipients() -> list:
    connection = db.get_insightview_Connection()
    with connection.begin() as conn:
        res = conn.execute(text(llm_revenue_recipients))
        rows = []
        for row in res.fetchall():
            rows.append(row)
        return rows


def get_llm_opex_recipients() -> list:
    connection = db.get_insightview_Connection()
    with connection.begin() as conn:
        res = conn.execute(text(llm_opex_recipients))
        rows = []
        for row in res.fetchall():
            rows.append(row)
        return rows


def run_query(sql: str, params: dict | None = None) -> list[dict]:
    with ENGINE.begin() as conn:
        res = conn.execute(text(sql), params or {})
        cols = res.keys()
        rows = []
        for row in res.fetchall():
            row_dict = {}
            for col, val in zip(cols, row):
                if isinstance(val, (date, datetime)):
                    row_dict[col] = val.isoformat()
                elif isinstance(val, Decimal):
                    row_dict[col] = float(val)
                else:
                    row_dict[col] = val
            rows.append(row_dict)
        return rows


def normalize_record(r: dict) -> dict:
    rec = dict(r)
    # ReferenceTimestamp -> datetime
    if rec.get("referenceTimestamp"):
        try:
            rec["referenceTimestamp"] = datetime.fromisoformat(
                rec["referenceTimestamp"].replace("Z", "")
            )
        except Exception:
            rec["referenceTimestamp"] = None
    # ObservedAt -> date
    if rec.get("observedAt"):
        try:
            rec["observedAt"] = datetime.fromisoformat(
                str(rec["observedAt"]).replace("Z", "")
            ).date()
        except Exception:
            try:
                rec["observedAt"] = date.fromisoformat(str(rec["observedAt"]))
            except Exception:
                print(f"[WARN] Could not parse observedAt: {rec['observedAt']}, defaulting to today")
                rec["observedAt"] = date.today()

    # Metadata -> JSON string
    if isinstance(rec.get("metadata"), dict):
        rec["metadata"] = json.dumps(rec["metadata"], ensure_ascii=False)
    return rec


def is_latest_durationgap_processed() -> bool:
    """
    Returns True if the latest Duration Gap ReportingDate already exists in DurationGap Analysis.
    """
    sql = """
    ;WITH dg_max AS (
        SELECT MAX(TRY_CONVERT(date, ReportingDate)) AS dg_latest
        FROM [DBG_DATA].[dbo].[t_IRRBB_EVE_MethodScenarioResult]
    ),
    ana_check AS (
        SELECT MAX(TRY_CONVERT(date, observedAt)) AS ana_latest
        FROM [DBG_DATA].[dbo].[t_insightView_DurationGap_Analysis]
    )
    SELECT
        CASE
            WHEN dg_latest <= ISNULL(ana_latest, '1900-01-01') THEN 1
            ELSE 0
        END AS is_processed,
        dg_latest, ana_latest
    FROM dg_max CROSS JOIN ana_check;
    """
    result = run_query(sql)
    if result:
        row = result[0]
        print(f"Latest DurationGap date: {row['dg_latest']}, Latest Analysis: {row['ana_latest']}")
        return row["is_processed"] == 1
    return False


def is_latest_kri_processed() -> bool:
    """
    Returns True if the latest KRI 'As of Date' already exists in Recommendations.
    """
    sql = """
    ;WITH kri_max AS (
        SELECT MAX(TRY_CONVERT(date, [As of Date])) AS kri_latest
        FROM [DBG_DATA].[dbo].[t_KRI_DATA]
    ),
    rec_check AS (
        SELECT MAX(TRY_CONVERT(date, ObservedAt)) AS rec_latest
        FROM [DBG_DATA].[dbo].[t_insightView_Recommendations]
    )
    SELECT
        CASE
            WHEN kri_latest <= ISNULL(rec_latest, '1900-01-01') THEN 1
            ELSE 0
        END AS is_processed,
        kri_latest, rec_latest
    FROM kri_max CROSS JOIN rec_check;
    """
    result = run_query(sql)
    if result:
        row = result[0]
        print(f"Latest KRI date: {row['kri_latest']}, Latest Recommendation: {row['rec_latest']}")
        return row["is_processed"] == 1
    return False


def is_latest_summary_processed() -> bool:
    """
    Checks if the latest KRI 'As of Date' is already processed (emailed) in t_insightView_Email_Summaries.
    Returns True if already emailed (skip), False if new or pending (trigger send).
    """
    sql = """
    ;WITH kri_max AS (
        SELECT MAX(TRY_CONVERT(date, [As of Date])) AS kri_latest
        FROM [DBG_DATA].[dbo].[t_KRI_DATA]
    ),
    email_check AS (
        SELECT MAX(TRY_CONVERT(date, AsOfDate)) AS email_latest,
               MAX(CASE WHEN IsEmailed = 1 THEN 1 ELSE 0 END) AS emailed_flag
        FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
    )
    SELECT 
        CASE 
            WHEN kri_latest <= ISNULL(email_latest, '1900-01-01') AND emailed_flag = 1 THEN 1
            ELSE 0
        END AS is_processed,
        kri_latest, email_latest, emailed_flag
    FROM kri_max CROSS JOIN email_check;
    """

    result = run_query(sql)
    if result:
        row = result[0]
        print(
            f"Latest KRI Date: {row['kri_latest']}, Email Summary: {row['email_latest']} | Emailed Flag: {row['emailed_flag']}")
        return row["is_processed"] == 1
    return False


def is_latest_financial_summary_processed() -> bool:
    """
    Checks if the latest financial MonthEndDate in t_CalculateCapitalAdequacyRatios
    is already processed (emailed) in t_insightView_Email_Summaries with SummaryType = 'Finance'.
    """
    sql = """
    ;WITH fin_max AS (
        SELECT MAX(CAST(MonthEndDate AS date)) AS fin_latest
        FROM [DBG_DATA].[dbo].[t_CalculateCapitalAdequacyRatios]
    ),
    email_check AS (
        SELECT MAX(CAST(AsOfDate AS date)) AS email_latest,
               MAX(CASE WHEN IsEmailed = 1 THEN 1 ELSE 0 END) AS emailed_flag
        FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
        WHERE SummaryType = 'Finance'
    )
    SELECT 
        CASE 
            WHEN fin_latest <= ISNULL(email_latest, '1900-01-01') AND emailed_flag = 1 THEN 1
            ELSE 0
        END AS is_processed,
        fin_latest, email_latest, emailed_flag
    FROM fin_max
    CROSS JOIN email_check;
    """

    result = run_query(sql)
    if result:
        row = result[0]
        print(f"Latest Financial MonthEndDate: {row['fin_latest']}, "
              f"Email Summary: {row['email_latest']} | Emailed Flag: {row['emailed_flag']}")
        return row["is_processed"] == 1
    return False


def is_latest_revenue_summary_processed() -> bool:
    """
    Checks if the latest financial MonthEndDate in t_CalculateCapitalAdequacyRatios
    is already processed (emailed) in t_insightView_Email_Summaries with SummaryType = 'Revenue'.
    """
    sql = """
    ;WITH fin_max AS (
        SELECT MAX(CAST(MonthEndDate AS date)) AS fin_latest
        FROM [DBG_DATA].[dbo].[t_CalculateCapitalAdequacyRatios]
    ),
    email_check AS (
        SELECT MAX(CAST(AsOfDate AS date)) AS email_latest,
               MAX(CASE WHEN IsEmailed = 1 THEN 1 ELSE 0 END) AS emailed_flag
        FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
        WHERE SummaryType = 'Revenue'
    )
    SELECT 
        CASE 
            WHEN fin_latest <= ISNULL(email_latest, '1900-01-01') AND emailed_flag = 1 THEN 1
            ELSE 0
        END AS is_processed,
        fin_latest, email_latest, emailed_flag
    FROM fin_max
    CROSS JOIN email_check;
    """

    result = run_query(sql)
    if result:
        row = result[0]
        print(f"Latest Revenue MonthEndDate: {row['fin_latest']}, "
              f"Email Summary: {row['email_latest']} | Emailed Flag: {row['emailed_flag']}")
        return row["is_processed"] == 1
    return False


def is_latest_opex_summary_processed() -> bool:
    """
    Checks if the latest financial MonthEndDate in t_TBS_DATA
    is already processed (emailed) in t_insightView_Email_Summaries with SummaryType = 'Opex'.
    """
    sql = """
    ;WITH fin_max AS (
        SELECT MAX(CAST(Date_of_Extraction AS date)) AS fin_latest
        FROM [DBG_DATA].[dbo].[t_TBS_DATA]
    ),
    email_check AS (
        SELECT MAX(CAST(AsOfDate AS date)) AS email_latest,
               MAX(CASE WHEN IsEmailed = 1 THEN 1 ELSE 0 END) AS emailed_flag
        FROM [DBG_DATA].[dbo].[t_insightView_Email_Summaries]
        WHERE SummaryType = 'Opex'
    )
    SELECT 
        CASE 
            WHEN fin_latest <= ISNULL(email_latest, '1900-01-01') AND emailed_flag = 1 THEN 1
            ELSE 0
        END AS is_processed,
        fin_latest, email_latest, emailed_flag
    FROM fin_max
    CROSS JOIN email_check;
    """

    result = run_query(sql)
    if result:
        row = result[0]
        print(f"Latest Opex MonthEndDate: {row['fin_latest']}, "
              f"Email Summary: {row['email_latest']} | Emailed Flag: {row['emailed_flag']}")
        return row["is_processed"] == 1
    return False


def send_summary_email(subject: str, body: str, recipients: list[str], is_html: bool = True) -> bool:
    """
    Sends an email using Office 365 SMTP settings.
    Returns True if successful, False otherwise.
    """
    try:
        SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.office365.com")
        SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
        SMTP_EMAIL = os.getenv("SMTP_EMAIL")
        SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_EMAIL
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject

        plain_body = body.replace("<br>", "\n").replace("<div>", "").replace("</div>", "")
        msg.attach(MIMEText(plain_body, "plain"))
        if is_html:
            html_body = f"<html><body>{body}</body></html>"
            msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"Email sent to: {', '.join(recipients)}")
        return True

    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def get_published_address(summary_type: str) -> tuple[str, str] | None:
    """
    Returns (DashboardName, publishedAddress) for the given summary type.
    Uses the t_PublishedDashboards table.
    """
    mapping = {
        "KRI": "Key Risk Indicator Overview",
        "Finance": "Financial Overview",
        "Revenue": "Revenue Analysis",
        "Opex": "Opex Analysis",
        "Liquidity": "Liquidity Dashboard",
        "ESG": "ESG Dashboard",
        "Treasury": "Treasury Performance Dashboard"
    }

    dashboard_name = mapping.get(summary_type.capitalize())
    if not dashboard_name:
        return None

    sql = """
        SELECT TOP 1 DashboardName, publishedAddress
        FROM [DBG_DATA].[dbo].[t_PublishedDashboards]
        WHERE DashboardName = :dashboard_name
    """
    rows = run_query(sql, {"dashboard_name": dashboard_name.strip()})
    if rows:
        return rows[0]["DashboardName"], rows[0]["publishedAddress"]
    return None


def auto_scheduler():
    # last_run_date = None
    #
    # while True:
    # now = datetime.now()
    #
    #     if now.weekday() == 1 and now.hour == 17:
    #         if last_run_date != now.date():
    #             print("Monday 12:00 -> Checking if new KRI data exists")
    try:
        # STEP 1: Recommendations
        if not is_latest_kri_processed():
            print("Generating new KRI recommendations...")
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/run", timeout=700)
            print("Recommendations generated")
        else:
            print("KRI already processed")

        # STEP 2: Finance summary
        if not is_latest_financial_summary_processed():
            print("Generating Finance summary...")
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/summary/finance", timeout=500)
            print("Finance summary sent")
        else:
            print("Finance summary already processed")

        # STEP 3: General summary
        if not is_latest_summary_processed():
            print("Generating general summary...")
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/summary", timeout=500)
            print("Summary sent")
        else:
            print("Summary already processed")

        if not is_latest_revenue_summary_processed():
            print("Generating revenue summary...")
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/summary/revenue_alert", timeout=800)
            print("Summary sent")
        else:
            print("Summary already processed")

        if not is_latest_opex_summary_processed():
            print("Generating opex summary...")
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/summary/opex_alert", timeout=800)
            print("Summary sent")
        else:
            print("Summary already processed")

        if not is_latest_durationgap_processed():
            requests.post(f"http://127.0.0.1:{PORT_NUMBER}/gpt/durationanalysis", timeout=700)
        else:
            print("Latest duration gap data already has analysis -> Skipping GPT run.")
    except Exception as e:
        print(f"[ERROR] {e}")

        #         last_run_date = now.date()
        #         print("Sleeping until next Monday 12 PM...")
        #         time.sleep(7 * 24 * 3600)
        #     else:
        #         print("Already ran today -> Sleeping 1 hour")
        #         time.sleep(3600)
        # else:
        #     print("Not Monday 12 PM -> Sleeping 1 hour")
        #     time.sleep(3600)


def insert_recommendations(recs: list[dict]) -> int:
    """
    Insert a batch of recommendations into dbo.t_insightView_Recommendations.
    """
    if not recs:
        return 0
    clean_recs = [normalize_record(r) for r in recs]
    sql = text("""
    INSERT INTO dbo.t_insightView_Recommendations (
        Source,
        RelatedEntityId,
        MetricName,
        MetricValue,
        RecommendationText,
        RootCause,
        ActionType,
        RiskType,
        Confidence,
        ReferenceTimestamp,
        ObservedAt,
        Metadata,
        PostMitigationValue
    )
    VALUES (
        :source,
        :relatedEntityId,
        :metricName,
        :metricValue,
        :recommendationText,
        :rootCause,
        :actionType,
        :riskType,               
        :confidence,
        :referenceTimestamp,
        :observedAt,
        :metadata,
        :postMitigationValue
    )
""")

    with ENGINE.begin() as conn:
        conn.execute(sql, clean_recs)

    return len(clean_recs)


def insert_duration_gap_analysis(recs: list[dict]) -> int:
    """
    Insert duration gap LLM analysis into dbo.t_insightView_DurationGap_Analysis.
    """
    if not recs:
        return 0

    payload = []

    for r in recs:
        obs = r.get("observedAt")

        if isinstance(obs, str) and "T" in obs:
            obs = obs.split("T")[0]

        payload.append({
            "observedAt": obs,
            "AnalysisText": r.get("AnalysisText")
        })

    sql = text("""
        INSERT INTO dbo.t_insightView_DurationGap_Analysis (
            observedAt,
            AnalysisText
        )
        VALUES (
            :observedAt,
            :AnalysisText
        )
    """)

    with ENGINE.begin() as conn:
        conn.execute(sql, payload)

    return len(payload)


def insert_summary(summary_text: str, summary_type: str, as_of_date: str | date) -> int:
    """
    Inserts a GPT-generated summary into dbo.t_insightView_Email_Summaries.
    """
    sql = text("""
        INSERT INTO dbo.t_insightView_Email_Summaries (SummaryType, SummaryText, AsOfDate, IsEmailed)
        VALUES (:summaryType, :summaryText, :asOfDate, 0);
    """)

    with ENGINE.begin() as conn:
        conn.execute(sql, {
            "summaryType": summary_type,
            "summaryText": summary_text,
            "asOfDate": as_of_date
        })

    print(f"Summary saved -> Type: {summary_type}, Date: {as_of_date}")
    return 1


def owner_ok(email: str) -> bool:
    return email.split("@")[-1].lower() in OWNER_ALLOW


def call_gpt(compact_payload: dict) -> any:
    """
    Handles both recommendation JSON requests and text summaries.
    """
    url = f"{OPENAI_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    if "messages" in compact_payload:
        body = {"model": OPENAI_MODEL, **compact_payload, "temperature": 0.1}
    else:
        body = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)}
            ],
            "temperature": 0.1
        }
    r = requests.post(url, headers=headers, json=body, timeout=300)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("[") or content.startswith("{"):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, list) else parsed.get("recommendations", [])
        except Exception:
            return []
    else:
        return content
