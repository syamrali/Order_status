import asyncio
import base64
import io
import json
import os
import re
import time
import struct
import uuid
import wave
from difflib import SequenceMatcher
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
from livekit.plugins import google, silero, groq

from order_lookup import OrderLookupService

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
if not SARVAM_API_KEY:
    raise ValueError("SARVAM_API_KEY environment variable is required but not set!")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Primary STT mode (default codemix for Indic+English). If transcript is empty, worker retries with
# transcribe/verbatim — empty STT causes LiveKit to skip the reply entirely (no agent speech).
SARVAM_STT_PRIMARY_MODE = (os.environ.get("SARVAM_STT_MODE") or "codemix").strip() or "codemix"

TARGET_SAMPLE_RATE = 16000

# When every STT attempt returns nothing, the agent should remain silent.
STT_EMPTY_FALLBACK_USER_TEXT = ""


ORDER_LOOKUP = OrderLookupService.from_env()

# How long silence must last before "end of user speech" (seconds). Higher = fewer false cuts but
# noticeably slower replies after the user stops talking (e.g. after giving a phone number).
def _env_float(key: str, default: str, lo: float, hi: float) -> float:
    try:
        v = float((os.environ.get(key) or default).strip() or default)
    except ValueError:
        v = float(default)
    return max(lo, min(hi, v))


# End-of-utterance silence (seconds). Using ~1s avoids clipping short names at turn boundaries.
_VAD_MIN_SILENCE = _env_float("VAD_MIN_SILENCE_DURATION", "1.0", 0.25, 2.0)

# Minimum speech length (seconds) before a segment counts.
# Keep this low so short names like "Mohan" are not dropped.
_VAD_MIN_SPEECH = _env_float("VAD_MIN_SPEECH_DURATION", "0.12", 0.08, 1.0)

# Silero probability thresholds (0–1). Higher activation = stricter “is this speech?” — helps reject
# side voices & ambient noise, but can miss very soft talkers (tune per deployment).
_VAD_ACTIVATION = _env_float("VAD_ACTIVATION_THRESHOLD", "0.68", 0.35, 0.95)
_VAD_DEACTIVATION = _env_float("VAD_DEACTIVATION_THRESHOLD", "0.38", 0.1, 0.9)
if _VAD_DEACTIVATION >= _VAD_ACTIVATION:
    _VAD_DEACTIVATION = max(0.1, _VAD_ACTIVATION - 0.05)

# Pre-load VAD model once at startup — avoids reloading per session (saves RAM + time)
_VAD_MODEL = silero.VAD.load(
    min_silence_duration=_VAD_MIN_SILENCE,
    min_speech_duration=_VAD_MIN_SPEECH,
    activation_threshold=_VAD_ACTIVATION,
    deactivation_threshold=_VAD_DEACTIVATION,
)
print(
    "[Worker] VAD:",
    f"silence={_VAD_MIN_SILENCE}s",
    f"min_speech={_VAD_MIN_SPEECH}s",
    f"activation={_VAD_ACTIVATION}",
    f"deactivation={_VAD_DEACTIVATION}",
)

# LLM provider selection: default Gemini, keep Groq available as backup.
LLM_PROVIDER = (os.environ.get("LLM_PROVIDER") or "google").strip().lower()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_MODEL = (os.environ.get("GOOGLE_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = (os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()


def _build_llm():
    if LLM_PROVIDER in {"google", "gemini"}:
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY is required when LLM_PROVIDER=google")
        print(f"[Worker] LLM provider=google model={GOOGLE_MODEL!r}")
        return google.LLM(
            model=GOOGLE_MODEL,
            api_key=GOOGLE_API_KEY,
        )

    if LLM_PROVIDER == "groq":
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        print(f"[Worker] LLM provider=groq model={GROQ_MODEL!r}")
        return groq.LLM(
            model=GROQ_MODEL,
            api_key=GROQ_API_KEY,
        )

    raise ValueError("Unsupported LLM_PROVIDER. Use 'google' or 'groq'.")

# Sarvam TTS has a ~500 char limit per request; longer text gets cut off silently.
TTS_MAX_CHARS = 450


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _coerce_tool_bool(value: Any) -> bool:
    """Groq/JSON sometimes yields strings; normalize for tool logic."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _digits_only(value: str | None) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _canonical_mobile_last10(value: str | None) -> str | None:
    """
    Indian mobiles: keep last 10 digits when caller says 10 digits, 11 with leading 0,
    or 12+ with country code 91… — STT often returns 11 digits.
    """
    d = _digits_only(value)
    if len(d) < 10:
        return None
    return d[-10:]


def _extract_order_ref_candidate(value: str | None) -> str:
    """
    Pull an order reference token from user text, e.g. '439473NE' or 'order id 439473NE'.
    Keeps alnum only; prefers tokens with at least one digit.
    """
    s = (value or "").strip()
    if not s:
        return ""
    compact = re.sub(r"[^A-Za-z0-9\s]", " ", s)
    tokens = [t.strip() for t in compact.split() if t.strip()]
    # Merge split refs like "439477 PF" -> "439477PF"
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if re.fullmatch(r"\d{4,}", a) and re.fullmatch(r"[A-Za-z]{1,4}", b):
            return f"{a}{b}".upper()
    # Prefer tokens that look like order refs (contains digits, at least 4 chars)
    for t in reversed(tokens):
        if re.search(r"\d", t) and len(t) >= 4:
            return t.upper()
    return ""


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
        self._client = httpx.AsyncClient(timeout=20.0)  # reuse connection across calls

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
        t_stt_total = time.perf_counter()
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

        # Only try 2 modes max — codemix handles most cases, transcribe as fallback.
        # Trying 4 modes sequentially adds 2-3s of extra latency per turn.
        modes: list[str] = []
        for m in (SARVAM_STT_PRIMARY_MODE, "transcribe"):
            if m and m not in modes:
                modes.append(m)

        text = ""
        for mode in modes:
            try:
                t_mode = time.perf_counter()
                text = await self._sarvam_stt_once(
                    self._client, wav_data, mode=mode, language_code=lang
                )
                print(f"[TIMING] STT Sarvam HTTP mode={mode} {_ms_since(t_mode):.0f}ms")
            except Exception as exc:
                print(f"[STT] mode={mode} lang={lang} failed: {exc}")
                continue
            if text:
                print(f"[STT] mode={mode} lang={lang} transcript: '{text}'")
                break

        # Spoken digits are often tagged en-IN; retry transcribe with English if still empty.
        if not text and lang not in ("en-IN", "en", "unknown"):
            try:
                t_fb = time.perf_counter()
                text = await self._sarvam_stt_once(
                    self._client, wav_data, mode="transcribe", language_code="en-IN"
                )
                print(f"[TIMING] STT Sarvam HTTP fallback en-IN {_ms_since(t_fb):.0f}ms")
                if text:
                    print(f"[STT] fallback en-IN transcribe transcript: '{text}'")
            except Exception as exc:
                print(f"[STT] fallback en-IN transcribe failed: {exc}")

        if not text:
            print(f"[TIMING] STT total {_ms_since(t_stt_total):.0f}ms (empty transcript)")
            print("[STT] All attempts returned empty — agent will remain silent")
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text="", language=self.language)],
            )

        # Clean up trailing punctuation that Sarvam STT adds (especially periods after numbers)
        text = text.rstrip('.!?,;:')

        print(
            f"[TIMING] STT total {_ms_since(t_stt_total):.0f}ms chars={len(text)} "
            f"preview={text[:72]!r}{'…' if len(text) > 72 else ''}"
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(text=text, language=self.language)],
        )


class SarvamChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts_instance: "SarvamTTS", input_text: str, conn_options=None) -> None:
        super().__init__(tts=tts_instance, input_text=input_text, conn_options=conn_options)
        self._tts = tts_instance

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        t_tts = time.perf_counter()
        # Use distinct natural voices per language (not bright/energetic ones)
        speaker_map = {
            "hi-IN": "rohan",   # Hindi - smooth, professional male
            "te-IN": "varun",   # Telugu - warm male
            "ta-IN": "mani",    # Tamil - clear male
            "ml-IN": "sunny",   # Malayalam - friendly male
            "kn-IN": "tarun",   # Kannada - natural male
            "bn-IN": "dev",     # Bengali - warm male
            "gu-IN": "kabir",   # Gujarati - smooth male
            "mr-IN": "sumit",   # Marathi - professional male
            "pa-IN": "manan",   # Punjabi - energetic male
            "or-IN": "rehan",   # Odia - calm male
            "en-IN": "ashutosh",# English - clear professional male
        }
        speaker = speaker_map.get(self._tts.language, "rohan")

        # Strip markdown artifacts the LLM may inject (bold, italic, bullets, etc.)
        clean_text = re.sub(r"\*+", "", self.input_text)
        clean_text = re.sub(r"_{1,2}(.+?)_{1,2}", r"\1", clean_text)
        clean_text = re.sub(r"`+", "", clean_text)
        clean_text = re.sub(r"^\s*[-*•]\s+", "", clean_text, flags=re.MULTILINE)
        clean_text = clean_text.strip()

        # Add natural pauses: replace periods with commas to avoid sentence breaks (tic-tic sounds)
        # Keep question marks and exclamations but add slight pause markers
        clean_text = re.sub(r"\.\s+", ", ", clean_text)  # period → comma (smoother flow)
        clean_text = re.sub(r"([?!])\s+", r"\1 ... ", clean_text)  # add pause after ? or !
        
        # Ensure natural breathing pauses in long sentences
        if len(clean_text) > 100 and "," not in clean_text:
            # Insert comma after ~50 chars at word boundary
            words = clean_text.split()
            char_count = 0
            for i, word in enumerate(words):
                char_count += len(word) + 1
                if char_count > 50 and i < len(words) - 1:
                    words[i] = words[i] + ","
                    break
            clean_text = " ".join(words)

        # Sarvam TTS silently truncates beyond ~500 chars; trim at sentence boundary
        if len(clean_text) > TTS_MAX_CHARS:
            truncated = clean_text[:TTS_MAX_CHARS]
            last_break = max(
                truncated.rfind("।"),  # Devanagari danda
                truncated.rfind("。"),
                truncated.rfind(". "),
                truncated.rfind("? "),
                truncated.rfind("! "),
            )
            clean_text = truncated[: last_break + 1].strip() if last_break > 0 else truncated.strip()

        payload = {
            "text": clean_text,
            "target_language_code": self._tts.language,
            "speaker": speaker,
            "pace": 1.05,          # slightly faster for clarity
            "sample_rate": 24000,  # Sarvam's native high-quality rate
            "enable_preprocessing": True,
            "model": "bulbul:v3",
        }
        print(f"[TTS] Synthesizing in {self._tts.language}: {clean_text[:60]}...")
        headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
        resp = await self._tts._client.post(SARVAM_TTS_URL, json=payload, headers=headers)
        if not resp.is_success:
            print(f"[TTS] Error {resp.status_code}: {resp.text}")
            resp.raise_for_status()
        audio_b64 = resp.json().get("audios", [""])[0]
        # Sarvam returns base64-encoded WAV - decode directly, no need to wrap again
        wav_bytes = base64.b64decode(audio_b64)
        print(f"[TTS] Got {len(wav_bytes)} WAV bytes")
        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=24000,  # match Sarvam's output
            num_channels=1,
            mime_type="audio/wav",
        )
        output_emitter.push(wav_bytes)
        output_emitter.flush()
        print(f"[TIMING] TTS Sarvam total {_ms_since(t_tts):.0f}ms chars_in={len(clean_text)} wav_bytes={len(wav_bytes)}")


class SarvamTTS(tts.TTS):
    def __init__(self, language: str = "en-IN"):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=24000, num_channels=1)
        self.language = language
        self._client = httpx.AsyncClient(timeout=20.0)  # reuse connection across calls

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
    "hi-IN": "एक मददगार दोस्त की तरह बात करें। नमस्ते से शुरू करें। English शब्दों (mobile, order, status) को हिंदी के साथ स्वाभाविक रूप से जोड़ें। बातचीत में अपनापन और गरमाहट लाएं।",
    "te-IN": "ఒక ఆప్త మిత్రుడిలా మాట్లాడండి। నమస్కారం తో మొదలుపెట్టండి। English పదాలను (mobile, order, status) తెలుగులో సహజంగా వాడండి। సంభాషణలో స్నేహపూర్వక భావన ఉండాలి।",
    "ta-IN": "ஒரு நல்ல நண்பர் மாதிரி பேசுங்க। வணக்கம் சொல்லி ஆரம்பிங்க। English சொற்களை (mobile, order, status) தமிழ்ல இயல்பா பயன்படுத்துங்க। பேச்சுல கனிவும் அன்பும் இருக்கட்டும்.",
    "ml-IN": "ഒരു സുഹൃത്തിനെപ്പോലെ സംസാരിക്കൂ। നമസ്കാരം പറഞ്ഞു തുടങ്ങൂ। ഇംഗ്ലീഷ് വാക്കുകൾ (mobile, order, status) മലയാളത്തിൽ സ്വാഭാവികമായി ഉപയോഗിക്കൂ। സംസാരത്തിൽ സ്നേഹവും കരുതലും വേണം.",
    "kn-IN": "ಒಬ್ಬ ಆಪ್ತ ಸ್ನೇಹಿತನಂತೆ ಮಾತನಾಡಿ। ನಮಸ್ಕಾರದಿಂದ ಶುರು ಮಾಡಿ। English ಪದಗಳನ್ನು (mobile, order, status) ಕನ್ನಡದಲ್ಲಿ ಸಹಜವಾಗಿ ಬಳಸಿ। ಸಂಭಾಷಣೆ ಆತ್ಮೀಯವಾಗಿರಲಿ.",
    "bn-IN": "একজন বন্ধুর মতো কথা বলুন। নমস্কার দিয়ে শুরু করুন। ইংরেজি শব্দগুলো (mobile, order, status) বাংলার সাথে স্বাভাবিকভাবে মিশিয়ে বলুন। কথায় আন্তরিকতা এবং উষ্ণতা রাখুন।",
    "gu-IN": "એક મદદગાર મિત્રની જેમ વાત કરો. નમસ્તેથી શરૂઆત કરો. English શબ્દો (mobile, order, status) ને ગુજરાતી સાથે સ્વાભાવિક રીતે જોડો. વાતચીતમાં આત્મીયતા અને હૂંફ લાવો.",
    "mr-IN": "एका जवळच्या मित्राप्रमाणे बोला। नमस्काराने सुरुवात करा। English शब्द (mobile, order, status) मराठीत नैसर्गिकरीत्या वापरा। बोलण्यात आपुलकी आणि जिव्हाळा असावा.",
    "pa-IN": "ਇੱਕ ਮਦਦਗਾਰ ਦੋਸਤ ਵਾਂਗ ਗੱਲ ਕਰੋ। ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ ਨਾਲ ਸ਼ੁਰੂ ਕਰੋ। ਅੰਗਰੇਜ਼ੀ ਸ਼ਬਦਾਂ (mobile, order, status) ਨੂੰ ਪੰਜਾਬੀ ਨਾਲ ਕੁਦਰਤੀ ਤੌਰ 'ਤੇ ਜੋੜੋ। ਗੱਲਬਾਤ ਵਿੱਚ ਨਿੱਘ ਅਤੇ ਹਮਦਰਦੀ ਰੱਖੋ।",
    "or-IN": "ଜଣେ ସାହାଯ୍ୟକାରୀ ବନ୍ଧୁ ଭଳି କଥା ହୁଅନ୍ତୁ। ନମସ୍କାରରୁ ଆରମ୍ଭ କରନ୍ତୁ। ଇଂରାଜୀ ଶବ୍ଦଗୁଡ଼ିକ (mobile, order, status) କୁ ଓଡ଼ିଆ ସହିତ ସ୍ୱାଭାବିକ ଭାବରେ ବ୍ୟବହାର କରନ୍ତୁ। କଥାବାର୍ତ୍ତାରେ ଆତ୍ମୀୟତା ଏବଂ ସହାନୁଭୂତି ରଖନ୍ତୁ।",
    "en-IN": "Talk like a warm, helpful friend on the phone. Start with a friendly greeting. Keep it casual, empathetic, and professional yet personal. Use natural pauses and fillers.",
}

LANGUAGE_SPEECH_EXAMPLE_MAP = {
    "hi-IN": {
        "confirm": "हाँ जी, आप Syam ही हैं ना?",
        "status": "हाँ भाई, आपका order delivered हो चुका है और उम्मीद है आपको पसंद आया होगा!",
        "name_confirmed": "धन्यवाद! नाम confirm हो गया। यहाँ आपके order की details हैं:",
        "name_mismatch": "माफ़ करिए, यह नाम हमारे records से match नहीं कर रहा। कृपया सही नाम बताइए।",
    },
    "te-IN": {
        "confirm": "మీరు శ్యామ్ గారే కదా?",
        "status": "అవునండి, మీ order delivered అయిపోయింది. ఇంకా ఏమైనా సహాయం కావాలా?",
        "name_confirmed": "ధన్యవాదాలు! పేరు confirm అయ్యింది. ఇవి మీ order details:",
        "name_mismatch": "క్షమించండి, ఈ పేరు మా records లో లేదు. దయచేసి సరైన పేరు చెప్పండి।",
    },
    "ta-IN": {
        "confirm": "நீங்க ஷியாம் தானே?",
        "status": "சொல்லுங்க, உங்க order delivered ஆயிடுச்சு. வேற ஏதாவது உதவி வேணுமா?",
        "name_confirmed": "நன்றி! பேர் confirm ஆயிடுச்சு. இதுங்க உங்க order details:",
        "name_mismatch": "மன்னிச்சுங்க, இந்த பேர் எங்க records-ல இல்ல. சரியான பேர சொல்லுங்க।",
    },
    "ml-IN": {
        "confirm": "നിങ്ങൾ ശ്യാം തന്നെയാണോ?",
        "status": "അതെ, നിങ്ങളുടെ order delivered ആയിട്ടുണ്ട്. വേറെ എന്തെങ്കിലും സഹായം വേണോ?",
        "name_confirmed": "നന്ദി! പേര് confirm ആയി. ഇതാ നിങ്ങളുടെ order details:",
        "name_mismatch": "ക്ഷമിക്കണം, ഈ പേര് ഞങ്ങളുടെ records ൽ ഇല്ല. ശരിയായ പേര് പറയൂ।",
    },
    "kn-IN": {
        "confirm": "ನೀವು ಶ್ಯಾಮ್ ಅಲ್ವಾ?",
        "status": "ಹೌದು, ನಿಮ್ಮ order delivered ಆಗಿದೆ. ಬೇರೆ ಏನಾದರೂ ಸಹಾಯ ಬೇಕಿತ್ತಾ?",
        "name_confirmed": "ಧನ್ಯವಾದಗಳು! ಹೆಸರು confirm ಆಯಿತು. ಇವು ನಿಮ್ಮ order details:",
        "name_mismatch": "ಕ್ಷಮಿಸಿ, ಈ ಹೆಸರು ನಮ್ಮ records ನಲ್ಲಿ ಇಲ್ಲ. ಸರಿಯಾದ ಹೆಸರು ಹೇಳಿ।",
    },
    "bn-IN": {
        "confirm": "আপনি শ্যাম তো?",
        "status": "হ্যাঁ, আপনার order delivered হয়ে গেছে। আর কিছু জানতে চান?",
    },
    "gu-IN": {
        "confirm": "તમે શ્યામ જ છો ને?",
        "status": "હા જી, તમારો order delivered થઈ ગયો છે. બીજી કોઈ મદદ જોઈએ?",
    },
    "mr-IN": {
        "confirm": "तुम्ही श्यामच आहात ना?",
        "status": "हो, तुमचा order delivered झाला आहे. अजून काही मदत हवी आहे का?",
    },
    "pa-IN": {
        "confirm": "ਤੁਸੀਂ ਸ਼ਿਆਮ ਹੋ ਨਾ?",
        "status": "ਹਾਂ ਜੀ, ਤੁਹਾਡਾ order delivered ਹੋ ਗਿਆ ਹੈ। ਹੋਰ ਕੋਈ ਸੇਵਾ?",
    },
    "or-IN": {
        "confirm": "ଆପଣ ଶ୍ୟାମ ତ?",
        "status": "ହଁ ଆଜ୍ଞା, ଆପଣଙ୍କ order delivered ହୋଇସାରିଛି। ଆଉ କିଛି ସାହାଯ್ಯ ଦରକାର କି?",
    },
    "en-IN": {
        "confirm": "You're Syam, right? Just wanted to be sure!",
        "status": "Great news! Your order has been delivered and is ready for you.",
        "name_confirmed": "Thank you! Name confirmed. Here are your order details:",
        "name_mismatch": "Sorry, that name doesn't match our records. Please provide the correct name.",
    },
}

INITIAL_SPEECH_MAP = {
    "hi-IN": "नमस्ते... मैं आपकी order support टीम से बात कर रहा हूँ, आपकी मदद के लिए मैं यहाँ हूँ, क्या आप अपना मोबाइल नंबर बता सकते हैं",
    "te-IN": "నమస్కారం... నేను order support నుంచి మాట్లాడుతున్నాను, మీకు సహాయం చేయడానికి ఇక్కడ ఉన్నాను, దయచేసి మీ మొబైల్ నంబర్ చెబుతారా",
    "ta-IN": "வணக்கம்... நான் order support-லிருந்து பேசுறேன், உங்களுக்கு உதவி செய்ய நான் இங்க இருக்கேன், தயவுசெஞ்சு உங்க மொபைல் நம்பரை சொல்ல முடியுமா",
    "ml-IN": "നമസ്കാരം... ഞാൻ order support-ൽ നിന്നാണ് സംസാരിക്കുന്നത്, സഹായിക്കാൻ ഞാൻ ഇവിടെയുണ്ട്, ദയവായി നിങ്ങളുടെ മൊബൈൽ നമ്പർ പറയാമോ",
    "kn-IN": "ನಮಸ್ಕಾರ... ನಾನು order support ಇಂದ ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ, ನಿಮಗೆ ಸಹಾಯ ಮಾಡಲು ನಾನು ಇಲ್ಲಿದ್ದೇನೆ, ದಯವಿಟ್ಟು ನಿಮ್ಮ ಮೊಬೈಲ್ ನಂಬರ್ ಹೇಳ್ತೀರಾ",
    "bn-IN": "নমস্কার... আমি order support থেকে বলছি, আমি আপনাকে সাহায্য করব, আপনি কি আপনার মোবাইল নাম্বারটা বলতে পারেন",
    "gu-IN": "નમસ્તે... હું order supportમાંથી વાત કરું છું, તમને મદદ કરવા માટે હું અહીં છું, શું તમે તમારો મોબાઈલ નંબર જણાવી શકો",
    "mr-IN": "नमस्कार... मी order support मधून बोलतोय, तुमच्या मदतीसाठी मी इथे आहे, कृपया तुमचा मोबाईल नंबर सांगू शकता का",
    "pa-IN": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ... ਮੈਂ order support ਤੋਂ ਬੋਲ ਰਿਹਾ ਹਾਂ, ਮੈਂ ਤੁਹਾਡੀ ਮਦਦ ਕਰਾਂਗਾ, ਕੀ ਤੁਸੀਂ ਆਪਣਾ ਮੋਬਾਈਲ ਨੰਬਰ ਦੱਸ ਸਕਦੇ ਹੋ",
    "or-IN": "ନମସ୍କାର... ମୁଁ order support ରୁ କହୁଛି, ଆପଣଙ୍କୁ ସାହାଯ୍ୟ କରିବା ପାଇଁ ମୁଁ ଏଠାରେ ଅଛି, ଦୟାକରି ଆପଣଙ୍କ ମୋବାଇଲ୍ ନମ୍ବର କହିବେ କି",
    "en-IN": "Hi there... I'm from the order support team, I'd love to help you with your order, could you please share your mobile number",
}

ORDER_SUPPORT_INSTRUCTIONS_TEMPLATE = """
You are a friendly, human-like voice support agent helping customers check their order status over a call.

## CRITICAL: HOW TO USE TOOLS
- Tools are SILENT background operations. Never speak tool names, JSON, or function syntax.
- After calling a tool, only speak the result in natural spoken language.
- The caller should feel like they are talking to a real person.
- Never expose system behavior.

## LANGUAGE
Speak ONLY in {language_name}.
Style Guidelines:
- {style_hint}
- Talk like a real local person on a phone call
- Keep tone warm, polite, and slightly casual
- Use natural fillers where needed (like "okay", "alright", "hmm", "one sec")
- Avoid robotic or scripted phrasing

Example responses:
- Asking name: {confirm_example}
- Giving status: {status_example}
- Name confirmed: {name_confirmed_example}
- Name mismatch: {name_mismatch_example}

## CONVERSATION BEHAVIOR
- If user interrupts, adapt naturally and continue from where they left
- Do NOT restart the flow unnecessarily
- Remember what user already told (like mobile number or name)
- If user sounds confused, guide them gently

## WORKFLOW - FOLLOW THIS EXACT FLOW

### STEP 1: COLLECT MOBILE NUMBER
- Ask for 10-digit mobile number in a friendly way
- When user speaks a number, extract ONLY the digits (ignore periods, spaces, dashes)
- If there are FEWER than 10 digits:
  → Respond politely: "Hmm, that number looks a bit off, can you say it once again?"
  → Do NOT call any tool
  → Ask again
- If there are 10 OR MORE digits (including 10-digit mobile, or 11 with a leading zero, or 12 with country code 91):
  → Silently call: get_order_status_from_db(phone_number=..., customer_confirmed=false)
  → Pass the digits as spoken or grouped; the tool keeps the **last 10 digits** for matching
  → Wait for tool response
  → If the tool returns confirmation_required: do NOT add filler or long preambles — in ONE short sentence, ask for their name only (this must feel instant after they give the number).

### STEP 2: NAME CONFIRMATION (STRICT VALIDATION REQUIRED)
- After phone number is validated, ALWAYS ask for name confirmation for security
- Ask casually: "Just to confirm, can I know your name?"
- When the user answers, they may say full sentences like "my name is Syam Mohan" — only the **name itself** counts; ignore filler phrases when reasoning about a match.
- Prefer short answers: "Syam Mohan" or "Mohan" are valid; do not ask them to repeat in a longer form.
- When you call the tool to confirm identity after they spoke their name, you MUST pass **caller_spoken_name** set to the **exact name phrase** they used (copy from the latest user transcript), e.g. `caller_spoken_name="Syam Mohan"` or `caller_spoken_name="my name is Syam Mohan"`. This is required so the server can match it to the database even if timing differs from the transcript buffer.
- When user provides their name, validate it against database records:
  → If name matches: Say "Thank you, name confirmed!" or similar positive confirmation
  → If name doesn't match: Say "The name doesn't match our records, please provide the correct name"
- Only proceed to orders after successful name validation
- After successful name confirmation:
  → Silently call: get_order_status_from_db(phone_number=..., customer_confirmed=true, caller_spoken_name=...same name phrase...)
  → Wait for tool response

### STEP 3: PROVIDE ORDER STATUS
- If tool returns name_mismatch:
  → Tell user the name doesn't match records
  → Ask them to provide the correct name again
  → Do NOT proceed to orders

- If tool returns success with name_confirmed=True:
  → First say the confirmation_message (e.g., "Thank you! Name confirmed.")
  → Then provide order details

- If tool returns single order with status:
  → Speak the status clearly in one sentence
  → Done
  
- If tool returns multiple orders (order_selection_required):
  → Tell how many orders found
  → Read only the exact order IDs from tool output (active_orders list)
  → NEVER use sample/placeholder IDs (like 1234, 5678, 9012)
  → Read each order ID as ONE complete number (never split)
  → Ask which one they want to check
  → After user picks one:
    → Silently call: get_order_status_from_db(customer_confirmed=true, external_order_id=..., caller_spoken_name=...optional if you still have their name phrase...)
    → Speak that order's status
    → Done

- If tool returns order_selection_already_shared:
  → Do NOT repeat the same order list again
  → In one short sentence, ask the caller to say one order ID from the displayed list

- If tool returns order_status_already_shared:
  → Do NOT repeat the same status again
  → In one short sentence, ask if they want another order or any other help

- If tool returns error (invalid_phone, not found):
  → Apologize politely
  → Ask if they want to try a different number

## RULES
- Keep responses short (1–2 sentences max)
- Speak like a human, not a system
- Phone numbers & order IDs: Always say as ONE full number (never split into groups)
- NEVER invent or guess order IDs/statuses/customer names. If the tool response is not ok, or reason is db_error/not_configured/customer_not_found/invalid_phone/name_mismatch, say that clearly and ask for retry/correction.
- If reason is confirmation_required, only ask for name confirmation; do not mention order count or order IDs yet.
- Only provide order status from tool response:
  → Use latest_status.latest_status
  → Fallback: order.status
- Never guess or invent data
- Do NOT mention payment or price unless user asks
- Follow the 3-step workflow strictly
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
        name_confirmed_example=examples.get("name_confirmed", examples["confirm"]),
        name_mismatch_example=examples.get("name_mismatch", "Sorry, that name doesn't match our records."),
    )


# Strip leading "my name is …" / "I am …" so we match on name tokens only (STT often returns full sentences).
_NAME_LEADING_PATTERN = re.compile(
    r"""^\s*(?:
        my\s+name\s+is|the\s+name\s+is|name\s+is|
        i\s*['\u2019]?\s*m|i\s+am|this\s+is|it\s*['\u2019]?\s*s|
        call\s+me|mine\s+is|here\s+is|you\s+can\s+call\s+me|
        मेरा\s+नाम|नाम\s+है
    )\s*[,:]?\s*""",
    re.VERBOSE | re.IGNORECASE,
)

_NAME_TRAILING_FILLER = re.compile(
    r"\s+(?:please|thanks|thank\s+you|thankyou|ji|only|actually|sir|madam)\s*$",
    re.IGNORECASE,
)

# Words that are not part of a person's name but appear in natural answers.
_NAME_NOISE_WORDS = frozenset(
    {
        "my",
        "name",
        "is",
        "the",
        "am",
        "i",
        "im",
        "its",
        "it",
        "this",
        "that",
        "call",
        "me",
        "yes",
        "yeah",
        "hello",
        "hi",
        "here",
        "please",
        "actually",
        "only",
        "correct",
        "right",
        "okay",
        "ok",
        "from",
        "side",
        "line",
    }
)

# Common STT / spelling variants for the same spoken name (Indian English). Extend as needed.
_NAME_TOKEN_ALIASES: dict[str, frozenset[str]] = {
    "syam": frozenset({"shyam", "sham", "shiyaam", "syaam", "shyaam"}),
    "shyam": frozenset({"syam", "sham", "shiyaam"}),
    "sankar": frozenset({"shankar", "sanker", "shanker"}),
    "shankar": frozenset({"sankar", "sanker", "shanker"}),
}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _sound_simplify(token: str) -> str:
    """
    Small phonetic normalization for common Indic-English STT variants:
    shyam/syam, shankar/sankar, f/ph, long vowels, etc.
    """
    t = (token or "").lower().strip()
    if not t:
        return t
    t = t.replace("ph", "f")
    t = t.replace("sh", "s")
    t = t.replace("ck", "k")
    t = t.replace("aa", "a").replace("ee", "i").replace("oo", "u")
    return t


def extract_spoken_name_for_match(raw: str | None) -> str:
    """Return text after stripping 'my name is …' style prefixes and trailing fillers."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = _NAME_LEADING_PATTERN.sub("", s, count=1)
    s = _NAME_TRAILING_FILLER.sub("", s).strip()
    s = re.sub(r"^[,\s:]+", "", s)
    return s.strip()


# Groq/OpenAI strict tool validation may treat inferred schemas as "all fields required".
# Explicit JSON schema: only customer_confirmed is required; other args may be omitted.
_GET_ORDER_STATUS_FROM_DB_RAW_SCHEMA: dict[str, Any] = {
    "name": "get_order_status_from_db",
    "description": (
        "Look up the customer by phone, then confirm identity and return order status. "
        "Use customer_confirmed=false with phone_number for the first lookup. "
        "Use customer_confirmed=true after the caller states their name; pass caller_spoken_name "
        "with their exact words from the conversation when possible. "
        "Use external_order_id only when the caller chooses one order among several active ones."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {
                "type": "string",
                "description": (
                    "Customer mobile: 10 digits, or with leading 0 / country code 91 (tool keeps last 10 digits). "
                    "Required on first lookup when customer_confirmed is false (unless session already stored the number)."
                ),
            },
            "customer_confirmed": {
                "type": "boolean",
                "description": "false when validating phone only; true after name confirmation or when resolving a chosen order id.",
            },
            "external_order_id": {
                "type": "string",
                "description": "App-facing order id when the caller selects one of multiple active orders; otherwise omit.",
            },
            "caller_spoken_name": {
                "type": "string",
                "description": "Verbatim name phrase the caller used (for identity match). Strongly recommended when customer_confirmed is true for name verification.",
            },
        },
        "required": ["customer_confirmed"],
    },
}


class OrderSupportAgent(Agent):
    def __init__(self, *, language_code: str) -> None:
        super().__init__(instructions=build_order_support_instructions(language_code))
        self._language_code = language_code
        self._user_turn_index = 0
        self._latest_user_text = ""
        self._recent_user_texts: list[str] = []
        self._pending_phone: str | None = None
        self._pending_customer: dict[str, Any] | None = None
        self._last_order_status_signature: str = ""
        self._last_active_orders_signature: str = ""

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        """Lowercase, drop punctuation, keep letters/digits from any script (Indic, Latin, etc.)."""
        s = (value or "").lower()
        s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def _tokens_alias_or_fuzzy_match(a: str, b: str) -> bool:
        """True if two name tokens are equal, alias-linked, or close enough for STT spelling drift."""
        if not a or not b:
            return False
        if a == b:
            return True
        for canon, variants in _NAME_TOKEN_ALIASES.items():
            bag = {canon} | set(variants)
            if a in bag and b in bag:
                return True
        max_len = max(len(a), len(b))
        if max_len <= 2:
            return a == b
        lev = _levenshtein(a, b)
        max_dist = 1 if max_len <= 5 else 2
        if lev <= max_dist:
            return True
        sa, sb = _sound_simplify(a), _sound_simplify(b)
        if sa == sb:
            return True
        if sa and sb and _levenshtein(sa, sb) <= (1 if max(len(sa), len(sb)) <= 5 else 2):
            return True
        return SequenceMatcher(None, a, b).ratio() >= 0.86

    def _spoken_name_tokens(self, spoken_text: str) -> list[str]:
        """Tokens used for matching: strip 'my name is…', drop filler words."""
        extracted = extract_spoken_name_for_match(spoken_text)
        base = extracted if extracted else spoken_text
        norm = self._normalize_text(base)
        parts = [p for p in norm.split() if p.strip() and p not in _NAME_NOISE_WORDS]
        if parts:
            return parts
        # Fallback: normalized full string (single unusual token)
        return [norm] if norm else []

    @staticmethod
    def _looks_like_phone_number_only(s: str) -> bool:
        """True if the string is almost only digits (stale transcript from the phone step)."""
        if not (s or "").strip():
            return False
        digits = re.sub(r"[^0-9]", "", s)
        has_letters = bool(re.search(r"[A-Za-z\u0080-\u1fff]", s))
        return len(digits) >= 8 and not has_letters

    def _snippet_for_name_match(
        self,
        caller_spoken_name: Optional[str],
        raw_arguments: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Prefer explicit name from the LLM tool args; else last user text unless it still looks
        like a phone number (common race: tool runs before transcript buffer updates).
        """
        explicit = (caller_spoken_name or "").strip()
        if explicit:
            return explicit
        # Some providers pass raw tool payload/meta; recover caller_spoken_name when present.
        if isinstance(raw_arguments, dict):
            raw_name = str(raw_arguments.get("caller_spoken_name") or "").strip()
            if raw_name:
                return raw_name
        latest = (self._latest_user_text or "").strip()
        if self._looks_like_phone_number_only(latest):
            # Race-safe fallback: walk recent user turns and pick the newest non-phone utterance.
            for prev in reversed(self._recent_user_texts):
                p = (prev or "").strip()
                if p and not self._looks_like_phone_number_only(p):
                    return p
            return ""
        return latest

    def _is_name_match(self, spoken_text: str, db_name: str) -> bool:
        """
        Match spoken name to DB (first+last). Handles:
        - Full sentences ('my name is …') via extraction + noise-word drop
        - Single first or last name
        - STT spelling variants (Syam/Shyam, Sankar/Shankar) via fuzzy + alias map
        """
        if not spoken_text or not db_name:
            return False

        db_normalized = self._normalize_text(db_name)
        if not db_normalized:
            return False

        spoken_parts = self._spoken_name_tokens(spoken_text)
        db_parts = [p for p in db_normalized.split() if p.strip()]

        if not spoken_parts or not db_parts:
            return False

        # If database name is just "Customer", accept any reasonable name (2+ chars total)
        if db_normalized == "customer":
            joined = " ".join(spoken_parts)
            return len(joined.strip()) > 1

        spoken_joined = " ".join(spoken_parts)
        # Whole-string fuzzy (helps 'syam mohan' vs 'shyam mohan')
        if SequenceMatcher(None, spoken_joined, db_normalized).ratio() >= 0.86:
            return True
        if spoken_joined == db_normalized:
            return True
        # Compact compare (spaces / minor STT punctuation differences)
        compact_s = spoken_joined.replace(" ", "")
        compact_d = db_normalized.replace(" ", "")
        if len(compact_d) >= 3 and compact_s and SequenceMatcher(None, compact_s, compact_d).ratio() >= 0.86:
            return True

        def each_db_token_matches_spoken() -> bool:
            for d in db_parts:
                if not any(self._tokens_alias_or_fuzzy_match(d, s) for s in spoken_parts):
                    return False
            return True

        def each_spoken_token_matches_db() -> bool:
            for s in spoken_parts:
                if not any(self._tokens_alias_or_fuzzy_match(s, d) for d in db_parts):
                    return False
            return True

        # Accept either direction so middle names / short spoken variants are not rejected.
        if each_db_token_matches_spoken() or each_spoken_token_matches_db():
            return True

        # Single spoken token: try against first name, last name, or full string
        if len(spoken_parts) == 1:
            one = spoken_parts[0]
            if len(db_parts) == 1:
                return self._tokens_alias_or_fuzzy_match(one, db_parts[0])
            if any(self._tokens_alias_or_fuzzy_match(one, d) for d in db_parts):
                return True
            if self._tokens_alias_or_fuzzy_match(one, db_normalized.replace(" ", "")):
                return True

        return False

    def _is_affirmative_confirmation(self, text: str, customer_name: str | None) -> bool:
        raw = (text or "").strip()
        raw_lower = raw.lower()
        
        # Normalize both the spoken text and database name for comparison
        normalized_spoken = self._normalize_text(text)
        normalized_db_name = self._normalize_text(customer_name)
        
        # If database name exists and spoken text contains it, it's a match
        if normalized_db_name and normalized_db_name in normalized_spoken:
            return True
        
        # Also check raw text (preserves script-specific characters)
        if customer_name and customer_name.strip().lower() in raw_lower:
            return True
        
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

        # If none of the above matched, it's NOT confirmed
        return False

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        self._user_turn_index += 1
        self._latest_user_text = new_message.text_content or ""
        self._recent_user_texts.append(self._latest_user_text)
        if len(self._recent_user_texts) > 8:
            self._recent_user_texts = self._recent_user_texts[-8:]
        raw = self._latest_user_text
        print(
            f"[TIMING] user_turn_completed #{self._user_turn_index} "
            f"chars={len(raw)} preview={raw[:100]!r}{'…' if len(raw) > 100 else ''}"
        )

    @llm.function_tool(raw_schema=_GET_ORDER_STATUS_FROM_DB_RAW_SCHEMA)
    async def get_order_status_from_db(
        self,
        phone_number: Optional[str] = None,
        customer_confirmed: bool = False,
        external_order_id: Optional[str] = None,
        caller_spoken_name: Optional[str] = None,
        raw_arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Resolve a customer by phone number, confirm identity, then fetch status for an active order.

        Args:
            phone_number: Customer phone number in any format; last 10 digits are used. After the first
                lookup, you may omit this — the session reuses the number from the previous step.
            customer_confirmed: Set to true only after the caller confirms the resolved customer name.
            external_order_id: After order_selection_required, the app-facing order ID the caller chose.
                Omit if only one active order.
            caller_spoken_name: When customer_confirmed=true for a name check, pass the exact name phrase
                the caller just said (from the latest user transcript). Strongly recommended — avoids
                mismatches when the internal transcript buffer still holds the phone number.
            raw_arguments: LiveKit raw tool payload passthrough (ignored by app logic).
        """
        _ = raw_arguments  # intentionally unused
        customer_confirmed = _coerce_tool_bool(customer_confirmed)

        # 1. Resolve effective phone — reuse cached value whenever caller omits it
        effective_phone = phone_number
        if self._pending_phone and (not effective_phone or not str(effective_phone).strip()):
            effective_phone = self._pending_phone

        # 1d. If caller is selecting an order but model forgot external_order_id, recover it from speech.
        if customer_confirmed and not (external_order_id or "").strip():
            order_ref = _extract_order_ref_candidate(caller_spoken_name) or _extract_order_ref_candidate(self._latest_user_text)
            if order_ref:
                external_order_id = order_ref
                print(f"[Worker] Recovered external_order_id from speech: {external_order_id!r}")

        # 1a. If LLM omitted phone on first lookup, recover digits from last user transcript (STT)
        if not customer_confirmed:
            cand = _canonical_mobile_last10(effective_phone)
            if not cand and self._latest_user_text:
                cand = _canonical_mobile_last10(self._latest_user_text)
                if cand:
                    effective_phone = cand
                    print(
                        f"[Worker] Using phone from user transcript tail=…{cand[-4:]} "
                        "(tool args omitted phone_number)"
                    )
            elif cand:
                effective_phone = cand

        # 1b. Validate phone — need at least 10 digits total; normalize to last 10 (India +91 / leading 0)
        if not customer_confirmed:
            final = _canonical_mobile_last10(effective_phone)
            if not final:
                digits_ct = len(_digits_only(effective_phone))
                return {
                    "ok": False,
                    "reason": "invalid_phone",
                    "message": (
                        f"The number has {digits_ct} digits; need at least 10 for a mobile. "
                        "Ask the caller to repeat their 10-digit number clearly, or call the tool "
                        "with phone_number set to what they said."
                    ),
                }
            effective_phone = final

        # 1c. Safety: if the model forgot to set customer_confirmed=true after user said their name,
        # auto-promote this turn to confirmation based on name-like speech.
        if not customer_confirmed and self._pending_customer and not (external_order_id or "").strip():
            snippet_candidate = self._snippet_for_name_match(caller_spoken_name, raw_arguments)
            if (
                snippet_candidate
                and not self._looks_like_phone_number_only(snippet_candidate)
                and not re.search(r"\d", snippet_candidate)
            ):
                if self._spoken_name_tokens(snippet_candidate):
                    customer_confirmed = True
                    print(
                        f"[Worker] Auto-promoted to customer_confirmed=True using snippet={snippet_candidate!r}"
                    )

        # 2. ALWAYS call the DB first — this populates _pending_customer on the first call
        t_db = time.perf_counter()
        result = await ORDER_LOOKUP.get_order_status(
            phone_number=effective_phone,
            customer_confirmed=customer_confirmed,
            external_order_id=external_order_id,
        )
        print(
            f"[TIMING] tool get_order_status_from_db (DB+logic) {_ms_since(t_db):.0f}ms "
            f"reason={result.get('reason')!r} ok={result.get('ok')}"
        )

        # 3. Store session state from the DB result BEFORE any guard check or return
        if result.get("reason") == "confirmation_required":
            self._pending_phone = ORDER_LOOKUP.normalize_phone(effective_phone) or self._pending_phone
            self._pending_customer = result.get("customer") or self._pending_customer
        elif result.get("ok"):
            self._pending_phone = None
            self._pending_customer = None

        # 3b. Safety bootstrap: if caller already confirmed name but pending_customer was lost,
        # recover from current confirmation_required payload so guard can proceed deterministically.
        if (
            customer_confirmed
            and not self._pending_customer
            and result.get("reason") == "confirmation_required"
            and result.get("customer")
        ):
            self._pending_customer = result.get("customer")
            self._pending_phone = ORDER_LOOKUP.normalize_phone(effective_phone) or self._pending_phone
            print("[Worker] Recovered pending customer from confirmation_required payload")

        # 4. Name-confirmation guard — strict name validation when customer_confirmed=True
        if customer_confirmed and self._pending_customer:
            picking_order = bool((external_order_id or "").strip())
            if not picking_order:  # This is name confirmation step, not order selection
                same_phone = (
                    ORDER_LOOKUP.normalize_phone(effective_phone)
                    == ORDER_LOOKUP.normalize_phone(self._pending_phone)
                )
                customer_name = (self._pending_customer or {}).get("name", "")
                snippet = self._snippet_for_name_match(caller_spoken_name, raw_arguments)
                is_name_match = self._is_name_match(snippet, customer_name) if snippet else False
                if not snippet:
                    print(
                        "[Worker] name guard: empty name snippet — pass caller_spoken_name from the "
                        f"user's last utterance. latest_user_text={self._latest_user_text!r} "
                        f"caller_spoken_name={caller_spoken_name!r}"
                    )
                else:
                    print(
                        f"[Worker] name guard: snippet={snippet!r} db_name={customer_name!r} "
                        f"tokens={self._spoken_name_tokens(snippet)!r} match={is_name_match}"
                    )

                if not same_phone or not is_name_match:
                    return {
                        "ok": False,
                        "reason": "name_mismatch", 
                        "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                        "customer": self._pending_customer,
                        "message": (
                            "The name you provided doesn't match our records. Please provide the correct name associated with this phone number."
                        ),
                    }
                
                # Name matches - add success message to result if database returned orders
                if result.get("ok"):
                    print(f"[Worker] Name confirmed: {customer_name}")
                    # Add name confirmation success message to the result
                    result["name_confirmed"] = True
                    result["confirmation_message"] = f"Thank you! Name confirmed. Here are your order details:"
                    return result
                else:
                    # Name matched, but initial result was not final (often confirmation_required).
                    # Force a confirmed DB fetch so we don't loop and the LLM doesn't hallucinate IDs.
                    print(
                        f"[Worker] Name confirmed: {customer_name}, forcing confirmed DB fetch "
                        f"(initial reason={result.get('reason')!r})"
                    )
                    t_db2 = time.perf_counter()
                    retry_result = await ORDER_LOOKUP.get_order_status(
                        phone_number=effective_phone,
                        customer_confirmed=True,
                        external_order_id=external_order_id,
                    )
                    print(
                        f"[TIMING] tool get_order_status retry {_ms_since(t_db2):.0f}ms "
                        f"reason={retry_result.get('reason')!r} ok={retry_result.get('ok')}"
                    )
                    # If backend still asks confirmation, convert to explicit error to prevent endless loop.
                    if retry_result.get("reason") == "confirmation_required":
                        return {
                            "ok": False,
                            "reason": "db_error",
                            "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                            "customer": self._pending_customer,
                            "message": (
                                "Identity was matched, but backend did not progress to orders. "
                                "Please try again in a moment."
                            ),
                        }

                    if retry_result.get("ok") or retry_result.get("reason") == "order_selection_required":
                        retry_result["name_confirmed"] = True
                        retry_result["confirmation_message"] = f"Thank you! Name confirmed. Here are your order details:"
                    return retry_result

        # 4b. If user asked with customer_confirmed=true and backend still asks for confirmation,
        # stop here with explicit error to prevent LLM from inventing order IDs/status.
        if customer_confirmed and result.get("reason") == "confirmation_required":
            return {
                "ok": False,
                "reason": "db_error",
                "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                "customer": self._pending_customer or result.get("customer"),
                "message": (
                    "Name confirmation did not progress to orders. "
                    "Ask the caller to repeat their name once and retry."
                ),
            }

        # 5. Publish active order IDs to UI when selection is required
        if result.get("reason") == "order_selection_required":
            active_orders = result.get("active_orders") or []
            order_ids = [
                str((row or {}).get("external_order_id") or "").strip().upper()
                for row in active_orders
                if isinstance(row, dict)
            ]
            current_list_sig = "|".join([oid for oid in order_ids if oid])
            if current_list_sig and current_list_sig == self._last_active_orders_signature:
                return {
                    "ok": False,
                    "reason": "order_selection_already_shared",
                    "already_shared": True,
                    "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                    "customer": result.get("customer") or self._pending_customer,
                    "active_orders": active_orders,
                    "message": (
                        "Order options were already shared. Do not repeat the same list again. "
                        "Ask the caller to say one order ID from the shown list."
                    ),
                }
            self._last_active_orders_signature = current_list_sig
            await publish_active_order_ids_to_ui(
                active_orders,
                "These are the external order IDs from your app (not internal order numbers). Tap or say the one you want.",
            )

        # 6. De-duplicate repeated status narration for same order+status payload.
        if result.get("ok"):
            order_id = str(((result.get("order") or {}) if isinstance(result.get("order"), dict) else {}).get("external_order_id") or "").strip().upper()
            latest_status = str(((result.get("latest_status") or {}) if isinstance(result.get("latest_status"), dict) else {}).get("status") or "").strip().lower()
            phone_tail = str(ORDER_LOOKUP.normalize_phone(effective_phone) or "")
            current_status_sig = f"{phone_tail}|{order_id}|{latest_status}"
            if (
                order_id
                and latest_status
                and current_status_sig == self._last_order_status_signature
            ):
                return {
                    "ok": False,
                    "reason": "order_status_already_shared",
                    "already_shared": True,
                    "phone_last10": phone_tail,
                    "customer": result.get("customer") or self._pending_customer,
                    "order": result.get("order"),
                    "latest_status": result.get("latest_status"),
                    "message": (
                        "This order status was already shared in the previous response. "
                        "Do not repeat it; ask what the caller wants next."
                    ),
                }
            self._last_order_status_signature = current_status_sig

        return result


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    print("[Worker] Waiting for participant to determine language...")
    participant = await ctx.wait_for_participant()
    lang = participant.metadata if participant.metadata else "en-IN"

    print(f"[Worker] Incoming call. Detected language: {lang}")

    # aec_warmup_duration=0: default 3s blocks echo-cancellation warmup from treating user audio as
    # interrupt; combined with interruptible greeting, avoids dropped user turns (no reply after phone).
    # discard_audio_if_uninterruptible=False: keep mic audio for STT even if a speech handle is briefly non-interruptible.
    # VAD: env-tunable (VAD_*). Stricter activation rejects more side-talk / noise; raise further in loud spaces.
    agent_session = AgentSession(
        vad=_VAD_MODEL,
        stt=SarvamSTT(language=lang),
        llm=_build_llm(),
        tts=SarvamTTS(language=lang),
        max_tool_steps=4,
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
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        worker_type=WorkerType.ROOM,
        num_idle_processes=1,  # Render has limited RAM — 1 warm process is enough
    ))
