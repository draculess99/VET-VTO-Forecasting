"""
Flask API for the Warehouse VET/VTO Forecaster.

This service loads the trained warehouse forecasting bundle from disk, accepts
forecast scenarios from the Streamlit frontend, and returns weekly workload,
VET/VTO staffing decisions, cost estimates, and summary recommendations.

Run locally:
    python flask_forecaster_cleaned.py

Health check:
    http://127.0.0.1:5000/

Forecast endpoint:
    POST http://127.0.0.1:5000/forecast
"""

from __future__ import annotations

import os
from typing import Any

import joblib
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request

# -----------------------------------------------------------------------------
# App configuration
# -----------------------------------------------------------------------------

MODEL_PATH = os.getenv("MODEL_PATH", "warehouse_system.pkl")
DEFAULT_WEEKS = 43
MAX_FORECAST_WEEKS = 43

DEFAULT_WORKERS_PER_UNIT = 5000
DEFAULT_OVERTIME_LABOR_COST_PER_WORKER = 30
DEFAULT_HOURLY_LABOR_COST_PER_WORKER = 20

DEFAULT_INPUTS = {
    "temperature": 45.0,
    "fuel_price": 3.2,
    "cpi": 225.0,
    "unemployment": 6.5,
    "isholiday": 0,
}

app = Flask(__name__)


# -----------------------------------------------------------------------------
# Model loading
# -----------------------------------------------------------------------------

def load_forecaster_bundle(model_path: str = MODEL_PATH) -> dict[str, Any]:
    """Load the saved forecasting system bundle."""
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"Model bundle not found at '{model_path}'. "
            "Set MODEL_PATH or place warehouse_system.pkl in this directory."
        )

    bundle = joblib.load(model_path)

    required_keys = {"forecaster", "vet_threshold", "vto_threshold"}
    missing_keys = required_keys.difference(bundle.keys())
    if missing_keys:
        raise KeyError(f"Model bundle is missing required key(s): {missing_keys}")

    return bundle


BUNDLE = load_forecaster_bundle()
FORECASTER = BUNDLE["forecaster"]
VET_THRESHOLD = BUNDLE["vet_threshold"]
VTO_THRESHOLD = BUNDLE["vto_threshold"]


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------

def clamp_weeks(value: Any) -> int:
    """Convert forecast horizon to a safe integer range."""
    try:
        weeks = int(value)
    except (TypeError, ValueError):
        weeks = DEFAULT_WEEKS

    return max(1, min(weeks, MAX_FORECAST_WEEKS))


def as_fixed_length_list(value: Any, weeks: int, default: float | int) -> list[Any]:
    """Return a list of exactly `weeks` values.

    Scalars are repeated. Short lists are padded. Long lists are truncated.
    """
    if isinstance(value, list):
        values = value.copy()
    else:
        values = [value] * weeks

    if len(values) < weeks:
        values.extend([default] * (weeks - len(values)))

    return values[:weeks]


def summarize_input(values: list[Any], field_name: str, provided_inputs: dict[str, Any]) -> dict[str, Any]:
    """Summarize scenario input values returned in the API response."""
    numeric_values = pd.to_numeric(pd.Series(values), errors="coerce")

    return {
        "default_used": field_name not in provided_inputs,
        "length": len(values),
        "min": float(numeric_values.min()),
        "max": float(numeric_values.max()),
        "avg": round(float(numeric_values.mean()), 2),
        "values": ", ".join(map(str, values)),
    }


def build_future_exog(inputs: dict[str, list[Any]], stress_controls: dict[str, list[Any]], weeks: int) -> pd.DataFrame:
    """Build the exogenous feature frame expected by the trained forecaster."""
    future_exog = pd.DataFrame(
        {
            "IsHoliday": inputs["isholiday"],
            "Temperature": inputs["temperature"],
            "Fuel_Price": inputs["fuel_price"],
            "CPI": inputs["cpi"],
            "Unemployment": inputs["unemployment"],
        }
    )

    future_exog["sales_velocity"] = np.array(stress_controls["velocity_pct"], dtype=float) / 100
    future_exog["backlog_proxy"] = np.array(stress_controls["shipping_delay_pct"], dtype=float) / 100
    future_exog["warehouse_congestion"] = np.array(stress_controls["congestion_pct"], dtype=float) / 100
    future_exog["logistics_stress"] = np.array(stress_controls["logistics_stress_pct"], dtype=float) / 100

    temperature_series = pd.Series(inputs["temperature"])
    high_temp = temperature_series.quantile(0.90)
    low_temp = temperature_series.quantile(0.10)

    future_exog["extreme_temp"] = (
        (future_exog["Temperature"] > high_temp)
        | (future_exog["Temperature"] < low_temp)
    ).astype(int)

    future_dates = pd.date_range(
        start=FORECASTER.last_window_.index[-1] + pd.Timedelta(weeks=1),
        periods=weeks,
        freq="W-FRI",
    )
    future_exog.index = future_dates

    return future_exog


def apply_stress_adjustments(predictions: pd.Series, stress_controls: dict[str, list[Any]]) -> np.ndarray:
    """Apply operational stress scenario adjustments to forecast output."""
    adjusted = predictions.to_numpy(dtype=float)

    adjusted *= 1 + (np.array(stress_controls["velocity_pct"], dtype=float) / 100)
    adjusted *= 1 - (np.array(stress_controls["shipping_delay_pct"], dtype=float) / 100)
    adjusted *= 1 - (np.array(stress_controls["congestion_pct"], dtype=float) / 100)
    adjusted *= 1 - (np.array(stress_controls["logistics_stress_pct"], dtype=float) / 100)

    return adjusted


def build_forecast_output(
    predictions: np.ndarray,
    workers_per_unit: float,
    overtime_cost_per_worker: float,
    hourly_cost_per_worker: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert predictions into VET/VTO decisions, costs, and summary metrics."""
    output = []
    cumulative_cost = 0.0

    summary = {
        "weeks_forecasted": len(predictions),
        "vet_weeks": 0,
        "vto_weeks": 0,
        "normal_weeks": 0,
        "total_extra_workers": 0,
        "total_workers_reduced": 0,
        "total_cost": 0.0,
        "peak_demand_week": 0,
        "peak_demand_value": 0.0,
    }

    for index, value in enumerate(predictions, start=1):
        if value >= VET_THRESHOLD:
            decision = "VET"
            extra_workers = int((value - VET_THRESHOLD) / workers_per_unit)
            workers_to_reduce = 0
            estimated_cost = extra_workers * overtime_cost_per_worker
            summary["vet_weeks"] += 1
        elif value <= VTO_THRESHOLD:
            decision = "VTO"
            extra_workers = 0
            workers_to_reduce = int((VTO_THRESHOLD - value) / workers_per_unit)
            estimated_cost = workers_to_reduce * hourly_cost_per_worker
            summary["vto_weeks"] += 1
        else:
            decision = "NORMAL"
            extra_workers = 0
            workers_to_reduce = 0
            estimated_cost = 0
            summary["normal_weeks"] += 1

        cumulative_cost += estimated_cost
        summary["total_extra_workers"] += extra_workers
        summary["total_workers_reduced"] += workers_to_reduce

        if value > summary["peak_demand_value"]:
            summary["peak_demand_value"] = float(value)
            summary["peak_demand_week"] = index

        output.append(
            {
                "week": index,
                "predicted_demand": round(float(value), 2),
                "decision": decision,
                "extra_workers_needed": extra_workers,
                "workers_to_reduce": workers_to_reduce,
                "estimated_cost": estimated_cost,
                "cumulative_future_cost": cumulative_cost,
            }
        )

    summary["total_cost"] = cumulative_cost
    return output, summary


def build_recommendations(summary: dict[str, Any]) -> list[str]:
    """Create simple operational recommendation messages from summary metrics."""
    recommendations = []

    if summary["vet_weeks"] > 0:
        recommendations.append(
            f"Increase staffing during {summary['vet_weeks']} week(s) of forecasted high demand."
        )

    if summary["vto_weeks"] > 0:
        recommendations.append(
            f"Offer VTO during {summary['vto_weeks']} low-demand week(s) to reduce labor cost."
        )

    if summary["peak_demand_week"] > 0:
        recommendations.append(
            f"Highest demand expected in Week {summary['peak_demand_week']}. Prepare staffing early."
        )

    if summary["total_extra_workers"] > 50:
        recommendations.append(
            "Large labor requirement detected. Consider temporary staffing support."
        )

    if summary["total_cost"] > 0:
        recommendations.append(
            f"Projected added labor cost is ${round(summary['total_cost'], 2)}."
        )

    if not recommendations:
        recommendations.append("Demand stable. Maintain standard staffing plan.")

    return recommendations


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    """Basic health-check endpoint."""
    return jsonify({"message": "Warehouse Forecast API Running", "status": "ok"})


@app.route("/forecast", methods=["POST"])
def forecast():
    """Return forecasted workload and VET/VTO decisions for a scenario."""
    data = request.get_json(silent=True) or {}

    request_id = data.get("request_id", "forecast_default")
    scenario_name = data.get("scenario_name", "Standard Forecast")
    mode = data.get("mode", "simple")
    weeks = clamp_weeks(data.get("weeks", DEFAULT_WEEKS))

    provided_inputs = data.get("inputs", {}) or {}
    settings = data.get("settings", {}) or {}

    inputs = {
        field: as_fixed_length_list(provided_inputs.get(field, default), weeks, default)
        for field, default in DEFAULT_INPUTS.items()
    }

    stress_controls = {
        "velocity_pct": as_fixed_length_list(settings.get("velocity_pct", 0), weeks, 0),
        "shipping_delay_pct": as_fixed_length_list(settings.get("shipping_delay_pct", 0), weeks, 0),
        "congestion_pct": as_fixed_length_list(settings.get("congestion_pct", 0), weeks, 0),
        "logistics_stress_pct": as_fixed_length_list(settings.get("logistics_stress_pct", 0), weeks, 0),
    }

    workers_per_unit = settings.get("workers_per_unit", DEFAULT_WORKERS_PER_UNIT)
    overtime_cost_per_worker = settings.get(
        "overtime_labor_cost_per_worker",
        DEFAULT_OVERTIME_LABOR_COST_PER_WORKER,
    )
    hourly_cost_per_worker = settings.get(
        "hourly_labor_cost_per_worker",
        DEFAULT_HOURLY_LABOR_COST_PER_WORKER,
    )

    try:
        future_exog = build_future_exog(inputs, stress_controls, weeks)
        raw_predictions = FORECASTER.predict(steps=weeks, exog=future_exog)
        adjusted_predictions = apply_stress_adjustments(raw_predictions, stress_controls)

        forecast_output, summary = build_forecast_output(
            adjusted_predictions,
            workers_per_unit,
            overtime_cost_per_worker,
            hourly_cost_per_worker,
        )

    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    inputs_used = {
        "weeks": weeks,
        "temperature": summarize_input(inputs["temperature"], "temperature", provided_inputs),
        "fuel_price": summarize_input(inputs["fuel_price"], "fuel_price", provided_inputs),
        "cpi": summarize_input(inputs["cpi"], "cpi", provided_inputs),
        "unemployment": summarize_input(inputs["unemployment"], "unemployment", provided_inputs),
        "isholiday": summarize_input(inputs["isholiday"], "isholiday", provided_inputs),
    }

    simulation_controls = {
        "mode": mode,
        "workers_per_unit": workers_per_unit,
        "overtime_labor_cost_per_worker": overtime_cost_per_worker,
        "hourly_labor_cost_per_worker": hourly_cost_per_worker,
        "demand_velocity_pct": ", ".join(map(str, stress_controls["velocity_pct"])),
        "shipping_delay_pct": ", ".join(map(str, stress_controls["shipping_delay_pct"])),
        "warehouse_congestion_pct": ", ".join(map(str, stress_controls["congestion_pct"])),
        "logistics_stress_pct": ", ".join(map(str, stress_controls["logistics_stress_pct"])),
    }

    return jsonify(
        {
            "status": "success",
            "request_id": request_id,
            "scenario_name": scenario_name,
            "inputs_used": inputs_used,
            "simulation_controls": simulation_controls,
            "summary": summary,
            "forecast": forecast_output,
            "recommendations": build_recommendations(summary),
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
