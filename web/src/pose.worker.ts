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
      const region = event.data.region;
      let input: ImageBitmap | OffscreenCanvas = bitmap;
      if (region) {
        const canvas = new OffscreenCanvas(bitmap.width, bitmap.height),
          ctx = canvas.getContext("2d")!;
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, bitmap.width, bitmap.height);
        const x = region.x1 * bitmap.width,
          y = region.y1 * bitmap.height,
          w = (region.x2 - region.x1) * bitmap.width,
          h = (region.y2 - region.y1) * bitmap.height;
        ctx.drawImage(bitmap, x, y, w, h, x, y, w, h);
        input = canvas;
      }
      const result = landmarker.detectForVideo(input, event.data.timestamp);
      const poses = result.landmarks.filter(
        (p) =>
          !region ||
          ((p[23].x + p[24].x) / 2 >= region.x1 &&
            (p[23].x + p[24].x) / 2 <= region.x2 &&
            (p[23].y + p[24].y) / 2 >= region.y1 &&
            (p[23].y + p[24].y) / 2 <= region.y2),
      );
      self.postMessage({
        type: "poses",
        poses,
        timestamp: event.data.timestamp,
      });
    } catch (error) {
      self.postMessage({ type: "error", error: String(error) });
    } finally {
      bitmap.close();
    }
  }
};
