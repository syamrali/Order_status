import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from livekit import api

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

app = FastAPI(title="Order Support Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

def is_livekit_configured() -> bool:
    return bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "livekit_configured": is_livekit_configured(),
    }

class StartCallRequest(BaseModel):
    language_code: str = "en-IN"

@app.post("/api/chat/start-call")
async def start_call(req: StartCallRequest):
    """
    Creates a standard LiveKit token allowing the React frontend to join
    a dynamically generated room. `worker.py` automatically binds to new rooms
    to provide the order-support voice agent.
    """
    if not is_livekit_configured():
        raise HTTPException(status_code=400, detail="LiveKit keys are missing in .env")

    room_name = f"call_{uuid.uuid4().hex[:8]}"
    participant_identity = f"customer_{uuid.uuid4().hex[:6]}"

    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(participant_identity) \
        .with_name("Customer") \
        .with_grants(api.VideoGrants(room_join=True, room=room_name)) \
        .with_metadata(req.language_code) # Encodes chosen language into the WebRTC session so worker.py reads it
        
    return {
        "id": room_name,
        "livekit_url": LIVEKIT_URL,
        "livekit_token": token.to_jwt()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
