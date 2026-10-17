import { mkdir, cp, access, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";

const root = fileURLToPath(new URL("../", import.meta.url));
// MediaPipe's WASM loader uses importScripts, which requires a classic worker.
// Prebundle to IIFE so the same worker works in Vite dev and the production build.
await build({
  absWorkingDir: root,
  entryPoints: ["./src/pose.worker.ts"],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2020",
  minify: true,
  outfile: path.join(root, "public/pose-worker.js"),
});
await mkdir(path.join(root, "public/models"), { recursive: true });
await cp(
  path.join(root, "node_modules/@mediapipe/tasks-vision/wasm"),
  path.join(root, "public/wasm"),
  { recursive: true },
);
const model = path.join(root, "public/models/pose_landmarker_lite.task");
try {
  await access(model);
} catch {
  const res = await fetch(
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
  );
  if (!res.ok) throw new Error(`Model download failed: ${res.status}`);
  await writeFile(model, Buffer.from(await res.arrayBuffer()));
}
console.log(
  "MediaPipe WASM and model ready locally. Raw video stays on the capture device; optional Gemini keyframes contain skeletons only.",
);
