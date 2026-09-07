// The panel (ui.html) in a headless browser against a running agent with a fake board.
//
//   python tests/fakeboard.py            -> {"port": ..., "drive": ...}
//   python devagent.py --port 8198 --token tok1 --add 'fake path=<drive> port=<port>'
//   node tests/test_panel.mjs [http://127.0.0.1:8198] [tok1]
//
// Needs playwright (npm i playwright) with a Chromium; PLAYWRIGHT_CHROMIUM overrides the binary.
// Checks: the token gate and password field, tabs, file rows with odd names built as text,
// the console keys (Ctrl-C prompt, AltGr character, Ctrl-]), a never-ending line, the add-form
// validation, the empty state after forgetting the only board, and re-adding it by typed path.
import { chromium } from 'playwright';
import fs from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8198';
const TOKEN = process.argv[3] || 'tok1';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0;
const check = (name, ok, detail = '') => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : '  ' + detail)); if (!ok) failed++; };

const health = await (await fetch(BASE + '/health', { headers: { 'X-Token': TOKEN } })).json();
const board = health.boards[0];
if (!board) { console.log('no board configured on the agent'); process.exit(2); }
const drive = board.drive;
if (drive) { fs.writeFileSync(drive + "/it's <b>.py", 'x'); fs.mkdirSync(drive + '/sub dir', { recursive: true }); }

const errs = [];
const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {});
const page = await browser.newPage();
page.on('pageerror', e => errs.push(e.message));
page.on('dialog', d => d.accept());

await page.goto(BASE + '/'); await sleep(500);
check('no token -> the pill says so', (await page.textContent('#p-agent')).includes('token'));
check('token field is a password field', await page.getAttribute('#token', 'type') === 'password');
await page.fill('#token', TOKEN); await page.press('#token', 'Enter'); await sleep(1500);
check('agent pill shows the version', /^v\d+/.test(await page.textContent('#p-agent')), await page.textContent('#p-agent'));
check('board tab present', (await page.textContent('#tabs')).includes(board.label || board.id));

if (drive) {
  await page.click('text=Files'); await sleep(800);
  const names = await page.$$eval('#files tr', trs => trs.map(t => t.querySelector('td')?.textContent));
  check("file name with ' and < is text", names.some(n => n && n.includes("it's <b>.py")), JSON.stringify(names));
  await page.click('#files a:has-text("sub dir/")'); await sleep(400);
  check('directory click sets the path', (await page.inputValue('#path')) === 'sub dir');
  await page.fill('#path', '');
}

await page.click('text=Console'); await sleep(300); await page.focus('#term');
await page.keyboard.press('Control+c'); await sleep(800);
const term1 = await page.textContent('#term');
check('Ctrl-C reaches the board (prompt back)', term1.includes('>>>'));
await page.evaluate(() => { for (const [key, ctrl, alt] of [['@', true, true], [']', true, false]])
  document.getElementById('term').dispatchEvent(new KeyboardEvent('keydown', { key, ctrlKey: ctrl, altKey: alt, bubbles: true, cancelable: true })); });
await sleep(800);
const term2 = await page.textContent('#term');
check('AltGr @ is sent as a character', term2.includes('@'));
await page.evaluate(() => TERM.write('Z'.repeat(20000)));
await sleep(150);
check('a 20000-char line wraps (no unbounded string)', (await page.evaluate(() => (TERM.el.textContent.match(/\n/g) || []).length)) >= 4);

await page.click('text=＋ board'); await sleep(800);
check('add form has typed path/port inputs', await page.isVisible('#f-path-manual') && await page.isVisible('#f-port-manual'));
await page.fill('#f-id', board.id); await page.click('#addform button:has-text("Save")'); await sleep(200);
check('duplicate id refused', (await page.textContent('#addnote')).includes('taken'));
await page.fill('#f-id', 'bad id!'); await page.click('#addform button:has-text("Save")'); await sleep(200);
check('malformed id refused', (await page.textContent('#addnote')).includes('letters'));
await page.click('#addform button:has-text("Cancel")'); await sleep(300);
check('cancel returns to the app', await page.isVisible('#app'));

await page.click('text=Forget'); await sleep(1500);
check('empty state after forgetting the only board', (await page.textContent('#info')).includes('no board configured'));
await page.click('text=＋ board'); await sleep(800);
await page.fill('#f-id', board.id); await page.fill('#f-label', board.label || '');
if (board.path) await page.fill('#f-path-manual', board.path);
if (board.port) await page.fill('#f-port-manual', board.port);
await page.fill('#f-baud', String(board.baud || 115200));
await page.click('#addform button:has-text("Save")'); await sleep(1500);
check('board re-added by typed path/port', (await page.textContent('#tabs')).includes(board.label || board.id));

check('no page errors', errs.length === 0, errs.join(' | '));
if (drive) { fs.rmSync(drive + "/it's <b>.py", { force: true }); fs.rmSync(drive + '/sub dir', { recursive: true, force: true }); }
await browser.close();
console.log(failed ? failed + ' FAILED' : 'all panel checks passed');
process.exit(failed ? 1 : 0);
