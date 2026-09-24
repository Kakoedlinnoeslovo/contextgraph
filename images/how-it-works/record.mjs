// Renders index.html frame by frame and encodes ../how-it-works.gif.
//
//   uv run --extra embed python images/how-it-works/extract.py /path/to/celery \
//       "What happens when a worker receives a revoke request?" 3000
//   cd images/how-it-works && npm install && node record.mjs
//
// `node record.mjs --stills 3,8.5,20` saves PNG stills of those seconds to ./stills instead.
import puppeteer from "puppeteer";
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FPS = 20;
const WIDTH = 1000, HEIGHT = 600;
const stillsAt = process.argv.includes("--stills") ? process.argv[process.argv.indexOf("--stills") + 1] : null;

const browser = await puppeteer.launch({ args: ["--force-color-profile=srgb"] });
const page = await browser.newPage();
page.on("pageerror", err => { console.error(err); process.exit(1); });
await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });
await page.goto("file://" + path.join(here, "index.html"), { waitUntil: "networkidle0" });
await page.waitForFunction("window.ready === true");
const duration = await page.evaluate("window.DURATION");

async function shoot(t, file) {
  await page.evaluate(t => window.renderFrame(t), t);
  await page.screenshot({ path: file, clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT } });
}

if (stillsAt) {
  const dir = path.join(here, "stills");
  mkdirSync(dir, { recursive: true });
  for (const s of stillsAt.split(",")) await shoot(Number(s), path.join(dir, `t${s}.png`));
  console.log("stills in", dir);
} else {
  const dir = path.join(here, "frames");
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir);
  const frames = Math.round(duration * FPS);
  for (let i = 0; i < frames; i++) await shoot(i / FPS, path.join(dir, `${String(i).padStart(4, "0")}.png`));
  const out = path.join(here, "..", "how-it-works.gif");
  execFileSync("ffmpeg", [
    "-y", "-v", "error", "-framerate", String(FPS), "-i", path.join(dir, "%04d.png"),
    "-filter_complex", "[0:v]split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
    out,
  ]);
  rmSync(dir, { recursive: true, force: true });
  console.log("wrote", out);
}
await browser.close();
