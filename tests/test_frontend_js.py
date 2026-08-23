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
  let className = '', text = '', inner = '';
  const el = {
    id, value: '', tagName: String(id || '').toUpperCase(),
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
  // Assigning innerHTML replaces the children, which is how pages clear a list
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

// clicking a chart makes it the active one
fromFrame('v3', {t: 'focus'});
out.activeAfterFocus = queryAll('.cell').filter(c => c._cls.has('active')).map(c => c.id);

// a range from one chart reaches the others and does not come back
fromFrame('v1', {t: 'range', from: 100, to: 200});
out.v1Posted = win('v1').contentWindowPosted || win('v1')._posted.slice();
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
        assert shell_result["v1Posted"] == []       # never echoed back
        assert shell_result["v2Posted"] == [{"t": "range", "from": 100, "to": 200}]
        assert shell_result["v3Posted"] == [{"t": "range", "from": 100, "to": 200}]

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
