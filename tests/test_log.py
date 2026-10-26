import logging
import re

from pakt import log
from pakt.config import get_config_dir

logger = logging.getLogger("pakt.test")


def sync_log() -> list[str]:
    """Read sync.log, each entry without its timestamp."""
    for handler in log.logger.handlers:
        handler.flush()
    text = (get_config_dir() / "sync.log").read_text(encoding="utf-8")
    return [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line) for line in text.splitlines()]


def test_each_entry_is_one_line_with_its_level():
    """A message carrying a newline or escape code stays on one line, after its level."""
    log.setup_logging()
    logger.warning("Title\nwith a newline\x1b[31m")
    assert sync_log() == ["WARNING Title with a newline [31m"]


def test_items_are_indented_and_lines_name_their_server():
    """Lines logged while a server syncs carry its name; items sit indented under them."""
    log.setup_logging()
    with log.server_prefix("Home"):
        logger.info("Movies: no changes")
        logger.info("Trakt watched: Film (2001)", extra={"item": True})
    logger.info("Sync finished")
    assert sync_log() == [
        "INFO    [Home] Movies: no changes",
        "INFO    [Home]   Trakt watched: Film (2001)",
        "INFO    Sync finished",
    ]


def test_tracebacks_reach_the_file_but_not_the_console(capsys):
    """An unexpected failure keeps its traceback in sync.log; the console shows the one line."""
    log.setup_logging()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Movies failed: RuntimeError: boom")
    assert "Traceback (most recent call last):" in sync_log()
    printed = capsys.readouterr().out
    assert "Movies failed: RuntimeError: boom" in printed and "Traceback" not in printed


def test_console_prints_brackets_as_text(capsys):
    """A title with square brackets prints as written instead of being read as markup."""
    log.setup_logging()
    logger.info("[/x] Title [bold]")
    assert "[/x] Title [bold]" in capsys.readouterr().out


def test_web_ui_gets_level_prefixes():
    """A web UI sync sees each line once, with the prefix its console colors by."""
    lines: list[str] = []
    with log.forward_log(lines.append), log.server_prefix("Home"):
        logger.info("Movies: no changes")
        logger.info("Trakt watched: Film (2001)", extra={"item": True})
        logger.warning("Episodes: skipped 8")
        logger.error("Episodes failed")
        logger.info("Sync finished", extra={"success": True})
    logger.info("after the sync")
    assert lines == [
        "[Home] Movies: no changes",
        "DETAIL:[Home]   Trakt watched: Film (2001)",
        "WARNING:[Home] Episodes: skipped 8",
        "ERROR:[Home] Episodes failed",
        "SUCCESS:[Home] Sync finished",
    ]
