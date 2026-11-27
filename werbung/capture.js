// Macht echte Handy-Screenshots der Website für das Tutorial-Video (video3.html).
// Aufruf: node capture.js   → schreibt screens/*.jpg und screens/screens.json
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const SITE = 'file://' + path.join(__dirname, '..', 'website', 'index.html');
const OUT = path.join(__dirname, 'screens');
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: 'light' });
  const p = await ctx.newPage();
  const info = {};
  const rect = async sel => p.evaluate(s => { const r = document.querySelector(s).getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height }; }, sel);
  const shot = async name => { await p.waitForTimeout(400); await p.screenshot({ path: `${OUT}/${name}.jpg`, type: 'jpeg', quality: 90 }); };

  await p.goto(SITE); await p.waitForTimeout(1200);
  await p.addStyleTag({ content: 'html { scroll-behavior: auto !important; }' });
  // 1) Startseite
  await shot('01-start');
  // 2) Lange Seite zum Scrollen (Kopfzeile separat)
  await p.evaluate(() => document.querySelector('header').style.visibility = 'hidden');
  const prodTop = await p.evaluate(() => document.getElementById('produkte').getBoundingClientRect().top + scrollY);
  const longH = Math.round(prodTop + 200);
  await p.screenshot({ path: `${OUT}/02-lang.jpg`, type: 'jpeg', quality: 88, fullPage: true, clip: { x: 0, y: 0, width: 390, height: longH } });
  await p.evaluate(() => document.querySelector('header').style.visibility = '');
  await p.screenshot({ path: `${OUT}/02-header.png`, clip: { x: 0, y: 0, width: 390, height: 73 } });
  const aboutTop = await p.evaluate(() => document.getElementById('ueber-uns').getBoundingClientRect().top + scrollY);
  info.lang = { height: longH, about: aboutTop, produkte: prodTop };
  // 3) Produkte mit Filter
  await p.evaluate(() => document.getElementById('produkte').scrollIntoView());
  await p.evaluate(() => scrollBy(0, -72));
  await shot('03-produkte');
  info.filterSchuhe = await rect('.filter[data-kat="schuhe"]');
  await p.click('.filter[data-kat="schuhe"]');
  await p.evaluate(() => { document.getElementById('produkte').scrollIntoView(); scrollBy(0, -72); });
  await shot('04-schuhe');
  // 4) Sneaker auswählen: Farbe Bunt + Grösse 42
  const card = '.product[data-i="2"]';
  await p.evaluate(s => { const el = document.querySelector(s); el.scrollIntoView(); scrollBy(0, -80); }, card);
  await p.waitForTimeout(300);
  await shot('05-karte');
  info.farbe = await rect(`${card} .color:nth-child(3)`);
  await p.click(`${card} .color:nth-child(3)`);
  await p.waitForTimeout(500);
  await shot('06-farbe');
  const sizeSel = `${card} .sizes[data-opt="Grösse"] .size:nth-child(5)`;
  await p.evaluate(s => { document.querySelector(s).scrollIntoView({ block: 'center' }); }, sizeSel);
  await p.waitForTimeout(300);
  await shot('07-vor-groesse');
  info.groesse = await rect(sizeSel);
  await p.click(sizeSel);
  await p.waitForTimeout(300);
  info.bestellen = await rect(`${card} .order`);
  await shot('08-groesse');
  // 5) Kasse mit Beispieldaten
  await p.click(`${card} .order`);
  await p.waitForTimeout(500);
  await shot('09-kasse-leer');
  await p.fill('#co-fullname', 'Max Muster');
  await p.fill('#co-street', 'Musterstrasse 1');
  await p.fill('#co-plz', '8000');
  await p.fill('#co-ort', 'Zürich');
  await p.fill('#co-tel', '079 123 45 67');
  await p.evaluate(() => document.activeElement.blur());
  await shot('10-kasse-voll');
  await p.evaluate(() => { const d = document.getElementById('checkout'); d.scrollTop = d.scrollHeight; });
  await p.waitForTimeout(300);
  info.abschicken = await rect('.co-submit');
  await shot('11-kasse-unten');
  // 6) Fertig-Ansicht (ohne E-Mail-App zu öffnen)
  await p.evaluate(() => {
    document.getElementById('co-done-sum').textContent = 'CHF 30.– + Versand';
    document.getElementById('co-done-tel').textContent = '079 123 45 67';
    document.getElementById('co-form').hidden = true; document.getElementById('co-done').hidden = false;
  });
  await shot('12-fertig');
  fs.writeFileSync(`${OUT}/screens.json`, JSON.stringify(info, null, 2));
  console.log(JSON.stringify(info));
  await b.close();
})();
