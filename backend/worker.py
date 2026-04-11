import asyncio
import base64
import io
import json
import os
import re
import struct
import uuid
import wave
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    APIConnectOptions,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    WorkerType,
    cli,
    get_job_context,
    llm,
    stt,
    tts,
    utils,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import openai, silero, groq

from order_lookup import OrderLookupService

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Primary STT mode (default codemix for Indic+English). If transcript is empty, worker retries with
# transcribe/verbatim — empty STT causes LiveKit to skip the reply entirely (no agent speech).
SARVAM_STT_PRIMARY_MODE = (os.environ.get("SARVAM_STT_MODE") or "codemix").strip() or "codemix"

TARGET_SAMPLE_RATE = 16000

# When every STT attempt returns nothing, emit this user text so the LLM still replies (asks to repeat).
STT_EMPTY_FALLBACK_USER_TEXT = (
    "Please ask for my mobile number again — I will speak the ten digits more slowly."
)

ORDER_LOOKUP = OrderLookupService.from_env()

# Google Gemini configuration (unused — using Groq)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = (os.environ.get("GROQ_MODEL") or "llama-3.1-8b-instant").strip()


async def publish_active_order_ids_to_ui(active_orders: list[dict[str, Any]], hint: str) -> None:
    """Push active-order rows to the web client via LiveKit data (chat panel)."""
    try:
        ctx = get_job_context()
        payload = {
            "type": "order_support",
            "action": "show_active_order_ids",
            "hint": hint,
            "orders": active_orders,
        }
        await ctx.room.local_participant.publish_data(
            json.dumps(payload),
            reliable=True,
            topic="order_support",
        )
    except Exception as exc:
        print(f"[Worker] publish active order IDs to UI failed: {exc}")


def resample_pcm(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Simple linear resampling for 16-bit mono PCM."""
    if src_rate == dst_rate:
        return pcm_bytes
    samples = struct.unpack(f"<{len(pcm_bytes) // 2}h", pcm_bytes)
    ratio = dst_rate / src_rate
    new_length = int(len(samples) * ratio)
    new_samples = []
    for i in range(new_length):
        src_idx = i / ratio
        idx0 = int(src_idx)
        idx1 = min(idx0 + 1, len(samples) - 1)
        frac = src_idx - idx0
        val = int(samples[idx0] * (1 - frac) + samples[idx1] * frac)
        new_samples.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{new_length}h", *new_samples)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class SarvamSTT(stt.STT):
    def __init__(self, language: str = "en-IN"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.language = language

    @staticmethod
    async def _sarvam_stt_once(
        client: httpx.AsyncClient,
        wav_data: bytes,
        *,
        mode: str,
        language_code: str | None,
    ) -> str:
        files = {"file": ("audio.wav", wav_data, "audio/wav")}
        data_payload = {"model": "saaras:v3", "mode": mode}
        if language_code and language_code != "unknown":
            data_payload["language_code"] = language_code
        headers = {"api-subscription-key": SARVAM_API_KEY}
        resp = await client.post(
            SARVAM_STT_URL,
            data=data_payload,
            files=files,
            headers=headers,
            timeout=30.0,
        )
        if not resp.is_success:
            print(f"[STT] Error {resp.status_code} mode={mode}: {resp.text}")
            resp.raise_for_status()
        body = resp.json()
        return (body.get("transcript") or "").strip()

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: str | None = None,
        conn_options: Any | None = None,
    ) -> stt.SpeechEvent:
        combined_frame = rtc.combine_audio_frames(buffer)
        src_rate = combined_frame.sample_rate
        src_channels = combined_frame.num_channels
        raw_pcm = bytes(combined_frame.data.tobytes())

        if src_channels == 2:
            samples = struct.unpack(f"<{len(raw_pcm) // 2}h", raw_pcm)
            raw_pcm = struct.pack(f"<{len(samples) // 2}h", *samples[::2])

        resampled_pcm = resample_pcm(raw_pcm, src_rate, TARGET_SAMPLE_RATE)
        wav_data = pcm_to_wav(resampled_pcm, TARGET_SAMPLE_RATE, channels=1)

        lang = language if language else self.language
        print(
            f"[STT] Sending {len(wav_data)} bytes ({src_rate}Hz->16kHz, "
            f"{src_channels}ch->1ch) lang={lang}"
        )

        # Order: user-configured primary, then modes that often work better for digit-only speech.
        modes: list[str] = []
        for m in (
            SARVAM_STT_PRIMARY_MODE,
            "transcribe",
            "verbatim",
            "codemix",
        ):
            if m and m not in modes:
                modes.append(m)

        text = ""
        async with httpx.AsyncClient() as client:
            for mode in modes:
                try:
                    text = await self._sarvam_stt_once(
                        client, wav_data, mode=mode, language_code=lang
                    )
                except Exception as exc:
                    print(f"[STT] mode={mode} lang={lang} failed: {exc}")
                    continue
                if text:
                    print(f"[STT] mode={mode} lang={lang} transcript: '{text}'")
                    break

            # Spoken digits are often tagged en-IN; retry transcribe with English if still empty.
            if not text and lang not in ("en-IN", "en", "unknown"):
                try:
                    text = await self._sarvam_stt_once(
                        client, wav_data, mode="transcribe", language_code="en-IN"
                    )
                    if text:
                        print(f"[STT] fallback en-IN transcribe transcript: '{text}'")
                except Exception as exc:
                    print(f"[STT] fallback en-IN transcribe failed: {exc}")

        if not text:
            print("[STT] All attempts returned empty — using fallback text so the agent still speaks")
            text = STT_EMPTY_FALLBACK_USER_TEXT

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(text=text, language=self.language)],
        )


class SarvamChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts_instance: "SarvamTTS", input_text: str, conn_options=None) -> None:
        super().__init__(tts=tts_instance, input_text=input_text, conn_options=conn_options)
        self._tts = tts_instance

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        speaker_map = {
            "hi-IN": "rahul",
            "te-IN": "aditya",
            "ta-IN": "vijay",
            "ml-IN": "gokul",
            "kn-IN": "vijay",
            "bn-IN": "amit",
            "gu-IN": "shubh",
            "mr-IN": "rahul",
            "pa-IN": "rahul",
            "or-IN": "aditya",
            "en-IN": "amit",
        }
        speaker = speaker_map.get(self._tts.language, "aditya")
        payload = {
            "text": self.input_text,
            "target_language_code": self._tts.language,
            "speaker": speaker,
            "pace": 1.0,
            "sample_rate": 16000,
            "enable_preprocessing": True,
            "model": "bulbul:v3",
        }
        print(f"[TTS] Synthesizing in {self._tts.language}: {self.input_text[:60]}...")
        async with httpx.AsyncClient() as client:
            headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
            resp = await client.post(SARVAM_TTS_URL, json=payload, headers=headers, timeout=30.0)
            if not resp.is_success:
                print(f"[TTS] Error {resp.status_code}: {resp.text}")
                resp.raise_for_status()
            audio_b64 = resp.json().get("audios", [""])[0]
            audio_bytes = base64.b64decode(audio_b64)
            print(f"[TTS] Got {len(audio_bytes)} audio bytes")
            output_emitter.initialize(
                request_id=uuid.uuid4().hex,
                sample_rate=16000,
                num_channels=1,
                mime_type="audio/wav",
            )
            output_emitter.push(audio_bytes)
            output_emitter.flush()


class SarvamTTS(tts.TTS):
    def __init__(self, language: str = "en-IN"):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=16000, num_channels=1)
        self.language = language

    def synthesize(self, text: str, *, conn_options=None) -> tts.ChunkedStream:
        return SarvamChunkedStream(tts_instance=self, input_text=text, conn_options=conn_options)


LANGUAGE_NAME_MAP = {
    "hi-IN": "Hindi",
    "te-IN": "Telugu",
    "ta-IN": "Tamil",
    "ml-IN": "Malayalam",
    "kn-IN": "Kannada",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "mr-IN": "Marathi",
    "pa-IN": "Punjabi",
    "or-IN": "Odia",
    "en-IN": "English",
}

LANGUAGE_STYLE_HINT_MAP = {
    "hi-IN": "Use friendly spoken Hinglish. Mix natural Hindi with common English support words like mobile number, confirm, order status, delivery, payment, and update.",
    "te-IN": "Use friendly spoken Telugu with light English mixing. Words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "ta-IN": "Use friendly spoken Tamil with light English mixing. Keep common support words like mobile number, confirm, order status, delivery, payment, and update natural and phone-call friendly.",
    "ml-IN": "Use friendly spoken Malayalam with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "kn-IN": "Use friendly spoken Kannada with light English mixing. Keep common support words like mobile number, confirm, order status, delivery, payment, and update natural and conversational.",
    "bn-IN": "Use friendly spoken Bengali with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "gu-IN": "Use friendly spoken Gujarati with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "mr-IN": "Use friendly spoken Marathi with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "pa-IN": "Use friendly spoken Punjabi with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "or-IN": "Use friendly spoken Odia with light English mixing. Common support words like mobile number, confirm, order status, delivery, payment, and update can stay in English where natural.",
    "en-IN": "Use warm spoken Indian English. Sound like a real support person on a call, not a formal script.",
}

LANGUAGE_SPEECH_EXAMPLE_MAP = {
    "hi-IN": {
        "confirm": "Hi Syam ji, aapka naam confirm kar dijiye.",
        "status": "Okay ji, main aapke order ka status bata deta hoon.",
    },
    "te-IN": {
        "confirm": "Hi Syam garu, mee name confirm chesthara?",
        "status": "Okay, mee order status chepta.",
    },
    "ta-IN": {
        "confirm": "Hi Syam, unga name confirm pannunga.",
        "status": "Okay, unga order status solluren.",
    },
    "ml-IN": {
        "confirm": "Hi Syam, ningalude name confirm cheyyamo?",
        "status": "Okay, ningalude order status parayunnu.",
    },
    "kn-IN": {
        "confirm": "Hi Syam avare, nimma name confirm maadthira?",
        "status": "Okay, nimma order status heluttene.",
    },
    "bn-IN": {
        "confirm": "Hi Syam, apnar name ta confirm korben?",
        "status": "Okay, apnar order er status bolchi.",
    },
    "gu-IN": {
        "confirm": "Hi Syam bhai, tamaru naam confirm kari do.",
        "status": "Okay, tamaru order nu status kahu chhu.",
    },
    "mr-IN": {
        "confirm": "Hi Syam, tumcha name confirm kara na.",
        "status": "Okay, tumchya order cha status sangto.",
    },
    "pa-IN": {
        "confirm": "Hi Syam ji, apna name confirm kar deo.",
        "status": "Okay ji, tuhade order da status das raha haan.",
    },
    "or-IN": {
        "confirm": "Hi Syam, apana name confirm karibe ki?",
        "status": "Okay, apankara order ra status kahuchi.",
    },
    "en-IN": {
        "confirm": "Hi Syam, please confirm your name.",
        "status": "Okay, here's your order status.",
    },
}

INITIAL_SPEECH_MAP = {
    "hi-IN": "नमस्ते, मैं आपका ऑर्डर सपोर्ट सहायक हूँ। कृपया अपना 10 अंकों का मोबाइल नंबर बताइए।",
    "te-IN": "నమస్తే, నేను మీ ఆర్డర్ సపోర్ట్ సహాయకుడిని. దయచేసి మీ 10 అంకెల మొబైల్ నంబర్ చెప్పండి.",
    "ta-IN": "வணக்கம், நான் உங்கள் ஆர்டர் உதவி உதவியாளர். தயவுசெய்து உங்கள் 10 இலக்க மொபைல் எண்ணை சொல்லுங்கள்.",
    "ml-IN": "നമസ്കാരം, ഞാൻ നിങ്ങളുടെ ഓർഡർ സപ്പോർട്ട് സഹായി ആണ്. ദയവായി നിങ്ങളുടെ 10 അക്ക മൊബൈൽ നമ്പർ പറയൂ.",
    "kn-IN": "ನಮಸ್ಕಾರ, ನಾನು ನಿಮ್ಮ ಆರ್ಡರ್ ಬೆಂಬಲ ಸಹಾಯಕ. ದಯವಿಟ್ಟು ನಿಮ್ಮ 10 ಅಂಕೆಯ ಮೊಬೈಲ್ ಸಂಖ್ಯೆಯನ್ನು ತಿಳಿಸಿ.",
    "bn-IN": "নমস্কার, আমি আপনার অর্ডার সাপোর্ট সহকারী। দয়া করে আপনার ১০ সংখ্যার মোবাইল নম্বর বলুন।",
    "gu-IN": "નમસ્તે, હું તમારો ઓર્ડર સપોર્ટ સહાયક છું. કૃપા કરીને તમારો 10 અંકનો મોબાઇલ નંબર કહો.",
    "mr-IN": "नमस्कार, मी तुमचा ऑर्डर सपोर्ट सहाय्यक आहे. कृपया तुमचा 10 अंकी मोबाईल नंबर सांगा.",
    "pa-IN": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ ਤੁਹਾਡਾ ਆਰਡਰ ਸਹਾਇਤਾ ਸਹਾਇਕ ਹਾਂ। ਕਿਰਪਾ ਕਰਕੇ ਆਪਣਾ 10 ਅੰਕਾਂ ਵਾਲਾ ਮੋਬਾਈਲ ਨੰਬਰ ਦੱਸੋ।",
    "or-IN": "ନମସ୍କାର, ମୁଁ ଆପଣଙ୍କ ଅର୍ଡର ସହାୟତା ସହକାରୀ। ଦୟାକରି ଆପଣଙ୍କ 10 ଅଙ୍କର ମୋବାଇଲ ନମ୍ବର କହନ୍ତୁ।",
    "en-IN": "Hello, I am your order support assistant. Please share your 10-digit phone number.",
}

ORDER_SUPPORT_INSTRUCTIONS_TEMPLATE = """
You are a friendly customer support person on a phone call, helping someone check their order status. Talk exactly like a real person would on a phone — casual, warm, short sentences. Never sound like a robot or a recorded IVR message.

You are speaking in {language_name}. {style_hint}

---

HOW THE CALL GOES:

First, ask for their mobile number naturally. Something like "Can I get your mobile number?" — not a formal announcement.
→ Call `get_order_status_from_db` with the number and `customer_confirmed=false`.

When the tool says `confirmation_required`, just ask them their name in a relaxed way. Like "And your name please?" or "Can you tell me your name?"
→ Do NOT say their name back to them. Just ask.
→ Wait for them to say it.

Once they say their name, call the tool again with `customer_confirmed=true`.

If there are multiple orders (`order_selection_required`):
→ Tell them how many orders you found, then read each order ID one at a time with a pause. Like a real person reading from a list — not all at once.
→ Example: "Okay so I can see two orders. First one is... [ID]. Second one is... [ID]. Which one do you want to check?"
→ Wait for them to pick one, then call the tool with that `external_order_id`.

Once you have the order status:
→ Just tell them the status in one natural sentence. Like "Okay so your order is out for delivery" or "Looks like it's still being processed."
→ Do NOT read out payment details, amounts, or item lists unless they ask.
→ Use `latest_status.latest_status` for the status. If that's empty, use `order.status`.

---

TONE RULES — this is the most important part:

Speak like a real support person on a call, not like a script reader.
Use short, natural sentences. Think of how you'd actually talk on the phone.
It's okay to say things like "okay", "sure", "got it", "one sec", "alright" — but don't overdo it.
Use the speaking style examples below as a feel for how to talk, not as exact scripts:
  - Name confirmation vibe: {confirm_example}
  - Status reply vibe: {status_example}

Never read a list of things in one long breath. Pause between items.
Never use formal phrases like "I have retrieved your order information" or "Please be informed that".
If something goes wrong (no orders, wrong number, etc.), just say it plainly and helpfully.

---

HARD RULES:
- Always call `get_order_status_from_db` before saying anything about orders — never guess or make up status
- Only tell the order status. Don't mention payment method, payment status, total amount, or items unless asked
- Never say the customer's name before they confirm it
- If the tool returns an error reason, explain it simply and guide them
"""


def build_order_support_instructions(language_code: str) -> str:
    language_name = LANGUAGE_NAME_MAP.get(language_code, "English")
    style_hint = LANGUAGE_STYLE_HINT_MAP.get(language_code, LANGUAGE_STYLE_HINT_MAP["en-IN"])
    examples = LANGUAGE_SPEECH_EXAMPLE_MAP.get(
        language_code,
        LANGUAGE_SPEECH_EXAMPLE_MAP["en-IN"],
    )
    return ORDER_SUPPORT_INSTRUCTIONS_TEMPLATE.format(
        language_name=language_name,
        language_code=language_code,
        style_hint=style_hint,
        confirm_example=examples["confirm"],
        status_example=examples["status"],
    )


class OrderSupportAgent(Agent):
    def __init__(self, *, language_code: str) -> None:
        super().__init__(instructions=build_order_support_instructions(language_code))
        self._language_code = language_code
        self._user_turn_index = 0
        self._latest_user_text = ""
        self._pending_phone: str | None = None
        self._pending_customer: dict[str, Any] | None = None

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        """Lowercase, drop punctuation, keep letters/digits from any script (Indic, Latin, etc.)."""
        s = (value or "").lower()
        s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
        return re.sub(r"\s+", " ", s).strip()

    def _is_affirmative_confirmation(self, text: str, customer_name: str | None) -> bool:
        raw = (text or "").strip()
        raw_lower = raw.lower()
        # Short scripted / spoken "yes" that regex would not match as Latin tokens
        if any(
            tok in raw_lower
            for tok in (
                "हाँ",
                "हां",
                "हा ",
                " हा",
                "जी हाँ",
                "जी हां",
                "అవును",
                "ఔను",
                "ஆம்",
                "ஆமாம்",
                "അതെ",
                "ಹೌದು",
                "হ্যাঁ",
                "હા",
                "ਹਾਂ",
            )
        ):
            return True

        normalized = self._normalize_text(text)
        if not normalized:
            return False

        affirmative_phrases = (
            "yes",
            "yeah",
            "yep",
            "correct",
            "confirmed",
            "confirm",
            "right",
            "thats right",
            "that is right",
            "yes thats me",
            "yes that is me",
            "i am",
            "this is",
            "speaking",
            "ha",
            "haan",
            "haa",
            "han",
            "haan ji",
            "ji haan",
            "jee haan",
            "sahi",
            "sahi hai",
            "theek",
            "theek hai",
            "bilkul",
            "ok",
            "okay",
            "avunu",
            "aama",
            "seri",
            "howo",
        )
        if any(phrase in normalized for phrase in affirmative_phrases):
            return True

        normalized_name = self._normalize_text(customer_name)
        if normalized_name and normalized_name in normalized:
            return True
        if normalized_name and normalized_name in raw_lower:
            return True

        identity_phrases = ("i am", "this is", "here", "speaking")
        return any(phrase in normalized for phrase in identity_phrases)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        self._user_turn_index += 1
        self._latest_user_text = new_message.text_content or ""

    @llm.function_tool
    async def get_order_status_from_db(
        self,
        phone_number: Optional[str] = None,
        customer_confirmed: bool = False,
        external_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Resolve a customer by phone number, confirm identity, then fetch status for an active order.

        Args:
            phone_number: Customer phone number in any format; last 10 digits are used. After the first
                lookup, you may omit this — the session reuses the number from the previous step.
            customer_confirmed: Set to true only after the caller confirms the resolved customer name.
            external_order_id: After order_selection_required, the app-facing order ID the caller chose.
                Omit if only one active order.
        """
        # 1. Resolve effective phone — reuse cached value whenever caller omits it
        effective_phone = phone_number
        if self._pending_phone and (not effective_phone or not str(effective_phone).strip()):
            effective_phone = self._pending_phone

        # 2. ALWAYS call the DB first — this populates _pending_customer on the first call
        result = await ORDER_LOOKUP.get_order_status(
            phone_number=effective_phone,
            customer_confirmed=customer_confirmed,
            external_order_id=external_order_id,
        )

        # 3. Store session state from the DB result BEFORE any guard check or return
        if result.get("reason") == "confirmation_required":
            self._pending_phone = ORDER_LOOKUP.normalize_phone(effective_phone) or self._pending_phone
            self._pending_customer = result.get("customer") or self._pending_customer
        elif result.get("ok"):
            self._pending_phone = None
            self._pending_customer = None

        # 4. Name-confirmation guard — only fires when DB itself returned confirmation_required
        #    AND customer_confirmed=True AND this is not an order-selection step
        if customer_confirmed and result.get("reason") == "confirmation_required":
            picking_order = bool((external_order_id or "").strip()) and self._pending_customer is not None
            if not picking_order:
                same_phone = (
                    ORDER_LOOKUP.normalize_phone(effective_phone)
                    == ORDER_LOOKUP.normalize_phone(self._pending_phone)
                )
                customer_name = (self._pending_customer or {}).get("name")
                is_affirmative = self._is_affirmative_confirmation(
                    self._latest_user_text, str(customer_name or "")
                )
                if not self._pending_customer or not same_phone or not is_affirmative:
                    return {
                        "ok": False,
                        "reason": "confirmation_required",
                        "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                        "customer": self._pending_customer,
                        "message": (
                            "Please tell me your name to confirm your identity."
                            if self._pending_customer
                            else "Ask the caller for their 10-digit phone number first."
                        ),
                    }

        # 5. Publish active order IDs to UI when selection is required
        if result.get("reason") == "order_selection_required":
            await publish_active_order_ids_to_ui(
                result.get("active_orders") or [],
                "These are the external order IDs from your app (not internal order numbers). Tap or say the one you want.",
            )

        return result


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    print("[Worker] Waiting for participant to determine language...")
    participant = await ctx.wait_for_participant()
    lang = participant.metadata if participant.metadata else "en-IN"

    print(f"[Worker] Incoming call. Detected language: {lang}")
    print(f"[Worker] Using Groq model={GROQ_MODEL!r}")

    # aec_warmup_duration=0: default 3s blocks echo-cancellation warmup from treating user audio as
    # interrupt; combined with interruptible greeting, avoids dropped user turns (no reply after phone).
    # discard_audio_if_uninterruptible=False: keep mic audio for STT even if a speech handle is briefly non-interruptible.
    # VAD settings optimized for primary speaker focus and background noise rejection:
    # - min_silence_duration=1.0: Longer silence required to end speech (filters brief background sounds)
    # - min_speech_duration=0.3: Slightly longer speech required to start (filters short background noises)
    # - activation_threshold=0.6: Higher threshold to activate (more confident it's actual speech, not noise)
    # - deactivation_threshold=0.3: Lower threshold to deactivate (quicker to stop when speech ends)
    agent_session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=1.0,
            min_speech_duration=0.3,
            activation_threshold=0.6,
            deactivation_threshold=0.3,
        ),
        stt=SarvamSTT(language=lang),
        llm=groq.LLM(
            model=GROQ_MODEL,
            api_key=GROQ_API_KEY,
        ),
        tts=SarvamTTS(language=lang),
        max_tool_steps=8,
        aec_warmup_duration=0,
        discard_audio_if_uninterruptible=False,
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(max_retry=5, retry_interval=2.5),
        ),
    )

    voice_agent = OrderSupportAgent(language_code=lang)

    print("[Worker] Starting AgentSession...")
    await agent_session.start(agent=voice_agent, room=ctx.room)

    # Greeting must run only AFTER the session is running. Scheduling say() before start() (e.g. call_later)
    # can fire while AgentSession isn't ready — TTS fails silently and later user turns may not get replies.
    initial_speech = INITIAL_SPEECH_MAP.get(lang, INITIAL_SPEECH_MAP["en-IN"])
    await asyncio.sleep(0.2)
    print(f"[Worker] Sending greeting in {lang}")
    agent_session.say(initial_speech, allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, worker_type=WorkerType.ROOM))
