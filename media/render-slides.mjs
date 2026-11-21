import { createRequire } from 'node:module';
import path from 'node:path';
const require = createRequire('/home/agni/projects/talent-scout/wt/client-favicon/package.json');
const { chromium } = require('@playwright/test');
const dir = path.resolve(process.argv[2] || 'linkedin');
const pdfName = process.argv[3] || 'talent-scout-flows.pdf';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1080, height: 1350 }, deviceScaleFactor: 2 });
await page.goto('file://' + dir + '/slides.html');
await page.evaluate(() => document.fonts.ready);
const ids = await page.$$eval('section.slide', (s) => s.map((x) => x.id));
for (const [i, id] of ids.entries()) {
  const el = await page.$('#' + id);
  // overflow check: any child extending past the slide box
  const over = await el.evaluate((s) => { const r = s.getBoundingClientRect(); return [...s.querySelectorAll('*')].filter(c => { const b = c.getBoundingClientRect(); return b.width && (b.bottom > r.bottom + 1 || b.right > r.right + 1); }).map(c => c.className || c.tagName).slice(0,5); });
  if (over.length) console.log('OVERFLOW', id, over);
  await el.screenshot({ path: `${dir}/slide-${String(i + 1).padStart(2, '0')}.png` });
}
await page.emulateMedia({ media: 'print' });
await page.addStyleTag({ content: '@page{size:1080px 1350px;margin:0} body{background:none} .slide{margin:0;break-after:page}' });
await page.pdf({ path: `${dir}/${pdfName}`, width: '1080px', height: '1350px', printBackground: true });
await browser.close();
console.log('done', ids.length);
