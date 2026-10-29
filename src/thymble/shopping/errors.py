"""Domain errors raised by the shopping service; the API maps them to HTTP statuses."""

from __future__ import annotations


class ShoppingError(Exception):
    status_code = 400


class NotFound(ShoppingError):
    status_code = 404


class Conflict(ShoppingError):
    status_code = 409
