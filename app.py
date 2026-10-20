import ast
import json
import os
import sys
import threading
import urllib
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
from openai import OpenAI
from sqlalchemy import create_engine

from npl_src.db import DatabaseConnection
from npl_src.db_extraction import extract
from npl_src.forecast import forecast
from npl_src.procedures import execute_procedures, list_procedures
from npl_src.processor import processor

from llm_suggestive.helper import auto_scheduler

app = Flask(__name__)
CORS(app)

db_con = DatabaseConnection()

ROOT = Path(__file__).resolve().parent
SRC = (ROOT / "src") if (ROOT / "src").exists() else ROOT
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ── Table descriptions (mirrors database_table_descriptions.csv) ───────────────
TABLE_DESCRIPTIONS = """Table Name:t_npl_raw_CBLR
Table Description:Community Bank Leverage Ratio. The columns are date and CBLR_diff. CBLR_diff is the community bank leverage value. It is a number

Table Name:t_npl_raw_DEGU
Table Description:Exchange Rate Depreciation. The columns are date and DEGU_diff. DEGU_diff is the exchange rate depreciation. It is a number

Table Name:t_npl_raw_GDP
Table Description:Gross Domestic Product. The columns are date and GDP. GDP is the gross domestic product. It is a number

Table Name:t_npl_raw_GLA
Table Description:Gross Leasable Area. The columns are date and GLA_diff. GLA is gross leasable area. It is a number

Table Name:t_npl_raw_NPL
Table Description:Non-Paying Loans. The columns are date and NPL_diff. NPL is non-paying loans. It is a number

Table Name:t_npl_transformed_CBLR
Table Description:Community Bank Leverage Ratio (transformed). The columns are date and CBLR_diff. It is a number

Table Name:t_npl_transformed_DEGU
Table Description:Exchange Rate Depreciation (transformed). The columns are date and DEGU_diff. It is a number

Table Name:t_npl_transformed_GDP
Table Description:Gross Domestic Product (transformed). The columns are date and GDP. It is a number

Table Name:t_npl_transformed_GLA
Table Description:Gross Leasable Area (transformed). The columns are date and GLA_diff. It is a number

Table Name:t_npl_transformed_NPL
Table Description:Non-Paying Loans (transformed). The columns are date and NPL_diff. It is a number
"""

# ── System prompt for the GPT-4o response stage ───────────────────────────────
CHAT_SYSTEM_PROMPT = """You are a senior financial analytics advisor for Development Bank Ghana (DBG), a wholesale bank that channels donor funds through partner banks to SME borrowers.

Your role is to interpret financial data and forecasts and communicate insights clearly to senior business stakeholders and decision-makers — people who understand finance but are not data scientists.

DOMAIN: You answer questions ONLY about:
- Non-Performing Loans (NPL): current levels, historical trends, forecasts, and what they mean for credit risk
- GDP and macroeconomic indicators and how they influence NPL outcomes
- Exchange rate movements (DEGU) and their impact on loan quality
- Community Bank Leverage Ratios (CBLR) and what they signal about banking system health
- Gross Leasable Area (GLA) and its relationship to credit performance
- Model performance, forecast accuracy, and data quality concerns
- SME lending, disbursements, and repayment trends across partner banks

If the user asks about anything outside these topics, politely decline and redirect them.

RESPONSE RULES — follow all of these without exception:

1. NEVER mention internal database table names (e.g. t_npl_raw_NPL, forecast_path_table_latest). 
   Refer to data by what it represents: "NPL data", "GDP figures", "exchange rate data".

2. ALWAYS interpret numbers in business context. Do not just list figures — explain what they mean.
   - A rise in NPL from 18% to 19.4% in one month is a notable deterioration that warrants attention.
   - A GDP freshness delay of 200+ days means the model is running on stale macroeconomic inputs.

3. FLAG anomalies and risks explicitly. Use clear language:
   - "This is a significant increase and should be monitored closely."
   - "This data quality issue could affect the reliability of the forecast."

4. PRIORITISE. Not all concerns carry equal weight. Lead with what matters most to decision-makers.

5. WRITE A NARRATIVE, not a bullet dump. Weave the data into a coherent story.
   Bad: "NPL was 21.74% in July. It will be 20.28% in November."
   Good: "The forecast suggests NPL ratios are expected to ease slightly over the next few months,
          declining from around 21.7% in July to approximately 20.3% by November — a modest improvement,
          though ratios at this level still represent elevated credit risk for the sector."

6. END with a clear takeaway or recommendation where the data supports one.

7. If data is limited or uncertain, say so plainly — but still offer what interpretation you can.
   Do not deflect or tell the user to consult another tool.

8. Keep responses concise — typically 150–300 words unless the question demands more detail.
"""

# ── SQL query prompt for the ReAct agent ──────────────────────────────────────
SQL_AGENT_PROMPT = """You are an agent designed to interact with a SQL Server (MSSQL) database.
Given an input question, create a syntactically correct MSSQL query to run, execute it, and return the results.

Available tables and their descriptions:
{table_details}

CRITICAL RULES:
- Only use tables listed above. Do not query any other tables.
- When multiple variables are requested, create ONE query that retrieves ALL of them together using JOINs on the DATE column.
- Never return DATE as a datetime type — cast it to VARCHAR or return it as a string.
- Never query all columns from a table — only retrieve columns relevant to the question.
- Always limit results to the most recent 2 years of data unless the user specifies otherwise.
- Order results by date in descending order unless otherwise specified.
- DO NOT run any DML statements (INSERT, UPDATE, DELETE, DROP, ALTER).
- You MUST execute the SQL query — do not stop after retrieving the schema.
- If a query fails, rewrite and retry it.
- Always start by checking the available tables in the database.
- Return results in this format: [[x, y]] or [[label, x, y]]
""".format(table_details=TABLE_DESCRIPTIONS)


# ── SQLQueryEngine ─────────────────────────────────────────────────────────────

class SQLQueryEngine:
    """
    Connects to the MSSQL database and runs natural-language questions
    through a LangChain ReAct SQL agent, returning structured results.
    """

    def __init__(self):
        from langchain.chat_models import init_chat_model
        from langchain_community.agent_toolkits import SQLDatabaseToolkit
        from langchain_community.utilities import SQLDatabase

        db_engine = db_con.get_db_connection()
        self.db = SQLDatabase(db_engine, view_support=True, schema="dbo")

        self.llm = init_chat_model("gpt-4o", temperature=0)
        toolkit = SQLDatabaseToolkit(db=self.db, llm=self.llm)
        self.tools = toolkit.get_tools()

    def query(self, user_question: str) -> dict:
        """
        Runs the SQL ReAct agent against the database for the given question.
        Retries up to 3 times. Returns {"data": [...], "sql_query": "..."}.
        """
        from langchain_core.messages import AIMessage, ToolMessage
        from langgraph.prebuilt import create_react_agent

        agent = create_react_agent(self.llm, self.tools, prompt=SQL_AGENT_PROMPT)

        ans_dict = {"data": [], "sql_query": None}

        for attempt in range(3):
            try:
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": user_question}]}
                )

                for message in result.get("messages", []):
                    if isinstance(message, ToolMessage) and message.name == "sql_db_query":
                        raw = message.content
                        try:
                            parsed = ast.literal_eval(raw)
                            if isinstance(parsed, list):
                                ans_dict["data"] = parsed
                        except Exception:
                            try:
                                parsed = json.loads(raw)
                                if isinstance(parsed, list):
                                    ans_dict["data"] = parsed
                            except Exception:
                                pass

                    elif isinstance(message, AIMessage):
                        if message.tool_calls:
                            first = message.tool_calls[0]
                            if first.get("name") == "sql_db_query":
                                ans_dict["sql_query"] = first.get("args", {}).get("query")

                if ans_dict["data"]:
                    break

            except Exception as e:
                print(f"[SQLQueryEngine] attempt {attempt + 1} failed: {e}")

        return ans_dict


def _query_database(user_question: str) -> str:
    """
    Runs the SQL agent for the user's question and formats results
    as a plain-text block to pass into the GPT-4o context.
    Returns an empty string if nothing is retrieved.
    """
    try:
        engine = SQLQueryEngine()
        result = engine.query(user_question)

        data = result.get("data", [])
        sql_query = result.get("sql_query", "")

        if not data:
            return ""

        lines = [f"SQL query executed:\n{sql_query}\n\nResults:"]
        for row in data:
            lines.append(str(row))

        return "\n".join(lines)

    except Exception as e:
        print(f"[_query_database] error: {e}")
        return ""


# ── Existing routes ────────────────────────────────────────────────────────────

@app.route('/api/train_model', methods=['GET'])
def get_train_model():
    try:
        def run_training_task(app_context):
            with app_context:
                print("--- Training Started in Background ---")
                obj = processor()
                obj.run_main()
                print("--- Training Completed ---")

        thread = threading.Thread(target=run_training_task, args=(app.app_context(),))
        thread.start()

        return jsonify({
            "status": "accepted",
            "message": "Model training has started in the background."
        }), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/forecast', methods=['GET'])
def get_forecast():
    try:
        obj1 = processor()
        obj = forecast()

        cfg_path = ROOT / "config.yml"
        if not cfg_path.exists():
            raise FileNotFoundError(f"config.yml not found at {cfg_path}")
        cfg = obj1.load_config(cfg_path)

        obj1._run_module(modname="data_transformation",
                         required=True,
                         candidates=["transform", "run", "build_panel", "main"],
                         cfg=cfg)

        obj1._run_module(modname="framework",
                         required=True,
                         candidates=["run_dq", "run_checks", "dq_main", "main"],
                         cfg=cfg)
        obj.run_main()

        obj1._run_module(modname="monitoring",
                         required=True,
                         candidates=["run", "monitor", "main"],
                         cfg=cfg)

        gdp_report = getattr(obj, "_gdp_report", None)

        return jsonify({"status": "ok", "gdp_imputation": gdp_report}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/procedures", methods=["POST"])
def procedures_handler():
    data = request.get_json()

    if not data or "action" not in data:
        return jsonify({"error": "Invalid request"}), 400

    action = data["action"]

    if action == "list":
        return jsonify({"procedures": list_procedures()})

    if action == "execute":
        procedure_ids = data.get("procedures", [])
        if not procedure_ids:
            return jsonify({"error": "No procedures selected"}), 400
        return jsonify(execute_procedures(procedure_ids))

    return jsonify({"error": "Unsupported action"}), 400


@app.route('/api/dbextract', methods=['GET'])
def extraction():
    obj = extract()
    tables = obj.db_t()

    json_tables = {}
    for table_name, df in tables.items():
        safe_df = df.replace([np.inf, -np.inf], pd.NA)
        records = json.loads(
            safe_df.to_json(orient='records', date_format='iso')
        )
        json_tables[table_name] = records

    return jsonify(json_tables)


# ── Chatbot endpoint ───────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Two-stage pipeline:
      Stage 1 — SQL ReAct agent queries MSSQL using the user's question
      Stage 2 — GPT-4o interprets the SQL results as a business narrative
    """
    try:
        body = request.get_json()
        if not body or "message" not in body:
            return jsonify({"error": "Missing 'message' field"}), 400

        user_message = body.get("message", "").strip()
        if not user_message:
            return jsonify({"error": "Empty message"}), 400

        # ── Stage 1: query the database ────────────────────────────────────────
        db_results = _query_database(user_question=user_message)

        # ── Stage 2: GPT-4o interprets and narrates ────────────────────────────
        if db_results:
            user_content = (
                f"The following data was retrieved from the database in response "
                f"to the user's question:\n\n{db_results}\n\n"
                f"User question: {user_message}"
            )
        else:
            # Fall back to a general financial knowledge answer if SQL returned nothing
            user_content = (
                f"No specific data was retrieved from the database for this question. "
                f"Answer based on your financial knowledge within the DBG domain.\n\n"
                f"User question: {user_message}"
            )

        messages = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=1024,
        )

        reply = response.choices[0].message.content.strip()
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# LLM Suggestive module
def start_auto_scheduler():
    """Launch scheduler in background thread."""
    thread = threading.Thread(target=auto_scheduler, daemon=True)
    thread.start()


if __name__ == "__main__":
    try:
        print("About to start server...")
        port = int(os.getenv("PORT", 5001))
        print(f"Server running on http://localhost:{port}")
        app.run(host="127.0.0.1", port=port, debug=True)
    except Exception as ex:
        print("Sorry... start up error: " + str(ex))
