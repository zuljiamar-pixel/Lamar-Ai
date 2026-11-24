// Rendert video.html Bild für Bild und erzeugt mit ffmpeg ein MP4.
// Aufruf: node render.js [ausgabe.mp4]        Vorschau: node render.js --preview ordner t1 t2 ...
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
(async () => {
  const args = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto('file://' + path.join(__dirname, 'video.html'));
  await p.evaluate(() => window.ready);
  if (args[0] === '--preview') {
    const dir = args[1];
    for (const t of args.slice(2)) {
      await p.evaluate(t => render(t), +t);
      await p.screenshot({ path: `${dir}/v-${t}.jpg`, type: 'jpeg', quality: 80 });
    }
    console.log('errors', errs);
    await b.close(); return;
  }
  const out = args[0] || path.join(__dirname, 'retail-shop-lgll-werbung.mp4');
  const { FPS, DURATION } = await p.evaluate(() => ({ FPS: window.FPS, DURATION: window.DURATION }));
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const N = Math.round(FPS * DURATION);
  for (let f = 0; f < N; f++) {
    await p.evaluate(t => render(t), f / FPS);
    const buf = await p.screenshot({ type: 'jpeg', quality: 95 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (f % 150 === 0) console.log(`Bild ${f}/${N}`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  console.log('fertig', out, 'errors', errs);
  await b.close();
})();
