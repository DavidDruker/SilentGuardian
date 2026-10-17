import { useEffect, useRef, useState } from "react";
import {
  Camera,
  FileVideo,
  LockKeyhole,
  Pause,
  Play,
  Radio,
  Download,
} from "lucide-react";
import {
  api,
  connectPose,
  type AppConfig,
  type IngestResult,
  type Landmark,
  type PoseFrame,
  type Profile,
} from "./api";
import { Skeleton } from "./Skeleton";
import { BaselinePanel } from "./Experience";

const center = (p: Landmark[]) => ({
  x: (p[23].x + p[24].x) / 2,
  y: (p[23].y + p[24].y) / 2,
});

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
  const [selected, setSelected] = useState(-1),
    [status, setStatus] = useState("Camera is paused"),
    [result, setResult] = useState<IngestResult | null>(null);
  const [scenario, setScenario] = useState("stumble"),
    [source, setSource] = useState(""),
    [calibrating, setCalibrating] = useState(false);
  const video = useRef<HTMLVideoElement>(null),
    worker = useRef<Worker | null>(null),
    media = useRef<MediaStream | null>(null);
  const [timeScale, setTimeScale] = useState(8),
    [simulatedAt, setSimulatedAt] = useState<number | null>(null);
  const [region, setRegion] = useState({
    x1: 0.15,
    y1: 0.05,
    x2: 0.85,
    y2: 0.98,
  });
  const [pendingCapture, setPendingCapture] = useState<File | null | undefined>(
    undefined,
  );
  const [sendGemini, setSendGemini] = useState(false),
    [showRaw, setShowRaw] = useState(false),
    [consentBusy, setConsentBusy] = useState(false);
  const active = useRef(false),
    busy = useRef(false),
    raf = useRef(0),
    sock = useRef<ReturnType<typeof connectPose> | null>(null);
  const poseRef = useRef<Landmark[][]>([]),
    chosen = useRef<{ x: number; y: number } | null>(null),
    lost = useRef(false);
  const session = useRef(crypto.randomUUID()),
    recording = useRef<PoseFrame[]>([]),
    timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const replayTime = useRef(0),
    lastVideoTime = useRef(-1),
    sourceRef = useRef(""),
    localUrl = useRef("");

  const stop = () => {
    active.current = false;
    cancelAnimationFrame(raf.current);
    clearTimeout(timer.current);
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
    chosen.current = null;
    lost.current = false;
    busy.current = false;
    setRunning(false);
    setPreview(false);
    setSelected(-1);
    setStatus("Camera is paused");
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

  const socket = () => {
    sock.current?.close();
    sock.current = connectPose((r) => {
      if (r.quality !== "buffering") setResult(r);
      if (r.event_id) onEvent();
    }, setStatus);
  };
  const choose = (index: number) => {
    if (!poseRef.current[index]) return;
    chosen.current = center(poseRef.current[index]);
    lost.current = false;
    setSelected(index);
    session.current = crypto.randomUUID();
    recording.current = [];
    socket();
    setRunning(true);
    setStatus("Tracking selected person");
  };
  const startVideo = async (file?: File) => {
    stop();
    setResult(null);
    recording.current = [];
    setSimulatedAt(null);
    try {
      setStatus("Loading local pose model…");
      setSource(file ? "Local video replay" : "Live camera");
      sourceRef.current = file ? "replay" : "live";
      if (file && !config.demo_enabled)
        throw new Error("Recorded replay is disabled in this deployment.");
      const element = video.current!;
      if (file) {
        localUrl.current = URL.createObjectURL(file);
        element.src = localUrl.current;
      } else {
        media.current = await navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, frameRate: { ideal: 24, max: 30 } },
          audio: false,
        });
        element.srcObject = media.current;
      }
      await element.play();
      setPreview(true);
      active.current = true;
      lastVideoTime.current = -1;
      replayTime.current = Date.now() / 1000;
      worker.current = new Worker("/pose-worker.js");
      worker.current.onmessage = (event) => {
        if (event.data.type === "error") {
          onError("Pose model failed. " + event.data.error);
          stop();
          return;
        }
        if (event.data.type === "ready") {
          setStatus("Select the person to monitor");
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
        poseRef.current = detected;
        setPoses(detected);
        if (!chosen.current || !sock.current) return;
        const distances = detected
          .map((p, i) => ({
            i,
            d: Math.hypot(
              center(p).x - chosen.current!.x,
              center(p).y - chosen.current!.y,
            ),
          }))
          .sort((a, b) => a.d - b.d);
        const index = distances[0]?.i ?? -1;
        const ambiguous =
          index < 0 ||
          distances[0].d > 0.18 ||
          (distances.length > 1 && distances[1].d - distances[0].d < 0.06);
        if (ambiguous) lost.current = true;
        if (lost.current)
          setStatus("Identity uncertain — select the person again");
        else {
          chosen.current = center(detected[index]);
          setSelected(index);
        }
        const frame: PoseFrame = {
          session_id: session.current,
          camera_id: "browser-camera",
          person_id: profile.id,
          timestamp:
            sourceRef.current === "live"
              ? Date.now() / 1000
              : replayTime.current + element.currentTime,
          poses: detected.map((p, i) => ({
            track_id: i === index ? "subject" : "helper",
            landmarks: p,
          })),
          monitored_track_id: "subject",
          tracking_ambiguous: lost.current,
          source: sourceRef.current as "live" | "replay",
          region,
          demo_time_scale: 1,
        };
        sock.current.send(frame);
        if (recording.current.length < 18000) recording.current.push(frame);
      };
      worker.current.postMessage({ type: "init" });
      const loop = async () => {
        if (!active.current) return;
        if (element.ended) {
          setStatus("Video replay complete");
          setRunning(false);
          active.current = false;
          sock.current?.close();
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
              { type: "frame", bitmap, timestamp: performance.now(), region },
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
        local_preview: accepted && showRaw,
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
  const replay = async () => {
    stop();
    setResult(null);
    if (!profile.consent)
      return onError("Enable monitoring consent in the profile first.");
    try {
      const frames = await api<PoseFrame[]>(
        `/demo/scenarios/${scenario}?person_id=${profile.id}&time_scale=${timeScale}`,
      );
      session.current = crypto.randomUUID();
      socket();
      active.current = true;
      setRunning(true);
      setSource("Synthetic skeleton replay");
      sourceRef.current = "replay";
      recording.current = frames;
      let i = 0;
      const tick = () => {
        if (!active.current) return;
        if (i >= frames.length) {
          active.current = false;
          setRunning(false);
          sock.current?.close();
          setStatus("Replay complete");
          return;
        }
        const frame = frames[i];
        frame.session_id = session.current;
        if (sock.current?.send(frame)) {
          setSimulatedAt(frame.timestamp);
          setPoses(frame.poses.map((p) => p.landmarks));
          setSelected(0);
          i++;
        }
        const delay =
          i < frames.length
            ? Math.max(
                20,
                ((frames[i].timestamp - frame.timestamp) * 1000) / timeScale,
              )
            : 20;
        timer.current = setTimeout(tick, delay);
      };
      tick();
    } catch (e) {
      onError(String(e));
      stop();
    }
  };
  const download = () => {
    const a = document.createElement("a"),
      url = URL.createObjectURL(
        new Blob(
          [
            JSON.stringify({
              kind: "skeleton-recording",
              source: sourceRef.current,
              frames: recording.current,
            }),
          ],
          { type: "application/json" },
        ),
      );
    a.href = url;
    a.download = `${profile.id}-skeleton-recording.json`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const calibration = async () => {
    try {
      await api(
        `/calibration/${profile.id}/${calibrating ? "stop" : "start"}`,
        "POST",
      );
      setCalibrating(!calibrating);
    } catch (e) {
      onError(String(e));
    }
  };
  return (
    <section className="panel capture-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">PRIVATE BY DESIGN</span>
          <h2>Movement, without the video.</h2>
        </div>
        <span className={`pill ${running ? "green" : ""}`}>
          <Radio size={13} />
          {running ? "Monitoring" : "Paused"}
        </span>
      </div>
      <div className="camera-stage">
        <video
          ref={video}
          muted
          playsInline
          className={
            preview && showRaw ? "camera-video visible" : "camera-video"
          }
          onEnded={() => {
            setRunning(false);
            setStatus("Video replay complete");
          }}
        />
        {(!preview || !showRaw) && <div className="room-grid" />}
        {preview && (
          <div
            className="roi-mask"
            style={{
              left: `${region.x1 * 100}%`,
              top: `${region.y1 * 100}%`,
              width: `${(region.x2 - region.x1) * 100}%`,
              height: `${(region.y2 - region.y1) * 100}%`,
            }}
          >
            <span>Monitored area</span>
          </div>
        )}
        {poses.length > 0 ? (
          <Skeleton poses={poses} selected={selected < 0 ? 0 : selected} />
        ) : (
          <div className="camera-empty">
            <div className="camera-icon">
              <Camera size={32} />
            </div>
            <strong>Your space stays private</strong>
            <span>
              Start a camera or explore a demo scenario.
              <br />
              Only body landmarks reach the server.
            </span>
          </div>
        )}
        <div className="camera-top">
          <span className="camera-label">
            <span className="dot" />
            {source || "Capture device"}
          </span>
          <span className="camera-label">
            <LockKeyhole size={12} /> On-device video
          </span>
        </div>
        <div className="camera-bottom">
          <span>{status}</span>
          <span>
            {result?.quality === "good"
              ? "Pose quality: good"
              : result?.quality === "uncertain"
                ? "Pose quality: uncertain"
                : "Waiting for movement"}
          </span>
        </div>
      </div>
      {preview && poses.length > 0 && (
        <div className="select-person">
          {poses.map((_, i) => (
            <button
              key={i}
              className={selected === i ? "small primary" : "small secondary"}
              onClick={() => choose(i)}
            >
              Monitor person {i + 1}
            </button>
          ))}
          <span>Select again after occlusion or crossed paths.</span>
        </div>
      )}
      <div className="capture-actions">
        <button
          className="primary"
          onClick={() =>
            running || preview ? stop() : setPendingCapture(null)
          }
        >
          {running || preview ? <Pause size={16} /> : <Camera size={16} />}
          {running || preview ? "Pause capture" : "Start camera"}
        </button>
        <label className="button secondary">
          <FileVideo size={16} /> Local video
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
        <button
          className="icon-button"
          title="Download skeleton recording"
          aria-label="Download skeleton recording"
          disabled={!recording.current.length}
          onClick={download}
        >
          <Download size={18} />
        </button>
      </div>
      <details className="calibration">
        <summary>
          Monitored area · bystanders masked before pose estimation
        </summary>
        <p>
          Only people whose hip center is inside this area are tracked or
          considered helpers. Pixels outside it are masked before MediaPipe. A
          partially visible person at the boundary can still be detected; keep
          the demo area clear.
        </p>
        <div className="roi-inputs">
          {(["x1", "y1", "x2", "y2"] as const).map((k) => (
            <label key={k}>
              {k}
              <input
                aria-label={`Monitored area ${k}`}
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={region[k]}
                disabled={running || preview}
                onChange={(e) => setRegion({ ...region, [k]: +e.target.value })}
              />
            </label>
          ))}
        </div>
      </details>
      {config.demo_enabled && (
        <div className="replay-controls">
          <div>
            <span className="eyebrow">DEMO SCENARIOS</span>
            <p>Synthetic poses · same detection pipeline</p>
          </div>
          <select
            aria-label="Replay scenario"
            value={scenario}
            onChange={(e) => setScenario(e.target.value)}
          >
            {[
              "stumble",
              "fall",
              "long_lie",
              "normal",
              "restricted_standing",
              "assisted_standing",
              "sway",
              "bed_exit",
              "low_visibility",
            ].map((s) => (
              <option key={s} value={s}>
                {s.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <select
            aria-label="Demo time scale"
            value={timeScale}
            disabled={running}
            onChange={(e) => setTimeScale(+e.target.value)}
          >
            <option value={8}>8× simulated time</option>
            <option value={1}>Real-time replay</option>
          </select>
          <button
            className="secondary"
            onClick={replay}
            disabled={running || !profile.consent}
          >
            <Play size={15} /> Replay
          </button>
        </div>
      )}
      {simulatedAt !== null && (
        <div className="simulated-clock" role="status">
          <strong>SIMULATED CLOCK · {timeScale}×</strong>
          <span>
            {new Date(simulatedAt * 1000).toLocaleTimeString("en-CA", {
              timeZone: profile.timezone,
            })}{" "}
            · {profile.timezone}
          </span>
          <span>
            120s of observed stillness ≈ {120 / timeScale}s of playback.
            Check-in: {(profile.check_in_seconds || 60) / timeScale}s. Real
            camera timing stays unchanged.
          </span>
        </div>
      )}
      <div className="feature-strip">
        <div>
          <span>Posture</span>
          <strong>{String(result?.features?.posture || "—")}</strong>
        </div>
        <div>
          <span>Trunk angle</span>
          <strong>{result?.features?.trunk_angle ?? "—"}°</strong>
        </div>
        <div>
          <span>Sway</span>
          <strong>{result?.features?.sway ?? "—"}</strong>
        </div>
        <div>
          <span>Personal baseline z-score</span>
          <strong>{result?.anomaly_score?.toFixed(2) ?? "Uncalibrated"}</strong>
        </div>
      </div>
      {result?.baseline_contributions?.length ? (
        <div className="baseline-reason">
          <strong>{result.baseline_state}</strong>
          <p>
            Current score: {result.anomaly_score?.toFixed(2)} · frozen anchor
            score: {result.anchor_score?.toFixed(2)}
          </p>
          {result.baseline_contributions.slice(0, 2).map((c, i) => (
            <p key={i}>
              {String(c.feature)}: {Number(c.value).toFixed(3)} vs personal
              median {Number(c.median).toFixed(3)} · z ={" "}
              {Number(c.z).toFixed(2)}
            </p>
          ))}
        </div>
      ) : null}
      <details className="calibration">
        <summary>Personal calibration · median and MAD</summary>
        <p>
          Record separate sessions of ordinary movement, review them as normal,
          then fit a posture-specific baseline. Three consecutive abnormal
          windows can trigger only after held-out evaluation. Novel movement is
          not necessarily unsafe.
        </p>
        <button className="secondary small" onClick={calibration}>
          {calibrating ? "Stop calibration" : "Record calibration"}
        </button>{" "}
        <a href={`/api/calibration/${profile.id}/export`} className="text-link">
          Export windows
        </a>
        <BaselinePanel profile={profile} onError={onError} />
      </details>
      {pendingCapture !== undefined && (
        <div className="modal-shade">
          <section
            className="modal consent-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="consent-title"
          >
            <span className="eyebrow">BEFORE MONITORING</span>
            <h2 id="consent-title">Your movement. Your choice.</h2>
            <p>
              Use synthetic data or consenting teammates only. Video stays on
              this device. Identifying skeleton landmarks and event keyframes
              are stored by the server for {profile.retain_days} days. You can
              pause capture or delete the stored data from Privacy.
            </p>
            <p>
              Skeleton-only is the default. No microphone, raw video upload or
              clinical use. This is not a medical device.
            </p>
            <label className="check-row">
              <input
                type="checkbox"
                checked={sendGemini}
                onChange={(e) => setSendGemini(e.target.checked)}
              />
              Allow category flags and skeleton-rendered keyframes to Gemini
              (requires a confirmed billed project)
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={showRaw}
                onChange={(e) => setShowRaw(e.target.checked)}
              />
              Show raw video on this device only (logged preview override)
            </label>
            <div className="action-row">
              <button
                className="secondary"
                disabled={consentBusy}
                onClick={() => consentCapture(false)}
              >
                Decline and pause
              </button>
              <button
                className="primary"
                disabled={
                  consentBusy ||
                  region.x1 >= region.x2 ||
                  region.y1 >= region.y2
                }
                onClick={() => consentCapture(true)}
              >
                I consent — start {pendingCapture ? "local video" : "camera"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
