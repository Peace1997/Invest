from __future__ import annotations
from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    """Unified data source interface. Concrete sources normalize akshare/baostock/tushare
    outputs to canonical English column names so downstream code is source-agnostic."""

    @abstractmethod
    def calendar(self) -> pd.DataFrame:
        """Return DataFrame[trade_date: DATE]."""

    @abstractmethod
    def stock_list(self) -> pd.DataFrame:
        """Return DataFrame[symbol, name, exchange, type='stock']."""

    @abstractmethod
    def etf_list(self) -> pd.DataFrame:
        """Return DataFrame[symbol, name, exchange, type='etf']."""

    @abstractmethod
    def st_symbols(self) -> set[str]:
        """Return set of symbols currently flagged ST/*ST."""

    @abstractmethod
    def stock_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...

    @abstractmethod
    def etf_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...

    @abstractmethod
    def index_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...

    @abstractmethod
    def northbound_hist(self, channel: str) -> pd.DataFrame:
        """channel in {'北向','沪股通','深股通'}."""
