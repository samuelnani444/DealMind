import json
import logging
import re
from flask import Flask, render_template, request, redirect, url_for, flash
from database.db import initialize_database, get_connection
from services.ai_service import (
    analyze_deal, normalize_analysis_currency, format_inr,
    calculate_health_score
)

app = Flask(__name__)
app.secret_key = "dealmind-development-key"
initialize_database()


def normalize_saved_analysis(analysis, deal):
    if not analysis or not deal:
        return analysis
    try:
        deal_value = float(deal.get("deal_value") or 0)
    except (ValueError, TypeError):
        return analysis
    correct_value = format_inr(deal_value)
    pattern = re.compile(r"([₹$])\s*([\d,]+(?:\.\d{1,2})?)")

    def fix_text(value):
        if not isinstance(value, str):
            return value

        def replace(match):
            try:
                number = float(match.group(2).replace(",", ""))
            except ValueError:
                return match.group(0)
            return correct_value if abs(number - deal_value) < 0.01 else match.group(0)

        return pattern.sub(replace, value)

    for field in ("summary", "risks", "opportunities", "next_actions"):
        content = analysis.get(field)
        if isinstance(content, str):
            analysis[field] = fix_text(content)
        elif isinstance(content, list):
            analysis[field] = [fix_text(item) if isinstance(item, str) else item for item in content]
    return analysis


def parse_analysis(row, deal):
    if not row:
        return None
    data = dict(row)
    for field in ("risks", "opportunities", "next_actions"):
        try:
            data[field] = json.loads(data.get(field) or "[]")
        except (json.JSONDecodeError, TypeError):
            data[field] = []
    data = normalize_analysis_currency(data, deal)
    return normalize_saved_analysis(data, deal)


@app.route("/")
def home():
    connection = get_connection()
    try:
        deals = connection.execute("SELECT * FROM deals ORDER BY id DESC").fetchall()
        deals = [dict(d) for d in deals]
        total_deals = len(deals)
        total_value = sum(float(d.get("deal_value") or 0) for d in deals)
        active_deals = sum(d.get("stage") not in ("Closed Won", "Closed Lost") for d in deals)
        won_deals = sum(d.get("stage") == "Closed Won" for d in deals)
        lost_deals = sum(d.get("stage") == "Closed Lost" for d in deals)
        weighted_value = sum(
            float(d.get("deal_value") or 0) * float(d.get("probability") or 0) / 100
            for d in deals if d.get("stage") != "Closed Lost"
        )
        stage_order = ["Discovery", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
        stage_counts = {stage: sum(d.get("stage") == stage for d in deals) for stage in stage_order}
        analyses = connection.execute("""
            SELECT a.* FROM analyses a
            INNER JOIN (SELECT deal_id, MAX(id) AS latest_id FROM analyses GROUP BY deal_id) x
            ON a.id = x.latest_id
        """).fetchall()
        latest_by_deal = {a["deal_id"]: dict(a) for a in analyses}
        for deal in deals:
            analysis = latest_by_deal.get(deal["id"])
            deal["health_score"] = (
                analysis.get("health_score") if analysis and analysis.get("health_score") is not None
                else calculate_health_score(deal, [])
            )
            if deal["health_score"] == 0 and int(deal.get("probability") or 0) > 0 and not analysis:
                deal["health_score"] = calculate_health_score(deal, [])
    finally:
        connection.close()

    return render_template(
        "index.html", deals=deals, total_deals=total_deals,
        total_value=total_value, weighted_value=weighted_value,
        active_deals=active_deals, won_deals=won_deals,
        lost_deals=lost_deals, stage_counts=stage_counts,
        win_rate=round(won_deals / max(1, won_deals + lost_deals) * 100, 1)
    )


@app.route("/deals")
def deals_page():
    return redirect(url_for("home"))


@app.route("/add-deal", methods=["POST"])
def add_deal():
    company_name = request.form.get("company_name", "").strip()
    deal_name = request.form.get("deal_name", "").strip()
    if not company_name or not deal_name:
        flash("Company name and deal name are required.")
        return redirect(url_for("home"))
    try:
        deal_value = float(request.form.get("deal_value") or 0)
        probability = int(request.form.get("probability") or 0)
        if deal_value < 0 or not 0 <= probability <= 100:
            raise ValueError
    except (ValueError, TypeError):
        flash("Enter a valid non-negative deal value and probability from 0 to 100.")
        return redirect(url_for("home"))

    stage = request.form.get("stage", "Discovery")
    allowed = ["Discovery", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
    if stage not in allowed:
        flash("Invalid sales stage.")
        return redirect(url_for("home"))

    connection = get_connection()
    try:
        cursor = connection.execute("""
            INSERT INTO deals (company_name, deal_name, deal_value, stage, probability,
                               contact_name, contact_email, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_name, deal_name, deal_value, stage, probability,
            request.form.get("contact_name", "").strip(),
            request.form.get("contact_email", "").strip(),
            request.form.get("notes", "").strip()
        ))
        deal_id = cursor.lastrowid
        connection.execute("""
            INSERT INTO activities (deal_id, activity_type, description)
            VALUES (?, ?, ?)
        """, (deal_id, "Deal Created", f"Deal created for {company_name}"))
        connection.commit()
    except Exception:
        connection.rollback()
        app.logger.exception("Error while adding deal")
        flash("Could not save the deal. Check the terminal for details.")
        return redirect(url_for("home"))
    finally:
        connection.close()
    flash("Deal added successfully.")
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.route("/deal/<int:deal_id>")
def deal_detail(deal_id):
    connection = get_connection()
    try:
        deal_row = connection.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal_row is None:
            return "Deal not found", 404
        deal = dict(deal_row)
        activities = connection.execute(
            "SELECT * FROM activities WHERE deal_id = ? ORDER BY id DESC", (deal_id,)
        ).fetchall()
        latest = connection.execute(
            "SELECT * FROM analyses WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,)
        ).fetchone()
    finally:
        connection.close()

    analysis = parse_analysis(latest, deal)
    score = analysis.get("health_score") if analysis else None
    try:
        score = int(score) if score is not None else None
    except (ValueError, TypeError):
        score = None
    if score is None or (score == 0 and int(deal.get("probability") or 0) > 0):
        score = calculate_health_score(deal, [dict(a) for a in activities])
    if analysis:
        analysis["health_score"] = score

    return render_template(
        "deal_detail.html", deal=deal, activities=activities,
        latest_analysis=analysis, health_score=score
    )


@app.route("/deal/<int:deal_id>/add-activity", methods=["POST"])
def add_activity(deal_id):
    activity_type = request.form.get("activity_type", "").strip()
    description = request.form.get("description", "").strip()
    allowed = ["Meeting", "Email", "Call", "Demo", "Follow-up", "Customer Feedback", "Competitor Update", "Other"]
    if activity_type not in allowed or not description:
        flash("Choose a valid activity type and enter a description.")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    connection = get_connection()
    try:
        exists = connection.execute("SELECT id FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if not exists:
            return "Deal not found", 404
        connection.execute(
            "INSERT INTO activities (deal_id, activity_type, description) VALUES (?, ?, ?)",
            (deal_id, activity_type, description)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        app.logger.exception("Could not save activity")
        flash("Could not save activity.")
    finally:
        connection.close()
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.route("/deal/<int:deal_id>/analyze", methods=["POST"])
def analyze_deal_route(deal_id):
    try:
        result = analyze_deal(deal_id)
        mode = result.get("analysis_mode", "UNKNOWN").upper()
        if mode == "DEMO":
            flash("Demo analysis saved. These are rule-based sample insights, not live AI results.")
        elif mode == "CLAUDE":
            flash("Claude analysis completed and saved.")
        elif mode == "HINDSIGHT":
            flash("Hindsight analysis completed and saved.")
        else:
            flash("Deal analysis completed and saved.")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc))
    except Exception:
        app.logger.exception("Analysis failed for deal %s", deal_id)
        flash("Analysis failed. Check the terminal for details.")
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.errorhandler(500)
def internal_error(error):
    app.logger.exception("Internal server error: %s", error)
    return "An internal error occurred. Check the application terminal.", 500


if __name__ == "__main__":
    app.run(debug=True)
