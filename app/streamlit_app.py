"""
Streamlit frontend for the Warehouse Workforce Forecast Dashboard.

Run locally:
    streamlit run streamlit_app.py

Environment variables:
    FORECAST_API_URL   Optional. Defaults to http://localhost:5000/forecast
    GEMINI_API_KEY     Optional. Enables Gemini executive summary
    GROQ_API_KEY       Optional. Enables Groq fallback executive summary

Required local file:
    scenario_templates.tsv
"""

from __future__ import annotations

import os
from typing import Any, Sequence

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


# =============================================================================
# Configuration
# =============================================================================

APP_TITLE = "Warehouse Workforce Forecast Dashboard 2012"
APP_CAPTION = (
    "Springboard Data Analytics Capstone Project • "
    "Forecasting VET/VTO decisions using retail demand proxy data • "
    "By WiL Low • 2026"
)

SCENARIO_TEMPLATE_PATH = os.getenv("SCENARIO_TEMPLATE_PATH", "scenario_templates.tsv")
FORECAST_API_URL = os.getenv("FORECAST_API_URL", "http://localhost:5000/forecast")

DEFAULT_FORECAST_WEEKS = 12
MIN_FORECAST_WEEKS = 1
MAX_FORECAST_WEEKS = 43

DEFAULT_TEMPERATURE = 45.0
DEFAULT_FUEL_PRICE = 3.20
DEFAULT_CPI = 225.0
DEFAULT_UNEMPLOYMENT = 6.50

DEFAULT_WORKERS_PER_UNIT = 5000
DEFAULT_REGULAR_LABOR_COST = 20
DEFAULT_OVERTIME_LABOR_COST = 30


# =============================================================================
# Page setup and styling
# =============================================================================

st.set_page_config(
    page_title=APP_TITLE,
    layout="wide",
    initial_sidebar_state="collapsed",
)


def apply_custom_css() -> None:
    """Apply lightweight styling for sidebar expanders and action buttons."""
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"] div[data-testid="stExpander"] details {
            border: 1px solid #8a6d1d;
            border-radius: 10px;
            background: rgba(138,109,29,0.18);
            margin-bottom: 10px;
        }

        section[data-testid="stSidebar"] div[data-testid="stExpander"] summary {
            background: rgba(138,109,29,0.35);
            color: #ffd76a;
            border-radius: 10px;
            padding: 6px 10px;
            font-weight: 600;
        }

        div.stButton > button:first-child {
            background: linear-gradient(135deg,#198754,#157347);
            color: white;
            border-radius: 10px;
            border: none;
            padding: 0.55rem 1rem;
            font-weight: 700;
            width: 100%;
            box-shadow: 0 4px 10px rgba(0,0,0,0.25);
        }

        div.stButton > button:first-child:hover {
            background: linear-gradient(135deg,#20a464,#198754);
            color: white;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# =============================================================================
# Data loading
# =============================================================================

@st.cache_data
def load_scenarios(path: str = SCENARIO_TEMPLATE_PATH) -> pd.DataFrame:
    """Load the rule-based recommendation lookup table."""
    return pd.read_csv(path, sep="\t")


scenario_df = load_scenarios()


# =============================================================================
# Classification and scenario helpers
# =============================================================================

def classify_demand_band(result_df: pd.DataFrame) -> str:
    """Classify forecast demand as Low, Normal, or High."""
    peak = result_df["predicted_demand"].max()
    average = result_df["predicted_demand"].mean()
    recent = result_df["predicted_demand"].tail(4).mean()

    score = (peak * 0.25) + (average * 0.45) + (recent * 0.30)
    q75 = result_df["predicted_demand"].quantile(0.75)
    q25 = result_df["predicted_demand"].quantile(0.25)

    if score >= q75:
        return "High"
    if score <= q25:
        return "Low"
    return "Normal"


def flatten_stress_values(*values: int | float | Sequence[int | float]) -> list[float]:
    """Convert scalar or list-based stress inputs into one numeric list."""
    flattened: list[float] = []

    for value in values:
        if isinstance(value, (list, tuple, pd.Series)):
            flattened.extend(float(x) for x in value)
        else:
            flattened.append(float(value))

    return flattened


def classify_stress_band(
    velocity_pct: int | float | Sequence[int | float],
    shipping_delay_pct: int | float | Sequence[int | float],
    congestion_pct: int | float | Sequence[int | float],
    logistics_stress_pct: int | float | Sequence[int | float],
) -> str:
    """Classify operational stress as Low, Medium, or High."""
    score = max(
        flatten_stress_values(
            velocity_pct,
            shipping_delay_pct,
            congestion_pct,
            logistics_stress_pct,
        )
    )

    if score >= 20:
        return "High"
    if score >= 8:
        return "Medium"
    return "Low"


def classify_cost_band(result_df: pd.DataFrame) -> str:
    """Classify total projected labor impact as Low, Medium, or High."""
    total_cost = result_df["estimated_cost"].sum()

    if total_cost >= 25_000:
        return "High"
    if total_cost >= 10_000:
        return "Medium"
    return "Low"


def get_scenario_row(
    demand_band: str,
    stress_band: str,
    cost_band: str,
) -> pd.Series | None:
    """Return the matching scenario recommendation row from the TSV lookup table."""
    row = scenario_df[
        (scenario_df["demand_band"] == demand_band)
        & (scenario_df["stress_band"] == stress_band)
        & (scenario_df["cost_band"] == cost_band)
    ]

    if row.empty:
        return None

    return row.iloc[0].copy()


def build_default_recommendation() -> pd.Series:
    """Fallback recommendation used if the TSV lookup table has no match."""
    return pd.Series(
        {
            "action": "NORMAL",
            "severity": "Info",
            "short_message": "Forecast workload remains operationally stable.",
            "final_recommendation": (
                "Maintain current staffing strategy with continued monitoring."
            ),
            "long_narrative": (
                "The forecast indicates relatively balanced workload conditions "
                "across the planning horizon. Current staffing levels appear "
                "sufficient, though operational monitoring should continue to "
                "identify emerging workload changes."
            ),
        }
    )


def align_recommendation_with_weekly_forecast(
    rec: pd.Series,
    result_df: pd.DataFrame,
    weeks: int,
) -> pd.Series:
    """Align the final action to the dominant weekly VET/VTO/NORMAL output."""
    vet_weeks = (result_df["decision"] == "VET").sum()
    vto_weeks = (result_df["decision"] == "VTO").sum()

    if vet_weeks >= (weeks * 0.6):
        rec["action"] = "VET"
        rec["severity"] = "Critical"
        rec["short_message"] = "Sustained elevated workload detected across forecast horizon."
        rec["final_recommendation"] = (
            "Maintain proactive overtime staffing plans to support forecast "
            "workload requirements."
        )
        rec["long_narrative"] = (
            "The forecast indicates sustained elevated workload conditions "
            "across the planning horizon. Operational planning should prioritize "
            "proactive overtime scheduling and workforce coordination to maintain "
            "throughput stability and reduce backlog risk."
        )

    elif vto_weeks >= (weeks * 0.6):
        rec["action"] = "VTO"
        rec["severity"] = "Warning"
        rec["short_message"] = "Sustained excess labor capacity detected."
        rec["final_recommendation"] = (
            "Use selective VTO and tighter labor scheduling to reduce excess labor costs."
        )
        rec["long_narrative"] = (
            "The forecast indicates sustained periods of excess labor capacity "
            "across the planning horizon. Operations leadership should consider "
            "selective VTO strategies and tighter scheduling discipline to control "
            "labor costs while maintaining staffing flexibility."
        )

    else:
        rec["action"] = "NORMAL"
        rec["severity"] = "Info"
        rec["short_message"] = "Forecast workload remains operationally stable."
        rec["final_recommendation"] = (
            "Maintain current staffing strategy with continued monitoring."
        )
        rec["long_narrative"] = (
            "The forecast indicates relatively balanced workload conditions across "
            "the planning horizon. Current staffing levels appear sufficient, though "
            "operational monitoring should continue to identify emerging workload changes."
        )

    return rec


# =============================================================================
# AI summary helpers
# =============================================================================

def average_value(value: int | float | Sequence[int | float]) -> float:
    """Return the average for scalar or sequence inputs."""
    if isinstance(value, (list, tuple, pd.Series)):
        return float(sum(value) / len(value))
    return float(value)


def calculate_forecast_intelligence(result_df: pd.DataFrame) -> dict[str, Any]:
    """Calculate summary metrics used by AI explanation providers."""
    peak_row = result_df.loc[result_df["predicted_demand"].idxmax()]

    trend_pct = (
        (result_df["predicted_demand"].iloc[-1] - result_df["predicted_demand"].iloc[0])
        / result_df["predicted_demand"].iloc[0]
    ) * 100

    if trend_pct > 5:
        trend_direction = "Rising"
    elif trend_pct < -5:
        trend_direction = "Declining"
    else:
        trend_direction = "Stable"

    volatility = (
        result_df["predicted_demand"].std() / result_df["predicted_demand"].mean()
    ) * 100

    if volatility > 8:
        volatility_band = "High"
    elif volatility > 4:
        volatility_band = "Moderate"
    else:
        volatility_band = "Low"

    return {
        "total_cost": result_df["estimated_cost"].sum(),
        "peak": result_df["predicted_demand"].max(),
        "average": result_df["predicted_demand"].mean(),
        "peak_week": int(peak_row["week"]),
        "trend_direction": trend_direction,
        "volatility_band": volatility_band,
        "max_vet_streak": max_decision_streak(result_df, "VET"),
        "max_vto_streak": max_decision_streak(result_df, "VTO"),
    }


def max_decision_streak(result_df: pd.DataFrame, decision: str) -> int:
    """Return the longest consecutive streak for a forecast decision."""
    max_streak = 0
    current_streak = 0

    for value in result_df["decision"]:
        if value == decision:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak


def build_ai_prompt(
    result_df: pd.DataFrame,
    rec: pd.Series,
    stress_band: str,
    velocity_pct: int | float | Sequence[int | float],
    shipping_delay_pct: int | float | Sequence[int | float],
    congestion_pct: int | float | Sequence[int | float],
    logistics_stress_pct: int | float | Sequence[int | float],
    include_rule_text: bool = True,
) -> str:
    """Build the controlled executive-summary prompt for an LLM provider."""
    metrics = calculate_forecast_intelligence(result_df)

    demand_band = classify_demand_band(result_df)
    cost_band = classify_cost_band(result_df)

    rule_section = ""
    if include_rule_text:
        rule_section = f"""
RULE-BASED RECOMMENDATION

{rec["final_recommendation"]}
"""

    return f"""
You are an operations forecasting analyst for a warehouse workforce planning system.

Your role is to explain forecast results clearly to warehouse leadership.

You MUST only use the supplied metrics.
Do NOT invent trends, causes, percentages, or operational assumptions.

FORECAST METRICS

- Peak forecast week: Week {metrics["peak_week"]}
- Peak forecast demand index: {metrics["peak"]:,.0f}
- Average forecast demand index: {metrics["average"]:,.0f}
- Demand classification: {demand_band}
- Labor cost classification: {cost_band}
- Projected labor cost impact: ${metrics["total_cost"]:,.0f}
- Recommended staffing action: {rec["action"]}

OPERATIONAL STRESS METRICS

- Operational stress level: {stress_band}
- Demand velocity pressure: {average_value(velocity_pct):.1f}%
- Shipping delay pressure: {average_value(shipping_delay_pct):.1f}%
- Warehouse congestion pressure: {average_value(congestion_pct):.1f}%
- Logistics stress pressure: {average_value(logistics_stress_pct):.1f}%

FORECAST INTELLIGENCE METRICS

- Forecast trend direction: {metrics["trend_direction"]}
- Forecast volatility level: {metrics["volatility_band"]}
- Maximum consecutive VET weeks: {metrics["max_vet_streak"]}
- Maximum consecutive VTO weeks: {metrics["max_vto_streak"]}

{rule_section}
RESPONSE REQUIREMENTS

1. Write exactly 5 concise executive sentences.
2. Use professional warehouse operations language.
3. Refer to demand as forecast demand or workload.
4. Do NOT mention AI, algorithms, models, or predictions.
5. Do NOT exaggerate urgency.
6. If operational stress is High, mention coordination pressure and workload balancing carefully.
7. If action is VET, recommend targeted overtime planning before peak workload periods.
8. If action is VTO, recommend cautious labor reduction to avoid operational instability.
9. If action is NORMAL, recommend maintaining staffing levels with continued monitoring.
10. Mention labor cost discipline if labor cost classification is High.
11. Keep the tone operational, realistic, and concise.
"""


def get_gemini_explanation(prompt: str) -> str:
    """Generate the executive summary using Gemini."""
    try:
        from google import genai

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        return response.text.strip()

    except Exception as exc:
        return f"Gemini unavailable: {exc}"


def get_groq_explanation(prompt: str) -> str:
    """Generate the executive summary using Groq as fallback."""
    try:
        from groq import Groq

        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

        return completion.choices[0].message.content.strip()

    except Exception as exc:
        return f"Groq unavailable: {exc}"


def get_ai_explanation(prompt: str) -> tuple[str, str]:
    """Try Gemini first, then Groq. Return the summary and provider name."""
    summary = get_gemini_explanation(prompt)

    if "unavailable" in summary.lower() or "busy" in summary.lower():
        summary = get_groq_explanation(prompt)
        return summary, "Groq"

    return summary, "Gemini"


# =============================================================================
# Sidebar input builders
# =============================================================================

def build_common_sidebar_inputs() -> dict[str, Any]:
    """Build shared sidebar controls."""
    st.sidebar.header("Scenario Inputs")
    st.sidebar.subheader("📅 Forecast Setup")

    weeks = st.sidebar.slider(
        "Forecast Horizon (Weeks)",
        MIN_FORECAST_WEEKS,
        MAX_FORECAST_WEEKS,
        DEFAULT_FORECAST_WEEKS,
    )

    mode = st.sidebar.radio(
        "Input Mode",
        ["Simple Scenario", "Advanced Weekly Table"],
    )

    st.sidebar.markdown("---")

    scenario_name = st.sidebar.text_input(
        "Scenario Name",
        value="Standard Forecast" if mode == "Simple Scenario" else "Advanced Scenario",
    )

    request_id = st.sidebar.text_input(
        "Request ID",
        value="REQ001" if mode == "Simple Scenario" else "REQ002",
    )

    st.sidebar.subheader("👷 Labor Planning")

    workers_per_unit = st.sidebar.number_input(
        "Units per Worker Capacity",
        value=DEFAULT_WORKERS_PER_UNIT,
        help="Estimated workload handled per worker",
    )

    overtime_labor_cost_per_worker = st.sidebar.number_input(
        "Overtime Cost per Hour ($)",
        value=DEFAULT_OVERTIME_LABOR_COST,
        help="Estimated overtime labor rate",
    )

    hourly_labor_cost_per_worker = st.sidebar.number_input(
        "Regular Labor Cost per Hour ($)",
        value=DEFAULT_REGULAR_LABOR_COST,
        help="Standard hourly labor cost",
    )

    st.sidebar.markdown("---")

    return {
        "weeks": weeks,
        "mode": mode,
        "scenario_name": scenario_name,
        "request_id": request_id,
        "workers_per_unit": workers_per_unit,
        "overtime_labor_cost_per_worker": overtime_labor_cost_per_worker,
        "hourly_labor_cost_per_worker": hourly_labor_cost_per_worker,
    }


def build_stress_controls(location: Any = st.sidebar) -> dict[str, int]:
    """Build the operational stress sliders."""
    velocity_pct = location.slider("Demand Velocity (%)", -20, 20, 0)
    shipping_delay_pct = location.slider("Shipping Delay (%)", 0, 30, 0)
    congestion_pct = location.slider("Warehouse Congestion (%)", 0, 30, 0)
    logistics_stress_pct = location.slider("Logistics Stress (%)", 0, 30, 0)

    return {
        "velocity_pct": velocity_pct,
        "shipping_delay_pct": shipping_delay_pct,
        "congestion_pct": congestion_pct,
        "logistics_stress_pct": logistics_stress_pct,
    }


def build_simple_payload(inputs: dict[str, Any]) -> dict[str, Any]:
    """Build API payload for simple scenario mode."""
    st.sidebar.subheader("📈 Economic Drivers")

    temperature = st.sidebar.number_input("Temperature", value=DEFAULT_TEMPERATURE)
    fuel_price = st.sidebar.number_input("Fuel Price", value=DEFAULT_FUEL_PRICE)
    cpi = st.sidebar.number_input("CPI Index", value=DEFAULT_CPI)
    unemployment = st.sidebar.number_input(
        "Unemployment Rate (%)",
        value=DEFAULT_UNEMPLOYMENT,
    )
    holiday = st.sidebar.selectbox("Holiday Demand Week", [0, 1])

    st.sidebar.markdown("---")

    with st.sidebar.expander("⚙️ Advanced Scenario Stress Testing"):
        stress = build_stress_controls(st)

    st.sidebar.markdown("---")

    weeks = inputs["weeks"]

    payload = {
        "mode": "simple",
        "request_id": inputs["request_id"],
        "scenario_name": inputs["scenario_name"],
        "weeks": weeks,
        "inputs": {
            "temperature": [temperature] * weeks,
            "fuel_price": [fuel_price] * weeks,
            "cpi": [cpi] * weeks,
            "unemployment": [unemployment] * weeks,
            "isholiday": [holiday] * weeks,
        },
        "settings": {
            "workers_per_unit": inputs["workers_per_unit"],
            "overtime_labor_cost_per_worker": inputs["overtime_labor_cost_per_worker"],
            "hourly_labor_cost_per_worker": inputs["hourly_labor_cost_per_worker"],
            **stress,
        },
    }

    return payload


def build_advanced_payload(inputs: dict[str, Any]) -> dict[str, Any]:
    """Build API payload for advanced weekly table mode."""
    with st.sidebar.expander("⚙️ Advanced Scenario Stress Testing"):
        stress_defaults = build_stress_controls(st)

    st.subheader("Advanced Weekly Scenario Table")

    weeks = inputs["weeks"]

    default_df = pd.DataFrame(
        {
            "week": range(1, weeks + 1),
            "temperature": [DEFAULT_TEMPERATURE] * weeks,
            "fuel_price": [DEFAULT_FUEL_PRICE] * weeks,
            "cpi": [DEFAULT_CPI] * weeks,
            "unemployment": [DEFAULT_UNEMPLOYMENT] * weeks,
            "isholiday": [0] * weeks,
            "velocity_pct": [stress_defaults["velocity_pct"]] * weeks,
            "shipping_delay_pct": [stress_defaults["shipping_delay_pct"]] * weeks,
            "congestion_pct": [stress_defaults["congestion_pct"]] * weeks,
            "logistics_stress_pct": [stress_defaults["logistics_stress_pct"]] * weeks,
        }
    )

    edited_df = st.data_editor(
        default_df,
        use_container_width=True,
        num_rows="fixed",
    )

    payload = {
        "mode": "advanced",
        "request_id": inputs["request_id"],
        "scenario_name": inputs["scenario_name"],
        "weeks": weeks,
        "inputs": {
            "temperature": edited_df["temperature"].tolist(),
            "fuel_price": edited_df["fuel_price"].tolist(),
            "cpi": edited_df["cpi"].tolist(),
            "unemployment": edited_df["unemployment"].tolist(),
            "isholiday": edited_df["isholiday"].tolist(),
        },
        "settings": {
            "workers_per_unit": inputs["workers_per_unit"],
            "overtime_labor_cost_per_worker": inputs["overtime_labor_cost_per_worker"],
            "hourly_labor_cost_per_worker": inputs["hourly_labor_cost_per_worker"],
            "velocity_pct": edited_df["velocity_pct"].tolist(),
            "shipping_delay_pct": edited_df["shipping_delay_pct"].tolist(),
            "congestion_pct": edited_df["congestion_pct"].tolist(),
            "logistics_stress_pct": edited_df["logistics_stress_pct"].tolist(),
        },
    }

    return payload


# =============================================================================
# API and rendering helpers
# =============================================================================

def call_forecast_api(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Call the Flask forecasting API and return JSON data."""
    try:
        response = requests.post(FORECAST_API_URL, json=payload, timeout=60)

        if response.status_code != 200:
            st.text(response.text[:1000])
            st.error("Backend returned an error.")
            return None

        st.success("Forecast Completed")
        return response.json()

    except requests.exceptions.RequestException as exc:
        st.error(f"Could not connect to Flask API: {exc}")
        return None

    except Exception as exc:
        st.error(f"Application error: {exc}")
        return None


def render_executive_summary(data: dict[str, Any]) -> None:
    """Render top-line KPI metrics."""
    st.subheader("Executive Summary")

    col1, col2, col3, col4 = st.columns(4)

    col1.metric("VET Weeks", data["summary"]["vet_weeks"])
    col2.metric("VTO Weeks", data["summary"]["vto_weeks"])
    col3.metric("Normal Weeks", data["summary"]["normal_weeks"])
    col4.metric("Total Cost", f'${data["summary"]["total_cost"]:,.0f}')


def render_forecast_charts(result_df: pd.DataFrame, weeks: int) -> None:
    """Render demand, weekly cost, and cumulative cost charts."""
    st.subheader("Forecast Output")

    common_layout = dict(
        height=260,
        margin=dict(l=20, r=20, t=45, b=20),
        title_x=0.0,
    )

    demand_fig = px.line(
        result_df,
        x="week",
        y="predicted_demand",
        markers=True,
        title=f"{weeks} Week Demand Forecast",
    )

    cost_fig = px.bar(
        result_df,
        x="week",
        y="estimated_cost",
        color="decision",
        title="Weekly Labor Cost",
    )

    cumulative_fig = px.line(
        result_df,
        x="week",
        y="cumulative_future_cost",
        markers=True,
        title="Cumulative Future Cost",
    )

    demand_fig.update_layout(**common_layout)
    cost_fig.update_layout(**common_layout)
    cumulative_fig.update_layout(height=260, margin=dict(l=10, r=10, t=35, b=10))

    col1, col2 = st.columns(2)

    with col1:
        st.plotly_chart(demand_fig, use_container_width=True)

    with col2:
        st.plotly_chart(cost_fig, use_container_width=True)

    col_a, col_b, col_c = st.columns([1, 2, 1])

    with col_b:
        st.plotly_chart(cumulative_fig, use_container_width=True)


def render_forecast_table(result_df: pd.DataFrame) -> None:
    """Render the detailed forecast table with decision styling."""
    with st.expander("Detailed Forecast Table"):
        styled_df = (
            result_df.style.format(
                {
                    "week": "{:.0f}",
                    "predicted_demand": "{:,.0f}",
                    "estimated_cost": "${:,.0f}",
                    "cumulative_future_cost": "${:,.0f}",
                    "extra_workers_needed": "{:.0f}",
                    "workers_to_reduce": "{:.0f}",
                }
            )
            .set_properties(
                subset=[
                    "predicted_demand",
                    "estimated_cost",
                    "cumulative_future_cost",
                    "extra_workers_needed",
                    "workers_to_reduce",
                ],
                **{"text-align": "right"},
            )
            .set_properties(
                subset=["week", "decision"],
                **{"text-align": "center"},
            )
            .apply(style_decision_column, axis=1)
            .set_table_styles(
                [
                    {
                        "selector": "th",
                        "props": [
                            ("background-color", "#111111"),
                            ("color", "white"),
                            ("font-weight", "bold"),
                            ("text-align", "center"),
                        ],
                    },
                    {
                        "selector": "td",
                        "props": [
                            ("border", "1px solid #333333"),
                            ("padding", "6px"),
                        ],
                    },
                ]
            )
        )

        st.dataframe(styled_df, use_container_width=True, height=420)


def style_decision_column(row: pd.Series) -> list[str]:
    """Apply badge-style colors to the decision column."""
    styles = []

    for column in row.index:
        if column != "decision":
            styles.append("")
            continue

        base_style = (
            "color:white;"
            "font-weight:bold;"
            "text-align:center;"
            "border-radius:4px;"
        )

        if row["decision"] == "VET":
            styles.append(f"background-color:#145a32;{base_style}")
        elif row["decision"] == "VTO":
            styles.append(f"background-color:#7d5a00;{base_style}")
        elif row["decision"] == "NORMAL":
            styles.append(f"background-color:#444444;{base_style}")
        else:
            styles.append("")

    return styles


def render_recommendations(
    result_df: pd.DataFrame,
    payload: dict[str, Any],
    weeks: int,
) -> None:
    """Render scenario recommendations and AI executive summary."""
    st.subheader("Operational Recommendations")

    settings = payload["settings"]

    demand_band = classify_demand_band(result_df)
    stress_band = classify_stress_band(
        settings["velocity_pct"],
        settings["shipping_delay_pct"],
        settings["congestion_pct"],
        settings["logistics_stress_pct"],
    )
    cost_band = classify_cost_band(result_df)

    rec = get_scenario_row(demand_band, stress_band, cost_band)
    if rec is None:
        rec = build_default_recommendation()

    rec = align_recommendation_with_weekly_forecast(rec, result_df, weeks)

    st.write(f"Scenario: Demand={demand_band} | Stress={stress_band} | Cost={cost_band}")

    render_recommendation_cards(rec, result_df)
    render_rule_engine_explanation(rec)
    render_ai_summary(result_df, rec, stress_band, settings)


def render_recommendation_cards(rec: pd.Series, result_df: pd.DataFrame) -> None:
    """Render recommendation cards."""
    action = rec["action"]
    severity = rec["severity"]

    if severity == "Critical":
        st.error("🔥 " + rec["short_message"])
    elif severity == "Warning":
        st.warning("⚠️ " + rec["short_message"])
    else:
        st.info("📊 " + rec["short_message"])

    st.info("📈 " + rec["final_recommendation"])

    total_cost = result_df["estimated_cost"].sum()
    st.info(f"💰 Projected total labor impact: ${total_cost:,.0f}")

    if action == "VET":
        st.success("✅ Recommended Action: Increase Staffing (VET)")
    elif action == "VTO":
        st.warning("💤 Recommended Action: Offer Voluntary Time Off (VTO)")
    else:
        st.info("🟦 Recommended Action: Maintain Current Staffing")

    peak_row = result_df.loc[result_df["predicted_demand"].idxmax()]
    peak_week = int(peak_row["week"])
    st.error(f"🔥 Highest demand expected in Week {peak_week}. Prepare early.")


def render_rule_engine_explanation(rec: pd.Series) -> None:
    """Render the deterministic rule-engine narrative."""
    with st.expander("### Rule Engine Interpretation"):
        st.info(rec["long_narrative"].replace(". ", ".\n\n"))


def render_ai_summary(
    result_df: pd.DataFrame,
    rec: pd.Series,
    stress_band: str,
    settings: dict[str, Any],
) -> None:
    """Render Gemini/Groq executive summary."""
    st.markdown("### AI Decision Summary")

    prompt = build_ai_prompt(
        result_df=result_df,
        rec=rec,
        stress_band=stress_band,
        velocity_pct=settings["velocity_pct"],
        shipping_delay_pct=settings["shipping_delay_pct"],
        congestion_pct=settings["congestion_pct"],
        logistics_stress_pct=settings["logistics_stress_pct"],
    )

    with st.spinner("Generating AI summary..."):
        ai_summary, provider = get_ai_explanation(prompt)

    formatted_summary = ai_summary.replace(". ", ".\n\n")

    if "unavailable" in ai_summary.lower() or "busy" in ai_summary.lower():
        st.warning(formatted_summary)
    elif provider == "Groq":
        st.warning(formatted_summary)
    else:
        st.success(formatted_summary)


def render_disclaimer() -> None:
    """Render public-data proxy disclaimer."""
    st.caption(
        "Public Walmart weekly sales data is used as a proxy for operational demand. "
        "Staffing outputs are scenario-based estimates using configurable capacity assumptions."
    )


# =============================================================================
# Main app
# =============================================================================

def main() -> None:
    """Main Streamlit application."""
    apply_custom_css()

    st.title(APP_TITLE)
    st.caption(APP_CAPTION)

    sidebar_inputs = build_common_sidebar_inputs()

    if sidebar_inputs["mode"] == "Simple Scenario":
        payload = build_simple_payload(sidebar_inputs)
    else:
        payload = build_advanced_payload(sidebar_inputs)

    run_clicked = st.sidebar.button("🚀 Run Forecast")

    if not run_clicked:
        st.info("Configure a scenario in the sidebar, then click **Run Forecast**.")
        return

    data = call_forecast_api(payload)
    if data is None:
        return

    result_df = pd.DataFrame(data["forecast"])

    render_executive_summary(data)
    render_forecast_charts(result_df, sidebar_inputs["weeks"])
    render_forecast_table(result_df)
    render_disclaimer()
    render_recommendations(result_df, payload, sidebar_inputs["weeks"])

    with st.expander("View Raw JSON Response"):
        st.json(data)


if __name__ == "__main__":
    main()
