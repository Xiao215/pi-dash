// Regenerates the README screenshots from the demo server (made-up data, nothing personal).
//   python3 tools/demo.py --port 9100 &  python3 tools/demo.py --port 9101 --trouble &
//   npm i -D playwright && npx playwright install chromium   (once)
//   node tools/screenshots.cjs
const { chromium } = require("playwright");
const path = require("path");

const OUT = path.join(__dirname, "..", "docs");
const NORMAL = "http://localhost:9100";
const TROUBLE = "http://localhost:9101";

async function shot(browser, { url, file, scheme = "dark", width = 1200, height = 900, mobile = false, before }) {
  const page = await browser.newPage({
    viewport: { width, height }, deviceScaleFactor: 2, colorScheme: scheme, isMobile: mobile, hasTouch: mobile,
    reducedMotion: "reduce",
  });
  await page.goto(url);
  await page.waitForSelector(".svc:not(.skel-card)");
  await page.waitForTimeout(600);
  if (before) await before(page);
  await page.screenshot({ path: path.join(OUT, file) });
  await page.close();
  console.log("wrote docs/" + file);
}

(async () => {
  const browser = await chromium.launch();
  await shot(browser, { url: NORMAL, file: "overview-dark.png", height: 1080 });
  await shot(browser, { url: NORMAL, file: "overview-light.png", scheme: "light", height: 1080 });
  await shot(browser, { url: TROUBLE, file: "trouble-dark.png", height: 1080 });
  await shot(browser, {
    url: NORMAL, file: "logs-dark.png", height: 760,
    before: async (page) => {
      await page.click('button[data-svc="discord-bot"][data-action="logs"]');
      await page.waitForSelector("#log .line");
      await page.fill("#log-search", "answered");
      await page.waitForTimeout(400);
    },
  });
  await shot(browser, {
    url: NORMAL, file: "activity-dark.png", height: 700,
    before: async (page) => {
      await page.click('#feed > li:not(.repeats) > button.ev');  // the crash, not the folded updates
      await page.waitForTimeout(400);
      await page.evaluate(() => document.querySelector("#feed").scrollIntoView({ block: "center" }));
      await page.waitForTimeout(300);
    },
  });
  await shot(browser, { url: NORMAL, file: "phone-dark.png", width: 390, height: 844, mobile: true });
  await browser.close();
})();
