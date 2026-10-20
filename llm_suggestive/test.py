# smtp_test.py
import os
import smtplib
from dotenv import load_dotenv
from helper import run_query
load_dotenv()  # ensure this runs in the same folder as your .env

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.office365.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

def test_smtp_login():
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("✖ SMTP credentials not found in environment.")
        return False

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(SMTP_EMAIL, SMTP_PASSWORD)
        print("✔ SMTP authentication successful.")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print("✖ Authentication failed (SMTPAuthenticationError). Check username/password or app password.")
        return False
    except smtplib.SMTPException as e:
        print(f"✖ SMTP error: {e}")
        return False
    except Exception as e:
        print(f"✖ Unexpected error: {e}")
        return False

# def get_published_address(summary_type: str) -> tuple[str, str] | None:
#     """
#     Returns (DashboardName, publishedAddress) for the given summary type.
#     Uses the t_PublishedDashboards table.
#     """
#     mapping = {
#         "KRI": "Key Risk Indicator Overview",
#         "Finance": "Financial Overview",
#         "ESG": "ESG Dashboard",
#         "Treasury": "Treasury Performance Dashboard"
#     }

#     dashboard_name = mapping.get(summary_type.upper())
#     if not dashboard_name:
#         return None

#     sql = """
#         SELECT TOP 1 DashboardName, publishedAddress
#         FROM [NPL].[dbo].[t_PublishedDashboards]
#         WHERE DashboardName = :dashboard_name;
#     """
#     rows = run_query(sql, {"dashboard_name": dashboard_name})
#     if rows:
#         return rows[0]["DashboardName"], rows[0]["publishedAddress"]
#     return None

from helper import get_published_address

# Step 1: Get the dashboard link
dashboard_info = get_published_address("KRI")

# Step 2: Sample summary text (simulate GPT response)
summary_text = """
As of May 2025, DBG’s overall risk profile indicates a concerning trend, 
with an increase in breaches across key risk indicators (KRIs) primarily 
within the credit and operational risk categories. 
Management must prioritize corrective actions in line with Basel III and 
Bank of Ghana regulatory frameworks.
"""

# Step 3: Build email body with HTML layout
summary_html = summary_text.replace("\n", "<br>")
email_body = f"""
<html>
<body style="font-family:Segoe UI, sans-serif; font-size:15px; color:#222; line-height:1.6;">

<div style="margin:20px auto; max-width:750px;">
    <p>{summary_html}</p>
"""

if dashboard_info:
    dashboard_name, dashboard_link = dashboard_info
    email_body += f"""
    <div style="text-align:center; margin-top:25px;">
        <a href="{dashboard_link}"
           style="background-color:#0078D4; color:#fff; padding:12px 24px;
                  text-decoration:none; border-radius:6px; font-weight:bold;
                  font-family:Segoe UI, sans-serif;">
            🔗 View {dashboard_name}
        </a>
    </div>
    <p style="text-align:center; font-size:12px; color:#555; margin-top:10px;">
        If the button above doesn’t work, copy and paste this link:<br>
        <a href="{dashboard_link}" style="color:#0078D4;">{dashboard_link}</a>
    </p>
    """
else:
    email_body += """
    <p style="text-align:center; color:#999;">Dashboard link currently unavailable.</p>
    """

email_body += """
</div>
</body>
</html>
"""

# Step 4: Print HTML or save it to a file for preview
print(email_body)

# Optional: save as preview.html to view in browser
with open("preview.html", "w", encoding="utf-8") as f:
    f.write(email_body)

print("\n✅ HTML email preview saved as 'preview.html'. Open it in your browser to view.")


# if __name__ == "__main__":
#     test_smtp_login()
#     result = get_published_address("KRI")
#     print("📊 Result:", result)






@bp.post("/gpt/run")
def gpt_run():
    sql = """
WITH maxd AS (
    SELECT MAX(TRY_CONVERT(date, [As of Date])) AS max_date
    FROM [NPL].[dbo].[t_insightView_KRI]
)
SELECT
    t.[KRI ID]              AS relatedEntityId,
    t.[KRI_Name]            AS metricName,
    t.[Adjusted Current Mth] AS metricValue,
    t.[As of Date]          AS observedAt,
    t.[KRI Standard]        AS kriStandard,
    t.[Risk Type]           AS riskType,
    t.[RiskW]               AS riskW,
    t.[ImpactBin_Col]       AS impactLevel,
    t.[LikelihoodBin_Col]   AS likelihoodBin,
    t.[RiskLevel_Col]       AS probabilityLevel,
    t.[Warning Limit1]      AS warningLimit,
    t.[Warning Limit1 Operator] AS warningLimitOperator,
    t.[Escalaltion Limit 1 Num] AS escalationLimit,
    t.[Escalation Limit1 Operator] AS escalationLimitOperator,
    t.[Threshold_Value]     AS thresholdLimit,
    t.[Threshold_Operator]  AS thresholdOperator,
    t.[ExposureScoreCol]    AS exposureScore,
    t.[KRI Status]          AS statusBand,
    CASE
        WHEN t.[KRI Status] = 'Breached' THEN 2
        WHEN t.[KRI Status] = 'Warning'  THEN 1
        ELSE 0
    END AS breachLevel
FROM [NPL].[dbo].[t_insightView_KRI] t
WHERE
    t.[TOP_KRIs] = 1
    AND t.[Breached KRIs] > 0
    AND TRY_CONVERT(date, t.[As of Date]) 
        BETWEEN '2025-01-01' AND '2025-02-28'
ORDER BY
    breachLevel DESC,
    TRY_CONVERT(date, t.[As of Date]) DESC
"""

    rows = run_query(sql)
    compact = {"source": "KRI", "window": "year_2025", "rows": rows}
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
