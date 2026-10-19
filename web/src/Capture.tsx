import { useEffect, useRef, useState } from "react";
import {
  Camera,
  CircleCheck,
  CircleX,
  FileVideo,
  Pause,
  TriangleAlert,
  X,
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
import { PoseStabilizer } from "./poseFilter";
import { urgencyNames } from "./EventDetail";

// Uploaded clips are analysed at a fixed rate of video time, whatever the machine's
// speed, so every computer scores the same frames and none are skipped.
const UPLOAD_STEP_SECONDS = 1 / 12;

const CONCERN_NAMES: Record<string, string> = {
  fall: "Possible fall",
  restricted_standing: "Standing during restricted hours",
  stumble: "Stumble",
  sway: "Sway",
  bed_exit: "Bed exit",
  prolonged_stillness: "Still on the floor",
  personal_baseline: "Unusual movement",
};
const REVIEW_LABELS: Record<string, string> = {
  pending: "Reviewing",
  validated: "Reviewed",
  fallback: "Review failed",
  model_supported_no_gemini: "ML confirmed",
  model_fallback: "Safety rule",
  rules_only: "Rule",
  not_reviewed: "Not sent",
};
const clipTime = (seconds: number) => {
  const s = Math.max(0, seconds);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
};

// Show a new status only after it has held for a moment, so per-frame wobble
// between two readings does not make the status card flicker.
function useSteady(key: string, holdMs: number, immediate: boolean) {
  const [shown, setShown] = useState(key);
  // A new alert shows at once; switching between two alert messages is held like
  // any other change, so tracking blips do not make it flicker.
  const now = immediate || (key.startsWith("alert|") && !shown.startsWith("alert|"));
  useEffect(() => {
    if (key === shown) return;
    if (now) { setShown(key); return; }
    const timer = setTimeout(() => setShown(key), holdMs);
    return () => clearTimeout(timer);
  }, [key, shown, holdMs, now]);
  return now ? key : shown;
}
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
  identity_state?: string;
  scored_windows: number;
  event: GuardianEvent | null;
  events: GuardianEvent[];
  candidate_decision: string | null;
  decision_reason: string | null;
};
type BaselineStatus = { recording: boolean; calibration_windows: number; evidence: { identity_anchor?: Record<string, number> } };

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
  const [pose, setPose] = useState<Landmark[] | null>(null),
    [people, setPeople] = useState(0),
    [running, setRunning] = useState(false),
    [preview, setPreview] = useState(false);
  const [status, setStatus] = useState("Camera is paused"),
    [result, setResult] = useState<IngestResult | null>(null);
  const [source, setSource] = useState("");
  const [fileName, setFileName] = useState("");
  const [videoAspect, setVideoAspect] = useState(0);
  const [verdict, setVerdict] = useState<CaptureResult | null>(null);
  const [verdictBusy, setVerdictBusy] = useState(false);
  // What is wrong at this moment (from the latest clear reading), separate from
  // the log of alerts raised earlier in the session.
  const [concern, setConcern] = useState<string | null>(null);
  const [alertLog, setAlertLog] = useState<GuardianEvent[]>([]);
  const [lastDecision, setLastDecision] = useState<string | null>(null);
  const [mlState, setMlState] = useState(config.ml_state ?? "");
  const [baselineStatus, setBaselineStatus] = useState<BaselineStatus | null>(null);
  const [calibrationBusy, setCalibrationBusy] = useState(false);
  // In-app notification shown when Gemini's verdict arrives.
  const [toast, setToast] = useState<GuardianEvent | null>(null);
  const toasted = useRef(new Set<string>());
  const panel = useRef<HTMLElement>(null);
  const video = useRef<HTMLVideoElement>(null),
    worker = useRef<Worker | null>(null),
    media = useRef<MediaStream | null>(null);
  const [pendingCapture, setPendingCapture] = useState<File | null | undefined>(
    undefined,
  );
  const [consentBusy, setConsentBusy] = useState(false);
  const active = useRef(false),
    busy = useRef(false),
    raf = useRef(0),
    playStart = useRef(0),
    sock = useRef<ReturnType<typeof connectPose> | null>(null);
  const lost = useRef(false);
  const seenEventIds = useRef(new Set<string>());
  const session = useRef(uid());
  const recordedFrames = useRef<PoseFrame[]>([]);
  const stabilizer = useRef(new PoseStabilizer()),
    captureTime = useRef(0),
    lastScored = useRef<IngestResult["action_prediction"]>(null),
    signals = useRef(new Map<string, { kind: SignalKind; title: string; detail: string }>());
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
    window.clearTimeout(raf.current);
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
    setPose(null);
    setPeople(0);
    stabilizer.current.reset();
    lastScored.current = null;
    setResult(null);
    setSource("");
    setFileName("");
    setVerdict(null);
    setVerdictBusy(false);
    setConcern(null);
    setAlertLog([]);
    setToast(null);
    toasted.current.clear();
    setLastDecision(null);
    seenEventIds.current.clear();
    setVideoAspect(0);
    selectedRef.current = false;
    finishing.current = false;
    setStatus("Camera is paused");
    recordedFrames.current = [];
  };
  useEffect(() => () => stop(), []);
  useEffect(() => {
    let current = true;
    stop();
    setResult(null);
    setSource("");
    setBaselineStatus(null);
    void api<BaselineStatus>(`/baseline/${profile.id}`).then((value) => {
      if (current) setBaselineStatus(value);
    }).catch(() => undefined);
    return () => {
      current = false;
      void api(`/calibration/${profile.id}/stop`, "POST").catch(() => undefined);
    };
  }, [profile.id]);
  useEffect(() => {
    if (!profile.consent) stop();
  }, [profile.consent]);
  const loadAlert = async (id: string) => {
    const thisRun = runId.current;
    try {
      const event = await api<GuardianEvent>(`/events/${id}`);
      if (runId.current !== thisRun) return;
      setAlertLog((log) => [...log.filter((item) => item.id !== id), event]
        .sort((a, b) => a.observed_at - b.observed_at));
    } catch {
      // The pending refresh below retries entries still under review.
    }
  };
  useEffect(() => {
    const fresh = alertLog.find((event) => event.verdict_status === "validated" && event.verdict
      && !toasted.current.has(event.id));
    if (!fresh) return;
    toasted.current.add(fresh.id);
    setToast(fresh);
    const timer = setTimeout(() => setToast(null), 12000);
    return () => clearTimeout(timer);
  }, [alertLog]);
  useEffect(() => {
    const pending = alertLog.filter((event) => event.verdict_status === "pending");
    if (!pending.length) return;
    const timer = setTimeout(() => pending.forEach((event) => void loadAlert(event.id)), 1500);
    return () => clearTimeout(timer);
  }, [alertLog]);

  const socket = () => {
    sock.current?.close();
    sock.current = connectPose((r) => {
      if (r.quality !== "buffering") setResult(r);
      if (r.action_prediction?.status === "scored") lastScored.current = r.action_prediction;
      if (r.candidate_decision) setLastDecision(r.candidate_decision);
      // Only a clear reading can say the person is fine; an unclear frame keeps
      // the last concern (someone down on the floor often tracks poorly).
      if (r.quality === "good") setConcern(r.concern ?? null);
      const ids = r.event_ids?.length ? r.event_ids : r.event_id ? [r.event_id] : [];
      const fresh = ids.filter((id) => !seenEventIds.current.has(id));
      fresh.forEach((id) => {
        seenEventIds.current.add(id);
        void loadAlert(id);
      });
      if (fresh.length) onEvent();
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
    window.clearTimeout(raf.current);
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
      // Gemini reviews an upload once it has stopped (about 3 s), then may take up to 30 s.
      for (let i = 0; i < 50; i++) {
        if (runId.current !== thisRun) return;
        last = await api<CaptureResult>(`/capture-result/${profile.id}/${session.current}`);
        setVerdict(last);
        setAlertLog([...last.events].sort((a, b) => a.observed_at - b.observed_at));
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
    // The model loads after the server starts; check its state for this capture.
    void api<AppConfig>("/config").then((fresh) => setMlState(fresh.ml_state ?? "")).catch(() => undefined);
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
      // Bring the clip and its analysis fully into view together.
      panel.current?.scrollIntoView({ behavior: "smooth", block: "start" });
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
          if (file) {
            playStart.current = performance.now();
            element.currentTime = 0.001; // the seek triggers the first capture
          } else loop();
          return;
        }
        if (event.data.type !== "poses") return;
        busy.current = false;
        if (sourceRef.current === "upload") advanceUpload();
        const detected: Landmark[][] = event.data.poses.map((p: Landmark[]) =>
          p.map((l) => ({
            x: l.x,
            y: l.y,
            z: l.z ?? 0,
            visibility: l.visibility ?? 0,
          })),
        );
        // Smooth jitter and bridge split-second dropouts before anything is shown or sent.
        const timestamp = captureTime.current;
        const stable = stabilizer.current.update(detected, timestamp);
        setPose(stable.pose);
        setPeople(stable.people);
        if (stable.people === 1 && !selectedRef.current) choose();
        lost.current = stable.people !== 1;
        if (stable.people > 1) setStatus("More than one person in view");
        else if (stable.people === 0) setStatus("Person not visible");
        else setStatus("Tracking one person");
        // A held pose is display-only: the backend tolerates the brief gap itself,
        // so it never scores a repeated, stale frame.
        if (!sock.current || stable.held) return;
        const frame: PoseFrame = {
          session_id: session.current,
          camera_id: "browser-camera",
          person_id: profile.id,
          timestamp,
          poses: stable.pose ? [{ track_id: "subject", landmarks: stable.pose }] : [],
          monitored_track_id: "subject",
          tracking_ambiguous: stable.people !== 1,
          source: sourceRef.current as "live" | "upload",
          demo_time_scale: 1,
        };
        if (sock.current.send(frame) && sourceRef.current === "upload") recordedFrames.current.push(frame);
      };
      worker.current.postMessage({ type: "init" });
      // Uploads: step through the clip one analysed frame at a time. Playback never
      // runs ahead of analysis (or faster than real time), so a slow machine or a
      // covered window only makes it take longer instead of skipping frames.
      const captureUpload = async () => {
        if (!active.current || busy.current) return;
        busy.current = true;
        lastVideoTime.current = element.currentTime;
        captureTime.current = replayTime.current + element.currentTime;
        try {
          const bitmap = await createImageBitmap(element);
          worker.current?.postMessage({ type: "frame", bitmap, timestamp: performance.now() }, [bitmap]);
        } catch {
          busy.current = false;
          advanceUpload();
        }
      };
      const advanceUpload = () => {
        if (!active.current) return;
        const next = lastVideoTime.current + UPLOAD_STEP_SECONDS;
        if (next >= element.duration) {
          active.current = false;
          void finishUpload();
          return;
        }
        const wait = playStart.current + next * 1000 - performance.now();
        raf.current = window.setTimeout(() => { element.currentTime = next; }, Math.max(0, wait));
      };
      element.onseeked = () => {
        if (sourceRef.current === "upload") void captureUpload();
      };
      // Live camera: analyse the newest frame whenever the worker is free.
      const loop = async () => {
        if (!active.current) return;
        if (
          !busy.current &&
          element.readyState >= 2 &&
          element.currentTime !== lastVideoTime.current
        ) {
          busy.current = true;
          lastVideoTime.current = element.currentTime;
          // Stamp the frame when it is captured, not when inference returns,
          // so joint speeds are measured on the real frame spacing.
          captureTime.current = Date.now() / 1000;
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
        // A timer, not requestAnimationFrame: the browser stops animation frames
        // whenever the page is not being drawn, which silently halted analysis.
        raf.current = window.setTimeout(loop, 30);
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
        gemini: accepted,
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
  const refreshBaseline = async () => {
    setBaselineStatus(await api<BaselineStatus>(`/baseline/${profile.id}`));
  };
  const setCalibration = async (action: "start" | "stop") => {
    setCalibrationBusy(true);
    try {
      await api(`/calibration/${profile.id}/${action}`, "POST");
      await refreshBaseline();
    } catch (error) { onError(String(error)); }
    finally { setCalibrationBusy(false); }
  };
  const fitCalibration = async () => {
    if (!window.confirm(`Confirm the recorded movement is ${profile.alias}'s normal movement before fitting this profile.`)) return;
    setCalibrationBusy(true);
    try {
      await api(`/baseline/${profile.id}/fit`, "POST", { reviewed_normal: true });
      await refreshBaseline();
      setResult(null);
    } catch (error) { onError(String(error)); }
    finally { setCalibrationBusy(false); }
  };
  let signal: { kind: SignalKind; title: string; detail: string } = {
    kind: "warning", title: "Checking movement", detail: "",
  };
  const logged = alertLog.length;
  const loggedNote = `${logged} alert${logged === 1 ? "" : "s"} logged.`;
  const identityState = verdict?.identity_state ?? result?.identity_state;
  if (identityState === 'mismatch') {
    signal = { kind: "warning", title: "Person may not match", detail: "Movement analysis paused for this profile." };
  } else if (identityState === 'checking' && !verdict) {
    signal = { kind: "warning", title: "Checking person", detail: "Comparing with the reviewed profile." };
  } else if (preview && people > 1 && !verdict) {
    signal = { kind: "warning", title: "One person only", detail: "Paused until one person remains." };
  } else if (concern === "on_floor" && (lost.current || result?.quality === "uncertain")) {
    // Out of clear view since the last reading on the floor: keep the alert, but say so.
    signal = { kind: "alert", title: "Last seen on the floor", detail: "Check on them." };
  } else if (concern === "on_floor") {
    signal = { kind: "alert", title: "Person is on the floor", detail: verdict
      ? "Still down at the end." : "Not up since the fall." };
  } else if (concern && concern !== "on_floor_watch") {
    signal = { kind: "alert", title: CONCERN_NAMES[concern] ?? concern.replaceAll("_", " "), detail: "Happening now." };
  } else if (concern === "on_floor_watch") {
    signal = { kind: "warning", title: "Person on the floor", detail: verdict
      ? "Fall not confirmed." : "Fall not confirmed." };
  } else if (verdictBusy && !verdict) {
    signal = { kind: "warning", title: "Reviewing video", detail: "" };
  } else if (verdict?.status === "uncertain" || (verdict && !verdict.scored_windows)) {
    signal = { kind: "warning", title: "Result uncertain", detail: "Person not clearly visible." };
  } else if (verdict) {
    signal = { kind: "good", title: logged ? "Person appears fine now" : "No concern detected",
      detail: logged ? loggedNote : "" };
  } else if (status.includes("Reconnecting") || status.includes("interrupted") || status.includes("expired")) {
    signal = { kind: "warning", title: "Connection interrupted", detail: "" };
  } else if (lost.current) {
    signal = { kind: "warning", title: "Person not visible", detail: "" };
  } else if (!selectedRef.current) {
    signal = { kind: "warning", title: "Waiting for one person", detail: "" };
  } else if (result?.quality === "uncertain") {
    signal = { kind: "warning", title: "Movement uncertain", detail: "" };
  } else if (result?.quality === "good" && result.accepted) {
    signal = { kind: "good", title: logged ? "Person appears fine now" : "No concern detected",
      detail: logged ? loggedNote : "" };
  }
  const signalKey = `${signal.kind}|${signal.title}`;
  signals.current.set(signalKey, signal);
  const shownKey = useSteady(signalKey, 700, verdictBusy || Boolean(verdict) || !preview);
  const shown = signals.current.get(shownKey) ?? signal;
  const geminiBlocker = !config.gemini_configured ? "No API key on server"
    : !config.gemini_billed_project_confirmed ? "Billing not confirmed on server" : undefined;
  // Triage follows the last alert: the one Gemini reviews for an upload.
  const latestAlert = alertLog.at(-1) ?? verdict?.event ?? null;
  const triage = <TriageProgress
    identityState={identityState}
    event={latestAlert}
    candidateDecision={verdict?.candidate_decision ?? lastDecision}
    prediction={lastScored.current ?? result?.action_prediction}
    geminiBlocker={geminiBlocker}
    mlNote={!mlState || mlState === "ready" ? undefined
      : mlState === "disabled" ? "Turned off on server"
      : mlState === "unavailable" ? "Not installed on server" : "Starting on server"}
    ruleHits={result?.rule_hits}
    hasIncident={logged > 0}
    quality={result?.quality}
    scoredWindows={verdict?.scored_windows}
  />;
  const alertTime = (event: GuardianEvent) => sourceRef.current === "upload"
    ? clipTime(event.observed_at - replayTime.current)
    : new Date(event.observed_at * 1000).toLocaleTimeString();
  const alertLogSection = logged > 0 && <section className="alert-log" aria-label="Alert log">
    <strong>Alert log</strong>
    <ol>
      {alertLog.map((event) => <li key={event.id}>
        <time>{alertTime(event)}</time>
        <span className="alert-log-name">{CONCERN_NAMES[event.rule_hits[0]] ?? event.summary}</span>
        <span className="alert-log-meta">{urgencyNames[event.urgency] ?? event.urgency} · {REVIEW_LABELS[event.verdict_status] ?? event.verdict_status.replaceAll("_", " ")}</span>
        {event.verdict_status === "fallback" && typeof event.features?.gemini_error === "string" &&
          <span className="alert-log-error" title={event.features.gemini_error}>{event.features.gemini_error.slice(0, 70)}</span>}
      </li>)}
    </ol>
  </section>;
  const bounds = preview && pose ? personBounds(pose) : null;
  return (
    <section className="panel capture-panel" ref={panel}>
      <div className="panel-heading">
        <div>
          <span className="eyebrow">{profile.alias}</span>
          <h2>Capture</h2>
        </div>
        <span className={`pill ${running ? "green" : ""}`}>
          {running ? "Monitoring" : verdictBusy ? "Reviewing" : verdict ? "Complete" : "Paused"}
        </span>
      </div>
      <div className="capture-layout">
      <div className="capture-media">
      <div className="camera-stage" style={videoAspect ? {
        aspectRatio: String(videoAspect),
        // Keep the clip compact (360 px tall at most) so the analysis fits beside it.
        width: `${Math.round(360 * videoAspect)}px`,
        maxWidth: "100%",
      } : undefined}>
        <video
          ref={video}
          muted
          playsInline
          className={preview ? "camera-video visible" : "camera-video"}
          onEnded={() => void finishUpload()}
        />
        {!preview && <div className="room-grid" />}
        {preview && pose && <Skeleton pose={pose} />}
        {bounds && <div className="person-tracking-box identified"
          style={bounds} aria-hidden="true" />}
        {!preview && !pose && (
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
      <div className="calibration-controls">
        <strong>Person check</strong>
        <span>{baselineStatus?.evidence.identity_anchor ? "Reviewed body profile ready" : "Needs reviewed normal movement from two capture sessions"}</span>
        <div className="action-row">
          <button className="secondary small" disabled={!running || calibrationBusy || baselineStatus?.recording}
            onClick={() => void setCalibration("start")}>Record normal movement</button>
          <button className="secondary small" disabled={!baselineStatus?.recording || calibrationBusy}
            onClick={() => void setCalibration("stop")}>Stop recording</button>
          <button className="secondary small" disabled={!baselineStatus?.calibration_windows || baselineStatus.recording || calibrationBusy}
            onClick={() => void fitCalibration()}>Fit reviewed profile</button>
        </div>
        {baselineStatus?.recording && <span>Recording normal movement. Capture a second session before fitting.</span>}
      </div>
      {fileName && <p className="capture-file">{fileName} · Video stays in this browser while movement is analyzed.</p>}
      </div>
      <div className="capture-analysis">
        {verdictBusy || verdict
          ? <section className={`capture-verdict status-${shown.kind}`} aria-live="polite">
            <div className="capture-verdict-heading"><SignalIcon kind={shown.kind} size={24} />
              <div><span className="eyebrow">VIDEO ANALYSIS</span><h3>{shown.title}</h3></div>
            </div>
            {triage}
            {alertLogSection}
          </section>
          : preview
            ? <>
              <div className={`camera-analysis-status status-${shown.kind}`} role="status" aria-live="polite">
                <SignalIcon kind={shown.kind} />
                <div><strong>{shown.title}</strong>{shown.detail && <span>{shown.detail}</span>}</div>
              </div>
              {triage}
              {alertLogSection}
            </>
            : <p className="capture-analysis-empty">Start the camera or upload a video. Analysis appears here.</p>}
      </div>
      </div>
      {toast?.verdict && <aside className={`verdict-toast urgency-${toast.urgency}`} role="alert">
        <div>
          <span className="eyebrow">HAYVN ALERT · GEMINI REVIEWED</span>
          <strong className="verdict-toast-level">{urgencyNames[toast.urgency]}</strong>
          <span>{CONCERN_NAMES[toast.rule_hits[0]] ?? "Alert"} · {toast.alias} · {alertTime(toast)}</span>
        </div>
        <button className="icon-button" aria-label="Dismiss notification" onClick={() => setToast(null)}><X size={16} /></button>
      </aside>}
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
