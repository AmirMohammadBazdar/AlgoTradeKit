"""
Behavioural tests for the shipped browser front-ends, run in a real JS engine.

The other front-end tests assert that certain strings exist in ``report.html`` /
``index.html``.  That catches a deleted handler but not a broken one: the v1.0.2
trade popup contained every string you would grep for and still could not be
clicked, because two hover handlers hid it before the pointer arrived.

Here the page's script block is executed by ``node`` against a small DOM stub,
so the assertions are about what the code *does*.  Skipped when node is absent
(it ships on the GitHub Actions runners, so CI does run these).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node") or shutil.which("nodejs")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

ROOT = Path(__file__).resolve().parents[1]
REPORT_HTML = ROOT / "src" / "AlgoTradeKit" / "report" / "static" / "report.html"


# ---------------------------------------------------------------------------
# DOM stub + loader
# ---------------------------------------------------------------------------
# Auto-vivifying elements: any getElementById returns a stub that records the
# style/textContent/class changes the page makes.  Document-level listeners are
# captured so a test can fire a synthetic event at a chosen target.

_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(PAGE, 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const src = blocks[blocks.length - 1];      // the app script (CDN ones have src=)

function mkEl(id) {
  const cls = new Set();
  return {
    id, textContent: '', innerHTML: '', value: '',
    style: new Proxy({}, {get: (t, k) => t[k] ?? '', set: (t, k, v) => (t[k] = v, true)}),
    classList: {
      add: c => cls.add(c), remove: c => cls.delete(c),
      contains: c => cls.has(c), toggle: (c, on) => on ? cls.add(c) : cls.delete(c),
    },
    _cls: cls,
    contains(node) { return node === this; },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({left: 0, top: 0, width: 800, height: 400}),
    getContext: () => ({}),
  };
}

const els = {}, docListeners = {};
const document = {
  getElementById(id) { return els[id] ??= mkEl(id); },
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener(type, fn) { (docListeners[type] ??= []).push(fn); },
  body: mkEl('body'), documentElement: mkEl('html'), createElement: mkEl,
};

const ctx = {
  document, console, JSON, Math, Date, Intl, Object, Array, String, Number, Boolean,
  window: {
    addEventListener() {}, open: u => { ctx.__opened = u; },
    innerWidth: 1200, innerHeight: 900,
    matchMedia: () => ({matches: false, addEventListener() {}}),
  },
  location: {host: 'vps.example:9100', hostname: 'vps.example', protocol: 'http:'},
  WebSocket: function () { return {readyState: 0, send() {}, close() {}}; },
  Chart: Object.assign(
    function () {
      return {data: {datasets: []}, options: {scales: {x: {}}}, update() {}, destroy() {}};
    },
    {register: () => {}, defaults: {font: {}, plugins: {}}, Tooltip: {positioners: {}}},
  ),
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
  requestAnimationFrame: () => 0, navigator: {}, alert: () => {},
};
ctx.window.location = ctx.location;
ctx.globalThis = ctx;
vm.createContext(ctx);

let topLevelError = null;
try { vm.runInContext(src, ctx, {filename: 'page.js'}); }
catch (e) { topLevelError = e.message; }

// `let`/`const` at script top level live in the realm's global lexical scope,
// which is not visible on the context object -- reach them by evaluating.
const ev = code => vm.runInContext(code, ctx);
const fire = (type, target, extra = {}) =>
  (docListeners[type] || []).forEach(fn => fn({target, ...extra}));
const out = {topLevelError};
"""


def _run(page: Path, body: str) -> dict:
    """Execute *body* after loading *page*'s script; return its ``out`` object."""
    script = _HARNESS.replace("PAGE", json.dumps(str(page))) + body
    script += "\nconsole.log(JSON.stringify(out));\n"
    proc = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# report.html — the equity-chart trade popup (v1.1.0 fix)
# ---------------------------------------------------------------------------

_TRADE = """
const trade = {uid: 11, trade_id: 7, symbol: 'BTCUSDT', direction: 'long',
               entry_price: 1, exit_price: 2, stop_loss: 0.5, take_profit: 3,
               net_pnl: 10, pnl_r: 1.5, close_reason: 'tp',
               open_time: 1700000000000, close_time: 1700003600000};
ctx.__t = trade;
const tip = document.getElementById('trade-tooltip');
"""


class TestTradePopupBehaviour:
    @pytest.fixture(scope="class")
    def result(self) -> dict:
        return _run(REPORT_HTML, _TRADE + """
ev('pinTooltip(__t, {clientX: 100, clientY: 100})');
out.pinnedId       = ev('pinnedTradeId');
out.displayOnPin   = tip.style.display;
out.hasPinnedClass = tip._cls.has('pinned');

// hover machinery must not touch a pinned box
ev('hideTooltip()');
out.displayAfterHide = tip.style.display;
ev('externalTooltip({chart: {canvas: document.getElementById("equity-chart")},'
   + ' tooltip: {opacity: 0, dataPoints: []}})');
out.displayAfterHoverOut = tip.style.display;
fire('mousemove', document.getElementById('elsewhere'));
out.displayAfterMouseMove = tip.style.display;

// the button the whole bug was about
ev('reportData = {has_chart: true, chart_port: 8712,'
   + ' trade_markers: [{uid: 11, trade_id: 7}]}');
ev('openChart()');
out.openedUrl        = ctx.__opened;
out.pinnedAfterOpen  = ev('pinnedTradeId');

// closing rules
ev('pinTooltip(__t, {clientX: 10, clientY: 10})');
fire('mousedown', tip);
out.pinnedAfterInsideClick = ev('pinnedTradeId');
fire('mousedown', document.getElementById('main'));
out.pinnedAfterOutsideClick = ev('pinnedTradeId');
out.displayAfterOutsideClick = tip.style.display;
out.classAfterOutsideClick = tip._cls.has('pinned');

ev('pinTooltip(__t, {clientX: 10, clientY: 10})');
fire('keydown', document.body, {key: 'Escape'});
out.pinnedAfterEscape = ev('pinnedTradeId');

// unpinned hover preview must still behave like a tooltip
ev('showTooltipForTrade(__t, {clientX: 10, clientY: 10})');
out.previewShown = tip.style.display;
fire('mousemove', document.getElementById('elsewhere'));
out.previewHidden = tip.style.display;
""")

    def test_page_script_runs_clean(self, result):
        assert result["topLevelError"] is None

    def test_click_pins_the_box(self, result):
        assert result["pinnedId"] == 11
        assert result["displayOnPin"] == "block"
        assert result["hasPinnedClass"] is True

    def test_hover_events_cannot_close_a_pinned_box(self, result):
        # the actual v1.0.2 bug: the box died before the pointer reached it
        assert result["displayAfterHide"] == "block"
        assert result["displayAfterHoverOut"] == "block"
        assert result["displayAfterMouseMove"] == "block"

    def test_open_chart_button_reaches_the_right_chart(self, result):
        assert result["openedUrl"] == "http://vps.example:8712/"
        assert result["pinnedAfterOpen"] is None

    def test_only_an_outside_click_or_escape_closes_it(self, result):
        assert result["pinnedAfterInsideClick"] == 11
        assert result["pinnedAfterOutsideClick"] is None
        assert result["displayAfterOutsideClick"] == "none"
        assert result["classAfterOutsideClick"] is False
        assert result["pinnedAfterEscape"] is None

    def test_unpinned_hover_preview_is_unchanged(self, result):
        assert result["previewShown"] == "block"
        assert result["previewHidden"] == "none"
