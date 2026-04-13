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

# Pre-load VAD model once at startup — avoids reloading per session (saves RAM + time)
_VAD_MODEL = silero.VAD.load(
    min_silence_duration=1.0,
    min_speech_duration=0.3,
    activation_threshold=0.6,
    deactivation_threshold=0.3,
)

# Google Gemini configuration (unused — using Groq)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = (os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()

# Sarvam TTS has a ~500 char limit per request; longer text gets cut off silently.
TTS_MAX_CHARS = 450


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
                text = await self._sarvam_stt_once(
                    self._client, wav_data, mode=mode, language_code=lang
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
                    self._client, wav_data, mode="transcribe", language_code="en-IN"
                )
                if text:
                    print(f"[STT] fallback en-IN transcribe transcript: '{text}'")
            except Exception as exc:
                print(f"[STT] fallback en-IN transcribe failed: {exc}")

        if not text:
            print("[STT] All attempts returned empty — agent will remain silent")
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text="", language=self.language)],
            )

        # Clean up trailing punctuation that Sarvam STT adds (especially periods after numbers)
        text = text.rstrip('.!?,;:')
        
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(text=text, language=self.language)],
        )


class SarvamChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts_instance: "SarvamTTS", input_text: str, conn_options=None) -> None:
        super().__init__(tts=tts_instance, input_text=input_text, conn_options=conn_options)
        self._tts = tts_instance

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
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
- Count ONLY the digit characters
- If NOT exactly 10 digits:
  → Respond politely: "Hmm, that number looks a bit off, can you say it once again?"
  → Do NOT call any tool
  → Ask again
- If exactly 10 digits:
  → Silently call: get_order_status_from_db(phone_number=..., customer_confirmed=false)
  → Pass the full number as spoken (the tool will clean it)
  → Wait for tool response

### STEP 2: NAME CONFIRMATION (STRICT VALIDATION REQUIRED)
- After phone number is validated, ALWAYS ask for name confirmation for security
- Ask casually: "Just to confirm, can I know your name?"
- When user provides their name, validate it against database records:
  → If name matches: Say "Thank you, name confirmed!" or similar positive confirmation
  → If name doesn't match: Say "The name doesn't match our records, please provide the correct name"
- Only proceed to orders after successful name validation
- After successful name confirmation:
  → Silently call: get_order_status_from_db(phone_number=..., customer_confirmed=true)
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
  → Read each order ID as ONE complete number (never split)
  → Ask which one they want to check
  → After user picks one:
    → Silently call: get_order_status_from_db(customer_confirmed=true, external_order_id=...)
    → Speak that order's status
    → Done

- If tool returns error (invalid_phone, not found):
  → Apologize politely
  → Ask if they want to try a different number

## RULES
- Keep responses short (1–2 sentences max)
- Speak like a human, not a system
- Phone numbers & order IDs: Always say as ONE full number (never split into groups)
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

    def _is_name_match(self, spoken_text: str, db_name: str) -> bool:
        """
        Strict name matching - checks if spoken name matches database name.
        Handles various formats: first name only, last name only, or full name.
        """
        if not spoken_text or not db_name:
            return False
            
        # Normalize both names for comparison
        spoken_normalized = self._normalize_text(spoken_text)
        db_normalized = self._normalize_text(db_name)
        
        if not spoken_normalized or not db_normalized:
            return False
        
        # If database name is just "Customer", accept any reasonable name
        if db_normalized == "customer":
            return len(spoken_normalized) > 1  # Any name with more than 1 character
        
        # Split database name into parts (first_name + last_name)
        db_parts = [part.strip() for part in db_normalized.split() if part.strip()]
        spoken_parts = [part.strip() for part in spoken_normalized.split() if part.strip()]
        
        if not db_parts or not spoken_parts:
            return False
        
        # Check various matching scenarios:
        # 1. Exact full name match
        if spoken_normalized == db_normalized:
            return True
        
        # 2. Spoken name contains all database name parts (as complete words)
        if all(any(part == spoken_word for spoken_word in spoken_parts) for part in db_parts):
            return True
        
        # 3. Database name contains all spoken name parts (as complete words)
        if all(any(part == db_word for db_word in db_parts) for part in spoken_parts):
            return True
        
        # 4. First name exact match (if user only says first name)
        if len(spoken_parts) == 1 and len(db_parts) > 0 and spoken_parts[0] == db_parts[0]:
            return True
        
        # 5. Last name exact match (if user only says last name)
        if len(spoken_parts) == 1 and len(db_parts) > 1 and spoken_parts[0] == db_parts[-1]:
            return True
        
        # No match found
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

        # 1b. Validate phone digit count — must be exactly 10 digits
        if effective_phone and not customer_confirmed:
            digits_only = re.sub(r"[^0-9]", "", str(effective_phone))
            if len(digits_only) != 10:
                return {
                    "ok": False,
                    "reason": "invalid_phone",
                    "message": (
                        f"The number provided has {len(digits_only)} digits, not 10. "
                        "Ask the caller to repeat their 10-digit mobile number clearly."
                    ),
                }

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

        # 4. Name-confirmation guard — strict name validation when customer_confirmed=True
        if customer_confirmed and self._pending_customer:
            picking_order = bool((external_order_id or "").strip())
            if not picking_order:  # This is name confirmation step, not order selection
                same_phone = (
                    ORDER_LOOKUP.normalize_phone(effective_phone)
                    == ORDER_LOOKUP.normalize_phone(self._pending_phone)
                )
                customer_name = (self._pending_customer or {}).get("name", "")
                
                # Strict name matching - check if spoken name matches database name
                is_name_match = self._is_name_match(self._latest_user_text, customer_name)
                
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
                    # Database didn't return orders yet, but name is confirmed - retry the call
                    print(f"[Worker] Name confirmed: {customer_name}, retrying database call")
                    retry_result = await ORDER_LOOKUP.get_order_status(
                        phone_number=effective_phone,
                        customer_confirmed=True,
                        external_order_id=external_order_id,
                    )
                    if retry_result.get("ok"):
                        retry_result["name_confirmed"] = True
                        retry_result["confirmation_message"] = f"Thank you! Name confirmed. Here are your order details:"
                    return retry_result

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
        vad=_VAD_MODEL,
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
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        worker_type=WorkerType.ROOM,
        num_idle_processes=1,  # Render has limited RAM — 1 warm process is enough
    ))
