import os
import json
import hmac
import hashlib
import logging
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
from sqlalchemy.orm import Session

# Local imports from the models.py file
from models import SessionLocal, RazorpayEvent, EventStatus, engine, Base

# ---------- BOOTSTRAP ----------
# Create database tables if they don't exist
Base.metadata.create_all(bind=engine)

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("razorpay_saleor")

# --- CONFIGURATION FROM ENVIRONMENT VARIABLES ---
WEBHOOK_SECRET  = os.environ["RAZORPAY_WEBHOOK_SECRET"]
SALEOR_API_URL  = os.environ["SALEOR_API_URL"]
SALEOR_TOKEN    = os.environ["SALEOR_APP_TOKEN"]
WEBHOOK_PATH    = os.getenv("WEBHOOK_PATH", "/webhooks/razorpay")
# The public hostname for this app itself (e.g., "razorpay.your-domain.com")
APP_HOSTNAME    = os.environ["APP_HOSTNAME"]


# ---------- FASTAPI APP INSTANCE ----------
app = FastAPI(title="Razorpay → Saleor Payment App", version="1.0.0")


# ---------- HELPER FUNCTIONS ----------
def _verify_signature(body: bytes, signature: str) -> bool:
    """Verifies the incoming webhook signature from Razorpay."""
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

saleor_headers = {
    "Authorization": f"Bearer {SALEOR_TOKEN}",
    "Content-Type": "application/json",
}

async def _saleor(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Sends a GraphQL request to the Saleor API."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            SALEOR_API_URL,
            json={"query": query, "variables": variables},
            headers=saleor_headers
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            log.error("Saleor API Error: %s", body["errors"])
            raise ValueError(body)
        return body

async def _retry(coro):
    """Retries a coroutine with exponential backoff."""
    delay = 1
    for attempt in range(1, 6):
        try:
            return await coro
        except Exception as e:
            log.warning("Retry %d/5 – %s", attempt, e)
            if attempt == 5:
                raise
            await asyncio.sleep(delay)
            delay *= 2


# ---------- SALEOR GRAPHQL MUTATIONS ----------
TX_UPDATE_MUTATION = """
mutation TxUpdate(
  $id: ID!
  $transaction: TransactionUpdateInput!
) {
  transactionUpdate(id: $id, transaction: $transaction) {
    transaction { id pspReference status }
    errors { field message code }
  }
}
"""

ORDER_MARK_PAID = """
mutation Mark($id: ID!) {
  orderMarkAsPaid(id: $id) {
    order { id }
    errors { field message code }
  }
}
"""

ORDER_CANCEL = """
mutation Cancel($id: ID!) {
  orderCancel(id: $id) {
    order { id }
    errors { field message code }
  }
}
"""

TX_CREATE_REFUND = """
mutation TxCreate(
  $id: ID!
  $transaction: TransactionCreateInput!
) {
  transactionCreate(id: $id, transaction: $transaction) {
    transaction { id pspReference }
    errors { field message code }
  }
}
"""


# ---------- PYDANTIC PAYLOAD MODELS ----------
class RazorpayPayload(BaseModel):
    event: str
    payload: Dict[str, Any]


# ---------- EVENT DISPATCHER ----------
async def _handle_event(
    event: str,
    entity: Dict[str, Any],
    saleor_order_id: str,
    session: Session,
    record_id: str,
    parent_id: Optional[str] = None
):
    """Processes a verified webhook event and updates Saleor."""
    row = session.query(RazorpayEvent).filter_by(id=record_id).one()
    amount = entity["amount"] / 100
    currency = entity["currency"]

    status_map = {
        "payment.authorized": "AUTHORIZED",
        "payment.captured":   "CHARGED",
        "payment.failed":     "FAILURE",
        "refund.failed":      "REFUND_FAILURE",
        "refund.processed":   "REFUND_SUCCESS",
    }
    status = status_map.get(event, "PENDING")

    # This example updates an existing transaction. A real implementation might
    # create new transactions for different events.
    await _retry(
        _saleor(
            TX_UPDATE_MUTATION,
            {
                "id": saleor_order_id,
                "transaction": {
                    "pspReference": record_id,
                    "amountCharged": {"amount": amount, "currency": currency} if status == "CHARGED" else None,
                    "status": status,
                    "metadata": [{"key": "razorpay_event", "value": event}]
                }
            }
        )
    )

    if event == "payment.captured":
        await _retry(_saleor(ORDER_MARK_PAID, {"id": saleor_order_id}))
    elif event == "payment.failed":
        await _retry(_saleor(ORDER_CANCEL, {"id": saleor_order_id}))
    elif event == "refund.processed":
        await _retry(
            _saleor(
                TX_CREATE_REFUND,
                {
                    "id": saleor_order_id,
                    "transaction": {
                        "pspReference": record_id,
                        "amountRefunded": {"amount": amount, "currency": currency},
                        "status": "REFUND_SUCCESS",
                        "metadata": [{"key": "razorpay_refund_event", "value": event}]
                    }
                }
            )
        )

    row.status = EventStatus.SUCCESS
    row.error_msg = None


# ---------- WEBHOOK ROUTE ----------
@app.post(WEBHOOK_PATH)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(...),
):
    body = await request.body()
    if not _verify_signature(body, x_razorpay_signature):
        log.error("Bad signature received for Razorpay webhook.")
        raise HTTPException(status_code=401, detail="Bad signature")

    data = RazorpayPayload(**json.loads(body))
    log.info("Received Razorpay event: %s", data.event)

    entity = (
        data.payload.get("payment", {}).get("entity") or
        data.payload.get("refund", {}).get("entity")
    )
    if not entity:
        log.warning("No entity found in webhook payload.")
        return Response(status_code=400)

    saleor_order_id = (entity.get("notes") or {}).get("saleor_order_id")
    if not saleor_order_id:
        log.warning("No saleor_order_id found in Razorpay notes.")
        return Response(status_code=400)

    record_id = entity["id"]
    parent_id = entity.get("payment_id") if data.event.startswith("refund") else None

    # Idempotency check and record creation
    with SessionLocal.begin() as session:
        if session.query(RazorpayEvent).get(record_id):
            log.info("Duplicate event %s ignored.", record_id)
            return Response(status_code=200)
        session.add(
            RazorpayEvent(
                id=record_id,
                parent_id=parent_id,
                event_name=data.event,
                amount=entity["amount"] / 100,
            )
        )

    # Process the event
    with SessionLocal.begin() as session:
        try:
            await _handle_event(
                data.event, entity, saleor_order_id, session, record_id, parent_id
            )
        except Exception as e:
            log.error("Failed to process event %s: %s", record_id, e, exc_info=True)
            row = session.query(RazorpayEvent).filter_by(id=record_id).one()
            row.status = EventStatus.SALEOR_ERR
            row.error_msg = str(e)[:2000]
            raise HTTPException(status_code=502, detail="Saleor API update failed")

    log.info("Successfully processed event %s.", record_id)
    return Response(status_code=200)


# ---------- SALEOR APP MANIFEST ----------
@app.get("/api/manifest", include_in_schema=False)
def manifest():
    """Provides the manifest for Saleor App installation."""
    app_url = f"https://{APP_HOSTNAME}"
    webhook_url = f"{app_url}{WEBHOOK_PATH}"

    return JSONResponse({
        "id": "razorpay.saleor.app",
        "version": "1.0.0",
        "name": "Razorpay Payments App",
        "about": "Receives Razorpay webhooks to update Saleor payment and order states.",
        "permissions": ["HANDLE_PAYMENTS", "MANAGE_ORDERS"],
        "appUrl": app_url,
        "tokenTargetUrl": f"{app_url}/register",
        "webhooks": [
            {
                "name": "Razorpay Webhook Handler",
                "asyncEvents": [],
                "syncEvents": [],
                "targetUrl": webhook_url,
                "query": ""
            }
        ]
    })


# ---------- HEALTHCHECK & APP REGISTRATION ROUTES ----------
@app.get("/healthz", include_in_schema=False)
def healthz():
    """A simple healthcheck endpoint."""
    return {"status": "ok"}

@app.post("/register", include_in_schema=False)
async def register(request: Request):
    """
    Saleor calls this endpoint once upon app installation to share the auth token.
    In this app's architecture, the token is managed via an environment variable,
    but this endpoint is still required for the installation handshake.
    """
    log.info("App successfully registered with Saleor.")
    return {"success": True}
