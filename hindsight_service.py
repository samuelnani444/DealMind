
import logging
import os
import re

from dotenv import load_dotenv
from hindsight_client import Hindsight

load_dotenv()

logger = logging.getLogger(__name__)

HINDSIGHT_BASE_URL = os.getenv(
    "HINDSIGHT_BASE_URL",
    "https://api.hindsight.vectorize.io"
)

HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY")

CURRENCY_CODE = "INR"
CURRENCY_NAME = "Indian Rupees"
CURRENCY_SYMBOL = "₹"


def format_inr(amount):
    """
    Format a numeric amount using Indian digit grouping.

    Example:
        500000 -> ₹5,00,000.00
    """

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


def get_client():
    """Create a Hindsight client using secure environment settings."""

    if not HINDSIGHT_API_KEY:
        raise ValueError(
            "Hindsight API key is missing. Check your .env file."
        )

    return Hindsight(
        base_url=HINDSIGHT_BASE_URL,
        api_key=HINDSIGHT_API_KEY
    )


def get_bank_id(deal_id):
    """Return a unique memory bank ID for each deal."""

    return f"dealmind-deal-{int(deal_id)}"


def ensure_bank(client, bank_id):
    """Create or update the deal's memory bank."""

    return client.create_bank(
        bank_id=bank_id,
        name=f"DealMind Deal {bank_id}",
        background=(
            "This memory bank stores sales deal information, "
            "customer interactions, deal risks, and opportunities. "
            "All monetary values in DealMind deal records are "
            "denominated in Indian Rupees (INR), never US dollars. "
            "Use only information supported by stored memories."
        )
    )


def analyze_with_hindsight(deal, activities):
    """
    Store deal information, recall relevant memories,
    and use Hindsight Reflect to generate an assessment.

    Deal values are explicitly identified as INR throughout
    the current snapshot and reflection instructions.
    """

    deal_id = int(deal["id"])
    bank_id = get_bank_id(deal_id)

    client = get_client()

    try:
        ensure_bank(client, bank_id)

        deal_value = deal.get("deal_value") or 0
        formatted_value = format_inr(deal_value)

        deal_snapshot = {
            "company_name": deal.get("company_name"),
            "deal_name": deal.get("deal_name"),
            "deal_value": deal_value,
            "deal_value_formatted": formatted_value,
            "currency_code": CURRENCY_CODE,
            "currency_name": CURRENCY_NAME,
            "currency_symbol": CURRENCY_SYMBOL,
            "stage": deal.get("stage"),
            "probability": deal.get("probability"),
            "contact_name": deal.get("contact_name"),
            "notes": deal.get("notes"),
            "activities": activities
        }

        # RETAIN: Store the current deal snapshot.
        client.retain(
            bank_id=bank_id,
            content=(
                "CURRENT DEALMIND RECORD.\n"
                "IMPORTANT: All monetary values in this record "
                "are Indian Rupees (INR), not US dollars.\n"
                f"Deal value: {formatted_value}\n"
                f"Currency code: {CURRENCY_CODE}\n"
                f"Deal information: {deal_snapshot}"
            ),
            context=(
                "Current DealMind sales deal record. "
                "Currency: Indian Rupees (INR)."
            )
        )

        # RECALL: Retrieve relevant memories.
        recall_result = client.recall(
            bank_id=bank_id,
            query=(
                "Retrieve relevant past deal information, "
                "customer concerns, commitments, interactions, "
                "risks, and opportunities for "
                f"{deal.get('company_name')} "
                f"and {deal.get('deal_name')}. "
                "The current deal's monetary values are in INR."
            ),
            budget="low",
            max_tokens=2000
        )

        recalled_memories = [
            memory.text
            for memory in (recall_result.results or [])
            if getattr(memory, "text", None)
        ]

        memory_context = (
            "\n".join(recalled_memories)
            if recalled_memories
            else "No relevant previous memories were retrieved."
        )

        # REFLECT: Generate a structured assessment.
        reflect_query = f"""
Analyze this sales deal using the current deal information
and retrieved memories.

CURRENCY REQUIREMENTS — MUST FOLLOW:

1. The deal value is denominated in Indian Rupees (INR).
2. The currency symbol is ₹.
3. The currency code is INR.
4. The exact current deal value is {formatted_value}.
5. Never describe this deal value in US dollars.
6. Never use the $ symbol when referring to this deal.
7. Do not perform currency conversion.
8. Do not invent exchange rates or alternative deal values.
9. If you mention the deal value in the summary, use exactly
   {formatted_value}.
10. If an older memory contains a dollar sign, do not treat
    that as the currency of the current deal. Follow the
    current deal record above.

ANALYSIS REQUIREMENTS:

- Do not invent facts.
- Distinguish known facts from recommendations.
- Return ONLY valid JSON.
- Use exactly these keys:
  risk_level (Low, Medium, or High),
  summary (string),
  risks (array of strings),
  opportunities (array of strings),
  next_actions (array of strings).
"""

        reflect_result = client.reflect(
            bank_id=bank_id,
            query=reflect_query,
            context=(
                "CURRENT DEAL — AUTHORITATIVE INFORMATION:\n"
                f"{deal_snapshot}\n\n"
                "CURRENCY: INR (Indian Rupees).\n"
                f"FORMATTED DEAL VALUE: {formatted_value}\n\n"
                "RECALLED MEMORIES (historical context only):\n"
                f"{memory_context}\n\n"
                "If historical memories conflict with the current "
                "deal's currency or value, use the current deal "
                "record as the source of truth."
            ),
            budget="low"
        )

        reflect_text = reflect_result.text

        if not reflect_text:
            raise ValueError(
                "Hindsight returned an empty analysis."
            )

        return {
            "reflect_text": reflect_text,
            "recalled_memories": recalled_memories,
            "bank_id": bank_id
        }

    finally:
        client.close()