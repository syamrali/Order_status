# Order Support Voice Agent

This is a real-time voice customer support agent for order status queries.

It keeps the same voice stack:
- Sarvam STT (`saaras:v3`)
- Groq OpenAI-compatible LLM endpoint (`llama-3.3-70b-versatile`)
- Sarvam TTS (`bulbul:v3`)
- LiveKit for real-time voice sessions

## Flow

1. User starts a LiveKit voice session from the frontend.
2. Speech is transcribed by Sarvam STT.
3. Agent collects the caller’s **10-digit mobile number** and calls `get_order_status_from_db` with `customer_confirmed=false`.
4. Backend resolves **name** and **user_id** from `master.users` (last 10 digits of phone) in the farmer-engagement DB.
5. Agent asks the caller to **confirm their name** (still `customer_confirmed=false` until they answer).
6. After verbal confirmation, the tool is called again with `customer_confirmed=true`. Orders are loaded **by `user_id`** from `transactions.orders` — only **active** orders (non-delivered by default).
7. **One active order:** status and items are returned; the agent speaks them. The tool prefers **external_order_id** (from `external_order_number`); if that column is empty, **order_number** or **order_id** is used so the flow never returns blank IDs.
8. **Several active orders:** the UI lists those same display IDs (external when present). The caller picks one; the agent passes it as `external_order_id` (lookup accepts external number, order number, or order id).
9. LLM answers only from DB tool output (no invented statuses). Sarvam TTS speaks the reply.

## Database Lookup Logic

1. Resolve `user_id` and customer name from `master.users` using the normalized last 10 digits of the phone number.
2. After the caller confirms their name, fetch **active** rows from `transactions.orders` for that `user_id` (status not in `ORDER_EXCLUDED_FROM_ACTIVE_STATUSES`, default `delivered`).
3. **Single active order:** load latest row from `transactions.order_status_history` and line items from `transactions.order_items`. The JSON returned to the agent uses **caller-safe** fields: `external_order_id` maps from `external_order_number`; internal ids are omitted.
4. **Multiple active orders:** the user chooses by **external_order_number** only; the chat panel shows the same **external_order_id** values.

## Required Environment

Set these in `.env`:

```env
SARVAM_API_KEY=...
GROQ_API_KEY=...
LIVEKIT_URL=...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...

DATABASE_URL=postgresql://...
FARMER_ENGAGEMENT_DATABASE_URL=postgresql://...

# Optional
ORDERS_PHONE_COL=phone
ORDER_STATUS_HISTORY_STATUS_COL=new_status
ORDER_STATUS_HISTORY_CHANGED_AT_COL=changed_at
ORDER_EXCLUDED_FROM_ACTIVE_STATUSES=delivered
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
```

Notes:
- `FARMER_ENGAGEMENT_DATABASE_URL` is optional. If missing, the app reuses `DATABASE_URL`.
- URLs can be `postgres://` / `postgresql://`; backend auto-converts to async driver format.

## Run

```bash
docker compose up --build -d
```

- Frontend: `http://localhost:3000`
- Backend API: `http://localhost:8000`

## API

- `POST /api/chat/start-call`
  - body: `{ "language_code": "en-IN" }`
  - returns: LiveKit room id, url, and token

- `GET /health`
  - returns backend health + livekit key configuration status

## Key Files

- `backend/worker.py` - voice agent, Sarvam STT/TTS, LLM, tool-calling
- `backend/order_lookup.py` - DB connection and order lookup queries
- `backend/main.py` - FastAPI call bootstrap + LiveKit token issuance
- `frontend/src/app/page.tsx` - UI for starting/ending voice support calls
