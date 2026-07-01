from __future__ import annotations


class BrokerError(Exception):
    """Base class for every error raised by the ``broker`` module."""


class AuthenticationError(BrokerError):
    """
    Raised when a private (account / trading) call is attempted without valid
    credentials, or when the venue rejects the supplied credentials.
    """


class NotSupportedError(BrokerError):
    """
    Raised when a unified operation is not available on a particular venue.

    Example: ``open_positions()`` on a Binance **spot** connector — spot has
    balances and orders but no leveraged positions.
    """


class OrderError(BrokerError):
    """Raised when an order is rejected by the venue or fails validation."""


class ConnectionFailed(BrokerError):
    """Raised when the transport (REST / WebSocket / bridge) cannot be reached."""
