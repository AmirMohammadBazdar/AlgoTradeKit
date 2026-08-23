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
CHART_HTML  = ROOT / "src" / "AlgoTradeKit" / "visual" / "static" / "index.html"


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
  let className = '', text = '';
  const el = {
    id, innerHTML: '', value: '',
    style: new Proxy({}, {get: (t, k) => t[k] ?? '', set: (t, k, v) => (t[k] = v, true)}),
    classList: {
      add: c => cls.add(c), remove: c => cls.delete(c),
      contains: c => cls.has(c), toggle: (c, on) => on ? cls.add(c) : cls.delete(c),
    },
    _cls: cls,
    contains(node) { return node === this || this.children.includes(node); },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.push(c); return c; },
    removeChild(c) { return c; },
    remove() {},
    children: [],
    previousElementSibling: null, nextElementSibling: null,
    querySelectorAll: () => [], querySelector: () => null,
    getBoundingClientRect: () => ({left: 0, top: 0, width: 800, height: 400}),
    getContext: () => autoStub('ctx2d'),
    clientWidth: 800, clientHeight: 400,
  };
  // className must stay in step with classList — the page sets both ways
  Object.defineProperty(el, 'className', {
    get: () => className,
    set(v) {
      className = String(v);
      cls.clear();
      className.split(/\s+/).filter(Boolean).forEach(c => cls.add(c));
    },
  });
  // textContent reads through to appended children, as a real node does
  Object.defineProperty(el, 'textContent', {
    get: () => text + el.children.map(c => c.textContent).join(''),
    set(v) { text = String(v); el.children.length = 0; },
  });
  return el;
}

// Any property access returns a callable stub, so a charting library's whole
// surface can be exercised without modelling it.
function autoStub(name, overrides = {}) {
  const cache = new Map();
  const base = function () { return autoStub(name + '()'); };
  return new Proxy(base, {
    get(t, k) {
      if (k in overrides) return overrides[k];
      if (k === 'toString' || k === Symbol.toPrimitive) return () => name;
      if (typeof k === 'symbol') return undefined;
      if (['clientWidth','clientHeight','width','height'].includes(k)) return 800;
      if (!cache.has(k)) cache.set(k, autoStub(name + '.' + String(k)));
      return cache.get(k);
    },
    set(t, k, v) { overrides[k] = v; return true; },
    apply() { return autoStub(name + '()'); },
    has() { return true; },
  });
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
ctx.LightweightCharts = autoStub('LightweightCharts');
ctx.ResizeObserver = function () { return {observe() {}, disconnect() {}}; };
ctx.devicePixelRatio = 1;
ctx.__sent = [];
EXTRA_GLOBALS
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


def _run(page: Path, body: str, extra_globals: str = "") -> dict:
    """Execute *body* after loading *page*'s script; return its ``out`` object."""
    script = (
        _HARNESS.replace("PAGE", json.dumps(str(page)))
        .replace("EXTRA_GLOBALS", extra_globals)
        + body
    )
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


@pytest.fixture(scope="module")
def popup_result() -> dict:
    """Drive the whole popup lifecycle once; each test asserts one rule."""
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


class TestTradePopupBehaviour:
    def test_page_script_runs_clean(self, popup_result):
        assert popup_result["topLevelError"] is None

    def test_click_pins_the_box(self, popup_result):
        assert popup_result["pinnedId"] == 11
        assert popup_result["displayOnPin"] == "block"
        assert popup_result["hasPinnedClass"] is True

    def test_hover_events_cannot_close_a_pinned_box(self, popup_result):
        # the actual v1.0.2 bug: the box died before the pointer reached it
        assert popup_result["displayAfterHide"] == "block"
        assert popup_result["displayAfterHoverOut"] == "block"
        assert popup_result["displayAfterMouseMove"] == "block"

    def test_open_chart_button_reaches_the_right_chart(self, popup_result):
        assert popup_result["openedUrl"] == "http://vps.example:8712/"
        assert popup_result["pinnedAfterOpen"] is None

    def test_only_an_outside_click_or_escape_closes_it(self, popup_result):
        assert popup_result["pinnedAfterInsideClick"] == 11
        assert popup_result["pinnedAfterOutsideClick"] is None
        assert popup_result["displayAfterOutsideClick"] == "none"
        assert popup_result["classAfterOutsideClick"] is False
        assert popup_result["pinnedAfterEscape"] is None

    def test_unpinned_hover_preview_is_unchanged(self, popup_result):
        assert popup_result["previewShown"] == "block"
        assert popup_result["previewHidden"] == "none"


# ---------------------------------------------------------------------------
# index.html — the timeframe selector (v1.1.0)
# ---------------------------------------------------------------------------

_INIT_5M = """
const init = {
  type: 'init', title: 'BTC', theme: 'dark', chartType: 'candlestick',
  volumeInMain: true, candleCountLimit: null,
  sourceTimeframe: '1m', displayTimeframe: '5m',
  timeframes: ['1m', '3m', '5m', '15m', '1h'],
  bars: [{time: 1700000000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10}],
  indicators: [], drawings: [],
};
ctx.__init = init;
ev('ws = {readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}');
ev('handleMsg(globalThis.__init)');
const menu = document.getElementById('tf-menu');
const btn  = document.getElementById('tf-btn');
"""


@pytest.fixture(scope="module")
def timeframe_result() -> dict:
    return _run(CHART_HTML, _INIT_5M + """
out.wrapShown   = document.getElementById('tf-wrap').style.display;
out.buttonLabel = btn.textContent;
out.optionCount = menu.children.length;
out.optionText  = menu.children.map(c => c.textContent);
out.activeOne   = menu.children.filter(c => c._cls.has('on')).map(c => c.textContent);

// picking a higher timeframe asks the server for it
menu.children.find(c => c.textContent.startsWith('15m')).onclick();
out.sent        = ctx.__sent.slice();
out.menuClosed  = !menu._cls.has('open');
out.busyWhilePending = btn._cls.has('busy');

// the server answers with a fresh init at the new timeframe
ctx.__init2 = Object.assign({}, ctx.__init, {displayTimeframe: '15m'});
ev('handleMsg(globalThis.__init2)');
out.labelAfter  = document.getElementById('tf-btn').textContent;
out.busyAfter   = document.getElementById('tf-btn')._cls.has('busy');

// picking the source sends null, which is how "show it unchanged" is spelled
ctx.__sent.length = 0;
document.getElementById('tf-menu').children.find(c => c.textContent.startsWith('1m')).onclick();
out.sentForSource = ctx.__sent.slice();

// a refusal from the server clears the pending state instead of sticking
ev("handleMsg({type: 'timeframe_error', message: 'nope'})");
out.busyAfterError = document.getElementById('tf-btn')._cls.has('busy');
""")


class TestTimeframeSelector:
    def test_page_script_runs_clean(self, timeframe_result):
        assert timeframe_result["topLevelError"] is None

    def test_selector_is_populated_from_the_server(self, timeframe_result):
        assert timeframe_result["wrapShown"] != "none"
        assert timeframe_result["optionCount"] == 5
        assert [t.split("source")[0] for t in timeframe_result["optionText"]] == [
            "1m", "3m", "5m", "15m", "1h"
        ]

    def test_current_timeframe_is_marked_and_labelled(self, timeframe_result):
        assert timeframe_result["buttonLabel"] == "5m"
        assert timeframe_result["activeOne"] == ["5m"]

    def test_source_timeframe_is_tagged(self, timeframe_result):
        assert "source" in timeframe_result["optionText"][0]

    def test_choosing_one_asks_the_server(self, timeframe_result):
        assert timeframe_result["sent"] == [{"type": "set_timeframe", "tf": "15m"}]
        assert timeframe_result["menuClosed"] is True
        assert timeframe_result["busyWhilePending"] is True

    def test_new_init_settles_the_button(self, timeframe_result):
        assert timeframe_result["labelAfter"] == "15m"
        assert timeframe_result["busyAfter"] is False

    def test_selecting_the_source_sends_null(self, timeframe_result):
        assert timeframe_result["sentForSource"] == [{"type": "set_timeframe", "tf": None}]

    def test_a_refused_switch_is_not_left_pending(self, timeframe_result):
        assert timeframe_result["busyAfterError"] is False


def test_selector_is_hidden_when_there_is_nothing_to_switch_to():
    """A chart whose timeframe could not be detected must not show the button."""
    result = _run(CHART_HTML, """
ev('ws = {readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}');
ctx.__init = {type: 'init', title: 'x', bars: [], indicators: [], drawings: [],
              sourceTimeframe: null, displayTimeframe: null, timeframes: []};
ev('handleMsg(globalThis.__init)');
out.wrapShown = document.getElementById('tf-wrap').style.display;
""")
    assert result["topLevelError"] is None
    assert result["wrapShown"] == "none"
