"""
Consistency tests between the shipped documentation, the packaging metadata
and the code.

These do not review prose — they assert the claims that the code can
contradict:

* every ``MT5_WINE_SETUP.md`` Part that a runtime error message points at
  really exists in the guide;
* the guide's install and deploy instructions match reality (the two-file
  bridge, the ``[mt5]`` extra exactly as declared in ``pyproject.toml``, the
  CLI flags the bridge really accepts);
* the public API surface each module advertises is what it actually exports.

If a rename breaks one of these, the documentation was lying — fix whichever
side is wrong.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

import AlgoTradeKit.broker.metatrader.bridge_server as bridge_server
from AlgoTradeKit.broker.metatrader import _bridge_client, _native

ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = ROOT / "MT5_WINE_SETUP.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def guide() -> str:
    return _read(GUIDE_PATH)


def _headings(text: str) -> list[str]:
    return re.findall(r"^#{1,4}\s+(.*)$", text, flags=re.M)


def _flat(text: str) -> str:
    """Markdown with blockquote markers and line wrapping removed, so prose
    assertions do not break on where a sentence happens to wrap."""
    unquoted = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    return re.sub(r"\s+", " ", unquoted)


# ===========================================================================
# MT5_WINE_SETUP.md — the guide the library's error messages point at
# ===========================================================================

class TestGuideStructure:
    @pytest.mark.parametrize(
        "part",
        ["Part A", "Part B", "Part C", "Part D", "Part E",
         "Part F", "Part G", "Part H", "Part I"],
    )
    def test_every_referenced_part_exists(self, guide, part):
        assert any(head.startswith(f"{part} ") for head in _headings(guide)), (
            f"{part} is referenced but has no heading"
        )

    def test_windows_and_diagnostics_sections_exist(self, guide):
        headings = _headings(guide)
        assert "Windows — no bridge needed" in headings
        assert "What the library's error messages mean" in headings
        assert "Troubleshooting" in headings

    def test_intro_anchors_resolve_to_real_headings(self, guide):
        for anchor, heading in (
            ("#windows--no-bridge-needed", "Windows — no bridge needed"),
            ("#what-the-librarys-error-messages-mean",
             "What the library's error messages mean"),
        ):
            assert anchor in guide
            assert heading in _headings(guide)


class TestGuideMatchesDiagnostics:
    """Every Part named by a runtime error message must exist in the guide."""

    def test_bridge_diagnostics_reference_real_parts(self, guide):
        source = Path(_bridge_client.__file__).read_text(encoding="utf-8")
        referenced = set(re.findall(r"MT5_WINE_SETUP\.md\s+(Part\s+[A-Z])", source))
        assert referenced, "the connection diagnostics must name guide sections"
        headings = _headings(guide)
        for part in referenced:
            assert any(head.startswith(f"{part} ") for head in headings), part

    def test_diagnostic_messages_are_all_documented(self, guide):
        assert "Wine is not installed" in guide
        assert "MT5 Wine prefix not found" in guide
        assert "Bridge is not running" in guide
        assert "Could not reach the MetaTrader bridge" in guide
        assert "accepted the connection but sent no reply" in guide

    def test_native_transport_hints_are_documented(self, guide):
        assert "The 'MetaTrader5' package is not installed in this Python" in guide
        assert "mt5.initialize() failed" in guide
        assert "pip install AlgoTradeKit[mt5]" in guide
        assert "pip install AlgoTradeKit[mt5]" in _native._INSTALL_HINT

    def test_ipc_timeout_guidance_matches_the_bridge(self, guide):
        message = bridge_server.initialize_failure_message(
            (bridge_server.IPC_TIMEOUT_CODE, "IPC timeout")
        )
        assert "wineserver -k" in message and "wineserver -k" in guide
        assert "-10005" in guide
        assert "--path" in message


class TestGuideInstructionsMatchReality:
    def test_bridge_deploy_ships_both_files(self, guide):
        part_e = guide.split("## Part E")[1].split("## Part F")[0]
        assert "bridge_server.py" in part_e
        assert "_ops.py" in part_e
        bridge_dir = Path(bridge_server.__file__).resolve().parent
        assert (bridge_dir / "bridge_server.py").is_file()
        assert (bridge_dir / "_ops.py").is_file()

    def test_mt5_extra_matches_pyproject(self, guide):
        # Text-parsed on purpose: tomllib is 3.11+, the package floor is 3.10.
        pyproject = _read(ROOT / "pyproject.toml")
        assert 'mt5 = [\'MetaTrader5; platform_system == "Windows"\']' in pyproject
        assert "pip install AlgoTradeKit[mt5]" in guide
        core = pyproject.split("dependencies = [")[1].split("]")[0]
        assert "MetaTrader5" not in core, "MetaTrader5 must never be a core dependency"

    def test_documented_bridge_flags_exist(self, guide):
        help_text = bridge_server.build_parser().format_help()
        for flag in ("--host", "--port", "--login", "--password", "--server", "--path"):
            assert flag in help_text
        assert "--host 127.0.0.1 --port 18812" in guide
        args = bridge_server.build_parser().parse_args([])
        assert (args.host, args.port) == ("127.0.0.1", 18812)

    def test_guide_never_recommends_path(self, guide):
        runnable = [
            line for line in guide.splitlines()
            if line.strip().startswith(("wine", "xvfb-run", "DISPLAY=", "ExecStart"))
        ]
        assert runnable
        assert all("--path" not in line for line in runnable), (
            "no runnable example may pass --path"
        )

    def test_stdout_workaround_is_documented(self, guide):
        assert "bridge.log" in guide
        assert "drive_c" in guide

    def test_metatrader5_is_not_a_linux_dependency_note(self, guide):
        flat = _flat(guide)
        assert "it is *not* a dependency of AlgoTradeKit" in flat
        assert "no Linux build exists" in flat
        assert "installed only inside the Wine Python" in flat


# ===========================================================================
# Public API surface
# ===========================================================================

def _exported(module_name: str) -> set[str]:
    module = importlib.import_module(f"AlgoTradeKit.{module_name}")
    return set(getattr(module, "__all__", ()))


class TestPublicApiSurface:
    @pytest.mark.parametrize(
        ("module", "names"),
        [
            ("trader", ["run_live", "Trader", "TraderConfig", "TraderPair", "EventStream",
                        "TerminalEventPrinter", "attach_terminal_printer", "SOURCE_SIM",
                        "SOURCE_LIVE", "ALL_EVENT_TYPES", "EXEC_CANDLE_CLOSE",
                        "DISPLAY_TRADES_SIM", "ON_STOP_KEEP", "CLOSE_REASON_MANUAL"]),
            ("simulate", ["Simulate", "SimulationStepper", "LiveSimulation", "SimulateConfig",
                          "EVENT_SIGNAL", "EVENT_CLOSE", "run_batch", "run_multi"]),
            ("strategy", ["BaseStrategy", "advance_live_candle", "evaluate_forming_candle",
                          "default_recompute_window", "has_update_hook"]),
            ("visual", ["Chart", "LivePosition", "add_simulation_positions"]),
            ("report", ["ReportServer", "build_combined_report_payload",
                        "show_combined_report", "save_combined_report_html"]),
            ("broker", ["Broker", "BaseBroker", "MetaTraderBroker", "TradingCosts",
                        "COMMISSION_TYPE_PER_LOT"]),
        ],
    )
    def test_advertised_exports_are_real(self, module, names):
        exported = _exported(module)
        namespace = importlib.import_module(f"AlgoTradeKit.{module}")
        for name in names:
            assert name in exported, f"{module}.{name} is advertised but not exported"
            assert hasattr(namespace, name), f"{module}.{name} is in __all__ but missing"

    def test_top_level_run_live_reexport(self):
        import AlgoTradeKit

        assert "run_live" in AlgoTradeKit.__all__
        assert AlgoTradeKit.run_live.__name__ == "run_live"

    def test_position_math_helpers_exist(self):
        from AlgoTradeKit.simulate import _position_math

        for name in ("compute_position_params", "can_open_position", "build_tp_level_prices",
                     "advance_multi_rr", "update_trailing_sl", "check_risk_free",
                     "check_close", "sl_reason", "make_closed_trade",
                     "apply_partial_close", "handle_tp_level_hit"):
            assert hasattr(_position_math, name), name


# ===========================================================================
# examples/ — the runnable demos ship with the package
# ===========================================================================

class TestExamplesShip:
    """A demo that is not packaged is a demo nobody runs."""

    def test_examples_are_in_the_sdist_allow_list(self):
        pyproject = _read(ROOT / "pyproject.toml")
        allow_list = pyproject.split("only-include = [")[1].split("]")[0]
        assert '"examples"' in allow_list

    def test_every_example_is_listed_in_the_readme(self):
        readme = _read(ROOT / "README.md")
        scripts = sorted(
            p.name for p in (ROOT / "examples").glob("*.py")
            if not p.name.startswith("_")
        )
        assert scripts, "examples/ has no demos"
        for name in scripts:
            assert name in readme, f"{name} is not mentioned in README.md"
