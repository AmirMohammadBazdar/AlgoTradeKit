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
    // As lightweight-charts does: a coordinate for a time the scale has, and
    // null for anything between candles.
    timeToCoordinate: t => {
      const i = (state.barTimes || []).indexOf(t);
      return i === -1 ? null : i * 10;
    },
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


def test_the_timeframe_menu_is_placed_clear_of_the_toolbar():
    """#tbar scrolls horizontally, and an overflow other than visible on one
    axis clips the other too — an absolutely positioned dropdown inside it is
    cut off at the toolbar's height. It must be positioned against the
    viewport instead, under the button."""
    result = _run(CHART_HTML, """
ev('handleMsg({type: "init", title: "x", bars: [], indicators: [], drawings: [],'
   + ' sourceTimeframe: "1m", displayTimeframe: "5m",'
   + ' timeframes: ["1m", "3m", "5m", "15m", "1h"]})');
const menu = document.getElementById('tf-menu');
const btn  = document.getElementById('tf-btn');
btn.getBoundingClientRect = () => ({left: 120, bottom: 30, top: 4, width: 40, height: 26});

ev('toggleTfMenu()');
out.open   = menu._cls.has('open');
out.left   = menu.style.left;
out.top    = menu.style.top;
out.count  = menu.children.length;

ev('toggleTfMenu()');
out.closed = !menu._cls.has('open');
""")
    assert result["topLevelError"] is None
    assert result["open"] is True
    assert result["left"] == "120px"           # under the button
    assert result["top"] == "34px"             # clear of the toolbar
    assert result["count"] == 5                # every timeframe, not a sliver
    assert result["closed"] is True


def test_the_active_chart_is_visibly_marked():
    """The chart that drives the others has to be tellable at a glance."""
    import AlgoTradeKit.visual.server as _srv
    page = (_srv.STATIC_DIR / "page.html").read_text(encoding="utf-8")
    assert ".cell.active::after" in page
    assert "inset 0 0 0 2px var(--accent)" in page, "a hairline is not visible enough"


def test_a_drawing_between_candles_still_has_a_position():
    """Switching timeframe leaves drawings on times that are no longer candle
    starts. Without interpolation they lose their coordinate and vanish, which
    is what made boxes look wrong after a switch."""
    result = _run(CHART_HTML, """
const bars = [];
for (let i = 0; i < 10; i++) bars.push({time: 1000 + i * 300, open: 1, high: 2,
                                        low: 0.5, close: 1.5, volume: 1});
ctx.__bars = bars;
ev('handleMsg({type: "init", title: "x", bars: globalThis.__bars,'
   + ' indicators: [], drawings: []})');
// tell the stubbed scale which times it knows, as a real chart would
mainChartStub()._state.barTimes = bars.map(b => b.time);

out.onCandle  = ev('timeToX(1300)');     // candle 1  → 10
out.between   = ev('timeToX(1450)');     // half way between candles 1 and 2
out.before    = ev('timeToX(850)');      // before the first candle
out.after     = ev('timeToX(3850)');     // past the last one
out.noBars    = ev('(() => {const keep = allBars; allBars = []; '
                 + 'const v = timeToX(1450); allBars = keep; return v;})()');
""")
    assert result["topLevelError"] is None
    assert result["onCandle"] == 10                  # straight from the scale
    assert result["between"] == 15                   # interpolated, not lost
    assert result["before"] == -5                    # extrapolated backwards
    assert result["after"] == 95                     # and forwards
    assert result["noBars"] is None                  # nothing to interpolate from


# ---------------------------------------------------------------------------
# Replay: a shape that outlives the cursor is cut short, not hidden
# ---------------------------------------------------------------------------

def test_a_zone_that_extends_past_the_cursor_is_drawn_up_to_it():
    """A zone marked from candle N to N+10 exists as soon as N is reached. It
    must appear, ending at the cursor — hiding it until N+10 loses information
    the trader already had, and drawing it whole gives away the future."""
    result = _run(CHART_HTML, _REPLAY_SETUP + """
const t = ctx.__bars.map(b => b.time);
ctx.__d = [
  {id: 'zone',   type: 'box',       time1: t[2], price1: 1, time2: t[9], price2: 2},
  {id: 'ahead',  type: 'box',       time1: t[8], price1: 1, time2: t[9], price2: 2},
  {id: 'past',   type: 'box',       time1: t[1], price1: 1, time2: t[3], price2: 2},
  {id: 'line',   type: 'trendline', time1: t[2], price1: 1, time2: t[9], price2: 2},
  {id: 'level',  type: 'hline',     price: 100},
  {id: 'trade',  type: 'position_box', open_time: t[2], close_time: t[9],
   label: 'TP +2R'},
];
ev('globalThis.__d.forEach(d => { drawings[d.id] = d; })');

ev('replayToggle()');
ev('handleMsg({type: "replay_data", stepSeconds: 60, sourceBars: globalThis.__src})');
ev('replayPick(globalThis.__bars[4].time)');       // cursor sits at candle 5

out.cursor  = ev('replay.cursor');
out.visible = ev('replayVisibleDrawings().map(d => d.id)');
out.shapes  = ev('(() => {const m = {};'
              + ' replayVisibleDrawings().forEach(d => m[d.id] ='
              + '   {t1: d.time1 ?? null, t2: d.time2 ?? null,'
              + '    close: d.close_time ?? null, label: d.label ?? null});'
              + ' return m;})()');
out.storedZoneEnd = ev('drawings.zone.time2');

// A real price scale only answers for the candles the series was given, so
// teach the stub exactly that. Without it the test cannot see the failure:
// the far corner of a zone lands on a candle replay has not revealed, the
// scale has no position for it, and the renderer drops the whole shape.
const onScale = (ev('mainSeries').setData._last || []).map(b => b.time);
mainChartStub()._state.barTimes = onScale;
out.shownCount = onScale.length;
out.totalBars  = ev('allBars.length');
out.xAtClip    = ev('timeToX(replay.cursor)');
out.xAtStart   = ev('timeToX(replay.shown[0].time)');
// what the old code did: measure against every candle, including hidden ones
out.xBeyond    = ev('timeToX(allBars[allBars.length - 1].time)');

ev('replayExit()');
out.afterExit = ev('replayVisibleDrawings().map(d => d.id).sort()');
out.zoneWhole = ev('replayVisibleDrawings().find(d => d.id === "zone").time2');
""")
    assert result["topLevelError"] is None
    cursor = result["cursor"]

    # the zone is on the chart, ending at the cursor rather than at N+10
    assert "zone" in result["visible"]
    assert result["shapes"]["zone"]["t2"] == cursor
    assert result["shapes"]["zone"]["t1"] < cursor          # it started earlier

    # a zone that has not begun is still withheld
    assert "ahead" not in result["visible"]

    # one that already finished is untouched
    assert result["shapes"]["past"]["t2"] < cursor

    # trendlines are spans too
    assert result["shapes"]["line"]["t2"] == cursor

    # timeless shapes are unaffected
    assert "level" in result["visible"]

    # a trade still open keeps its outcome hidden
    assert result["shapes"]["trade"]["close"] == cursor
    assert result["shapes"]["trade"]["label"] == ""

    # only the drawn copy is clipped, and the clipped edge has a real position
    assert result["storedZoneEnd"] > cursor
    assert result["shownCount"] < result["totalBars"]      # replay is hiding some
    assert isinstance(result["xAtClip"], (int, float))     # the clipped edge lands
    assert result["xAtClip"] > result["xAtStart"]          # and to the right of the start
    assert isinstance(result["xBeyond"], (int, float))     # extrapolated, not lost

    # leaving replay restores every shape in full
    assert result["afterExit"] == ["ahead", "level", "line", "past", "trade", "zone"]
    assert result["zoneWhole"] == result["storedZoneEnd"]


# ---------------------------------------------------------------------------
# index.html — the screenshot mode (v1.2.0)
# ---------------------------------------------------------------------------
# BTCUSDT 15m from 2026-10-14 08:00 to 14:00 UTC, shown in Asia/Tehran (+03:30):
# what a chart picture's headless browser opens.

_T0800, _T1400 = 1791964800, 1791986400

# Every chart the page builds records the options applied to it, and its line series.
_RECORD_OPTIONS = """
ctx.__applied = [];
ctx.__lines = [];
ctx.LightweightCharts = autoStub('LightweightCharts', {createChart: () => {
  const chart = makeChartStub();
  const seen = [];
  ctx.__applied.push(seen);
  chart.applyOptions = o => { seen.push(o); };
  chart.addLineSeries = opts => {
    const series = autoStub('line');
    ctx.__lines.push({opts: opts || {}, series});
    return series;
  };
  return chart;
}});
"""

_SNAPSHOT = _RECORD_OPTIONS + f"""
ctx.location.search = '?snapshot=1&from={_T0800}&to={_T1400}&tz=Asia%2FTehran'
  + '&title=BTCUSDT%20%C2%B7%2015m%20%C2%B7%202026-10-14%2011%3A30%20%E2%80%93%2017%3A30';
"""

_SNAPSHOT_INIT = f"""
const bars = [];
for (let t = {_T0800} - 20 * 900; t <= {_T1400}; t += 900)
  bars.push({{time: t, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1}});
ctx.__init = {{type: 'init', title: 'BTCUSDT', bars,
  indicators: [{{name: 'RSI(14)', color: '#7b1fa2', overlay: false, pane: 1, lineWidth: 1,
                 seriesType: 'line',
                 data: bars.slice(14).map(b => ({{time: b.time, value: 50}}))}}],
  drawings: [], sourceTimeframe: '15m', timeframes: ['15m']}};
ev('ws = {{readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}}');
const frames = () => new Promise(r => setTimeout(r, 30));
"""


@pytest.fixture(scope="module")
def snapshot_result() -> dict:
    return _run(CHART_HTML, _SNAPSHOT_INIT + f"""
out.snapshot = ev('snapshot');
out.bodyClass = document.body._cls.has('snapshot');
out.hidden = ['tbar', 'sb', 'rp-hint', 'ctx-menu', 'ind-overlay', 'settings-overlay']
  .map(id => [id, document.getElementById(id).style.display]);
out.readyAtLoad = ctx.window.__atkSnapshotReady;

ev('handleMsg(globalThis.__init)');
out.readyRightAfterInit = ctx.window.__atkSnapshotReady;
await frames();
out.readyAfterFrames = ctx.window.__atkSnapshotReady;

out.title = document.getElementById('chart-title').textContent;
out.paneCount = ev('Object.keys(panes).length');
out.ranges = ev('Object.values(panes).map(p => p.chart.timeScale().getVisibleRange())');

// the zone, through the formatters the page gave every pane
const zoned = ctx.__applied.flat().filter(o => o.timeScale && o.timeScale.tickMarkFormatter);
out.zonedPanes = zoned.length;
const tick = zoned[0].timeScale.tickMarkFormatter, cross = zoned[0].localization.timeFormatter;
out.tick1400 = tick({_T1400}, 3);
out.tickDay = tick({_T1400}, 2);
out.tickMonth = tick({_T1400}, 1);
out.tickSeconds = tick({_T1400} + 5, 4);
out.cross1400 = cross({_T1400});
out.readBack = ev('snapTicks.map(t => t[2])');

// the sub-pane got every candle's time, as whitespace, to line up with the candles
out.whitespace = ctx.__lines.filter(l => l.opts.visible === false)
  .map(l => l.series.setData._last);
out.barTimes = bars.map(b => b.time);
out.sent = ctx.__sent.slice();
""", extra_globals=_SNAPSHOT)


class TestScreenshotMode:
    def test_page_script_runs_clean(self, snapshot_result):
        assert snapshot_result["topLevelError"] is None
        assert snapshot_result["snapshot"] is True

    def test_controls_are_hidden(self, snapshot_result):
        assert snapshot_result["bodyClass"] is True
        assert dict(snapshot_result["hidden"]) == {
            "tbar": "none", "sb": "none", "rp-hint": "none", "ctx-menu": "none",
            "ind-overlay": "none", "settings-overlay": "none",
        }

    def test_ready_only_after_the_data_is_painted(self, snapshot_result):
        assert snapshot_result["readyAtLoad"] is False
        assert snapshot_result["readyRightAfterInit"] is False    # frames still to come
        assert snapshot_result["readyAfterFrames"] is True

    def test_the_title_line_replaces_the_chart_title(self, snapshot_result):
        assert snapshot_result["title"] == "BTCUSDT · 15m · 2026-10-14 11:30 – 17:30"

    def test_every_pane_shows_from_to(self, snapshot_result):
        assert snapshot_result["paneCount"] == 2                   # the candles and RSI
        assert snapshot_result["ranges"] == [{"from": _T0800, "to": _T1400}] * 2

    def test_the_axis_reads_in_the_zone(self, snapshot_result):
        assert snapshot_result["zonedPanes"] == 2
        assert snapshot_result["tick1400"] == "17:30"
        assert snapshot_result["tickDay"] == "14"
        assert snapshot_result["tickMonth"] == "Oct"
        assert snapshot_result["tickSeconds"] == "17:30:05"
        assert snapshot_result["cross1400"] == "2026-10-14 17:30"
        assert "17:30" in snapshot_result["readBack"]

    def test_a_sub_pane_lines_up_with_the_candles(self, snapshot_result):
        # RSI's points start 14 candles in; its pane also gets every candle's time
        (whitespace,) = snapshot_result["whitespace"]
        assert whitespace == [{"time": t} for t in snapshot_result["barTimes"]]

    def test_it_sends_the_server_nothing(self, snapshot_result):
        assert snapshot_result["sent"] == []


# A time range sets the stub's logical range as the library would: candles 0 to 24.
_LOGICAL_FROM_TIME = """
const _plainCreate = ctx.LightweightCharts.createChart;
ctx.LightweightCharts = autoStub('LightweightCharts', {createChart: (...a) => {
  const chart = _plainCreate(...a);
  const scale = chart.timeScale();
  const setRange = scale.setVisibleRange;
  scale.setVisibleRange = r => { setRange(r); chart._state.logical = {from: 0, to: 24}; };
  return chart;
}});
"""


def test_a_picture_leaves_room_for_an_open_position():
    """An OPEN's picture ends at the entry: the live position's lines need room after
    the last candle, and get the plot's right edge."""
    position = (f"{{id: 'pos', type: 'live_position', open_time: {_T1400}, entry_price: 1.5,"
                " stop_loss: 1, direction: 'long', next_tp: 2}")
    result = _run(CHART_HTML, _SNAPSHOT_INIT + f"""
ctx.__init.drawings = [{position}];
ev('handleMsg(globalThis.__init)');
await frames();
out.logical = ev('Object.values(panes).map(p => p.chart.timeScale().getVisibleLogicalRange())');
out.ready = ctx.window.__atkSnapshotReady;
""", extra_globals=_SNAPSHOT + _LOGICAL_FROM_TIME)
    assert result["topLevelError"] is None and result["ready"] is True
    # 25 candles shown, so an eighth of them is 3: at least 8 candles of room
    assert result["logical"] == [{"from": 0, "to": 32}] * 2


def test_a_picture_without_a_position_ends_at_its_last_candle():
    result = _run(CHART_HTML, _SNAPSHOT_INIT + """
ev('handleMsg(globalThis.__init)');
await frames();
out.logical = ev('Object.values(panes).map(p => p.chart.timeScale().getVisibleLogicalRange())');
""", extra_globals=_SNAPSHOT + _LOGICAL_FROM_TIME)
    assert result["logical"] == [{"from": 0, "to": 24}] * 2


def test_the_interactive_page_has_no_screenshot_mode():
    """Without ?snapshot=1 nothing of it runs: no flag, no hidden controls, UTC axis."""
    result = _run(CHART_HTML, _SNAPSHOT_INIT + """
ev('handleMsg(globalThis.__init)');
await frames();
out.snapshot = ev('snapshot');
out.flag = ctx.window.__atkSnapshotReady === undefined ? 'unset' : ctx.window.__atkSnapshotReady;
out.bodyClass = document.body._cls.has('snapshot');
out.toolbar = document.getElementById('tbar').style.display;
out.zoned = ctx.__applied.flat().filter(o => o.timeScale && o.timeScale.tickMarkFormatter).length;
out.title = document.getElementById('chart-title').textContent;
""", extra_globals=_RECORD_OPTIONS)
    assert result["topLevelError"] is None
    assert result["snapshot"] is False
    assert result["flag"] == "unset"
    assert result["bodyClass"] is False
    assert result["toolbar"] == ""
    assert result["zoned"] == 0
    assert result["title"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# index.html / page.html — colours and the grid (v1.3.0)
# ---------------------------------------------------------------------------
# Three layers, each over the one before: the theme's default, the program's
# style (the init's, for the theme the chart starts in), and the browser's own
# choice from the Settings panel, saved per theme.

# Every chart the page builds: the options it was made with, what was applied to
# it after, and its candle and volume series.
_STYLE_RECORD = """
ctx.__charts = [];
ctx.LightweightCharts = autoStub('LightweightCharts', {createChart: (el, opts) => {
  const chart = makeChartStub();
  const rec = {made: opts || {}, applied: [], candles: null, volume: null};
  ctx.__charts.push(rec);
  chart.applyOptions = o => { rec.applied.push(o); };
  chart.addCandlestickSeries = o => {
    const series = autoStub('candles');
    rec.candles = {made: o || {}, applied: []};
    series.applyOptions = more => { rec.candles.applied.push(more); };
    return series;
  };
  chart.addHistogramSeries = () => {
    const series = autoStub('volume');
    rec.volume = series;
    return series;
  };
  return chart;
}});
// What a chart or a series shows now: the options it was made with, then every
// applyOptions on top, merged the way lightweight-charts merges them.
ctx.__merge = (made, applied) => {
  const deep = (to, from) => {
    Object.entries(from || {}).forEach(([k, v]) => {
      to[k] = v && typeof v === 'object' && !Array.isArray(v)
        ? deep(to[k] && typeof to[k] === 'object' ? {...to[k]} : {}, v) : v;
    });
    return to;
  };
  return applied.reduce((acc, o) => deep(acc, o), deep({}, made));
};
"""

# A browser's storage, which a test can read and fill
_STORAGE = """
ctx.__store = {};
ctx.window.localStorage = {
  getItem: k => (k in ctx.__store ? ctx.__store[k] : null),
  setItem: (k, v) => { ctx.__store[k] = String(v); },
};
"""

_STYLE_KEY = "atk.chart.style.v1"

# An up candle, then a down one; `look()` is what the newest main chart shows.
_STYLE_INIT = """
const bars = [{time: 1700000000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1},
              {time: 1700000060, open: 1.5, high: 2, low: 0.5, close: 1, volume: 1}];
const init = (extra = {}) => ev('handleMsg')(Object.assign(
  {type: 'init', title: 'BTC', theme: 'dark', bars, indicators: [], drawings: [],
   timeframes: []}, extra));
const look = () => {
  const m = ctx.__charts[ctx.__charts.length - 1];
  const opts = ctx.__merge(m.made, m.applied);
  const candles = ctx.__merge(m.candles.made, m.candles.applied);
  return {background: opts.layout.background.color,
          grid: [opts.grid.vertLines.visible, opts.grid.horzLines.visible],
          up: candles.upColor, down: candles.downColor,
          borders: [candles.borderUpColor, candles.borderDownColor],
          wicks: [candles.wickUpColor, candles.wickDownColor],
          volume: (m.volume.setData._last || []).map(p => p.color)};
};
const saved = () => JSON.parse(ctx.__store['atk.chart.style.v1'] || 'null');
"""

_DEFAULT_LOOK = {
    "background": "#0d1117", "grid": [True, True], "up": "#3fb950", "down": "#f85149",
    "borders": ["#3fb950", "#f85149"], "wicks": ["#3fb950", "#f85149"],
    "volume": ["#3fb95055", "#f8514955"],
}


@pytest.fixture(scope="module")
def colours_result() -> dict:
    """A chart with no style of its own, in a browser that has saved nothing:
    every control of the panel in turn, in both themes."""
    return _run(CHART_HTML, _STYLE_INIT + """
init();
out.start = look();

ev("onStyleInput('background', '#000000')");
out.background = look().background;
ev("onStyleInput('upColor', '#ffffff')");
ev("onStyleInput('downColor', '#808080')");
out.candles = look();
ev("onGridToggle(false)");
out.gridOff = look().grid;
out.savedDark = saved();

ev('toggleTheme()');                          // ☀: light keeps its own colours
out.light = look();
out.savedAfterSun = saved();
ev("onStyleInput('background', '#eeeeee')");
out.lightChanged = look().background;
ev('toggleTheme()');                          // and dark has kept its own
out.darkAgain = look();

ev('resetStyle()');                           // forgets dark's, never light's
out.afterReset = look();
out.savedAfterReset = saved();
""", extra_globals=_STYLE_RECORD + _STORAGE)


class TestColours:
    def test_page_script_runs_clean(self, colours_result):
        assert colours_result["topLevelError"] is None

    def test_a_chart_starts_in_its_themes_colours(self, colours_result):
        assert colours_result["start"] == _DEFAULT_LOOK

    def test_the_background_control_sets_the_charts_background(self, colours_result):
        assert colours_result["background"] == "#000000"

    def test_the_candle_controls_set_body_border_wick_and_volume(self, colours_result):
        look = colours_result["candles"]
        assert (look["up"], look["down"]) == ("#ffffff", "#808080")
        assert look["borders"] == ["#ffffff", "#808080"]
        assert look["wicks"] == ["#ffffff", "#808080"]
        assert look["volume"] == ["#ffffff55", "#80808055"]       # at a third, as before

    def test_grid_off_hides_both_lines(self, colours_result):
        assert colours_result["gridOff"] == [False, False]

    def test_only_what_was_changed_is_saved_for_the_theme(self, colours_result):
        assert colours_result["savedDark"] == {
            "dark": {"background": "#000000", "upColor": "#ffffff", "downColor": "#808080",
                     "grid": False},
            "light": {},
        }

    def test_each_theme_keeps_its_own_colours(self, colours_result):
        assert colours_result["light"] == dict(_DEFAULT_LOOK, background="#ffffff")
        assert colours_result["savedAfterSun"] == colours_result["savedDark"]  # ☀ not saved
        assert colours_result["lightChanged"] == "#eeeeee"
        dark = colours_result["darkAgain"]
        assert (dark["background"], dark["up"], dark["grid"]) == ("#000000", "#ffffff",
                                                                  [False, False])

    def test_reset_forgets_the_current_themes_choice_only(self, colours_result):
        assert colours_result["afterReset"] == _DEFAULT_LOOK
        assert colours_result["savedAfterReset"] == {"dark": {},
                                                     "light": {"background": "#eeeeee"}}


@pytest.fixture(scope="module")
def program_style_result() -> dict:
    """A program's style, with this browser's own choice saved over part of it."""
    return _run(CHART_HTML, _STYLE_INIT + f"""
ctx.__store[{_STYLE_KEY!r}] = JSON.stringify({{dark: {{background: '#000000'}}}});
init({{style: {{background: '#101820', up_color: '#ffffff', grid: false}}}});
out.start = look();
ev('resetStyle()');
out.afterReset = look();
ev('toggleTheme()');
out.light = look();
ev('toggleTheme()');

// chart.set_style(): the program's whole style, the browser's choice still on top
ev("onStyleInput('downColor', '#808080')");
ev('handleMsg')({{type: 'style', style: {{background: '#202020', down_color: '#00ff00'}}}});
out.afterSetStyle = look();
""", extra_globals=_STYLE_RECORD + _STORAGE)


class TestProgramColours:
    def test_the_browsers_choice_sits_over_the_programs(self, program_style_result):
        look = program_style_result["start"]
        assert look["background"] == "#000000"                    # the browser's
        assert (look["up"], look["grid"]) == ("#ffffff", [False, False])   # the program's
        assert look["down"] == "#f85149"                           # the theme's

    def test_reset_shows_the_programs_colours_again(self, program_style_result):
        look = program_style_result["afterReset"]
        assert (look["background"], look["up"], look["grid"]) == ("#101820", "#ffffff",
                                                                  [False, False])

    def test_q1_2_a_the_programs_colours_belong_to_its_theme(self, program_style_result):
        """☀ on a chart whose program set dark colours: light as it has always been."""
        assert program_style_result["light"] == dict(_DEFAULT_LOOK, background="#ffffff")

    def test_set_style_replaces_the_programs_style_under_the_browsers(
            self, program_style_result):
        look = program_style_result["afterSetStyle"]
        assert look["background"] == "#202020"
        assert look["down"] == "#808080"                           # the browser's, on top
        assert look["up"] == "#3fb950" and look["grid"] == [True, True]   # no longer set


def test_a_colour_with_alpha_or_three_digits_reaches_the_panel_and_the_volume():
    result = _run(CHART_HTML, _STYLE_INIT + """
init({style: {background: '#abc', up_color: '#11223344'}});
ev('toggleSettings()');
out.panel = ['sc-background', 'sc-up', 'sc-down']
  .map(id => document.getElementById(id).value);
out.grid = document.getElementById('stog-grid').checked;
out.look = look();
""", extra_globals=_STYLE_RECORD + _STORAGE)
    assert result["topLevelError"] is None
    assert result["panel"] == ["#aabbcc", "#112233", "#f85149"]   # what a picker can hold
    assert result["grid"] is True
    assert result["look"]["background"] == "#abc"                 # the colour as written
    assert result["look"]["up"] == "#11223344"
    assert result["look"]["volume"] == ["#11223355", "#f8514955"]  # 55 in place of 44


def test_a_selected_drawings_hollow_handles_take_the_charts_background():
    result = _run(CHART_HTML, _STYLE_INIT + """
init({style: {background: '#101820'}});
const pen = {save() {}, restore() {}, beginPath() {}, arc() {}, fill() {}, stroke() {}};
ev('drawHandle')(pen, 10, 10, '#ff0000', false);
out.hollow = pen.fillStyle;
ev('drawHandle')(pen, 10, 10, '#ff0000', true);
out.filled = pen.fillStyle;
""", extra_globals=_STYLE_RECORD + _STORAGE)
    assert (result["hollow"], result["filled"]) == ("#101820", "#ff0000")


def test_a_picture_shows_the_programs_colours_and_never_touches_storage():
    stored = json.dumps({"dark": {"background": "#000000", "grid": False}})
    result = _run(CHART_HTML, _STYLE_INIT + f"""
ctx.__store[{_STYLE_KEY!r}] = {json.dumps(stored)};
init({{style: {{background: '#101820'}}}});
out.look = look();
ev("onStyleInput('background', '#ff0000')");
out.stored = ctx.__store[{_STYLE_KEY!r}];
""", extra_globals=_STYLE_RECORD + _STORAGE
        + f"ctx.location.search = '?snapshot=1&from={_T0800}&to={_T1400}&tz=UTC';\n")
    assert result["topLevelError"] is None
    assert result["look"]["background"] == "#101820"
    assert result["look"]["grid"] == [True, True]
    assert result["stored"] == stored


@pytest.mark.parametrize("storage", [
    # a private window, or site data blocked: the property itself throws
    "Object.defineProperty(ctx.window, 'localStorage', "
    "{get() { throw new Error('SecurityError'); }});\n",
    # storage there, but every call refused
    "ctx.window.localStorage = {getItem() { throw new Error('SecurityError'); },"
    " setItem() { throw new Error('QuotaExceededError'); }};\n",
], ids=["no-storage", "refused"])
def test_storage_that_throws_leaves_a_working_chart(storage):
    result = _run(CHART_HTML, _STYLE_INIT + """
init({style: {background: '#101820'}});
out.start = look().background;
ev("onStyleInput('background', '#000000')");
ev("onGridToggle(false)");
out.changed = look();
ev('resetStyle()');
out.reset = look().background;
""", extra_globals=_STYLE_RECORD + storage)
    assert result["topLevelError"] is None
    assert result["start"] == "#101820"
    assert result["changed"]["background"] == "#000000"     # applies, though not kept
    assert result["changed"]["grid"] == [False, False]
    assert result["reset"] == "#101820"


def test_a_frame_tells_the_page_when_a_colour_is_picked_and_follows_the_others():
    result = _run(CHART_HTML, _STYLE_INIT + f"""
init();
ctx.__toParent.length = 0;
ev("onStyleInput('background', '#000000')");     // the picker moving: this chart only
out.whileMoving = ctx.__toParent.slice();
ev('onStylePicked()');                           // a colour picked: the page follows
ev('onGridToggle(false)');
ev('resetStyle()');
out.told = ctx.__toParent.slice();

// another chart of the page saved the grid off, and the shell says so
ctx.__toParent.length = 0;
ctx.__store[{_STYLE_KEY!r}] = JSON.stringify({{dark: {{grid: false}}, light: {{}}}});
(docListeners.message || []).forEach(fn =>
  fn({{source: ctx.window.parent, data: {{t: 'style'}}}}));
out.followed = look().grid;
out.panelGrid = document.getElementById('stog-grid').checked;
out.echoed = ctx.__toParent.slice();
""", extra_globals=_STYLE_RECORD + _STORAGE + _EMBED)
    assert result["topLevelError"] is None
    assert result["whileMoving"] == []
    assert result["told"] == [{"view": "v2", "t": "style"}] * 3
    assert result["followed"] == [False, False]
    assert result["panelGrid"] is False
    assert result["echoed"] == []                   # a relayed change is not sent back


def test_the_shell_passes_a_colour_change_to_every_other_chart():
    result = _run(PAGE_HTML, _SETTLE + """
clearAll();
fromFrame('v2', {t: 'style'});
out.posted = Object.fromEntries(['v1', 'v2', 'v3'].map(id => [id, win(id)._posted.slice()]));
""", extra_globals=_LAYOUT)
    assert result["topLevelError"] is None
    assert result["posted"] == {"v1": [{"t": "style"}], "v2": [], "v3": [{"t": "style"}]}


# ---------------------------------------------------------------------------
# index.html / page.html — edits on the page reach Python (v1.3.0, Part 2)
# ---------------------------------------------------------------------------
# A 5m display of 1m data: ten 5m candles 300 s apart, 10 px apart on screen.
# Prices map linearly: y = 1000 - 10 * price, so 100 is the top of the 400 px pane.

# Elements that keep their listeners, a socket that keeps its address, and a
# canvas pen that records what is drawn.
_EDIT_HARNESS = """
['draw-cv', 'panes'].forEach(id => {
  const el = document.getElementById(id);
  el._ls = {};
  el.addEventListener = (type, fn) => { (el._ls[type] ??= []).push(fn); };
});
ctx.__fireOn = (id, type, ev) =>
  (document.getElementById(id)._ls[type] || []).forEach(fn => fn(ev));
ctx.__wsUrls = [];
ctx.WebSocket = function (url) {
  ctx.__wsUrls.push(url);
  return {readyState: 0, send() {}, close() {}};
};
ctx.__penCalls = [];
ctx.__pen = new Proxy({}, {
  get: (t, k) => (k in t ? t[k] : (...a) => { ctx.__penCalls.push([String(k), ...a]);
                                              return {width: 10}; }),
  set: (t, k, v) => { t[k] = v; return true; },
});
document.getElementById('draw-cv').getContext = () => ctx.__pen;
ctx.__confirms = [];
ctx.__confirmAnswer = true;
ctx.confirm = text => { ctx.__confirms.push(text); return ctx.__confirmAnswer; };
"""

_EDIT_SETUP = """
const bars = [];
for (let i = 0; i < 10; i++) bars.push({time: 1000 + i * 300, open: 90, high: 95, low: 85,
                                        close: 92, volume: 1});
ctx.__bars = bars;
ev('ws = {readyState: 1, send: m => globalThis.__sent.push(JSON.parse(m))}');
const init = (extra = {}) => {
  ev('handleMsg')(Object.assign({type: 'init', title: 'BTC', bars, indicators: [],
    drawings: [], sourceTimeframe: '1m', displayTimeframe: '5m', timeframes: ['1m', '5m']},
    extra));
  const chart = mainChartStub();
  chart._state.barTimes = bars.map(b => b.time);
  const scale = chart.timeScale();
  scale.coordinateToTime = x => 1000 + x * 30;
  scale.coordinateToLogical = x => x / 10;
  const series = ev('mainSeries');
  series.priceToCoordinate = p => 1000 - p * 10;
  series.coordinateToPrice = y => (1000 - y) / 10;
};
// A box from candle 2 to candle 6 (x 20 to 60), price 90 to 80 (y 100 to 200)
const box = (extra = {}) => Object.assign({type: 'box', id: 'zone', time1: 1600, price1: 90,
  time2: 2800, price2: 80, color: '#26a69a', borderColor: '#26a69a', opacity: 0.2, label: 'Zone',
  locked: false, source: 'server'}, extra);
const press = (x, y) => ctx.__fireOn('draw-cv', 'mousedown',
  {button: 0, clientX: x, clientY: y, stopPropagation() {}});
const dragTo = (x, y) => fire('mousemove', document.body, {clientX: x, clientY: y});
const release = () => fire('mouseup', document.body);
const lastSent = type => ctx.__sent.filter(m => m.type === type).slice(-1)[0];
const rightClick = (id, x, y) => {
  const e = {clientX: x, clientY: y, prevented: false,
             preventDefault() { this.prevented = true; }, stopPropagation() {}};
  ctx.__fireOn(id, 'contextmenu', e);
  return e;
};
const shown = id => document.getElementById(id).style.display;
"""


def _edits(body: str, extra_globals: str = "") -> dict:
    return _run(CHART_HTML, _EDIT_SETUP + body, extra_globals=_EDIT_HARNESS + extra_globals)


def test_a_price_only_box_moves_only_its_prices():
    result = _edits("""
init({drawings: [box({editable: ['price']})]});
out.rightEdge = ev('hitTest(60, 150)');         // its time edge: nothing to grab
out.inside    = ev('hitTest(40, 150)');         // its inside would move it: no
out.topEdge   = ev('hitTest(40, 100)');

press(40, 100); dragTo(40, 80); release();      // the top edge, 2 up in price
const top = lastSent('drawing_updated').drawing;
out.afterTop = [top.time1, top.time2, top.price1, top.price2];

press(60, 80); dragTo(90, 70); release();       // a corner, up and to the right
const corner = lastSent('drawing_updated').drawing;
out.afterCorner = [corner.time1, corner.time2, corner.price1, corner.price2];
""")
    assert result["topLevelError"] is None
    assert result["rightEdge"] is None and result["inside"] is None
    assert result["topEdge"] == {"id": "zone", "handle": "tc"}
    assert result["afterTop"] == [1600, 2800, 92, 80]
    assert result["afterCorner"] == [1600, 2800, 93, 80]   # vertically only


def test_an_editable_box_moves_as_before():
    result = _edits("""
init({drawings: [box()]});
press(60, 150); dragTo(90, 150); release();     // its right edge, three candles on
const d = lastSent('drawing_updated').drawing;
out.after = [d.time1, d.time2, d.price1, d.price2];
""")
    assert result["after"] == [1600, 3700, 90, 80]


def test_the_menu_follows_editable_and_deletable():
    result = _edits("""
init({drawings: [box({editable: false, deletable: false, locked: true, title: 'Sell POI'})]});
rightClick('draw-cv', 40, 150);
out.fixed = {menu: shown('ctx-menu'), title: document.getElementById('ctx-title').textContent,
             unlock: shown('ctx-lock-row'), del: shown('ctx-delete-row')};
ev('ctxTargetId = "zone"'); ev('ctxToggleLock()'); ev('ctxDelete()');
out.fixedAfter = {locked: ev('drawings.zone.locked'), there: ev('"zone" in drawings'),
                  sent: ctx.__sent.length};

ev('handleMsg')({type: 'add_drawing', drawing: box({id: 'free', time1: 2200, time2: 2500,
                                                     price1: 98, price2: 96})});
rightClick('draw-cv', 55, 30);                  // inside the second box
out.free = {title: shown('ctx-title'), unlock: shown('ctx-lock-row'), del: shown('ctx-delete-row')};
""")
    assert result["fixed"] == {"menu": "block", "title": "Sell POI", "unlock": "none",
                               "del": "none"}
    assert result["fixedAfter"] == {"locked": True, "there": True, "sent": 0}
    assert result["free"] == {"title": "none", "unlock": "", "del": ""}


def test_deleting_a_keyed_drawing_asks_first():
    result = _edits("""
init({drawings: [box({key: 'poi:sell:1', title: 'Sell POI 4019.5–4026.7'}),
                 box({id: 'labelled', key: 'poi:buy:2', label: 'Buy POI', time1: 1000,
                      time2: 1300}),
                 box({id: 'plain', label: '', time1: 1000, time2: 1300, key: 'z:3'}),
                 box({id: 'unkeyed', time1: 1000, time2: 1300})]});
ctx.__confirmAnswer = false;
ev('ctxTargetId = "zone"'); ev('ctxDelete()');
out.kept = ev('"zone" in drawings') && !lastSent('drawing_deleted');
ctx.__confirmAnswer = true;
['zone', 'labelled', 'plain', 'unkeyed'].forEach(id => {
  ev(`ctxTargetId = "${id}"`);
  ev('ctxDelete()');
});
out.confirms = ctx.__confirms;
out.deleted = ctx.__sent.filter(m => m.type === 'drawing_deleted').map(m => m.id);
""")
    assert result["kept"] is True
    assert result["confirms"] == [
        "Delete Sell POI 4019.5–4026.7? The program is told.",
        "Delete Sell POI 4019.5–4026.7? The program is told.",
        "Delete Buy POI? The program is told.",
        "Delete box? The program is told.",
    ]                                           # the unkeyed box goes without asking
    assert result["deleted"] == ["zone", "labelled", "plain", "unkeyed"]


def test_the_candle_menu_sends_the_candles_opening():
    result = _edits("""
init({actions: ['Mark here', 'Restart here']});
const e = rightClick('panes', 34, 150);         // over candle 3 (x 30), price 85
out.menu = shown('candle-menu');
out.items = document.getElementById('candle-menu').children.map(c => c.textContent);
out.prevented = e.prevented;
document.getElementById('candle-menu').children[1].onclick();
out.sent = lastSent('chart_action');
out.closed = shown('candle-menu');

rightClick('panes', 112, 120);                  // two candles past the last one
document.getElementById('candle-menu').children[0].onclick();
out.future = lastSent('chart_action');

ev('handleMsg')({type: 'actions', actions: []});
out.noneLeft = rightClick('panes', 34, 150).prevented;
""")
    assert result["topLevelError"] is None
    assert result["menu"] == "block" and result["prevented"] is True
    assert result["items"] == ["Mark here", "Restart here"]
    assert result["sent"] == {"type": "chart_action", "label": "Restart here", "time": 1900,
                              "price": 85}
    assert result["closed"] == "none"
    assert result["future"]["time"] == 1000 + 11 * 300       # a candle not there yet
    assert result["noneLeft"] is False                       # the browser's own menu


def test_without_actions_a_right_click_is_left_alone():
    result = _edits("""
init();
const e = rightClick('panes', 34, 150);
out.prevented = e.prevented;
out.menu = shown('candle-menu');
""")
    assert result["prevented"] is False
    assert result["menu"] in ("", "none")


def test_a_vertical_line_sits_on_the_candle_that_holds_its_time():
    result = _edits("""
init({drawings: [{type: 'vline', id: 'v', time: 2300, color: '#e3b341', label: 'Restart',
                  style: 'dashed', locked: true, source: 'server', key: 'restart:1',
                  editable: false, deletable: true, title: null}]});
ctx.__penCalls.length = 0;
ev('redrawAll()');
const calls = ctx.__penCalls;
const i = calls.findIndex(c => c[0] === 'moveTo' && c[1] === 40);
out.line = [calls[i], calls[i + 1]];
out.dash = calls.slice(0, i).filter(c => c[0] === 'setLineDash').slice(-1)[0];
out.label = calls.find(c => c[0] === 'fillText' && c[1] === 'Restart');
out.hit = ev('hitTestAny(42, 300)');
out.miss = ev('hitTestAny(55, 300)');
out.drag = ev('hitTest(42, 300)');
rightClick('draw-cv', 42, 300);
out.menu = {unlock: shown('ctx-lock-row'), del: shown('ctx-delete-row')};
""")
    assert result["topLevelError"] is None
    assert result["line"] == [["moveTo", 40, 0], ["lineTo", 40, 400]]   # 2300 is in candle 4
    assert result["dash"] == ["setLineDash", [5, 4]]
    assert result["label"] is not None
    assert result["hit"] == {"id": "v", "handle": "body"}
    assert result["miss"] is None and result["drag"] is None             # never dragged
    assert result["menu"] == {"unlock": "none", "del": ""}


def test_a_page_without_the_key_only_looks():
    result = _edits("""
init({actions: ['Mark here'], drawings: [box({key: 'z', title: 'Zone'}),
     {type: 'box', id: 'mine', time1: 1000, price1: 99, time2: 1300, price2: 97, label: '',
      locked: false, source: 'user', color: '#fff'}]});
ev('handleMsg')({type: 'access', edit: false});
out.body = document.body._cls.has('no-edit');
out.tfTitle = document.getElementById('tf-btn').title;
out.dragProgram = ev('hitTest(40, 100)');
out.dragMine = ev('hitTest(10, 30)');
rightClick('draw-cv', 40, 150);
out.programMenu = {unlock: shown('ctx-lock-row'), del: shown('ctx-delete-row'),
  rename: document.getElementById('ctx-rename-row')._cls.has('disabled')};
ev('ctxTargetId = "zone"'); ev('ctxDelete()');
out.zoneThere = ev('"zone" in drawings');
out.candleMenu = rightClick('panes', 34, 150).prevented;
ctx.__sent.length = 0;
ev('selectTimeframe("1m")'); ev('toggleIndPanel()'); ev('removeIndicator("EMA")');
out.sent = ctx.__sent.slice();
out.panelOpen = document.getElementById('ind-overlay')._cls.has('open');
out.status = document.getElementById('sbmsg').textContent;
ev('ctxTargetId = "mine"'); ev('ctxDelete()');
out.mineGone = !ev('"mine" in drawings');
""")
    assert result["topLevelError"] is None
    assert result["body"] is True and result["tfTitle"] == "needs the edit key"
    assert result["dragProgram"] is None
    assert result["dragMine"] == {"id": "mine", "handle": "br"}   # your own lines move
    assert result["programMenu"] == {"unlock": "none", "del": "none", "rename": True}
    assert result["zoneThere"] is True
    assert result["candleMenu"] is False
    assert result["sent"] == []
    assert result["panelOpen"] is False
    assert result["status"] == "needs the edit key"
    assert result["mineGone"] is True


def test_a_refusal_says_why_in_the_status_bar():
    result = _edits("""
init({drawings: [box({key: 'z'})]});
ev('handleMsg')({type: 'update_drawing', drawing: {id: 'zone', price1: 90}});
ev('handleMsg')({type: 'edit_refused', id: 'zone', message: 'too tall'});
out.status = document.getElementById('sbmsg').textContent;
out.price1 = ev('drawings.zone.price1');
""")
    assert result["status"] == "Refused: too tall"
    assert result["price1"] == 90


def test_the_page_gives_its_key_when_it_connects():
    result = _edits("""
out.urls = ctx.__wsUrls.slice();
""", extra_globals=_EMBED.replace("title=BTC%205m'", "title=BTC%205m&key=s%203cret'"))
    assert result["urls"][-1] == "ws://vps.example:9100/ws?view=v2&key=s+3cret"


def test_the_shell_passes_its_key_to_every_frame():
    keyed = _run(PAGE_HTML, _SETTLE + "out.src = iframes.map(f => f.src);",
                 extra_globals=_LAYOUT + "ctx.location.search = '?key=s3cret';\n")
    plain = _run(PAGE_HTML, _SETTLE + "out.src = iframes.map(f => f.src);",
                 extra_globals=_LAYOUT)
    assert all(src.endswith("&key=s3cret") for src in keyed["src"]) and len(keyed["src"]) == 3
    assert not any("key=" in src for src in plain["src"])


def test_the_programs_buttons_ask_first_and_only_an_editor_has_them():
    """``chart.add_button`` (v1.3.0): «Re-run with edits» is one."""
    result = _edits("""
const question = 'Re-run with the saved edits? The session stops and starts again.';
init({buttons: [{label: 'Re-run with edits', confirm: question}, {label: 'Plain', confirm: null}]});
const bar = document.getElementById('prog-btns');
out.labels = bar.children.map(b => b.textContent);
ctx.__confirmAnswer = false;
bar.children[0].onclick();
out.afterNo = ctx.__sent.filter(m => m.type === 'button');
ctx.__confirmAnswer = true;
bar.children[0].onclick();
bar.children[1].onclick();
out.sent = ctx.__sent.filter(m => m.type === 'button');
out.confirms = ctx.__confirms.slice();
ev('handleMsg')({type: 'access', edit: false});
out.viewer = bar.children.length;
ev('handleMsg')({type: 'buttons', buttons: [{label: 'Another'}]});
out.viewerLater = bar.children.length;
""")
    assert result["topLevelError"] is None
    assert result["labels"] == ["Re-run with edits", "Plain"]
    assert result["afterNo"] == []
    assert result["sent"] == [{"type": "button", "label": "Re-run with edits"},
                              {"type": "button", "label": "Plain"}]
    question = "Re-run with the saved edits? The session stops and starts again."
    assert result["confirms"] == [question, question]
    assert result["viewer"] == 0 and result["viewerLater"] == 0


# A box reaching into the future (v1.3.0, found in the user's check of Part 3): the
# library's coordinateToTime answers only where it has a candle, so a drag that read
# a time for the box's far edge gave up, and the box could not be resized at all.
_ONLY_ON_CANDLES = """
ev('mainChart').timeScale().coordinateToTime = x =>        // as lightweight-charts does
  (x >= 0 && x <= 90 ? 1000 + Math.round(x / 10) * 300 : null);
"""


def test_a_box_reaching_past_the_last_candle_can_be_resized():
    result = _edits("""
init({drawings: [box({editable: ['price'], time2: 1000 + 12 * 300}),          // to x 120
                 box({id: 'free', time1: 1600, time2: 4600, price1: 99, price2: 97})]});
""" + _ONLY_ON_CANDLES + """
press(70, 100); dragTo(70, 80); release();       // the price-only box's top edge, up 2
const top = lastSent('drawing_updated').drawing;
out.priceOnly = [top.time1, top.time2, top.price1, top.price2];

press(120, 20); dragTo(140, 20); release();      // the other box's right edge, further out
const right = lastSent('drawing_updated').drawing;
out.further = [right.time1, right.time2, right.price1, right.price2];
""")
    assert result["topLevelError"] is None
    assert result["priceOnly"] == [1600, 4600, 92, 80]      # its edges past the candles kept
    assert result["further"] == [1600, 5200, 99, 97]        # 3700 + 50 px at 30 s a pixel


def test_a_corner_drag_keeps_the_edges_it_does_not_move():
    result = _edits("""
init({drawings: [box({time1: 1650, time2: 2750, price1: 90.25, price2: 80.125})]});
""" + _ONLY_ON_CANDLES + """
press(60, 98); dragTo(70, 90); release();        // the top-right corner
const d = lastSent('drawing_updated').drawing;
out.after = [d.time1, d.time2, d.price1, d.price2];
""")
    after = result["after"]
    assert (after[0], after[3]) == (1650, 80.125)           # left and bottom exactly as they were
    assert after[1] == 3100 and after[2] == 91


def test_a_box_edge_can_be_grabbed_away_from_its_handles():
    """A zone reaching into the future has its middle handles off the screen: its
    edges answer anywhere along them (v1.3.0, the user's check of Part 3)."""
    result = _edits("""
init({drawings: [box({editable: ['price']})]});      // x 20 to 60, y 100 to 200
out.alongTop  = ev('hitTest(30, 101)');              // between the handles at 20 and 40
out.alongLeft = ev('hitTest(20, 130)');              // a time edge, and this box is price only
press(30, 101); dragTo(30, 80); release();
const d = lastSent('drawing_updated').drawing;
out.after = [d.time1, d.time2, d.price1, d.price2];
""")
    assert result["alongTop"] == {"id": "zone", "handle": "tc"}
    assert result["alongLeft"] is None
    assert result["after"] == [1600, 2800, 92, 80]


def test_a_view_address_without_its_view_opens_the_page():
    """A ChartPage's frame is ``/view?view=<id>``.  Typed without the view — the
    user's check of Part 4: ``/view?key=…`` stayed blank — it opens the page, with
    the key; a frame, and a chart served on its own, stay where they are."""
    go = ("ctx.location.pathname = '/view';\n"
          "ctx.location.replace = u => { ctx.__replaced = u; };\n")
    read = "out.to = ctx.__replaced === undefined ? null : ctx.__replaced;"
    keyed = _run(CHART_HTML, read, extra_globals=go + "ctx.location.search = '?key=s 3';\n")
    bare = _run(CHART_HTML, read, extra_globals=go)
    framed = _run(CHART_HTML, read,
                  extra_globals=go + "ctx.location.search = '?view=v1&key=k';\n")
    alone = _run(CHART_HTML, read, extra_globals=go.replace("'/view'", "'/'")
                 + "ctx.location.search = '?key=k';\n")
    assert keyed["to"] == "/?key=s%203"
    assert bare["to"] == "/"
    assert framed["to"] is None and alone["to"] is None


# ---------------------------------------------------------------------------
# v1.3.0 Part 4b — go to a date and time, the brand, the countdown
# ---------------------------------------------------------------------------

#: 200 five-minute candles from T0; `chart` is the main chart's stub.
_TOOLS_INIT = """
const T0 = 1793000100;
const bars = [];
for (let i = 0; i < 200; i++) {
  bars.push({time: T0 + i * 300, open: 1, high: 2, low: 0.5, close: 1.5 + (i % 2) * 0.1,
             volume: 1});
}
ev('handleMsg')({type: 'init', title: 'BTC', bars, indicators: [], drawings: [],
                 timeframes: ['5m'], sourceTimeframe: '5m', displayTimeframe: '5m'});
const chart = mainChartStub();
let cross = null;
chart.setCrosshairPosition = (price, time) => { cross = [price, time]; };
const status = () => document.getElementById('sbmsg').textContent;
"""


def _utc_minute(sec: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d %H:%M")


def test_go_to_centres_the_candle_that_holds_the_time_at_the_zoom_the_chart_has():
    result = _run(CHART_HTML, _TOOLS_INIT + """
chart._state.logical = {from: 10, to: 70};             // a view 60 candles wide
ctx.__toParent.length = 0;
ev('goToTime')(T0 + 120 * 300 + 170, {announce: true});  // 2 min 50 s into candle 120
out.range = chart._state.logical;
out.cross = cross;
out.told = ctx.__toParent.filter(m => m.t === 'navigate');
out.status = status();
""", extra_globals=_EMBED_SYNC)
    t0 = 1793000100
    assert result["topLevelError"] is None
    assert result["range"] == {"from": 90, "to": 150}          # candle 120 in the middle
    assert result["cross"] == [1.5, t0 + 120 * 300]           # the crosshair on it
    assert result["told"] == [{"view": "v2", "t": "navigate", "time": t0 + 120 * 300 + 170,
                               "keep": True}]
    assert result["status"] == ""


def test_go_to_before_the_first_after_the_last_and_past_the_replay():
    result = _run(CHART_HTML, _TOOLS_INIT + """
ev('goToTime')(T0 - 86400);
out.before = [chart._state.logical, status()];
ev('goToTime')(T0 + 400 * 300);
out.after = [chart._state.logical, status()];
ev('replay').active = true;
ev('replay').shown = bars.slice(0, 50);
ev('goToTime')(T0 + 80 * 300);
out.replay = [chart._state.logical, status()];
""")
    t0 = 1793000100
    before, after, replay = result["before"], result["after"], result["replay"]
    assert before == [{"from": -50, "to": 50},
                      f"{_utc_minute(t0 - 86400)} is before the first candle on this chart "
                      f"({_utc_minute(t0)})"]
    assert after == [{"from": 149, "to": 249},
                     f"{_utc_minute(t0 + 400 * 300)} is after the last candle on this chart "
                     f"({_utc_minute(t0 + 199 * 300)})"]
    assert replay == [{"from": -1, "to": 99},
                      f"the replay has not reached {_utc_minute(t0 + 80 * 300)}"]


def test_a_go_to_relayed_by_the_shell_keeps_this_charts_zoom_and_is_not_sent_back():
    result = _run(CHART_HTML, _TOOLS_INIT + """
chart._state.logical = {from: 0, to: 20};
ctx.__toParent.length = 0;
(docListeners.message || []).forEach(fn => fn({source: ctx.window.parent,
  data: {t: 'navigate', time: T0 + 100 * 300, keep: true}}));
out.range = chart._state.logical;
out.sentBack = ctx.__toParent.filter(m => m.t === 'navigate');
""", extra_globals=_EMBED_SYNC)
    assert result["range"] == {"from": 90, "to": 110}
    assert result["sentBack"] == []


def test_the_shell_relays_a_go_to_with_keep():
    result = _run(PAGE_HTML, _SETTLE + """
clearAll();
fromFrame('v1', {t: 'navigate', time: 1700001234, keep: true});
out.v2 = win('v2')._posted.slice();
""", extra_globals=_LAYOUT)
    assert result["v2"] == [{"t": "navigate", "time": 1700001234, "keep": True}]


def test_alt_g_opens_go_to_at_the_middle_of_the_view_and_enter_goes():
    result = _run(CHART_HTML, _TOOLS_INIT + """
chart._state.logical = {from: 40, to: 60};
const box = document.getElementById('goto-box');
const input = document.getElementById('goto-at');
fire('keydown', document.body, {altKey: true, code: 'KeyG', key: 'g', preventDefault() {}});
out.open = box.classList.contains('open');
out.value = input.value;
input.value = ev('gotoValue')(T0 + 30 * 300 + 60);
ev('onGotoKey')({key: 'Enter', preventDefault() {}});
out.afterEnter = [box.classList.contains('open'), chart._state.logical];
fire('keydown', document.body, {altKey: true, code: 'KeyG', key: 'g', preventDefault() {}});
ev('onGotoKey')({key: 'Escape', preventDefault() {}});
out.afterEscape = box.classList.contains('open');
input.value = 'not a time';
ev('gotoSubmit')();
out.bad = status();
out.parsed = [ev('gotoParse')('2026-10-26T18:00'), ev('gotoParse')('')];
""")
    t0 = 1793000100
    assert result["open"] is True
    assert result["value"] == _utc_minute(t0 + 50 * 300).replace(" ", "T")
    assert result["afterEnter"] == [False, {"from": 20, "to": 40}]   # candle 30, 20 wide
    assert result["afterEscape"] is False
    assert result["bad"] == "Go to: that is not a date and a time"
    assert result["parsed"] == [1793037600, None]


def test_the_countdown_reads_minutes_hours_and_days():
    result = _run(CHART_HTML, """
const f = ev('countdownText');
out.texts = [f(228000), f(9011000), f(((2 * 24 + 4) * 3600 + 20 * 60 + 11) * 1000),
             f(0), f(-5000), f(59999)];
""")
    assert result["texts"] == ["3:48", "2:30:11", "2d 04:20:11", "0:00", "0:00", "0:59"]


_COUNTDOWN_STAGE = """
const last = bars[bars.length - 1];
const closeMs = (last.time + 300) * 1000;
const clockAt = left => ev('handleMsg')({type: 'clock', now: closeMs - left});
ev('mainChart').priceScale = () => ({width: () => 60});
ev('mainSeries').priceToCoordinate = () => 120;
ev('panes').main.paneWrap.getBoundingClientRect =
  () => ({left: 0, top: 40, right: 800, bottom: 440, width: 800, height: 400});
const tag = document.getElementById('cd-tag');
const look = () => { ev('updateCountdown')();
  return tag.style.display === 'block' ? [tag.textContent, tag.style.left, tag.style.top,
                                           tag.style.width, tag.style.background] : null; };
"""


def test_the_countdown_shows_under_the_last_price_once_the_feeds_clock_is_known():
    result = _run(CHART_HTML, _TOOLS_INIT + _COUNTDOWN_STAGE + """
out.noClock = look();
clockAt(228500);                                  // 3:48 left, with half a second to spare
out.shown = look();
clockAt(-30000);                                  // closed 30 s ago, no new candle yet
out.justClosed = look();
clockAt(-90000);                                  // closed a minute and a half ago: shut
out.shut = look();
clockAt(228500);
ev('onCountdownToggle')(false);
out.off = [look(), ctx.__store['atk.chart.countdown.v1']];
ev('onCountdownToggle')(true);
ev('replay').active = true;
out.replay = look();
""", extra_globals=_STORAGE + _EMBED_SYNC)
    assert result["topLevelError"] is None
    assert result["noClock"] is None
    # 1.6 is the last close, over its open: the up colour; 12px font → 17px tall
    assert result["shown"] == ["3:48", "740px", "168.5px", "60px", "#3fb950"]
    assert result["justClosed"][0] == "0:00"
    assert result["shut"] is None
    assert result["off"] == [None, "0"]
    assert result["replay"] is None


def test_the_countdown_switch_is_kept_and_travels_with_the_colours():
    result = _run(CHART_HTML, _TOOLS_INIT + """
out.atStart = [ev('countdownOn'), document.getElementById('stog-countdown').checked];
ctx.__store['atk.chart.countdown.v1'] = '1';      // another chart of the page switched it on
(docListeners.message || []).forEach(fn => fn({source: ctx.window.parent,
  data: {t: 'style'}}));
out.relayed = ev('countdownOn');
""", extra_globals=_STORAGE + _EMBED_SYNC + "ctx.__store['atk.chart.countdown.v1'] = '0';\n")
    assert result["atStart"] == [False, False]
    assert result["relayed"] is True


def test_a_picture_never_shows_the_countdown():
    result = _run(CHART_HTML, _TOOLS_INIT + """
ev('handleMsg')({type: 'clock', now: (bars[199].time + 100) * 1000});
out.now = ev('countdownNow')();
""", extra_globals="ctx.location.search = '?snapshot=1&from=1&to=2&tz=UTC';\n")
    assert result["now"] is None


def test_the_brand_opens_the_repository_in_a_new_tab_on_both_pages():
    from html.parser import HTMLParser

    class Find(HTMLParser):
        def __init__(self):
            super().__init__()
            self.logo = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") == "logo":
                self.logo = (tag, attrs)

    for page in (CHART_HTML, PAGE_HTML):
        finder = Find()
        finder.feed(page.read_text(encoding="utf-8"))
        tag, attrs = finder.logo
        assert tag == "a"
        assert attrs["href"] == "https://github.com/AmirMohammadBazdar/AlgoTradeKit"
        assert attrs["target"] == "_blank" and "noopener" in attrs["rel"]
