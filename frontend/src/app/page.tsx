"use client";

import { useState, useCallback, useEffect, useRef, useMemo } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useLocalParticipant,
  useParticipants,
  TrackToggle,
  BarVisualizer,
  useVoiceAssistant,
  useRoomContext,
} from "@livekit/components-react";
import {
  Track,
  ConnectionState,
  RoomEvent,
  ParticipantKind,
  type AudioCaptureOptions,
  type Participant,
  type TranscriptionSegment,
} from "livekit-client";
import "@livekit/components-styles";

/**
 * WebRTC capture: Advanced audio processing for clear voice isolation.
 * - echoCancellation: Removes echo from speakers
 * - noiseSuppression: Removes background noise (high setting for maximum effect)
 * - autoGainControl: Normalizes volume levels
 * - Advanced constraints for better voice isolation
 */
const VOICE_FOCUS_AUDIO_CAPTURE: AudioCaptureOptions = {
  echoCancellation: { ideal: true },
  noiseSuppression: { ideal: true },
  autoGainControl: { ideal: true },
  // Advanced constraints for better voice isolation
  channelCount: { ideal: 1 }, // Mono for better voice focus
  sampleRate: { ideal: 48000 }, // High quality sample rate
  sampleSize: { ideal: 16 }, // 16-bit audio
  latency: { ideal: 0.01 }, // Low latency for real-time
};

export const SUPPORTED_LANGUAGES = [
  { code: "hi-IN", name: "Hindi", label: "Hindi" },
  { code: "te-IN", name: "Telugu", label: "Telugu" },
  { code: "ta-IN", name: "Tamil", label: "Tamil" },
  { code: "ml-IN", name: "Malayalam", label: "Malayalam" },
  { code: "kn-IN", name: "Kannada", label: "Kannada" },
  { code: "bn-IN", name: "Bengali", label: "Bengali" },
  { code: "gu-IN", name: "Gujarati", label: "Gujarati" },
  { code: "mr-IN", name: "Marathi", label: "Marathi" },
  { code: "pa-IN", name: "Punjabi", label: "Punjabi" },
  { code: "or-IN", name: "Odia", label: "Odia" },
  { code: "en-IN", name: "English", label: "English" },
];

const CONNECTING_FILLER_BY_LANGUAGE: Record<string, string> = {
  "hi-IN": "वॉइस एजेंट से कनेक्ट हो रहा है, कृपया प्रतीक्षा करें...",
  "te-IN": "వాయిస్ ఏజెంట్‌కు కనెక్ట్ అవుతోంది, దయచేసి వేచి ఉండండి...",
  "ta-IN": "வாய்ஸ் ஏஜென்டுடன் இணைக்கப்படுகிறது, தயவுசெய்து காத்திருக்கவும்...",
  "ml-IN": "വോയ്‌സ് ഏജന്റുമായി കണക്റ്റ് ചെയ്യുന്നു, ദയവായി കാത്തിരിക്കുക...",
  "kn-IN": "ವಾಯ್ಸ್ ಏಜೆಂಟ್‌ಗೆ ಸಂಪರ್ಕಿಸಲಾಗುತ್ತಿದೆ, ದಯವಿಟ್ಟು ಕಾಯಿರಿ...",
  "bn-IN": "ভয়েস এজেন্টের সাথে সংযোগ করা হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন...",
  "gu-IN": "વૉઇસ એજન્ટ સાથે કનેક્ટ થઈ રહ્યું છે, કૃપા કરીને રાહ જુઓ...",
  "mr-IN": "व्हॉइस एजंटशी कनेक्ट होत आहे, कृपया थांबा...",
  "pa-IN": "ਵੋਇਸ ਏਜੰਟ ਨਾਲ ਕਨੈਕਟ ਕੀਤਾ ਜਾ ਰਿਹਾ ਹੈ, ਕਿਰਪਾ ਕਰਕੇ ਉਡੀਕ ਕਰੋ...",
  "or-IN": "ଭଏସ୍ ଏଜେଣ୍ଟ ସହିତ ସଂଯୋଗ କରାଯାଉଛି, ଦୟାକରି ଅପେକ୍ଷା କରନ୍ତୁ...",
  "en-IN": "Connecting to voice agent, please wait...",
};
const ENGLISH_VOICE_FILLER = "Connecting to voice agent, please wait...";

const AGENT_STATUS_BY_LANGUAGE: Record<string, { speaking: string; listening: string }> = {
  "hi-IN": { speaking: "बोल रहा है...", listening: "सुन रहा है..." },
  "te-IN": { speaking: "మాట్లాడుతోంది...", listening: "వింటోంది..." },
  "ta-IN": { speaking: "பேசுகிறது...", listening: "கேட்கிறது..." },
  "ml-IN": { speaking: "സംസാരിക്കുന്നു...", listening: "കേൾക്കുന്നു..." },
  "kn-IN": { speaking: "ಮಾತನಾಡುತ್ತಿದೆ...", listening: "ಕೇಳುತ್ತಿದೆ..." },
  "bn-IN": { speaking: "বলছে...", listening: "শুনছে..." },
  "gu-IN": { speaking: "બોલી રહ્યું છે...", listening: "સાંભળી રહ્યું છે..." },
  "mr-IN": { speaking: "बोलत आहे...", listening: "ऐकत आहे..." },
  "pa-IN": { speaking: "ਬੋਲ ਰਿਹਾ ਹੈ...", listening: "ਸੁਣ ਰਿਹਾ ਹੈ..." },
  "or-IN": { speaking: "କହୁଛି...", listening: "ଶୁଣୁଛି..." },
  "en-IN": { speaking: "Speaking...", listening: "Listening..." },
};

function getConnectingFiller(languageCode: string): string {
  return CONNECTING_FILLER_BY_LANGUAGE[languageCode] ?? CONNECTING_FILLER_BY_LANGUAGE["en-IN"];
}

function getAgentStatusLabels(languageCode: string): { speaking: string; listening: string } {
  return AGENT_STATUS_BY_LANGUAGE[languageCode] ?? AGENT_STATUS_BY_LANGUAGE["en-IN"];
}

const InfoIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <line x1="12" x2="12" y1="16" y2="12" />
    <line x1="12" x2="12" y1="8" y2="8" />
  </svg>
);

const CallEndIcon = () => (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7 2 2 0 0 1 1.72 2v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.42 19.42 0 0 1-3.33-2.67m-2.67-3.34a19.79 19.79 0 0 1-3.07-8.63A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91" />
    <line x1="22" x2="2" y1="2" y2="22" />
  </svg>
);

type TranscriptRole = "user" | "agent";

type OrderRowFromAgent = {
  external_order_id?: string;
  status?: unknown;
  created_at?: unknown;
};

/** Live panel line: speech transcript or structured active-order IDs from the agent. */
type ChatPanelLine =
  | {
      kind: "transcript";
      key: string;
      role: TranscriptRole;
      text: string;
      final: boolean;
      updatedAt: number;
      speakerLabel: string;
    }
  | {
      kind: "order_ids";
      key: string;
      updatedAt: number;
      hint: string;
      orders: OrderRowFromAgent[];
    };

function PersonCutout({ className = "" }: { className?: string }) {
  return (
    <div className={`person-cutout ${className}`}>
      <span className="person-cutout__head" />
      <span className="person-cutout__body" />
    </div>
  );
}

function timeLabel(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Show order / reference ids as one token: remove spaces between digits only. */
function formatOrderIdForDisplay(id: string | undefined | null): string {
  const s = id?.trim();
  if (!s) {
    return "—";
  }
  return s.replace(/(?<=\d)\s+(?=\d)/g, "").trim();
}

/** Collapse spaces between digits (phones read as "9 4 9 3 3 3...") and split order IDs like "453746 NE". */
function formatTranscriptDisplay(text: string): string {
  if (!text) {
    return text;
  }
  let t = text;
  // Phone-style: remove spaces between digits only (keep spaces before/after words)
  t = t.replace(/(?<=\d)\s+(?=\d)/g, "");
  // External order id often read as digits + space + letters
  t = t.replace(/\b(\d{4,})\s+([A-Za-z]{2,8})\b/g, "$1$2");
  return t;
}

function shouldMergeTranscriptTail(prev: string, next: string): boolean {
  const p = prev.trimEnd();
  const n = next.trim();
  if (!p || !n || n.length > 12) {
    return false;
  }
  const pDigits = p.replace(/(?<=\d)\s+(?=\d)/g, "");
  // Continue phone / digit string across STT segments: "949" + "3336321"
  if (/\d$/.test(pDigits) && /^\d[\d\s]*$/.test(n)) {
    return true;
  }
  // Order id: "453746" + "NE" or "453746" + "N" + "E" (handled in two merges)
  if (/\d{4,}$/.test(pDigits) && /^[A-Za-z]{1,8}$/.test(n)) {
    return true;
  }
  const pAlnum = pDigits.replace(/\s/g, "");
  if (/\d{4,}[A-Za-z]{0,6}$/.test(pAlnum) && /^[A-Za-z]{1,2}$/.test(n)) {
    return true;
  }
  return false;
}

function mergeTranscriptText(prev: string, next: string): string {
  const p = prev.trimEnd();
  const n = next.trim();
  if (/\d$/.test(p.replace(/(?<=\d)\s+(?=\d)/g, "")) && /^\d/.test(n)) {
    return p.replace(/\s+$/g, "") + n.replace(/\s/g, "");
  }
  if (/\d{4,}$/.test(p.replace(/(?<=\d)\s+(?=\d)/g, "")) && /^[A-Za-z]/.test(n)) {
    return p.replace(/\s+$/g, "") + n;
  }
  const pCompact = p.replace(/(?<=\d)\s+(?=\d)/g, "").replace(/\s/g, "");
  if (/\d{4,}[A-Za-z]{0,6}$/.test(pCompact) && /^[A-Za-z]{1,2}$/.test(n)) {
    return p.replace(/\s+$/g, "") + n;
  }
  return `${p} ${n}`;
}

function coalesceAdjacentTranscripts(lines: ChatPanelLine[]): ChatPanelLine[] {
  const sorted = [...lines].sort((a, b) => a.updatedAt - b.updatedAt);
  const out: ChatPanelLine[] = [];
  for (const line of sorted) {
    if (line.kind !== "transcript") {
      out.push(line);
      continue;
    }
    const last = out[out.length - 1];
    if (
      last?.kind === "transcript" &&
      last.role === line.role &&
      shouldMergeTranscriptTail(last.text, line.text)
    ) {
      out[out.length - 1] = {
        ...last,
        text: mergeTranscriptText(last.text, line.text),
        updatedAt: Math.max(last.updatedAt, line.updatedAt),
        final: last.final && line.final,
      };
    } else {
      out.push(line);
    }
  }
  return out;
}

/** Row styles for active-order list (distinct backgrounds for scanability). */
const ORDER_ROW_STYLES: { bg: string; border: string; accent: string }[] = [
  { bg: "rgba(34, 197, 94, 0.16)", border: "rgba(34, 197, 94, 0.5)", accent: "#6ee7b7" },
  { bg: "rgba(59, 130, 246, 0.14)", border: "rgba(59, 130, 246, 0.45)", accent: "#93c5fd" },
  { bg: "rgba(168, 85, 247, 0.14)", border: "rgba(168, 85, 247, 0.45)", accent: "#d8b4fe" },
  { bg: "rgba(245, 158, 11, 0.14)", border: "rgba(245, 158, 11, 0.45)", accent: "#fcd34d" },
  { bg: "rgba(236, 72, 153, 0.12)", border: "rgba(236, 72, 153, 0.4)", accent: "#f9a8d4" },
  { bg: "rgba(20, 184, 166, 0.14)", border: "rgba(20, 184, 166, 0.45)", accent: "#5eead4" },
];

function ActiveCallRoom({
  onDisconnected,
  connectingMessage,
  speakingMessage,
  listeningMessage,
  languageCode,
}: {
  onDisconnected: () => void;
  connectingMessage: string;
  speakingMessage: string;
  listeningMessage: string;
  languageCode: string;
}) {
  const room = useRoomContext();
  const roomState = useConnectionState();
  const {
    localParticipant,
    isMicrophoneEnabled,
    microphoneTrack,
    lastMicrophoneError,
  } = useLocalParticipant();
  const participants = useParticipants();
  const { agent, audioTrack: agentAudioTrack, state: agentState } = useVoiceAssistant();

  const [conversation, setConversation] = useState<ChatPanelLine[]>([]);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const hasAgentRespondedRef = useRef(false);
  const fillerLoopTimeoutRef = useRef<number | null>(null);
  const isFillerSpeakingRef = useRef(false);
  const activeFillerLangRef = useRef("");

  const localMicTrackRef = useMemo(
    () => ({
      participant: localParticipant,
      source: Track.Source.Microphone,
      publication: microphoneTrack,
    }),
    [localParticipant, microphoneTrack],
  );
  const remoteAgents = participants.filter((p) => !p.isLocal);
  const isConnected = roomState === ConnectionState.Connected;
  const isAgentSpeaking = agentState === "speaking";

  // Debug: log room state changes
  useEffect(() => {
    console.log("[Room] connectionState:", roomState, "| agentState:", agentState, "| remoteAgents:", remoteAgents.length);
  }, [roomState, agentState, remoteAgents.length]);
  const micStatus = lastMicrophoneError
    ? "Microphone access failed"
    : !microphoneTrack
      ? "Waiting for microphone..."
      : isMicrophoneEnabled
        ? "Mic on — we hear you"
        : "Mic off — tap Unmute only when you speak (reduces background noise)";

  // Manual mic only: no auto mute/unmute from agent state (avoids fighting the user and always-on capture).
  const startedMutedRef = useRef(false);
  useEffect(() => {
    if (roomState !== ConnectionState.Connected || !microphoneTrack || lastMicrophoneError || startedMutedRef.current) {
      return;
    }
    startedMutedRef.current = true;
    void localParticipant.setMicrophoneEnabled(false);
  }, [roomState, microphoneTrack, lastMicrophoneError, localParticipant]);

  const stopFillerLoop = useCallback((): void => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) {
      return;
    }
    if (fillerLoopTimeoutRef.current != null) {
      window.clearTimeout(fillerLoopTimeoutRef.current);
      fillerLoopTimeoutRef.current = null;
    }
    window.speechSynthesis.cancel();
    isFillerSpeakingRef.current = false;
  }, []);

  const scheduleFillerLoop = useCallback(function scheduleFillerLoopInner(): void {
    if (
      !isConnected ||
      hasAgentRespondedRef.current ||
      isFillerSpeakingRef.current ||
      typeof window === "undefined" ||
      !("speechSynthesis" in window)
    ) {
      return;
    }

    const utterance = new SpeechSynthesisUtterance(ENGLISH_VOICE_FILLER);
    utterance.lang = "en-IN";
    utterance.rate = 1;
    utterance.pitch = 1;

    const voices = window.speechSynthesis.getVoices();
    const matchedVoice =
      voices.find((v) => v.lang.toLowerCase() === utterance.lang.toLowerCase()) ||
      voices.find((v) => v.lang.toLowerCase().startsWith("en"));
    if (matchedVoice) {
      utterance.voice = matchedVoice;
    }

    utterance.onstart = () => {
      isFillerSpeakingRef.current = true;
    };
    utterance.onend = () => {
      isFillerSpeakingRef.current = false;
      if (hasAgentRespondedRef.current || !isConnected) {
        return;
      }
      fillerLoopTimeoutRef.current = window.setTimeout(() => {
        scheduleFillerLoopInner();
      }, 2000);
    };
    utterance.onerror = () => {
      isFillerSpeakingRef.current = false;
      if (hasAgentRespondedRef.current || !isConnected) {
        return;
      }
      fillerLoopTimeoutRef.current = window.setTimeout(() => {
        scheduleFillerLoopInner();
      }, 2000);
    };
    window.speechSynthesis.resume();
    window.speechSynthesis.speak(utterance);
  }, [isConnected]);

  useEffect(() => {
    if (!isConnected) {
      hasAgentRespondedRef.current = false;
      activeFillerLangRef.current = "";
      stopFillerLoop();
      return;
    }
    if (agentState === "speaking") {
      hasAgentRespondedRef.current = true;
      stopFillerLoop();
      return;
    }
    if (hasAgentRespondedRef.current) {
      return;
    }
    if (activeFillerLangRef.current !== languageCode) {
      stopFillerLoop();
      activeFillerLangRef.current = languageCode;
    }
    scheduleFillerLoop();
  }, [isConnected, agentState, languageCode, scheduleFillerLoop, stopFillerLoop]);

  useEffect(() => {
    return () => {
      stopFillerLoop();
    };
  }, [stopFillerLoop]);

  useEffect(() => {
    const handleTranscription = (
      segments: TranscriptionSegment[],
      participant?: Participant,
    ) => {
      if (!segments.length || !participant) {
        return;
      }

      const speakerIdentity = participant.identity;
      const isAgentSpeaker =
        Boolean(agent?.identity && speakerIdentity === agent.identity) ||
        participant.kind === ParticipantKind.AGENT;

      const role: TranscriptRole = isAgentSpeaker ? "agent" : "user";
      if (isAgentSpeaker) {
        hasAgentRespondedRef.current = true;
      }

      const speakerLabel =
        role === "agent"
          ? agent?.name || "Support Agent"
          : speakerIdentity === localParticipant.identity
            ? "You"
            : participant.name || "Customer";

      setConversation((prev) => {
        const next = [...prev];

        for (const segment of segments) {
          const text = segment.text.trim();
          if (!text) {
            continue;
          }

          const key = `${speakerIdentity}:${segment.id}`;
          const updatedAt = segment.lastReceivedTime || Date.now();

          const item: ChatPanelLine = {
            kind: "transcript",
            key,
            role,
            text,
            final: segment.final,
            updatedAt,
            speakerLabel,
          };

          const existingIndex = next.findIndex((msg) => msg.kind === "transcript" && msg.key === key);
          if (existingIndex >= 0) {
            next[existingIndex] = item;
          } else {
            next.push(item);
          }
        }

        next.sort((a, b) => a.updatedAt - b.updatedAt);
        const merged = coalesceAdjacentTranscripts(next);
        return merged.slice(-80);
      });
    };

    room.on(RoomEvent.TranscriptionReceived, handleTranscription);
    return () => {
      room.off(RoomEvent.TranscriptionReceived, handleTranscription);
    };
  }, [room, agent?.identity, agent?.name, localParticipant.identity]);

  useEffect(() => {
    const handleData = (payload: Uint8Array, participant: Participant | undefined, _kind: unknown, topic?: string) => {
      if (topic !== "order_support" || !participant || participant.isLocal) {
        return;
      }
      try {
        const text = new TextDecoder().decode(payload);
        const data = JSON.parse(text) as {
          type?: string;
          action?: string;
          hint?: string;
          orders?: OrderRowFromAgent[];
        };
        if (data.type !== "order_support" || data.action !== "show_active_order_ids") {
          return;
        }
        const orders = Array.isArray(data.orders) ? data.orders : [];
        if (orders.length === 0) {
          return;
        }
        const line: ChatPanelLine = {
          kind: "order_ids",
          key: `order-ids:${Date.now()}:${Math.random().toString(36).slice(2, 9)}`,
          updatedAt: Date.now(),
          hint:
            data.hint ||
            "External order IDs below match what you see in the app. Say which one you want.",
          orders,
        };
        setConversation((prev) => {
          const next = coalesceAdjacentTranscripts([...prev, line]);
          next.sort((a, b) => a.updatedAt - b.updatedAt);
          return next.slice(-80);
        });
      } catch {
        /* ignore malformed payloads */
      }
    };

    room.on(RoomEvent.DataReceived, handleData);
    return () => {
      room.off(RoomEvent.DataReceived, handleData);
    };
  }, [room]);

  useEffect(() => {
    if (!conversationRef.current) {
      return;
    }
    conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
  }, [conversation]);

  return (
    <div className="call-overlay">
      <div className="call-video-layer" />

      <div className="top-nav">
        <div className="mentor-info">
          <span>Order Support Voice Agent</span>
          <span style={{ opacity: 0.6, cursor: "pointer" }}>
            <InfoIcon />
          </span>
        </div>
        <div className="top-actions">
          {isConnected && (
            <div className="live-pill">
              <div className="live-pill__dot" />
              Live
            </div>
          )}
          <div className="participant-meta">
            Agents:{" "}
            {remoteAgents.length > 0
              ? remoteAgents.map((p) => p.identity).join(", ")
              : agentState
                ? connectingMessage
                : connectingMessage}
          </div>
        </div>
      </div>

      <div className="call-main">
        <div className="call-center-stage">
          <div className={`agent-avatar ${isAgentSpeaking ? "agent-avatar--speaking" : ""}`}>
            <div className="agent-avatar__halo" />
            <PersonCutout className="person-cutout--agent" />
            <div className="agent-avatar__name">{agent?.name || "Support Agent"}</div>
            <div className="agent-avatar__status">
              {isConnected
                ? isAgentSpeaking
                  ? speakingMessage
                  : listeningMessage
                : connectingMessage}
            </div>

            <BarVisualizer
              className="agent-voice-bars"
              track={agentAudioTrack}
              state={agentState}
              barCount={20}
              options={{ minHeight: 10, maxHeight: 100 }}
            />
          </div>
        </div>

        <aside className="conversation-panel">
          <div className="conversation-panel__header">Live Conversation</div>
          <div ref={conversationRef} className="conversation-panel__list">
            {conversation.length === 0 ? (
              <p className="conversation-panel__empty">
                Waiting for speech... User and agent transcripts will appear here in real time.
              </p>
            ) : (
              conversation.map((item) =>
                item.kind === "order_ids" ? (
                  <div key={item.key} className="conversation-row conversation-row--agent conversation-row--order-ids">
                    <div className="conversation-row__meta">
                      <span>Active orders</span>
                      <span>{timeLabel(item.updatedAt)}</span>
                    </div>
                    <div className="order-ids-panel">
                      <p className="order-ids-panel__hint">{item.hint}</p>
                      <p className="order-ids-panel__label">Order ID (app id when available)</p>
                      <ul className="order-ids-panel__list">
                        {item.orders.map((row, idx) => {
                          const palette = ORDER_ROW_STYLES[idx % ORDER_ROW_STYLES.length];
                          const idDisplay = formatOrderIdForDisplay(row.external_order_id);
                          return (
                            <li
                              key={`${item.key}-row-${idx}`}
                              className="order-ids-panel__item"
                              style={{
                                background: palette.bg,
                                borderColor: palette.border,
                              }}
                            >
                              <span className="order-ids-panel__index" style={{ color: palette.accent }}>
                                {idx + 1}.
                              </span>
                              <span className="order-ids-panel__id-wrap">
                                <span className="order-ids-panel__id" style={{ color: palette.accent }}>
                                  {idDisplay}
                                </span>
                                {row.status != null && row.status !== "" && (
                                  <span className="order-ids-panel__status"> · {String(row.status)}</span>
                                )}
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  </div>
                ) : (
                  <div
                    key={item.key}
                    className={`conversation-row conversation-row--${item.role} ${
                      item.final ? "" : "conversation-row--interim"
                    }`}
                  >
                    <div className="conversation-row__meta">
                      <span>{item.speakerLabel}</span>
                      <span>{timeLabel(item.updatedAt)}</span>
                    </div>
                    <div className="conversation-row__bubble transcript-bubble-text">
                      {formatTranscriptDisplay(item.text)}
                    </div>
                  </div>
                ),
              )
            )}
          </div>
        </aside>
      </div>

      <div className="bottom-row">
        <div
          className={`user-voice-card ${isMicrophoneEnabled ? "" : "user-voice-card--muted"} ${
            lastMicrophoneError ? "user-voice-card--error" : ""
          }`}
        >
          <div className="user-voice-card__content">
            <PersonCutout />
            <div className="user-voice-card__audio">
              <div className="user-voice-card__title">Your Voice</div>
              <div className="user-voice-card__status">{micStatus}</div>
              <BarVisualizer
                className="user-voice-bars"
                trackRef={localMicTrackRef}
                barCount={8}
                options={{ minHeight: 24, maxHeight: 100 }}
              />
              {lastMicrophoneError && (
                <div className="user-voice-card__error">
                  Allow microphone access in the browser, then rejoin the call.
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="bottom-dock">
          <div className="dock-bar" style={{ display: "flex", gap: "1rem" }}>
            <TrackToggle source={Track.Source.Microphone} className="dock-btn dock-btn--dark" style={{ flex: 1 }}>
              {isMicrophoneEnabled ? "Mute" : "Unmute"}
            </TrackToggle>

            <button className="dock-btn dock-btn--red" onClick={onDisconnected} style={{ flex: 1 }}>
              <CallEndIcon /> End Chat
            </button>
          </div>
        </div>
      </div>

      <RoomAudioRenderer />
    </div>
  );
}

export default function Home() {
  const [token, setToken] = useState<string | null>(null);
  const [serverUrl, setServerUrl] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  const [selectedLanguage, setSelectedLanguage] = useState(SUPPORTED_LANGUAGES[0]);
  const [showLangModal, setShowLangModal] = useState(false);
  const connectingMessage = getConnectingFiller(selectedLanguage.code);
  const agentStatusLabels = getAgentStatusLabels(selectedLanguage.code);

  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

  const startCall = useCallback(async () => {
    setConnecting(true);
    setErrorMsg("");

    try {
      const res = await fetch(`${backendUrl}/api/chat/start-call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language_code: selectedLanguage.code }),
      });

      const raw = await res.text();
      let data: { detail?: string; livekit_url?: string; livekit_token?: string };
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(
          "Backend did not return JSON (tunnel down or wrong URL). Keep the API ngrok running and check NEXT_PUBLIC_BACKEND_URL.",
        );
      }
      if (!res.ok) {
        throw new Error(data.detail || "Failed to initiate call");
      }

      const livekitUrl = data.livekit_url?.trim();
      const livekitToken = data.livekit_token?.trim();
      if (!livekitUrl || !livekitToken) {
        throw new Error("Backend response missing LiveKit URL or token");
      }
      setServerUrl(livekitUrl);
      setToken(livekitToken);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to connect to backend proxy";
      setErrorMsg(message);
      setConnecting(false);
    }
  }, [backendUrl, selectedLanguage]);

  const onDisconnected = useCallback(() => {
    setToken(null);
    setServerUrl(null);
    setConnecting(false);
  }, []);

  if (token && serverUrl) {
    return (
      <div className="page-root">
        <LiveKitRoom
          video={false}
          audio={VOICE_FOCUS_AUDIO_CAPTURE}
          options={{ audioCaptureDefaults: VOICE_FOCUS_AUDIO_CAPTURE }}
          token={token}
          serverUrl={serverUrl}
          onConnected={() => {
            setConnecting(false);
            setErrorMsg("");
          }}
          onDisconnected={onDisconnected}
          onError={(error) => {
            setErrorMsg(error.message || "Failed to connect to the voice session");
            setConnecting(false);
          }}
          onMediaDeviceFailure={() => {
            setErrorMsg("Microphone access failed. Allow mic permission and retry the call.");
            setConnecting(false);
          }}
          data-lk-theme="default"
        >
          <ActiveCallRoom
            onDisconnected={onDisconnected}
            connectingMessage={connectingMessage}
            speakingMessage={agentStatusLabels.speaking}
            listeningMessage={agentStatusLabels.listening}
            languageCode={selectedLanguage.code}
          />
        </LiveKitRoom>
      </div>
    );
  }

  return (
    <div className="page-root">
      <div className="call-shell" style={{ border: errorMsg ? "2px solid #ef4444" : "" }}>
        <div
          className="call-video-layer"
          style={{
            backgroundColor: "#000",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: "2rem",
          }}
        >
          <div className="placeholder-avatar">
            <div className="avatar-glow" />
          </div>

          <div style={{ textAlign: "center", maxWidth: "400px" }}>
            <h1 style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>Order Support</h1>
            <p style={{ opacity: 0.7, marginBottom: "2rem" }}>
              Connect with your real-time order status assistant.
            </p>

            <button
              onClick={startCall}
              disabled={connecting}
              style={{
                padding: "1rem 2rem",
                fontSize: "1.2rem",
                borderRadius: "16px",
                background: "#22c55e",
                color: "#fff",
                border: "none",
                cursor: connecting ? "not-allowed" : "pointer",
                opacity: connecting ? 0.7 : 1,
                width: "100%",
                marginBottom: "1rem",
              }}
            >
              {connecting ? connectingMessage : "Start Support Call"}
            </button>
            <br />
            <button
              className="dock-btn dock-btn--dark"
              onClick={() => setShowLangModal(true)}
              style={{ width: "100%", justifyContent: "center" }}
            >
              <span>Language</span>: {selectedLanguage.name} ▾
            </button>
          </div>
        </div>
      </div>

      {errorMsg && (
        <div
          className="error-box"
          style={{
            position: "fixed",
            top: "1rem",
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 100,
            background: "#ef4444",
            color: "white",
            padding: "1rem",
            borderRadius: "8px",
          }}
        >
          {errorMsg}
        </div>
      )}

      {showLangModal && (
        <div
          className="translate-modal-backdrop"
          onClick={() => setShowLangModal(false)}
          style={{
            pointerEvents: "auto",
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyItems: "center",
            zIndex: 1000,
          }}
        >
          <div
            className="translate-modal"
            onClick={(e) => e.stopPropagation()}
            style={{ background: "#222", padding: "2rem", borderRadius: "1rem", maxWidth: "400px", margin: "auto" }}
          >
            <h3 className="translate-modal-title" style={{ marginBottom: "1rem" }}>
              Select Language
            </h3>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0.5rem",
                maxHeight: "300px",
                overflowY: "auto",
                padding: "0.5rem",
              }}
            >
              {SUPPORTED_LANGUAGES.map((lang) => (
                <button
                  key={lang.code}
                  className="dock-btn dock-btn--dark"
                  style={{
                    justifyContent: "center",
                    background: selectedLanguage.code === lang.code ? "rgba(34,197,94,0.2)" : "rgba(255,255,255,0.05)",
                    borderColor: selectedLanguage.code === lang.code ? "#22c55e" : "transparent",
                    border: "1px solid",
                    padding: "0.5rem",
                  }}
                  onClick={() => {
                    setSelectedLanguage(lang);
                    setShowLangModal(false);
                  }}
                >
                  {lang.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
