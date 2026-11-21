import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import path from 'node:path';
const require = createRequire('/home/agni/projects/talent-scout/wt/client-favicon/package.json');
const { chromium } = require('@playwright/test');
const file = 'file://' + path.resolve('video/motion.html') + '?t=0';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1080, height: 1350 } });
await page.goto(file);
await page.evaluate(() => document.fonts.ready);
const stills = process.argv.slice(2).map(Number);
if (stills.length) {
  for (const t of stills) { await page.evaluate((t) => render(t), t); await page.screenshot({ path: `video/still-${t}.png` }); }
  await browser.close(); process.exit(0);
}
const { FPS, DURATION } = await page.evaluate(() => ({ FPS, DURATION }));
const fs = await import('node:fs');
const mj = 'video/frames.mjpeg';
const outStream = fs.createWriteStream(mj);
const total = Math.round(FPS * DURATION);
for (let i = 0; i < total; i++) {
  await page.evaluate((t) => render(t), i / FPS);
  const buf = await page.screenshot({ type: 'jpeg', quality: 95 });
  if (!outStream.write(buf)) await new Promise((r) => outStream.once('drain', r));
}
await new Promise((r) => outStream.end(r));
const ff = spawn(process.env.HOME + '/.cache/ms-playwright/ffmpeg-1013/ffmpeg-linux',
  ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-c:v', 'mjpeg', '-framerate', String(FPS), '-i', mj,
   '-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', '24', '-row-mt', '1', '-deadline', 'good', '-cpu-used', '2', '-pix_fmt', 'yuv420p', 'video/talent-scout-motion.webm'],
  { stdio: ['ignore', 'inherit', 'inherit'] });
const code = await new Promise((r) => ff.on('close', r));
if (code === 0) fs.unlinkSync(mj); else console.log('ffmpeg exit', code);
await browser.close();
console.log('frames', total);
