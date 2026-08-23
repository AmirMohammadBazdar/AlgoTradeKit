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
PAGE_HTML   = ROOT / "src" / "AlgoTradeKit" / "visual" / "static" / "page.html"


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

const allElements = [];

function mkEl(id) {
  const cls = new Set();
  let className = '', text = '', inner = '', elementId = id;
  const el = {
    value: '', tagName: String(id || '').toUpperCase(),
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
    dataset: {}, firstChild: null, lastChild: null,
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
  // Assigning innerHTML replaces the children, which is how pages clear a list
  // Giving a created element an id makes it findable, as attaching it would
  Object.defineProperty(el, 'id', {
    get: () => elementId,
    set(v) { elementId = String(v); els[elementId] = el; },
  });
  Object.defineProperty(el, 'innerHTML', {
    get: () => inner,
    set(v) { inner = String(v); el.children.length = 0; },
  });
  if (el.tagName === 'IFRAME') {
    el.contentWindow = {
      _posted: [],
      postMessage(m) { this._posted.push(m); },
    };
  }
  allElements.push(el);
  return el;
}

// Walk everything ever created plus the tree under `page`, so a selector can
// find elements the page built after load.
function matches(el, sel) {
  if (sel.startsWith('.')) return el._cls.has(sel.slice(1));
  return el.tagName === sel.toUpperCase();
}
function queryAll(sel) {
  return allElements.filter(el => matches(el, sel));
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
      if (k === 'setData' || k === 'update') {
        if (!cache.has(k)) {
          const fn = (...a) => { fn._calls.push(a[0]); fn._last = a[0]; };
          fn._calls = [];
          cache.set(k, fn);
        }
        return cache.get(k);
      }
      if (!cache.has(k)) cache.set(k, autoStub(name + '.' + String(k)));
      return cache.get(k);
    },
    set(t, k, v) { overrides[k] = v; return true; },
    apply() { return autoStub(name + '()'); },
    has() { return true; },
  });
}

const els = {}, docListeners = {};

// Only ids the page actually declares can be found, exactly as in a browser.
// An auto-vivifying stub answers every lookup with an element, which hides the
// one mistake this harness most needs to catch: code dereferencing an element
// that is not there. Elements the page creates register themselves below.
const KNOWN_IDS = new Set(
  [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1])
);

const document = {
  getElementById(id) {
    if (els[id]) return els[id];
    if (!KNOWN_IDS.has(id)) return null;
    return (els[id] = mkEl(id));
  },
  querySelectorAll: sel => queryAll(sel),
  querySelector: sel => queryAll(sel)[0] || null,
  addEventListener(type, fn) { (docListeners[type] ??= []).push(fn); },
  body: mkEl('body'), documentElement: mkEl('html'), createElement: mkEl,
  title: '',
};

const ctx = {
  document, console, JSON, Math, Date, Intl, Object, Array, String, Number, Boolean,
  window: {
    // Same registry as document: a window listener really does see events
    // that bubble up from the document, and tests fire one kind of event.
    addEventListener(type, fn) { (docListeners[type] ??= []).push(fn); },
    removeEventListener() {},
    open: u => { ctx.__opened = u; },
    innerWidth: 1200, innerHeight: 900,
    matchMedia: () => ({matches: false, addEventListener() {}}),
  },
  location: {host: 'vps.example:9100', hostname: 'vps.example', protocol: 'http:',
             search: '', href: 'http://vps.example:9100/'},
  URLSearchParams, URL, TextEncoder, TextDecoder,
  CSS: {escape: v => String(v).replace(/[^\w-]/g, '_')},
  WebSocket: function () { return {readyState: 0, send() {}, close() {}}; },
  Chart: Object.assign(
    function () {
      return {data: {datasets: []}, options: {scales: {x: {}}}, update() {}, destroy() {}};
    },
    {register: () => {}, defaults: {font: {}, plugins: {}}, Tooltip: {positioners: {}}},
  ),
  setTimeout, clearTimeout, setInterval, clearInterval, Promise,
  requestAnimationFrame: cb => setTimeout(cb, 0), navigator: {}, alert: () => {},
};
ctx.window.location = ctx.location;
ctx.globalThis = ctx;
// A chart stub that records its subscriptions, so a test can fire a range or
// crosshair notification the way the real library does: on a later frame,
// after the call that caused it has returned.
const chartStubs = [];
// The page builds a chart at load and another when init arrives; the live one
// is always the most recent.
const mainChartStub = () => chartStubs[chartStubs.length - 1];
function makeChartStub() {
  const cbs = {range: [], logical: [], cross: []};
  const state = {range: null};
  const timeScale = autoStub('timeScale', {
    subscribeVisibleTimeRangeChange: cb => cbs.range.push(cb),
    subscribeVisibleLogicalRangeChange: cb => cbs.logical.push(cb),
    setVisibleRange: r => { state.range = r; },
    getVisibleRange: () => state.range,
    setVisibleLogicalRange: r => { state.logical = r; },
    getVisibleLogicalRange: () => state.logical,
  });
  const chart = autoStub('chart', {
    timeScale: () => timeScale,
    subscribeCrosshairMove: cb => cbs.cross.push(cb),
    _cbs: cbs,
    _state: state,
  });
  chartStubs.push(chart);
  return chart;
}
ctx.LightweightCharts = autoStub('LightweightCharts', {createChart: () => makeChartStub()});
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
        # An async wrapper lets a body await the page's own promises (the
        # shell fetches its layout); a synchronous body is unaffected.
        # process.exit: the page schedules real timers (its WebSocket retry),
        # and node will not exit on its own while any of them is pending.
        + "(async () => {\n" + body
        + "\nconsole.log(JSON.stringify(out));\nprocess.exit(0);\n})();\n"
    )
    script = script + "\n"
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


# ---------------------------------------------------------------------------
# page.html — the ChartPage shell (v1.1.0)
# ---------------------------------------------------------------------------

_LAYOUT = """
ctx.fetch = () => Promise.resolve({json: () => Promise.resolve({
  title: 'BTC desk', theme: 'dark',
  sync: {time: true, crosshair: true},
  rows: [['v1'], ['v2', 'v3']],
  views: [{id: 'v1', title: 'BTC 3m'}, {id: 'v2', title: 'BTC 5m'},
          {id: 'v3', title: 'ETH 5m'}],
})});
"""

_SETTLE = """
await new Promise(r => setTimeout(r, 20));      // let loadLayout() finish
const iframes = queryAll('iframe');
iframes.forEach(f => f.onload && f.onload());   // frames announce themselves
const win = id => iframes.find(f => f.src.includes('view=' + id)).contentWindow;
const fromFrame = (id, msg) => (docListeners.message || [])
  .forEach(fn => fn({source: win(id), data: Object.assign({view: id}, msg)}));
const clearAll = () => iframes.forEach(f => f.contentWindow._posted.length = 0);
"""


@pytest.fixture(scope="module")
def shell_result() -> dict:
    return _run(PAGE_HTML, _SETTLE + """
out.rowCount    = queryAll('.row').length;
out.cellCount   = queryAll('.cell').length;
out.frameCount  = iframes.length;
out.frameSrc    = iframes.map(f => f.src);
out.rowDividers = queryAll('.rdiv').length;
out.colDividers = queryAll('.cdiv').length;
out.title       = document.getElementById('page-title').textContent;

// the first chart starts active
out.activeCells = queryAll('.cell').filter(c => c._cls.has('active')).map(c => c.id);

// pointing at a chart makes it the active one
fromFrame('v3', {t: 'focus'});
out.activeAfterFocus = queryAll('.cell').filter(c => c._cls.has('active')).map(c => c.id);

// a range from the active chart reaches the others and does not come back
clearAll();
fromFrame('v3', {t: 'range', from: 100, to: 200});
out.v1FromActive = win('v1')._posted.slice();
out.v2FromActive = win('v2')._posted.slice();
out.v3FromActive = win('v3')._posted.slice();

// ...and one from a chart nobody is pointing at is ignored, however far its
// value has drifted: that is the rounding chase that made the charts shake
clearAll();
fromFrame('v1', {t: 'range', from: 137, to: 242});
out.v2FromIdle = win('v2')._posted.slice();

fromFrame('v1', {t: 'focus'});
fromFrame('v1', {t: 'range', from: 900, to: 1000});
out.v1Posted = win('v1')._posted.slice();
out.v2Posted = win('v2')._posted.slice();
out.v3Posted = win('v3')._posted.slice();

// crosshair relays the same way
win('v2')._posted.length = 0;
fromFrame('v1', {t: 'crosshair', time: 1700000000, price: 42});
out.v2Crosshair = win('v2')._posted.slice();

// turning a sync off stops the relay
ev('toggleSync("time")');
win('v2')._posted.length = 0;
fromFrame('v1', {t: 'range', from: 1, to: 2});
out.v2AfterTimeOff = win('v2')._posted.slice();
out.timeBtnOff = !document.getElementById('sync-time')._cls.has('on');

// a message whose source is not the frame it claims to be is ignored
ev('toggleSync("time")');
win('v2')._posted.length = 0;
(docListeners.message || []).forEach(fn =>
  fn({source: {}, data: {view: 'v1', t: 'range', from: 9, to: 9}}));
out.v2AfterForged = win('v2')._posted.slice();
""", extra_globals=_LAYOUT)


class TestChartPageShell:
    def test_shell_script_runs_clean(self, shell_result):
        assert shell_result["topLevelError"] is None

    def test_rows_and_cells_match_the_layout(self, shell_result):
        assert shell_result["rowCount"]  == 2       # [[v1], [v2, v3]]
        assert shell_result["cellCount"] == 3
        assert shell_result["frameCount"] == 3
        assert shell_result["title"] == "BTC desk"

    def test_each_frame_loads_its_own_view(self, shell_result):
        srcs = shell_result["frameSrc"]
        assert all(s.startswith("view?view=") for s in srcs)
        assert [s.split("view=")[1].split("&")[0] for s in srcs] == ["v1", "v2", "v3"]
        assert all("chrome=compact" in s for s in srcs)

    def test_dividers_exist_on_both_axes(self, shell_result):
        assert shell_result["rowDividers"] == 1     # between the two rows
        assert shell_result["colDividers"] == 1     # between v2 and v3

    def test_first_chart_starts_active(self, shell_result):
        assert shell_result["activeCells"] == ["cell-v1"]

    def test_focus_follows_the_clicked_chart(self, shell_result):
        assert shell_result["activeAfterFocus"] == ["cell-v3"]

    def test_a_range_reaches_the_others_but_not_the_sender(self, shell_result):
        relayed = {"t": "range", "from": 100, "to": 200}
        assert shell_result["v3FromActive"] == []          # never echoed back
        assert shell_result["v1FromActive"] == [relayed]
        assert shell_result["v2FromActive"] == [relayed]
        # and the same once the pointer moves to another chart
        assert shell_result["v1Posted"] == []
        assert shell_result["v2Posted"] == [{"t": "range", "from": 900, "to": 1000}]

    def test_only_the_chart_being_used_moves_the_others(self, shell_result):
        """Each chart snaps a range to its own bars, so if every chart could
        answer back they would chase each other's rounding and shake."""
        assert shell_result["v2FromIdle"] == []

    def test_crosshair_relays_time_and_price(self, shell_result):
        assert shell_result["v2Crosshair"] == [
            {"t": "crosshair", "time": 1700000000, "price": 42}
        ]

    def test_turning_a_sync_off_stops_the_relay(self, shell_result):
        assert shell_result["v2AfterTimeOff"] == []
        assert shell_result["timeBtnOff"] is True

    def test_a_forged_message_is_ignored(self, shell_result):
        """The view id must belong to the window that sent it — otherwise any
        page embedding this one could drive the charts."""
        assert shell_result["v2AfterForged"] == []


# ---------------------------------------------------------------------------
# index.html — the frame side of the page bridge (v1.1.0)
# ---------------------------------------------------------------------------

_EMBED = """
ctx.location.search = '?view=v2&chrome=compact&title=BTC%205m';
ctx.__toParent = [];
ctx.window.parent = {postMessage: m => ctx.__toParent.push(m)};
"""

_EMBED_BODY = """
ev('ws = {readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}');
ctx.__init = {type: 'init', title: 'BTC 5m', bars: [
    {time: 1700000000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1}],
  indicators: [], drawings: [], sourceTimeframe: '1m', displayTimeframe: '5m',
  timeframes: ['1m', '5m']};
ev('handleMsg(globalThis.__init)');

out.embedded  = ev('embedded');
out.viewId    = ev('viewId');
out.compact   = document.body._cls.has('compact');
out.wsUrl     = ev('String(ws && ws.url || "")');

// a click anywhere tells the shell this chart is the active one
(docListeners.mousedown || []).forEach(fn => fn({target: document.body}));
out.focusSent = ctx.__toParent.filter(m => m.t === 'focus');

// the shell can steer our range and crosshair
ctx.__toParent.length = 0;
(docListeners.message || []).forEach(fn =>
  fn({source: ctx.window.parent, data: {t: 'range', from: 10, to: 20}}));
out.echoedRange = ctx.__toParent.filter(m => m.t === 'range');

// a message from anywhere else is ignored
out.appliedForeign = null;
(docListeners.message || []).forEach(fn =>
  fn({source: {}, data: {t: 'range', from: 1, to: 2}}));
out.afterForeign = ctx.__toParent.filter(m => m.t === 'range');

// a layout change from the server is passed up to the shell
ev("handleMsg({type: 'layout_changed'})");
out.layoutRelayed = ctx.__toParent.filter(m => m.t === 'layout-changed').length;
"""


@pytest.fixture(scope="module")
def embed_result() -> dict:
    return _run(CHART_HTML, _EMBED_BODY, extra_globals=_EMBED)


class TestChartFrameBridge:
    def test_frame_script_runs_clean(self, embed_result):
        assert embed_result["topLevelError"] is None

    def test_it_knows_it_is_embedded(self, embed_result):
        assert embed_result["embedded"] is True
        assert embed_result["viewId"] == "v2"

    def test_compact_chrome_is_applied(self, embed_result):
        assert embed_result["compact"] is True

    def test_click_reports_focus_to_the_shell(self, embed_result):
        assert embed_result["focusSent"] == [{"view": "v2", "t": "focus"}]

    def test_a_relayed_range_is_not_echoed_back(self, embed_result):
        """Without the applyingSync guard the frames would ping-pong for ever."""
        assert embed_result["echoedRange"] == []

    def test_only_the_shell_may_steer_the_chart(self, embed_result):
        assert embed_result["afterForeign"] == []

    def test_layout_changes_are_passed_up(self, embed_result):
        assert embed_result["layoutRelayed"] == 1


def test_a_standalone_chart_is_not_embedded():
    """With no ?view= the page must behave exactly as it always has."""
    result = _run(CHART_HTML, """
out.embedded = ev('embedded');
out.viewId   = ev('viewId');
out.compact  = document.body._cls.has('compact');
""")
    assert result["topLevelError"] is None
    assert result["embedded"] is False
    assert result["viewId"] is None
    assert result["compact"] is False


# ---------------------------------------------------------------------------
# index.html — bar replay (v1.1.0)
# ---------------------------------------------------------------------------
# 1m source candles displayed at 5m, so a cursor inside a bucket has something
# finer to build the forming candle from.

_REPLAY_SETUP = """
const MIN = 60, T0 = 1700000000 - (1700000000 % 300);
const src = [];
for (let i = 0; i < 60; i++) {
  src.push({time: T0 + i * MIN, open: 100 + i, high: 110 + i,
            low: 90 + i, close: 105 + i, volume: 10});
}
// the 5m candles the server would have sent
const bars = [];
for (let b = 0; b < 12; b++) {
  const slice = src.slice(b * 5, b * 5 + 5);
  bars.push({time: slice[0].time, open: slice[0].open,
             high: Math.max(...slice.map(x => x.high)),
             low: Math.min(...slice.map(x => x.low)),
             close: slice[4].close, volume: 50});
}
ctx.__src = src; ctx.__bars = bars; ctx.__T0 = T0;

ev('ws = {readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}');
ctx.__init = {type: 'init', title: 'BTC', bars: bars, drawings: [],
  sourceTimeframe: '1m', displayTimeframe: '5m', timeframes: ['1m', '5m'],
  indicators: [{name: 'EMA', data: bars.map(b => ({time: b.time, value: b.close})),
                color: '#fff', overlay: true, pane: 0, lineWidth: 1,
                seriesType: 'line', group: 'EMA'}]};
ev('handleMsg(globalThis.__init)');

// what the series was told to show, most recent call wins
const shown = () => {
  const calls = ev('mainSeries').setData._calls || [];
  return calls.length ? calls[calls.length - 1] : null;
};
"""


@pytest.fixture(scope="module")
def replay_result() -> dict:
    return _run(CHART_HTML, _REPLAY_SETUP + """
// arming asks the server for the source candles
ev('replayToggle()');
out.armRequest = ctx.__sent.filter(m => m.type === 'replay_arm');
out.picking    = document.body._cls.has('rp-picking');

ev('handleMsg({type: "replay_data", sourceTimeframe: "1m", stepSeconds: 60,'
   + ' sourceBars: globalThis.__src})');
out.stepSec = ev('replay.stepSec');

// pick the 4th candle (index 3) as the start
ev('replayPick(globalThis.__bars[3].time)');
out.active     = ev('replay.active');
out.barVisible = ev('document.getElementById("rp-bar")._cls.has("open")');
out.cursor     = ev('replay.cursor');
out.shownBars  = ev('replay.shownBars');

// the cursor sits on a boundary, so nothing is forming yet
out.formingAtBoundary = ev('buildForming(4, replay.cursor, 300)');

// one 1m step into the next 5m candle
ev('replayStep(1)');
out.cursorAfterStep = ev('replay.cursor');
out.forming1 = ev('buildForming(4, replay.cursor, 300)');
ev('replayStep(1)');
out.forming2 = ev('buildForming(4, replay.cursor, 300)');

// five 1m steps close that candle
ev('replayStep(1); replayStep(1); replayStep(1)');
out.shownAfterClose = ev('replay.shownBars');
out.cursorClosed    = ev('replay.cursor');

// indicators never run past the last CLOSED candle
out.indCut = ev('(() => {const m = Object.values(indMeta)[0];'
              + ' const d = m.series.setData._last || [];'
              + ' return d.length ? d[d.length - 1].time : null;})()');
out.lastClosedOpen = ev('allBars[replay.shownBars - 1].time');

// stepping back
ev('replayStep(-1)');
out.cursorBack = ev('replay.cursor');

// play / pause flips the button and the flag
ev('replayPlayPause()');
out.playing    = ev('replay.playing');
out.playLabel  = document.getElementById('rp-play').textContent;
ev('replayPlayPause()');
out.pausedFlag = ev('replay.playing');

// the cursor cannot run past the data
ev('replaySeek(globalThis.__T0 + 999999)');
out.clamped = ev('replay.cursor === replayBounds().last');

// leaving replay puts everything back
ev('replayExit()');
out.exited      = ev('replay.active');
out.barHidden   = !ev('document.getElementById("rp-bar")._cls.has("open")');
out.restoredLen = ev('(mainSeries.setData._last || []).length');
""")


class TestBarReplay:
    def test_page_script_runs_clean(self, replay_result):
        assert replay_result["topLevelError"] is None

    def test_arming_asks_the_server_for_source_candles(self, replay_result):
        assert replay_result["armRequest"] == [{"type": "replay_arm"}]
        assert replay_result["picking"] is True
        assert replay_result["stepSec"] == 60

    def test_picking_a_bar_starts_the_replay_there(self, replay_result):
        assert replay_result["active"] is True
        assert replay_result["barVisible"] is True
        # the picked candle is complete, so four candles are shown
        assert replay_result["shownBars"] == 4

    def test_nothing_is_forming_exactly_on_a_boundary(self, replay_result):
        assert replay_result["formingAtBoundary"] is None

    def test_the_forming_candle_grows_a_minute_at_a_time(self, replay_result):
        """The whole point of stepping by the source timeframe: a 5m candle is
        watched being built out of its 1m candles."""
        one, two = replay_result["forming1"], replay_result["forming2"]
        assert one["volume"] == 10 and two["volume"] == 20
        assert two["high"] >= one["high"]
        assert one["open"] == two["open"]           # the bucket's open is fixed

    def test_a_candle_closes_on_its_boundary(self, replay_result):
        assert replay_result["shownAfterClose"] == 5

    def test_indicators_stop_at_the_last_closed_candle(self, replay_result):
        assert replay_result["indCut"] == replay_result["lastClosedOpen"]

    def test_stepping_back_moves_the_cursor_back(self, replay_result):
        assert replay_result["cursorBack"] == replay_result["cursorClosed"] - 60

    def test_play_and_pause(self, replay_result):
        assert replay_result["playing"] is True
        assert replay_result["playLabel"] == "⏸"
        assert replay_result["pausedFlag"] is False

    def test_the_cursor_is_clamped_to_the_data(self, replay_result):
        assert replay_result["clamped"] is True

    def test_leaving_restores_every_candle(self, replay_result):
        assert replay_result["exited"] is False
        assert replay_result["barHidden"] is True
        assert replay_result["restoredLen"] == 12


def test_replay_hides_drawings_that_have_not_happened_yet():
    """A trade that opens after the cursor must not be on the chart, and one
    still open must not show the label that says how it ends."""
    result = _run(CHART_HTML, _REPLAY_SETUP + """
const t = ctx.__bars.map(b => b.time);
ctx.__d = [
  {id: 'past',   type: 'position_box', open_time: t[1], close_time: t[2], label: 'TP +2R'},
  {id: 'open',   type: 'position_box', open_time: t[2], close_time: t[9], label: 'SL -1R'},
  {id: 'future', type: 'position_box', open_time: t[8], close_time: t[9], label: 'TP +3R'},
  {id: 'line',   type: 'hline',        price: 100},
];
ev('globalThis.__d.forEach(d => { drawings[d.id] = d; })');

ev('replayToggle()');
ev('handleMsg({type: "replay_data", stepSeconds: 60, sourceBars: globalThis.__src})');
ev('replayPick(globalThis.__bars[4].time)');

out.visible = ev('replayVisibleDrawings().map(d => d.id)');
out.clipped = ev('(() => {const d = replayVisibleDrawings().find(x => x.id === "open");'
              + ' return {close: d.close_time, label: d.label};})()');
out.cursor  = ev('replay.cursor');
out.stored  = ev('drawings.open.close_time');    // the original is untouched

ev('replayExit()');
out.afterExit = ev('replayVisibleDrawings().map(d => d.id)');
""")
    assert result["topLevelError"] is None
    assert result["visible"] == ["past", "open", "line"]      # 'future' withheld
    assert result["clipped"]["close"] == result["cursor"]     # stops at the cursor
    assert result["clipped"]["label"] == ""                   # outcome not leaked
    assert result["stored"] != result["cursor"]               # only the copy is clipped
    assert sorted(result["afterExit"]) == ["future", "line", "open", "past"]


def test_an_embedded_chart_hides_its_own_replay_controls():
    """In a page the shell owns the cursor; a second set of controls in each
    frame would let the charts drift apart."""
    result = _run(CHART_HTML, """
out.btn = document.getElementById('rp-btn').style.display;
out.sep = document.getElementById('rp-sep').style.display;
""", extra_globals="ctx.location.search = '?view=v1&chrome=compact';"
                   "ctx.window.parent = {postMessage: () => {}};")
    assert result["btn"] == "none"
    assert result["sep"] == "none"


# ---------------------------------------------------------------------------
# page.html — replay across every chart at once (v1.1.0)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def page_replay_result() -> dict:
    return _run(PAGE_HTML, _SETTLE + """
const posted = id => win(id)._posted.slice();
const clear  = () => ['v1','v2','v3'].forEach(id => win(id)._posted.length = 0);

// arming reaches every frame and asks each what it can replay
ev('replayToggle()');
out.armBroadcast = posted('v2');
out.noteShown    = document.getElementById('rp-note')._cls.has('show');

// the frames answer: v1 is 1m under a 3m display, v2 and v3 are 1m under 5m
fromFrame('v1', {t: 'replay-info', first: 1000, last: 9000, step: 60,  tf: 180});
fromFrame('v2', {t: 'replay-info', first: 1200, last: 8000, step: 60,  tf: 300});
fromFrame('v3', {t: 'replay-info', first:  900, last: 9500, step: 300, tf: 300});
out.step  = ev('replay.step');
out.first = ev('replay.first');
out.last  = ev('replay.last');

// clicking a candle on one chart starts the replay everywhere
clear();
fromFrame('v2', {t: 'replay-pick', time: 3000});
out.active      = ev('replay.active');
out.pickedTo1   = posted('v1');
out.pickedTo3   = posted('v3');
out.controlsOpen = document.getElementById('rp-controls')._cls.has('open');

// one step moves the shared cursor by the finest step on the page
clear();
ev('replayStep(1)');
out.cursorAfterStep = ev('replay.cursor');
out.stepTo1 = posted('v1');
out.stepTo3 = posted('v3');

// the cursor stays inside the span every chart has data for
ev('replaySeek(99999)');
out.clampedHigh = ev('replay.cursor');
ev('replaySeek(0)');
out.clampedLow  = ev('replay.cursor');

// play / pause
ev('replayPlayPause()');
out.playing   = ev('replay.playing');
out.playLabel = document.getElementById('rp-play').textContent;
ev('replayPlayPause()');
out.paused    = ev('replay.playing');

// leaving tells every frame to restore itself
clear();
ev('replayExit()');
out.exitTo1  = posted('v1');
out.exitTo2  = posted('v2');
out.inactive = ev('replay.active');
""", extra_globals=_LAYOUT)


class TestPageWideReplay:
    def test_shell_script_runs_clean(self, page_replay_result):
        assert page_replay_result["topLevelError"] is None

    def test_arming_reaches_every_chart(self, page_replay_result):
        assert {m["t"] for m in page_replay_result["armBroadcast"]} == {
            "replay-arm", "replay-info"
        }
        assert page_replay_result["noteShown"] is True

    def test_the_page_steps_as_finely_as_its_finest_chart(self, page_replay_result):
        assert page_replay_result["step"] == 60          # min(60, 60, 300)

    def test_the_range_is_what_every_chart_can_cover(self, page_replay_result):
        assert page_replay_result["first"] == 1200       # max of the firsts
        assert page_replay_result["last"]  == 8000       # min of the lasts

    def test_picking_on_one_chart_starts_them_all(self, page_replay_result):
        assert page_replay_result["active"] is True
        assert page_replay_result["controlsOpen"] is True
        assert page_replay_result["pickedTo1"] == [{"t": "replay-cursor", "time": 3000}]
        assert page_replay_result["pickedTo3"] == [{"t": "replay-cursor", "time": 3000}]

    def test_one_step_moves_every_chart_to_the_same_instant(self, page_replay_result):
        """Not 'one candle each' — a 3m and a 5m chart would drift apart."""
        assert page_replay_result["cursorAfterStep"] == 3060
        assert page_replay_result["stepTo1"] == [{"t": "replay-cursor", "time": 3060}]
        assert page_replay_result["stepTo3"] == [{"t": "replay-cursor", "time": 3060}]

    def test_the_cursor_is_clamped_to_the_shared_span(self, page_replay_result):
        assert page_replay_result["clampedHigh"] == 8000
        assert page_replay_result["clampedLow"]  == 1200

    def test_play_and_pause(self, page_replay_result):
        assert page_replay_result["playing"] is True
        assert page_replay_result["playLabel"] == "⏸"
        assert page_replay_result["paused"] is False

    def test_leaving_restores_every_chart(self, page_replay_result):
        assert page_replay_result["exitTo1"] == [{"t": "replay-exit"}]
        assert page_replay_result["exitTo2"] == [{"t": "replay-exit"}]
        assert page_replay_result["inactive"] is False


def test_a_timeframe_switch_during_replay_keeps_the_cursor():
    """A switch arrives as a fresh init; without re-cutting it, the chart would
    silently show the whole history again while still 'in' replay."""
    result = _run(CHART_HTML, _REPLAY_SETUP + """
ev('replayToggle()');
ev('handleMsg({type: "replay_data", stepSeconds: 60, sourceBars: globalThis.__src})');
ev('replayPick(globalThis.__bars[3].time)');
const cursor = ev('replay.cursor');

// the server answers a timeframe switch with a whole new init
ctx.__init2 = Object.assign({}, ctx.__init, {displayTimeframe: '15m'});
ev('handleMsg(globalThis.__init2)');

out.stillActive = ev('replay.active');
out.cursorKept  = ev('replay.cursor') === cursor;
out.shownCount  = ev('(mainSeries.setData._last || []).length');
out.totalBars   = ev('allBars.length');
""")
    assert result["topLevelError"] is None
    assert result["stillActive"] is True
    assert result["cursorKept"] is True
    assert result["shownCount"] < result["totalBars"]      # still cut at the cursor


def test_live_candles_do_not_draw_past_the_replay_cursor():
    """A chart can be replaying and still be fed by a live feed. Those candles
    belong to a time ahead of the cursor, so they must not be drawn."""
    result = _run(CHART_HTML, _REPLAY_SETUP + """
ev('replayToggle()');
ev('handleMsg({type: "replay_data", stepSeconds: 60, sourceBars: globalThis.__src})');
ev('replayPick(globalThis.__bars[3].time)');
const drawnAtPick = ev('(mainSeries.setData._last || []).length');
const barsAtPick  = ev('allBars.length');

// two live candles arrive mid-replay
const next = ctx.__bars[ctx.__bars.length - 1].time;
ev(`streamBar({time: ${next + 300}, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1})`);
ev(`streamBar({time: ${next + 600}, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1})`);

out.drawnUnchanged = ev('(mainSeries.setData._last || []).length') === drawnAtPick;
out.updatesDuring  = ev('mainSeries.update._calls.length');
out.recorded       = ev('allBars.length') - barsAtPick;

// leaving replay shows everything, including what arrived meanwhile
ev('replayExit()');
out.afterExit = ev('(mainSeries.setData._last || []).length');
out.total     = ev('allBars.length');
""")
    assert result["topLevelError"] is None
    assert result["drawnUnchanged"] is True      # the view did not move
    assert result["updatesDuring"] == 0          # nothing drawn ahead of the cursor
    assert result["recorded"] == 2               # but both were kept
    assert result["afterExit"] == result["total"]


# ---------------------------------------------------------------------------
# The sync echo — charts must not relay a range back and forth for ever
# ---------------------------------------------------------------------------

_EMBED_SYNC = """
ctx.location.search = '?view=v2&chrome=compact';
ctx.__toParent = [];
ctx.window.parent = {postMessage: m => ctx.__toParent.push(m)};
"""


def test_a_relayed_range_is_not_reported_back_when_the_chart_notices_it():
    """The chart library reports a range change on a *later* frame, so a flag
    set and cleared around the call that caused it is already off by then. That
    is what made two heavy charts wedge a tab: each kept relaying the other's
    range straight back."""
    result = _run(CHART_HTML, """
ev('handleMsg({type: "init", title: "x", bars: [{time: 1700000000, open: 1,'
   + ' high: 2, low: 0.5, close: 1.5, volume: 1}], indicators: [], drawings: []})');
const chart = mainChartStub();
const fireRange = r => chart._cbs.range.forEach(cb => cb(r));
const fireCross = p => chart._cbs.cross.forEach(cb => cb(p));

// nobody is pointing at this chart, so it never drives the others
ctx.__toParent.length = 0;
fireRange({from: 10, to: 20});
out.idleMove = ctx.__toParent.filter(m => m.t === 'range');

// the pointer arrives — now this chart is the one being used
(docListeners.mouseover || []).forEach(fn => fn({}));
out.focusSent = ctx.__toParent.filter(m => m.t === 'focus').length;

// the shell tells us where to look; the chart snaps that to its own bars and
// reports something slightly different a frame later. That must stay quiet.
ev('applyPageRange(100, 200)');
ctx.__toParent.length = 0;
fireRange({from: 100.4, to: 200.6});        // ← snapped, and late
out.echoed = ctx.__toParent.filter(m => m.t === 'range');

// a real scroll by this user still reports
ev('syncMuteUntil = 0');
fireRange({from: 300, to: 400});
out.realMove = ctx.__toParent.filter(m => m.t === 'range');

// and it goes quiet again once the pointer leaves
(docListeners.mouseout || []).forEach(fn => fn({relatedTarget: null}));
ctx.__toParent.length = 0;
fireRange({from: 700, to: 800});
out.afterPointerLeft = ctx.__toParent.filter(m => m.t === 'range');

// same rule for the crosshair
ctx.__toParent.length = 0;
ev('applyPageCrosshair(1700000000, 42)');
fireCross({time: 1700000000, seriesData: new Map()});
out.crossEchoed = ctx.__toParent.filter(m => m.t === 'crosshair');
fireCross({time: 1700000600, seriesData: new Map()});
out.crossReal = ctx.__toParent.filter(m => m.t === 'crosshair');
""", extra_globals=_EMBED_SYNC)
    assert result["topLevelError"] is None
    assert result["idleMove"] == []                     # not the chart in use
    assert result["focusSent"] >= 1
    assert result["echoed"] == []                       # the loop is broken
    assert len(result["realMove"]) == 1
    assert result["realMove"][0]["from"] == 300
    assert result["afterPointerLeft"] == []
    assert result["crossEchoed"] == []
    assert len(result["crossReal"]) == 1


def test_the_shell_drops_a_range_it_just_relayed():
    """Second line of defence: even if a frame does echo, the shell must not
    pass the same range round again."""
    result = _run(PAGE_HTML, _SETTLE + """
const posted = id => win(id)._posted.slice();
fromFrame('v1', {t: 'focus'});
fromFrame('v1', {t: 'range', from: 100, to: 200});
out.firstRelay = posted('v2');

// v1 is still the chart in use, and sends the identical range again
win('v2')._posted.length = 0;
fromFrame('v1', {t: 'range', from: 100, to: 200});
out.repeatRelay = posted('v2');

// a genuinely different range still goes round
win('v2')._posted.length = 0;
fromFrame('v1', {t: 'range', from: 500, to: 600});
out.newRelay = posted('v2');
""", extra_globals=_LAYOUT)
    assert result["topLevelError"] is None
    assert result["firstRelay"] == [{"t": "range", "from": 100, "to": 200}]
    assert result["repeatRelay"] == []                  # not passed round again
    assert result["newRelay"] == [{"t": "range", "from": 500, "to": 600}]


# ---------------------------------------------------------------------------
# "Open on Candle Chart" — the whole page goes to the trade, and stays there
# ---------------------------------------------------------------------------

def test_navigating_to_a_trade_takes_the_page_with_it():
    """The report links one chart. On a page the others should follow, and
    nothing should drag the view back afterwards."""
    result = _run(CHART_HTML, """
const bars = [];
for (let i = 0; i < 400; i++) {
  bars.push({time: 1700000000 + i * 300, open: 1, high: 2, low: 0.5,
             close: 1.5, volume: 1});
}
ctx.__bars = bars;
ev('handleMsg({type: "init", title: "x", bars: globalThis.__bars,'
   + ' indicators: [], drawings: []})');
const chart = mainChartStub();

// the server replays the navigate the report asked for
ctx.__toParent.length = 0;
ev('handleMsg({type: "navigate_to_candle", timestamp: ' + bars[100].time + '})');
out.announced = ctx.__toParent.filter(m => m.t === 'navigate');
out.jumped = chart._state.logical || null;

// a jump must not be mistaken for a user scroll and broadcast as a range
(docListeners.mouseover || []).forEach(fn => fn({}));
ctx.__toParent.length = 0;
ev('scrollToTime(' + bars[200].time + ')');
chart._cbs.range.forEach(cb => cb({from: 1, to: 2}));   // the snap, a frame later
out.rangeAfterJump = ctx.__toParent.filter(m => m.t === 'range');

// and the other charts are told, without answering back
ctx.__toParent.length = 0;
(docListeners.message || []).forEach(fn => fn({source: ctx.window.parent,
  data: {t: 'navigate', time: bars[300].time}}));
out.relayedBack = ctx.__toParent.filter(m => m.t === 'navigate');
""", extra_globals=_EMBED_SYNC)
    assert result["topLevelError"] is None
    assert len(result["announced"]) == 1              # the page is told
    # candle 100 centred: 50 either side
    assert result["jumped"] == {"from": 50, "to": 150}
    assert result["rangeAfterJump"] == []             # not read as a user scroll
    assert result["relayedBack"] == []                # a relayed jump is silent


def test_the_shell_passes_a_trade_jump_to_every_other_chart():
    result = _run(PAGE_HTML, _SETTLE + """
clearAll();
fromFrame('v1', {t: 'navigate', time: 1700001234});
out.v2 = win('v2')._posted.slice();
out.v3 = win('v3')._posted.slice();
out.v1 = win('v1')._posted.slice();
""", extra_globals=_LAYOUT)
    assert result["topLevelError"] is None
    assert result["v2"] == [{"t": "navigate", "time": 1700001234}]
    assert result["v3"] == [{"t": "navigate", "time": 1700001234}]
    assert result["v1"] == []
