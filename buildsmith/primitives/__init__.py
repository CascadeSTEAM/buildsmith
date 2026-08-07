"""Shared building blocks for both workflows.

Everything here is pure data: functions that take dicts and return dicts. No
module in this package may import an HTTP client or take a credential. Tools
emit files; something else applies them.
"""
