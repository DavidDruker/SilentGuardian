import { useEffect, useRef, useState } from "react";
import {
  Camera,
  CircleCheck,
  CircleX,
  FileVideo,
  Pause,
  TriangleAlert,
} from "lucide-react";
import {
  api,
  connectPose,
  type AppConfig,
  type IngestResult,
  type GuardianEvent,
  type Landmark,
  type PoseFrame,
  type Profile,
  uid,
} from "./api";
import { Skeleton } from "./Skeleton";
import { TriageProgress } from "./TriageProgress";
function personBounds(landmarks: Landmark[]) {
  const visible = landmarks.filter((point) =>
    point.visibility >= 0.35 && Number.isFinite(point.x) && Number.isFinite(point.y) &&
    point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1,
  );
  if (visible.length < 5) return null;
  const left = Math.max(0, Math.min(...visible.map((point) => point.x)) - 0.035);
  const right = Math.min(1, Math.max(...visible.map((point) => point.x)) + 0.035);
  const top = Math.max(0, Math.min(...visible.map((point) => point.y)) - 0.035);
  const bottom = Math.min(1, Math.max(...visible.map((point) => point.y)) + 0.035);
  if (right - left < 0.08 || bottom - top < 0.15) return null;
  return { left: `${left * 100}%`, top: `${top * 100}%`, width: `${(right - left) * 100}%`, height: `${(bottom - top) * 100}%` };
}

type SignalKind = "good" | "warning" | "alert";
function SignalIcon({ kind, size = 22 }: { kind: SignalKind; size?: number }) {
  if (kind === "good") return <CircleCheck size={size} aria-hidden="true" />;
  if (kind === "alert") return <CircleX size={size} aria-hidden="true" />;
  return <TriangleAlert size={size} aria-hidden="true" />;
}

type CaptureResult = {
  status: "reviewing" | "uncertain" | "ready";
  scored_windows: number;
  event: GuardianEvent | null;
  events: GuardianEvent[];
  candidate_decision: string | null;
  decision_reason: string | null;
};

export function Capture({
  profile,
  config,
  onEvent,
  onError,
}: {
  profile: Profile;
  config: AppConfig;
  onEvent: () => void;
  onError: (s: string) => void;
}) {
  const [poses, setPoses] = useState<Landmark[][]>([]),
    [running, setRunning] = useState(false),
    [preview, setPreview] = useState(false);
  const [status, setStatus] = useState("Camera is paused"),
    [result, setResult] = useState<IngestResult | null>(null);
  const [source, setSource] = useState("");
  const [fileName, setFileName] = useState("");
  const [videoAspect, setVideoAspect] = useState(0);
  const [verdict, setVerdict] = useState<CaptureResult | null>(null);
  const [verdictBusy, setVerdictBusy] = useState(false);
  const [activeAlert, setActiveAlert] = useState(false);
  const [activeEventIds, setActiveEventIds] = useState<string[]>([]);
  const [activeEventId, setActiveEventId] = useState<string | null>(null);
  const [activeEvent, setActiveEvent] = useState<GuardianEvent | null>(null);
  const video = useRef<HTMLVideoElement>(null),
    worker = useRef<Worker | null>(null),
    media = useRef<MediaStream | null>(null);
  const [pendingCapture, setPendingCapture] = useState<File | null | undefined>(
    undefined,
  );
  const [sendGemini, setSendGemini] = useState(false),
    [consentBusy, setConsentBusy] = useState(false);
  const active = useRef(false),
    busy = useRef(false),
    raf = useRef(0),
    sock = useRef<ReturnType<typeof connectPose> | null>(null);
  const lost = useRef(false);
  const seenEventIds = useRef(new Set<string>());
  const session = useRef(uid());
  const recordedFrames = useRef<PoseFrame[]>([]);
  const selectedRef = useRef(false),
    finishing = useRef(false),
    runId = useRef(0);
  const replayTime = useRef(0),
    lastVideoTime = useRef(-1),
    sourceRef = useRef(""),
    localUrl = useRef("");

  const stop = () => {
    runId.current += 1;
    active.current = false;
    cancelAnimationFrame(raf.current);
    sock.current?.close();
    sock.current = null;
    worker.current?.terminate();
    worker.current = null;
    media.current?.getTracks().forEach((t) => t.stop());
    media.current = null;
    if (video.current) {
      video.current.pause();
      video.current.srcObject = null;
      video.current.removeAttribute("src");
    }
    if (localUrl.current) URL.revokeObjectURL(localUrl.current);
    localUrl.current = "";
    lost.current = false;
    busy.current = false;
    setRunning(false);
    setPreview(false);
    setPoses([]);
    setResult(null);
    setSource("");
    setFileName("");
    setVerdict(null);
    setVerdictBusy(false);
    setActiveAlert(false);
    setActiveEventIds([]);
    seenEventIds.current.clear();
    setActiveEventId(null);
    setActiveEvent(null);
    setVideoAspect(0);
    selectedRef.current = false;
    finishing.current = false;
    setStatus("Camera is paused");
    recordedFrames.current = [];
  };
  useEffect(() => () => stop(), []);
  useEffect(() => {
    stop();
    setPoses([]);
    setResult(null);
    setSource("");
  }, [profile.id]);
  useEffect(() => {
    if (!profile.consent) stop();
  }, [profile.consent]);
  useEffect(() => {
    if (!activeEventId) return;
    setActiveEvent(null);
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const event = await api<GuardianEvent>(`/events/${activeEventId}`);
        if (cancelled) return;
        setActiveEvent(event);
        if (event.verdict_status === "pending") timer = setTimeout(() => void refresh(), 1500);
      } catch {
        if (!cancelled) timer = setTimeout(() => void refresh(), 3000);
      }
    };
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [activeEventId]);

  const socket = () => {
    sock.current?.close();
    sock.current = connectPose((r) => {
      if (r.quality !== "buffering") setResult(r);
      const ids = r.event_ids?.length ? r.event_ids : r.event_id ? [r.event_id] : [];
      if (ids.length) {
        setActiveAlert(true);
        setActiveEventIds(ids);
        setActiveEventId(r.event_id ?? ids[0]);
        if (ids.some((id) => !seenEventIds.current.has(id))) onEvent();
        ids.forEach((id) => seenEventIds.current.add(id));
      }
    }, setStatus);
  };
  const choose = () => {
    selectedRef.current = true;
    lost.current = false;
    session.current = uid();
    recordedFrames.current = [];
    socket();
    setRunning(true);
    setStatus("Tracking one person");
  };
  const finishUpload = async () => {
    if (sourceRef.current !== "upload" || finishing.current) return;
    finishing.current = true;
    active.current = false;
    cancelAnimationFrame(raf.current);
    const thisRun = runId.current;
    setRunning(false);
    setVerdictBusy(true);
    setStatus("Video complete · preparing analysis");
    // Let the last MediaPipe frame and its WebSocket response finish before querying.
    for (let i = 0; busy.current && i < 20; i++)
      await new Promise((resolve) => setTimeout(resolve, 100));
    await new Promise((resolve) => setTimeout(resolve, 350));
    sock.current?.close();
    worker.current?.terminate();
    worker.current = null;
    let last: CaptureResult | null = null;
    try {
      for (let i = 0; i < 20; i++) {
        if (runId.current !== thisRun) return;
        last = await api<CaptureResult>(`/capture-result/${profile.id}/${session.current}`);
        setVerdict(last);
        if (last.status !== "reviewing" && last.event?.verdict_status !== "pending") break;
        await new Promise((resolve) => setTimeout(resolve, 700));
      }
      if (runId.current === thisRun) {
        setVerdictBusy(false);
        setStatus(last?.status === "reviewing" || last?.event?.verdict_status === "pending" ? "Review still in progress" : last?.status === "uncertain" ? "Movement needs review" : "Analysis complete");
        onEvent();
      }
    } catch (cause) {
      if (runId.current === thisRun) {
        setVerdictBusy(false);
        onError(`Could not finish analyzing the video: ${String(cause)}`);
      }
    }
  };
  const exportSkeleton = () => {
    if (!recordedFrames.current.length) return;
    const blob = new Blob([JSON.stringify({ kind: "skeleton-recording", frames: recordedFrames.current })],
      { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `hayvn-movement-${session.current}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const startVideo = async (file?: File) => {
    stop();
    session.current = uid();
    setResult(null);
    try {
      setStatus("Preparing video analysis…");
      setSource(file ? "Uploaded video" : "Live camera");
      setFileName(file?.name || "");
      sourceRef.current = file ? "upload" : "live";
      const element = video.current!;
      if (file) {
        localUrl.current = URL.createObjectURL(file);
        element.src = localUrl.current;
        await new Promise<void>((resolve, reject) => {
          if (element.readyState >= 1) { resolve(); return; }
          element.onloadedmetadata = () => resolve();
          element.onerror = () => reject(new Error("This video cannot be played in the browser"));
          element.load();
        });
        setVideoAspect(element.videoWidth / element.videoHeight);
      } else {
        media.current = await navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, frameRate: { ideal: 24, max: 30 } },
          audio: false,
        });
        element.srcObject = media.current;
        await element.play();
      }
      setPreview(true);
      active.current = true;
      lastVideoTime.current = -1;
      replayTime.current = Date.now() / 1000;
      worker.current = new Worker("/pose-worker.js");
      worker.current.onmessage = (event) => {
        if (event.data.type === "error") {
          onError("Video analysis failed. " + event.data.error);
          stop();
          return;
        }
        if (event.data.type === "ready") {
          setStatus(file ? "Analyzing video · waiting for person" : "Select the person to monitor");
          if (file) void element.play();
          loop();
          return;
        }
        if (event.data.type !== "poses") return;
        busy.current = false;
        const detected: Landmark[][] = event.data.poses.map((p: Landmark[]) =>
          p.map((l) => ({
            x: l.x,
            y: l.y,
            z: l.z ?? 0,
            visibility: l.visibility ?? 0,
          })),
        );
        setPoses(detected);
        if (detected.length === 1 && !selectedRef.current) choose();
        lost.current = detected.length !== 1;
        if (detected.length > 1) setStatus("More than one person in view");
        else if (detected.length === 0) setStatus("Person not visible");
        else setStatus("Tracking one person");
        if (!sock.current) return;
        const frame: PoseFrame = {
          session_id: session.current,
          camera_id: "browser-camera",
          person_id: profile.id,
          timestamp:
            sourceRef.current === "live"
              ? Date.now() / 1000
              : replayTime.current + element.currentTime,
          poses: detected.length === 1 ? [{ track_id: "subject", landmarks: detected[0] }] : [],
          monitored_track_id: "subject",
          tracking_ambiguous: detected.length !== 1,
          source: sourceRef.current as "live" | "upload",
          demo_time_scale: 1,
        };
        if (sock.current.send(frame) && sourceRef.current === "upload") recordedFrames.current.push(frame);
      };
      worker.current.postMessage({ type: "init" });
      const loop = async () => {
        if (!active.current) return;
        if (element.ended) {
          active.current = false;
          void finishUpload();
          return;
        }
        if (
          !busy.current &&
          element.readyState >= 2 &&
          element.currentTime !== lastVideoTime.current
        ) {
          busy.current = true;
          lastVideoTime.current = element.currentTime;
          try {
            const bitmap = await createImageBitmap(element);
            worker.current?.postMessage(
              { type: "frame", bitmap, timestamp: performance.now() },
              [bitmap],
            );
          } catch {
            busy.current = false;
          }
        }
        raf.current = requestAnimationFrame(loop);
      };
    } catch (e) {
      stop();
      onError(String(e));
    }
  };
  const consentCapture = async (accepted: boolean) => {
    setConsentBusy(true);
    try {
      await api(`/privacy/${profile.id}/consent`, "POST", {
        accepted,
        gemini: accepted && sendGemini,
        local_preview: accepted,
      });
      const file = pendingCapture;
      setPendingCapture(undefined);
      onEvent();
      if (accepted) await startVideo(file || undefined);
      else stop();
    } catch (e) {
      onError(String(e));
    } finally {
      setConsentBusy(false);
    }
  };
  let signal: { kind: SignalKind; title: string; detail: string } = {
    kind: "warning", title: "Checking movement", detail: "Keep the person clearly in view.",
  };
  const alertCount = verdict?.events?.length ?? activeEventIds.length;
  if (preview && poses.length > 1 && !verdict) {
    signal = { kind: "warning", title: "One person only", detail: "Clear the room before monitoring continues." };
  } else if (activeAlert || verdict?.events?.length) {
    signal = { kind: "alert", title: alertCount > 1 ? `${alertCount} concerns detected` : "Concern detected", detail: "Review the alerts for details." };
  } else if (verdictBusy && !verdict) {
    signal = { kind: "warning", title: "Reviewing video", detail: "Checking the recorded movement." };
  } else if (verdict?.status === "uncertain" || verdict?.status === "reviewing" || (verdict && !verdict.scored_windows)) {
    signal = { kind: "warning", title: "Result uncertain", detail: "Try a clearer video with the person fully visible." };
  } else if (verdict?.status === "ready") {
    signal = { kind: "good", title: "No concern detected", detail: "No alert was triggered in this video." };
  } else if (status.includes("Reconnecting") || status.includes("interrupted") || status.includes("expired")) {
    signal = { kind: "warning", title: "Connection interrupted", detail: "Monitoring may be paused." };
  } else if (lost.current) {
    signal = { kind: "warning", title: "Person not visible", detail: "Keep one person clearly in view." };
  } else if (!selectedRef.current) {
    signal = { kind: "warning", title: "Waiting for one person", detail: "Monitoring starts when one person is visible." };
  } else if (result?.quality === "uncertain" || result?.rule_hits?.length) {
    signal = { kind: "warning", title: "Movement uncertain", detail: "Continue monitoring for a clearer reading." };
  } else if (result?.quality === "good" && result.accepted) {
    signal = { kind: "good", title: "No concern detected", detail: "One person is in view and monitoring is active." };
  }
  const triage = <TriageProgress
    event={verdict?.event ?? activeEvent}
    candidateDecision={verdict?.candidate_decision}
    prediction={result?.action_prediction}
    ruleHits={result?.rule_hits}
    hasIncident={activeAlert}
    quality={result?.quality}
    scoredWindows={verdict?.scored_windows}
  />;
  const bounds = preview && poses.length === 1 ? personBounds(poses[0]) : null;
  return (
    <section className="panel capture-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">{profile.alias}</span>
          <h2>Capture</h2>
        </div>
        <span className={`pill ${running ? "green" : ""}`}>
          {running ? "Monitoring" : verdictBusy ? "Reviewing" : verdict ? "Complete" : "Paused"}
        </span>
      </div>
      <div className="camera-stage" style={videoAspect ? {
        aspectRatio: String(videoAspect),
        width: videoAspect < 1.5 ? `min(100%, ${Math.round(560 * videoAspect)}px)` : undefined,
      } : undefined}>
        <video
          ref={video}
          muted
          playsInline
          className={preview ? "camera-video visible" : "camera-video"}
          onEnded={() => void finishUpload()}
        />
        {!preview && <div className="room-grid" />}
        {preview && poses.length === 1 && <Skeleton pose={poses[0]} />}
        {bounds && <div className="person-tracking-box identified"
          style={bounds} aria-hidden="true"><span>Monitored person</span></div>}
        {!preview && poses.length === 0 && (
          <div className="camera-empty">
            <div className="camera-icon">
              <Camera size={32} />
            </div>
            <strong>Camera off</strong>
            <span>Start the camera or select a video.</span>
          </div>
        )}
        <div className="camera-top">
          <span className="camera-label">
            <span className="dot" />
            {source || "Capture device"}
          </span>
        </div>
      </div>
      {preview && <div className={`camera-analysis-status status-${signal.kind}`} role="status" aria-live="polite">
        <SignalIcon kind={signal.kind} />
        <div><strong>{signal.title}</strong><span>{signal.detail}</span></div>
      </div>}
      <div className="capture-actions">
        <button
          className="primary"
          onClick={() =>
            running || preview ? stop() : setPendingCapture(null)
          }
        >
          {running || preview ? <Pause size={16} /> : <Camera size={16} />}
          {running || preview ? "Stop capture" : "Start camera"}
        </button>
        <label className="button secondary">
          <FileVideo size={16} /> Upload demo video
          <input
            type="file"
            accept="video/*"
            hidden
            onChange={(e) => {
              if (e.target.files?.[0]) setPendingCapture(e.target.files[0]);
              e.target.value = "";
            }}
          />
        </label>
        {verdict && recordedFrames.current.length > 0 && <button className="secondary small" onClick={exportSkeleton}>Download movement data</button>}
      </div>
      {fileName && <p className="capture-file">{fileName} · Video stays in this browser while movement is analyzed.</p>}
      {(verdictBusy || verdict) && <section className={`capture-verdict status-${signal.kind}`} aria-live="polite">
        <div className="capture-verdict-heading"><SignalIcon kind={signal.kind} size={24} />
          <div><span className="eyebrow">VIDEO ANALYSIS</span><h3>{alertCount > 1 ? `${alertCount} alerts detected` : verdict?.event?.summary || signal.title}</h3></div>
        </div>
        {triage}
      </section>}
      {preview && !verdictBusy && !verdict && triage}
      {pendingCapture !== undefined && (
        <div className="modal-shade">
          <section
            className="modal consent-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="consent-title"
          >
            <h2 id="consent-title">Start monitoring?</h2>
            <p>Movement measurements from {pendingCapture ? "this video" : "the camera"} will be stored with {profile.alias}'s profile.</p>
            {config.gemini_configured && config.gemini_billed_project_confirmed && <label className="check-row">
              <input type="checkbox" checked={sendGemini} onChange={(e) => setSendGemini(e.target.checked)} />
              Allow Gemini to review flagged movement using this profile's care context
            </label>}
            <div className="action-row">
              <button
                className="secondary"
                disabled={consentBusy}
                onClick={() => setPendingCapture(undefined)}
              >
                Cancel
              </button>
              <button
                className="primary"
                disabled={consentBusy}
                onClick={() => consentCapture(true)}
              >
                Start {pendingCapture ? "video" : "camera"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
