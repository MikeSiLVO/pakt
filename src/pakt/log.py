"""Pakt's log: every message reaches sync.log, the console and a web UI sync, one line each."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import os
import re
from collections.abc import Callable, Generator

from rich.console import Console
from rich.text import Text

from pakt.config import get_config_dir

LOG_MAX_BYTES = 5 * 1024 * 1024
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
CONSOLE_STYLES = {logging.WARNING: "yellow", logging.ERROR: "red", logging.CRITICAL: "red"}
WEB_PREFIXES = {logging.WARNING: "WARNING:", logging.ERROR: "ERROR:", logging.CRITICAL: "ERROR:"}

logger = logging.getLogger("pakt")
logger.setLevel(logging.INFO)
console = Console()
_server: contextvars.ContextVar[str] = contextvars.ContextVar("pakt_server", default="")


def _line(record: logging.LogRecord) -> str:
    """Flatten a record to one line, indented for an item and tagged with the server syncing."""
    text = CONTROL_CHARS.sub(" ", record.getMessage())
    if getattr(record, "item", False):
        text = f"  {text}"
    server = _server.get()
    return f"[{server}] {text}" if server else text


class _FileFormatter(logging.Formatter):
    """Formatter writing time, level and the record's line, with any traceback below."""

    def format(self, record: logging.LogRecord) -> str:
        """Format one record for sync.log."""
        text = f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<7} {_line(record)}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class _ConsoleHandler(logging.Handler):
    """Terminal handler coloring records by level, with items dimmed and no tracebacks."""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit one record to the terminal; brackets in titles stay literal text."""
        try:
            style = "dim" if getattr(record, "item", False) else CONSOLE_STYLES.get(record.levelno, "")
            console.print(Text(_line(record), style=style))
        except Exception:
            self.handleError(record)


class _WebHandler(logging.Handler):
    """Handler passing records to a web UI sync with the prefixes its console colors by."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """Emit one record to the web UI sync, prefixed by kind or level."""
        if getattr(record, "item", False):
            prefix = "DETAIL:"
        elif getattr(record, "success", False):
            prefix = "SUCCESS:"
        else:
            prefix = WEB_PREFIXES.get(record.levelno, "")
        self._sink(prefix + _line(record))


def setup_logging() -> None:
    """Set up the pakt log to write to sync.log and the console only, once per process."""
    if any(isinstance(h, _ConsoleHandler) for h in logger.handlers):
        return
    log_file = get_config_dir() / "sync.log"
    log_file.touch(exist_ok=True)
    if os.name != "nt":
        log_file.chmod(0o600)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(_FileFormatter())
    logger.addHandler(file_handler)
    logger.addHandler(_ConsoleHandler())
    logger.propagate = False


@contextlib.contextmanager
def forward_log(sink: Callable[[str], None] | None) -> Generator[None, None, None]:
    """Forward every log line to a web UI sync while the block runs."""
    if sink is None:
        yield
        return
    handler = _WebHandler(sink)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


@contextlib.contextmanager
def server_prefix(name: str) -> Generator[None, None, None]:
    """Tag log lines with a server's name while the block runs, its worker threads included."""
    token = _server.set(name)
    try:
        yield
    finally:
        _server.reset(token)
