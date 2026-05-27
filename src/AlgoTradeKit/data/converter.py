import pandas as pd


class Converter:
    """
    Convert OHLCV candles from a lower timeframe to a higher timeframe.
    ...
    """

    def __init__(self, source, target_timeframe, source_timeframe=None):
        ...

    def convert(self) -> str:
        ...

    def _load_source(self):
        # Load CSV or accept DataFrame
        ...

    def _detect_timeframe(self, df) -> str:
        # Infer TF from timestamp gaps
        ...

    def _validate_conversion(self, source_tf, target_tf) -> None:
        # Raise ValueError if conversion is not possible
        ...

    def _resample(self, df, source_tf, target_tf) -> pd.DataFrame:
        # The actual OHLCV resampling logic
        ...

    def _resolve_filepath(self, source_tf, target_tf) -> str:
        # Build output path, same logic as Collector
        ...
