import os
import json
import logging
import re
from datetime import datetime, timezone
from dotenv import load_dotenv
from database.db import get_connection
from services.hindsight_service import analyze_with_hindsight

load_dotenv()
logger = logging.getLogger(__name__)
MODE = os.getenv("DEALMIND_MODE", "demo").strip().lower()
API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CURRENCY_CODE = "INR"
CURRENCY_SYMBOL = "₹"
CURRENCY_NAME = "Indian Rupees"


def format_inr(amount):
    try:
        amount = float(amount or 0)
    except (ValueError, TypeError):
        amount = 0.0
    formatted = f"{amount:,.2f}"
    integer_part, decimal_part = formatted.split(".")
    if len(integer_part) > 3:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        integer_part = ",".join(groups) + "," + last_three
    return f"{CURRENCY_SYMBOL}{integer_part}.{decimal_part}"


def enrich_deal_currency(deal):
    result = dict(deal)
    result.update({
        "currency_code": CURRENCY_CODE,
        "currency_name": CURRENCY_NAME,
        "currency_symbol": CURRENCY_SYMBOL,
        "deal_value_formatted": format_inr(result.get("deal_value") or 0)
    })
    return result


def normalize_analysis_currency(analysis, deal):
    try:
        amount = float(deal.get("deal_value") or 0)
    except (ValueError, TypeError):
        amount = 0.0
    formatted = format_inr(amount)
    pattern = re.compile(r"₹\s*[\d,]+(?:\.\d{1,2})?")
    def normalize(value):
        if isinstance(value, str):
            return pattern.sub(formatted, value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    return normalize(analysis)


def calculate_health_score(deal, activities=None):
    """Heuristic 0-100 score using probability, stage, engagement and risk signals."""
    activities = activities or []
    try:
        probability = max(0, min(100, int(float(deal.get("probability") or 0))))
    except (ValueError, TypeError):
        probability = 0

    score = float(probability)
    stage = str(deal.get("stage") or "").lower()
    stage_adjustments = {
        "discovery": -5, "qualification": 0, "proposal": 5,
        "negotiation": 8, "closed won": 15, "closed lost": -35
    }
    score += stage_adjustments.get(stage, 0)

    if activities:
        score += min(10, len(activities) * 2)
        latest = str(activities[0].get("created_at") or "") if isinstance(activities[0], dict) else ""
        if latest:
            try:
                dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
                if days >= 30:
                    score -= 15
                elif days >= 14:
                    score -= 8
                elif days >= 7:
                    score -= 4
            except ValueError:
                pass
    else:
        score -= 8

    text = " ".join([
        str(deal.get("notes") or ""),
        " ".join(str(a.get("description") or "") for a in activities if isinstance(a, dict))
    ]).lower()
    risk_terms = ("budget concern", "no response", "unresponsive", "competitor", "delayed", "blocked", "price objection")
    positive_terms = ("interested", "approved", "positive", "demo successful", "ready to proceed")
    score -= min(18, sum(4 for term in risk_terms if term in text))
    score += min(8, sum(2 for term in positive_terms if term in text))

    if stage == "closed won":
        score = 100
    elif stage == "closed lost":
        score = 0
    return max(0, min(100, round(score)))


def _risk_level(score, risks):
    if score < 40 or len(risks) >= 3:
        return "High"
    if score < 70 or risks:
        return "Medium"
    return "Low"


def get_deal_information(deal_id):
    connection = get_connection()
    try:
        deal = connection.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError(f"Deal with ID {deal_id} was not found.")
        activities = connection.execute(
            "SELECT activity_type, description, created_at FROM activities WHERE deal_id = ? ORDER BY id DESC",
            (deal_id,)
        ).fetchall()
        return dict(deal), [dict(a) for a in activities]
    finally:
        connection.close()


def save_analysis(deal_id, risk_level, summary, risks, opportunities, next_actions, health_score):
    connection = get_connection()
    try:
        connection.execute("""
            INSERT INTO analyses (deal_id, risk_level, summary, risks, opportunities, next_actions, health_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            deal_id, risk_level, summary,
            json.dumps(risks, ensure_ascii=False),
            json.dumps(opportunities, ensure_ascii=False),
            json.dumps(next_actions, ensure_ascii=False),
            health_score
        ))
        connection.commit()
    except Exception:
        connection.rollback()
        logger.exception("Failed to save analysis for deal %s", deal_id)
        raise
    finally:
        connection.close()


def validate_analysis(analysis):
    for field in ("risk_level", "summary", "risks", "opportunities", "next_actions"):
        if field not in analysis:
            raise ValueError(f"Analysis is missing: {field}")
    if analysis["risk_level"] not in ("Low", "Medium", "High"):
        raise ValueError("Invalid risk level.")
    for field in ("risks", "opportunities", "next_actions"):
        if not isinstance(analysis[field], list):
            raise ValueError(f"Invalid {field} format.")
    return analysis


def parse_reflect_response(response_text):
    if not response_text:
        raise ValueError("Hindsight returned an empty analysis.")
    text = response_text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Hindsight response did not contain valid JSON.")
    return validate_analysis(json.loads(text[start:end + 1]))


def generate_demo_analysis(deal, activities):
    company = deal.get("company_name") or "the customer"
    name = deal.get("deal_name") or "this opportunity"
    stage = deal.get("stage") or "Unknown"
    probability = int(deal.get("probability") or 0)
    score = calculate_health_score(deal, activities)
    notes = str(deal.get("notes") or "").lower()
    activity_text = " ".join(str(a.get("description") or "") for a in activities).lower()
    all_text = notes + " " + activity_text
    risks, opportunities, actions = [], [], []

    if probability < 40:
        risks.append("The stated closing probability is below 40%; clarify blockers and decision criteria.")
    if not activities:
        risks.append("No activity history is recorded. Establish a documented customer follow-up plan.")
    if any(word in all_text for word in ("pricing", "price", "budget", "cost", "roi")):
        risks.append("Pricing, budget, or ROI questions need a clear response.")
        actions.append("Prepare an itemized pricing breakdown and explain expected business value.")
    if any(word in all_text for word in ("competitor", "alternative")):
        risks.append("A competitor or alternative solution is mentioned; clarify the customer's comparison criteria.")
        actions.append("Ask which evaluation criteria matter most and address them factually.")
    if any(word in all_text for word in ("interested", "successful demo", "positive feedback")):
        opportunities.append("Recorded notes indicate customer interest or positive engagement.")
    if stage.lower() in ("proposal", "negotiation"):
        opportunities.append("The deal is in a later sales stage; resolving open questions may help move it toward a decision.")
    if activities:
        opportunities.append(f"{len(activities)} activity record(s) provide context for the next customer conversation.")
    if not risks:
        risks.append("No major risk signal was detected in the available notes; continue monitoring engagement.")
    actions.extend([
        "Confirm the customer's decision timeline and stakeholders.",
        "Schedule a follow-up and record the outcome.",
        "Reassess deal probability after the next meaningful interaction."
    ])
    risks = list(dict.fromkeys(risks))
    opportunities = list(dict.fromkeys(opportunities))
    actions = list(dict.fromkeys(actions))
    risk_level = _risk_level(score, risks)
    summary = (
        f"DEMO MODE — This is a rule-based sample assessment, not a live AI response. "
        f"{name} for {company} is in {stage} with a stated closing probability of {probability}%. "
        f"The deal value is {format_inr(deal.get('deal_value') or 0)}. "
        f"The calculated deal health score is {score}/100. Review the risks and next actions before making decisions."
    )
    return {
        "risk_level": risk_level, "summary": summary, "risks": risks,
        "opportunities": opportunities, "next_actions": actions,
        "analysis_mode": "DEMO"
    }


def generate_claude_analysis(deal, activities):
    if not API_KEY or API_KEY == "your_key_goes_here":
        raise ValueError("Anthropic API key is missing or invalid.")
    from anthropic import Anthropic
    client = Anthropic(api_key=API_KEY)
    information = enrich_deal_currency(deal)
    information["activities"] = activities
    prompt = f"""
You are DealMind, a business intelligence assistant. Analyze this sales deal using only supplied facts.
All deal values are INR. Use ₹ and the supplied formatted deal value. Do not invent facts.
Return only JSON with keys risk_level (Low/Medium/High), summary, risks (array),
opportunities (array), next_actions (array).
DEAL DATA:
{json.dumps(information, indent=2, ensure_ascii=False)}
"""
    response = client.messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    result = normalize_analysis_currency(json.loads(text), deal)
    result["analysis_mode"] = "CLAUDE"
    return validate_analysis(result)


def generate_hindsight_analysis(deal, activities):
    result = analyze_with_hindsight(enrich_deal_currency(deal), activities)
    analysis = parse_reflect_response(result["reflect_text"])
    analysis = normalize_analysis_currency(analysis, deal)
    analysis["analysis_mode"] = "HINDSIGHT"
    analysis["memory_bank_id"] = result.get("bank_id")
    analysis["recalled_memory_count"] = len(result.get("recalled_memories", []))
    return analysis


def analyze_deal(deal_id):
    deal, activities = get_deal_information(deal_id)
    if MODE == "demo":
        analysis = generate_demo_analysis(deal, activities)
    elif MODE == "claude":
        analysis = generate_claude_analysis(deal, activities)
    elif MODE == "hindsight":
        analysis = generate_hindsight_analysis(deal, activities)
    else:
        raise ValueError("Invalid DEALMIND_MODE. Use 'demo', 'claude', or 'hindsight'.")

    analysis = normalize_analysis_currency(analysis, deal)
    validate_analysis(analysis)
    analysis["health_score"] = calculate_health_score(deal, activities)
    save_analysis(
        deal_id, analysis["risk_level"], analysis["summary"],
        analysis["risks"], analysis["opportunities"],
        analysis["next_actions"], analysis["health_score"]
    )
    return analysis
