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

function ActiveCallRoom({ onDisconnected }: { onDisconnected: () => void }) {
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
        ? "Listening for your voice"
        : "Microphone is muted";

  // Auto mute/unmute microphone when agent speaks to prevent echo and feedback
  useEffect(() => {
    if (!microphoneTrack || !isConnected) {
      return;
    }

    // Mute when agent is speaking or thinking (processing)
    const shouldMute = agentState === "speaking" || agentState === "thinking";
    
    if (shouldMute && isMicrophoneEnabled) {
      localParticipant.setMicrophoneEnabled(false);
    }
    // Unmute only when agent is listening (ready for user input)
    else if (agentState === "listening" && !isMicrophoneEnabled && !lastMicrophoneError) {
      localParticipant.setMicrophoneEnabled(true);
    }
  }, [agentState, isConnected, microphoneTrack, isMicrophoneEnabled, localParticipant, lastMicrophoneError]);

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
        return next.slice(-80);
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
          const next = [...prev, line];
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
                ? `Connecting (${agentState})`
                : "Waiting for agent..."}
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
                  ? "Speaking..."
                  : "Listening..."
                : `Room: ${roomState}...`}
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
                      <ol className="order-ids-panel__list">
                        {item.orders.map((row, idx) => (
                          <li key={`${item.key}-row-${idx}`} className="order-ids-panel__item">
                            <span className="order-ids-panel__id">
                              {row.external_order_id?.trim() || "—"}
                            </span>
                            {row.status != null && row.status !== "" && (
                              <span className="order-ids-panel__status"> · {String(row.status)}</span>
                            )}
                          </li>
                        ))}
                      </ol>
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
                    <div className="conversation-row__bubble">{item.text}</div>
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
          <ActiveCallRoom onDisconnected={onDisconnected} />
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
              {connecting ? "Handshaking with LiveKit..." : "Start Support Call"}
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
