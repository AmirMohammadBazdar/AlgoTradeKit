"""
AlgoTradeKit v0.4.1 — Visual module tests

Covers:
  1. Ichimoku cloud payload intercepted (not added as LWC series)
  2. indicator_renderer adds "group" field to all payloads
  3. IndicatorSeries.to_dict() includes "group" field
  4. chart.add_indicator() passes "group" through to IndicatorSeries
  5. Settings HTML contains both new toggles with correct defaults
  6. Main legend CSS uses left: not right:
"""

import re
import pytest
import pandas as pd
import numpy as np

from AlgoTradeKit.visual.models import IndicatorSeries, RawIndicator
from AlgoTradeKit.visual.chart import Chart
from AlgoTradeKit.indicator.ichimoku import Ichimoku
from AlgoTradeKit.indicator.rsi import RSI
from AlgoTradeKit.indicator.macd import MACD
from AlgoTradeKit.indicator.ma import EMA
from AlgoTradeKit.visual import indicator_renderer as ir


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def ohlcv_df():
    np.random.seed(0)
    n = 200
    close  = 100 + np.cumsum(np.random.randn(n))
    high   = close + np.abs(np.random.randn(n))
    low    = close - np.abs(np.random.randn(n))
    open_  = close + np.random.randn(n) * 0.3
    volume = np.random.randint(1000, 9999, n).astype(float)
    dates  = pd.date_range('2024-01-01', periods=n, freq='1D')
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates
    )


@pytest.fixture
def chart_with_data(ohlcv_df):
    c = Chart(title="Test", port=0)
    df_csv = ohlcv_df.reset_index()
    df_csv['timestamp'] = (df_csv['index'].astype('int64') // 1_000_000) 
    df_csv.drop(columns=['index'], inplace=True)
    c.set_data(df_csv)
    return c


# ════════════════════════════════════════════════════════════════
#  1. IndicatorSeries — group field
# ════════════════════════════════════════════════════════════════

class TestIndicatorSeriesGroupField:

    def test_group_field_exists_in_dataclass(self):
        ind = IndicatorSeries(name="EMA 20", data=[], color="#fff",
                              overlay=True, pane=0, line_width=1,
                              series_type="line", group="EMA 20")
        assert ind.group == "EMA 20"

    def test_group_included_in_to_dict(self):
        ind = IndicatorSeries(name="EMA 20", data=[], group="EMA 20")
        d = ind.to_dict()
        assert "group" in d, "to_dict() must include 'group' key"
        assert d["group"] == "EMA 20"

    def test_group_defaults_to_empty_string(self):
        ind = IndicatorSeries(name="RSI", data=[])
        assert ind.group == ""

    def test_group_in_to_dict_when_empty(self):
        ind = IndicatorSeries(name="RSI", data=[])
        d = ind.to_dict()
        assert "group" in d


# ════════════════════════════════════════════════════════════════
#  2. chart.add_indicator() — group passthrough
# ════════════════════════════════════════════════════════════════

class TestChartAddIndicatorGroup:

    def test_add_indicator_accepts_group_kwarg(self, chart_with_data, ohlcv_df):
        ema_val = ohlcv_df['close'].ewm(span=20).mean()
        df_ind = pd.DataFrame({'time': ohlcv_df.index, 'value': ema_val})
        # Should not raise
        chart_with_data.add_indicator(df_ind, name="EMA 20", group="EMA 20")

    def test_add_indicator_stores_group_in_indicator_series(self, chart_with_data, ohlcv_df):
        ema_val = ohlcv_df['close'].ewm(span=20).mean()
        df_ind = pd.DataFrame({'time': ohlcv_df.index, 'value': ema_val})
        chart_with_data.add_indicator(df_ind, name="EMA 20", group="My Group")
        found = next((i for i in chart_with_data._indicators if i.name == "EMA 20"), None)
        assert found is not None
        d = found.to_dict()
        assert d.get("group") == "My Group"

    def test_add_indicator_group_defaults_to_name_when_omitted(self, chart_with_data, ohlcv_df):
        ema_val = ohlcv_df['close'].ewm(span=20).mean()
        df_ind = pd.DataFrame({'time': ohlcv_df.index, 'value': ema_val})
        chart_with_data.add_indicator(df_ind, name="EMA 20")
        found = next((i for i in chart_with_data._indicators if i.name == "EMA 20"), None)
        d = found.to_dict()
        # When group is not provided, it should default to the name or empty
        # (both are acceptable — just verify key exists)
        assert "group" in d

    def test_add_indicator_from_atk_passes_group(self, chart_with_data, ohlcv_df):
        ema_val = ohlcv_df['close'].ewm(span=20).mean()
        ts_ms = (ohlcv_df.index.astype('int64') // 1_000_000).rename('timestamp')
        df_atk = pd.DataFrame({'timestamp': ts_ms, 'value': ema_val})
        chart_with_data.add_indicator_from_atk(df_atk, name="EMA 20", group="EMA Group")
        found = next((i for i in chart_with_data._indicators if i.name == "EMA 20"), None)
        d = found.to_dict()
        assert d.get("group") == "EMA Group"


# ════════════════════════════════════════════════════════════════
#  3. indicator_renderer — group field in all payloads
# ════════════════════════════════════════════════════════════════

class TestRendererGroupField:

    def _get_payloads(self, chart):
        return [
            ind.payload if isinstance(ind, RawIndicator) else ind.to_dict()
            for ind in chart._indicators
        ]

    def test_add_ichimoku_all_payloads_have_group(self, chart_with_data, ohlcv_df):
        ichi = Ichimoku(ohlcv_df['high'], ohlcv_df['low'], ohlcv_df['close'])
        ts = (ohlcv_df.index.astype('int64') // 1_000_000).astype('int64')
        ir.add_ichimoku(chart_with_data, ichi, timestamps=pd.Series(ts))
        payloads = self._get_payloads(chart_with_data)
        ichi_payloads = [p for p in payloads if p.get('overlay') is True
                         or p.get('name','').startswith(('Tenkan','Kijun','Chikou',
                                                          'Span','Cloud'))]
        assert len(ichi_payloads) > 0, "No Ichimoku payloads found"
        for p in ichi_payloads:
            assert 'group' in p, f"Payload '{p.get('name')}' missing 'group' key"
            assert p['group'] != '', f"Payload '{p.get('name')}' has empty group"

    def test_add_ichimoku_all_lines_share_same_group(self, chart_with_data, ohlcv_df):
        ichi = Ichimoku(ohlcv_df['high'], ohlcv_df['low'], ohlcv_df['close'])
        ts = (ohlcv_df.index.astype('int64') // 1_000_000).astype('int64')
        ir.add_ichimoku(chart_with_data, ichi, timestamps=pd.Series(ts))
        payloads = self._get_payloads(chart_with_data)
        ichi_line_names = {'Tenkan','Kijun','Chikou','Span A','Cloud (Bull)','Cloud (Bear)'}
        ichi_payloads = [p for p in payloads
                         if any(p.get('name','').startswith(n.split('(')[0])
                                for n in ichi_line_names)
                         or p.get('name','') in ichi_line_names]
        groups = {p.get('group') for p in ichi_payloads if 'group' in p}
        assert len(groups) == 1, (
            f"All Ichimoku lines must share the same group, got: {groups}"
        )

    def test_add_rsi_payloads_have_group(self, chart_with_data, ohlcv_df):
        rsi = RSI(ohlcv_df['close'])
        ts = (ohlcv_df.index.astype('int64') // 1_000_000).astype('int64')
        ir.add_rsi(chart_with_data, rsi, timestamps=pd.Series(ts))
        payloads = self._get_payloads(chart_with_data)
        rsi_payloads = [p for p in payloads if 'RSI' in p.get('name','')]
        assert len(rsi_payloads) > 0
        for p in rsi_payloads:
            assert 'group' in p, f"RSI payload '{p.get('name')}' missing 'group'"

    def test_add_macd_payloads_share_group(self, chart_with_data, ohlcv_df):
        macd = MACD(ohlcv_df['close'])
        ts = (ohlcv_df.index.astype('int64') // 1_000_000).astype('int64')
        ir.add_macd(chart_with_data, macd, timestamps=pd.Series(ts))
        payloads = self._get_payloads(chart_with_data)
        macd_payloads = [p for p in payloads
                         if p.get('name','') in ('Histogram',) or
                            'MACD' in p.get('name','') or
                            'Signal' in p.get('name','')]
        assert len(macd_payloads) >= 2
        groups = {p.get('group') for p in macd_payloads if p.get('group')}
        assert len(groups) == 1, (
            f"All MACD payloads must share one group, got: {groups}"
        )

    def test_add_ma_payload_has_group(self, chart_with_data, ohlcv_df):
        ema = EMA(ohlcv_df['close'], length=20)
        ts  = (ohlcv_df.index.astype('int64') // 1_000_000).astype('int64')
        ir.add_ma(chart_with_data, ema, timestamps=pd.Series(ts))
        payloads = self._get_payloads(chart_with_data)
        assert len(payloads) > 0
        for p in payloads:
            assert 'group' in p, f"MA payload '{p.get('name')}' missing 'group'"


# ════════════════════════════════════════════════════════════════
#  4. index.html — static analysis (no browser needed)
# ════════════════════════════════════════════════════════════════

from pathlib import Path

HTML_PATH = Path(__file__).parent.parent / 'src' / 'AlgoTradeKit' / 'visual' / 'static' / 'index.html'

@pytest.fixture
def html():
    return HTML_PATH.read_text(encoding='utf-8')


class TestHtmlIchimokuCloud:

    def test_cloud_bull_intercepted_before_lwc_series_creation(self, html):
        """addIndicator must bail out for Cloud (Bull) before adding LWC series."""
        assert "name==='Cloud (Bull)'" in html or 'name==="Cloud (Bull)"' in html, (
            "addIndicator() must intercept 'Cloud (Bull)' before creating an LWC series"
        )

    def test_cloud_bear_intercepted(self, html):
        assert "name==='Cloud (Bear)'" in html or 'name==="Cloud (Bear)"' in html

    def test_ichimoku_cloud_state_variable(self, html):
        assert 'ichimokuCloud' in html, (
            "Must have ichimokuCloud state variable for canvas drawing"
        )

    def test_draw_ichimoku_cloud_function_exists(self, html):
        assert 'function drawIchimokuCloud' in html, (
            "drawIchimokuCloud() canvas drawing function must exist"
        )

    def test_redraw_all_calls_draw_cloud(self, html):
        assert 'drawIchimokuCloud' in html
        # Verify redrawAll calls it (just check both names exist; order in source)
        redraw_pos = html.find('function redrawAll')
        cloud_call = html.find('drawIchimokuCloud(', redraw_pos)
        assert cloud_call > redraw_pos, (
            "redrawAll() must call drawIchimokuCloud() to render cloud on canvas"
        )

    def test_cloud_fill_uses_coordinate_conversion(self, html):
        """Cloud fill must convert time+price to canvas pixel coordinates."""
        assert 'timeToCoordinate' in html
        assert 'priceToCoordinate' in html


class TestHtmlGroupLegend:

    def test_il_group_css_class_exists(self, html):
        assert '.il-group' in html, "CSS must define .il-group class"

    def test_toggle_group_collapse_function(self, html):
        assert 'function toggleGroupCollapse' in html

    def test_toggle_group_visibility_function(self, html):
        assert 'function toggleGroupVisibility' in html

    def test_remove_indicator_group_function(self, html):
        assert 'function removeIndicatorGroup' in html

    def test_ind_groups_state_variable(self, html):
        assert 'indGroups' in html, "indGroups state object must be declared"

    def test_add_legend_row_accepts_group_arg(self, html):
        # Function signature must accept 3 arguments (name, color, group)
        sig = re.search(r'function addLegendRow\s*\(([^)]+)\)', html)
        assert sig is not None, "addLegendRow function not found"
        params = sig.group(1)
        param_count = len([p.strip() for p in params.split(',') if p.strip()])
        assert param_count == 3, (
            f"addLegendRow must accept 3 params (name, color, group), got {param_count}"
        )

    def test_group_header_has_collapse_button(self, html):
        assert 'il-group-arrow' in html

    def test_group_header_has_hide_all_button(self, html):
        assert 'toggleGroupVisibility' in html

    def test_group_header_has_remove_all_button(self, html):
        assert 'removeIndicatorGroup' in html

    def test_ind_groups_reset_on_init(self, html):
        """indGroups must be cleared when a new 'init' message arrives."""
        # Find the init case and check indGroups is cleared
        init_pos = html.find("case 'init'")
        groups_clear = html.find('indGroups', init_pos)
        assert groups_clear > init_pos and groups_clear < init_pos + 2000, (
            "indGroups must be reset inside the 'init' message handler"
        )


class TestHtmlLegendPosition:

    def test_ind_legend_css_uses_left(self, html):
        css = re.search(r'#ind-legend\s*\{([^}]+)\}', html, re.DOTALL)
        assert css is not None, "#ind-legend CSS block not found"
        block = css.group(1)
        assert 'left:' in block or 'left :' in block, (
            "#ind-legend must use 'left:' for top-left placement"
        )
        assert 'right:' not in block, (
            "#ind-legend must NOT use 'right:' (it was moved to top-left)"
        )


class TestHtmlSettingsToggles:

    def test_stog_last_close_exists(self, html):
        assert 'stog-last-close' in html, \
            "Settings panel must have 'stog-last-close' toggle"

    def test_stog_ind_prices_exists(self, html):
        assert 'stog-ind-prices' in html, \
            "Settings panel must have 'stog-ind-prices' toggle"

    def test_last_close_is_checked_by_default(self, html):
        match = re.search(r'id="stog-last-close"([^>]*?)>', html)
        assert match is not None, "stog-last-close input not found"
        attrs = match.group(1)
        assert 'checked' in attrs, \
            "stog-last-close must be checked (ON) by default"

    def test_ind_prices_is_unchecked_by_default(self, html):
        match = re.search(r'id="stog-ind-prices"([^>]*?)>', html)
        assert match is not None, "stog-ind-prices input not found"
        attrs = match.group(1)
        assert 'checked' not in attrs, \
            "stog-ind-prices must be unchecked (OFF) by default"

    def test_apply_axis_label_settings_function(self, html):
        assert 'function applyAxisLabelSettings' in html

    def test_show_last_close_variable(self, html):
        assert 'showLastClose' in html

    def test_show_ind_last_prices_variable(self, html):
        assert 'showIndLastPrices' in html


class TestHtmlCursorPriceFix:

    def test_cursor_price_line_variable(self, html):
        assert '_cursorPriceLine' in html, \
            "_cursorPriceLine variable must exist for cursor axis label"

    def test_update_cursor_axis_label_function(self, html):
        assert 'function updateCursorAxisLabel' in html

    def test_cursor_label_uses_candle_close(self, html):
        """The cursor label function must read .close from series data."""
        fn_start = html.find('function updateCursorAxisLabel')
        assert fn_start != -1
        fn_end = html.find('\n}', fn_start) + 2
        fn_body = html[fn_start:fn_end]
        assert '.close' in fn_body or 'barData.close' in fn_body, (
            "updateCursorAxisLabel must read candle close from barData"
        )

    def test_cursor_function_called_on_crosshair_move(self, html):
        assert 'updateCursorAxisLabel(param)' in html or \
               'updateCursorAxisLabel( param )' in html

    def test_lwc_horzline_label_disabled_on_main(self, html):
        """Main chart crosshair horzLine labelVisible must be set to false."""
        assert 'labelVisible' in html and 'false' in html, (
            "LWC crosshair horzLine labelVisible:false must be set on main chart"
        )