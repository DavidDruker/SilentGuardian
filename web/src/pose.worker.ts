import { FilesetResolver, PoseLandmarker } from "@mediapipe/tasks-vision";

let landmarker: PoseLandmarker | null = null;
self.onmessage = async (event) => {
  if (event.data.type === "init") {
    try {
      const vision = await FilesetResolver.forVisionTasks("/wasm");
      landmarker = await PoseLandmarker.createFromOptions(vision, {
        baseOptions: {
          modelAssetPath: "/models/pose_landmarker_lite.task",
          delegate: "CPU",
        },
        runningMode: "VIDEO",
        // Detect a second person so capture can pause scoring, but submit only
        // single-person frames to the monitoring pipeline.
        numPoses: 2,
        minPoseDetectionConfidence: 0.55,
        minTrackingConfidence: 0.6,
      });
      self.postMessage({ type: "ready" });
    } catch (error) {
      self.postMessage({ type: "error", error: String(error) });
    }
  } else if (event.data.type === "frame" && landmarker) {
    const bitmap: ImageBitmap = event.data.bitmap;
    try {
      const result = landmarker.detectForVideo(bitmap, event.data.timestamp);
      self.postMessage({
        type: "poses",
        poses: result.landmarks,
        timestamp: event.data.timestamp,
      });
    } catch (error) {
      self.postMessage({ type: "error", error: String(error) });
    } finally {
      bitmap.close();
    }
  }
};
