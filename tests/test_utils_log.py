from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import tempfile  # NEW: needed to create temporary log files for rotation tests
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock  # NEW: needed to patch loguru internals without real file I/O

import pytest
from loguru import logger as loguru_logger  # NEW: needed to inspect loguru sink state
from testfixtures import LogCapture
from twisted.python.failure import Failure

from scrapy.settings import Settings
from scrapy.utils.log import (
    LogCounterHandler,
    LoguruHandler,  # NEW: the handler class introduced by the rotation feature
    SpiderLoggerAdapter,
    StreamLogger,
    TopLevelFormatter,
    _get_handler,  # NEW: internal helper that decides which handler type to create
    _install_loguru_sink,  # NEW: internal helper that registers a loguru sink
    _loguru_sink_ids,  # NEW: the dict that tracks one sink id per log file
    _uninstall_scrapy_root_handler,  # NEW: needed for teardown so tests don't leak state
    failure_to_exc_info,
    install_scrapy_root_handler,  # NEW: needed to test the full install→uninstall cycle
)
from scrapy.utils.test import get_crawler
from tests.spiders import LogSpider

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, MutableMapping

    from scrapy.crawler import Crawler


class TestFailureToExcInfo:
    def test_failure(self):
        try:
            0 / 0
        except ZeroDivisionError:
            exc_info = sys.exc_info()
            failure = Failure()

        assert exc_info == failure_to_exc_info(failure)

    def test_non_failure(self):
        assert failure_to_exc_info("test") is None


class TestTopLevelFormatter:
    def setup_method(self):
        self.handler = LogCapture()
        self.handler.addFilter(TopLevelFormatter(["test"]))

    def test_top_level_logger(self):
        logger = logging.getLogger("test")
        with self.handler as log:
            logger.warning("test log msg")
        log.check(("test", "WARNING", "test log msg"))

    def test_children_logger(self):
        logger = logging.getLogger("test.test1")
        with self.handler as log:
            logger.warning("test log msg")
        log.check(("test", "WARNING", "test log msg"))

    def test_overlapping_name_logger(self):
        logger = logging.getLogger("test2")
        with self.handler as log:
            logger.warning("test log msg")
        log.check(("test2", "WARNING", "test log msg"))

    def test_different_name_logger(self):
        logger = logging.getLogger("different")
        with self.handler as log:
            logger.warning("test log msg")
        log.check(("different", "WARNING", "test log msg"))


class TestLogCounterHandler:
    @pytest.fixture
    def crawler(self) -> Crawler:
        settings = {"LOG_LEVEL": "WARNING"}
        return get_crawler(settings_dict=settings)

    @pytest.fixture
    def logger(self, crawler: Crawler) -> Generator[logging.Logger]:
        logger = logging.getLogger("test")
        logger.setLevel(logging.NOTSET)
        logger.propagate = False
        handler = LogCounterHandler(crawler)
        logger.addHandler(handler)

        yield logger

        logger.propagate = True
        logger.removeHandler(handler)

    def test_init(self, crawler: Crawler, logger: logging.Logger) -> None:
        assert crawler.stats
        assert crawler.stats.get_value("log_count/DEBUG") is None
        assert crawler.stats.get_value("log_count/INFO") is None
        assert crawler.stats.get_value("log_count/WARNING") is None
        assert crawler.stats.get_value("log_count/ERROR") is None
        assert crawler.stats.get_value("log_count/CRITICAL") is None

    def test_accepted_level(self, crawler: Crawler, logger: logging.Logger) -> None:
        logger.error("test log msg")
        assert crawler.stats
        assert crawler.stats.get_value("log_count/ERROR") == 1

    def test_filtered_out_level(self, crawler: Crawler, logger: logging.Logger) -> None:
        logger.debug("test log msg")
        assert crawler.stats
        assert crawler.stats.get_value("log_count/INFO") is None


class TestStreamLogger:
    def test_redirect(self):
        logger = logging.getLogger("test")
        logger.setLevel(logging.WARNING)
        old_stdout = sys.stdout
        sys.stdout = StreamLogger(logger, logging.ERROR)

        with LogCapture() as log:
            print("test log msg")
        log.check(("test", "ERROR", "test log msg"))

        sys.stdout = old_stdout


@pytest.mark.parametrize(
    ("base_extra", "log_extra", "expected_extra"),
    [
        (
            {"spider": "test"},
            {"extra": {"log_extra": "info"}},
            {"extra": {"log_extra": "info", "spider": "test"}},
        ),
        (
            {"spider": "test"},
            {"extra": None},
            {"extra": {"spider": "test"}},
        ),
        (
            {"spider": "test"},
            {"extra": {"spider": "test2"}},
            {"extra": {"spider": "test"}},
        ),
    ],
)
def test_spider_logger_adapter_process(
    base_extra: Mapping[str, Any], log_extra: MutableMapping, expected_extra: dict
) -> None:
    logger = logging.getLogger("test")
    spider_logger_adapter = SpiderLoggerAdapter(logger, base_extra)

    log_message = "test_log_message"
    result_message, result_kwargs = spider_logger_adapter.process(
        log_message, log_extra
    )

    assert result_message == log_message
    assert result_kwargs == expected_extra


class TestLogging:
    @pytest.fixture
    def log_stream(self) -> StringIO:
        return StringIO()

    @pytest.fixture
    def spider(self) -> LogSpider:
        return LogSpider()

    @pytest.fixture(autouse=True)
    def logger(self, log_stream: StringIO) -> Generator[logging.Logger]:
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("log_spider")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        yield logger

        logger.removeHandler(handler)

    def test_debug_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo message"
        spider.log_debug(log_message)
        log_contents = log_stream.getvalue()

        assert log_contents == f"{log_message}\n"

    def test_info_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Bar message"
        spider.log_info(log_message)
        log_contents = log_stream.getvalue()

        assert log_contents == f"{log_message}\n"

    def test_warning_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Baz message"
        spider.log_warning(log_message)
        log_contents = log_stream.getvalue()

        assert log_contents == f"{log_message}\n"

    def test_error_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo bar message"
        spider.log_error(log_message)
        log_contents = log_stream.getvalue()

        assert log_contents == f"{log_message}\n"

    def test_critical_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo bar baz message"
        spider.log_critical(log_message)
        log_contents = log_stream.getvalue()

        assert log_contents == f"{log_message}\n"


class TestLoggingWithExtra:
    regex_pattern = re.compile(r"^<LogSpider\s'log_spider'\sat\s[^>]+>$")

    @pytest.fixture
    def log_stream(self) -> StringIO:
        return StringIO()

    @pytest.fixture
    def spider(self) -> LogSpider:
        return LogSpider()

    @pytest.fixture(autouse=True)
    def logger(self, log_stream: StringIO) -> Generator[logging.Logger]:
        handler = logging.StreamHandler(log_stream)
        formatter = logging.Formatter(
            '{"levelname": "%(levelname)s", "message": "%(message)s", "spider": "%(spider)s", "important_info": "%(important_info)s"}'
        )
        handler.setFormatter(formatter)
        logger = logging.getLogger("log_spider")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        yield logger

        logger.removeHandler(handler)

    def test_debug_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo message"
        extra = {"important_info": "foo"}
        spider.log_debug(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "DEBUG"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]

    def test_info_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Bar message"
        extra = {"important_info": "bar"}
        spider.log_info(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "INFO"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]

    def test_warning_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Baz message"
        extra = {"important_info": "baz"}
        spider.log_warning(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "WARNING"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]

    def test_error_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo bar message"
        extra = {"important_info": "foo bar"}
        spider.log_error(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "ERROR"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]

    def test_critical_logging(self, log_stream: StringIO, spider: LogSpider) -> None:
        log_message = "Foo bar baz message"
        extra = {"important_info": "foo bar baz"}
        spider.log_critical(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "CRITICAL"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]

    def test_overwrite_spider_extra(
        self, log_stream: StringIO, spider: LogSpider
    ) -> None:
        log_message = "Foo message"
        extra = {"important_info": "foo", "spider": "shouldn't change"}
        spider.log_error(log_message, extra)
        log_contents_str = log_stream.getvalue()
        log_contents = json.loads(log_contents_str)

        assert log_contents["levelname"] == "ERROR"
        assert log_contents["message"] == log_message
        assert self.regex_pattern.match(log_contents["spider"])
        assert log_contents["important_info"] == extra["important_info"]


# NEW: Builds a minimal Settings object pointing LOG_FILE at a given path.
# Keeping this as a module-level helper avoids repeating the same dict in every
# test method. The `extra` argument lets individual tests override or add keys
# (e.g. to set LOG_FILE_ROTATE) without having to spell out all the base keys.
def _settings_with_file(path: str, extra: dict | None = None):

    base = {
        "LOG_FILE": path,
        "LOG_LEVEL": "DEBUG",
        "LOG_FORMAT": "%(message)s",
        "LOG_DATEFORMAT": "%Y-%m-%d",
        "LOG_ENCODING": "utf-8",
        "LOG_ENABLED": True,
        "LOG_SHORT_NAMES": False,
        "LOG_FILE_APPEND": False,
    }
    if extra:
        base.update(extra)
    return Settings(base)


# ── NEW: TestLoguruHandler ────────────────────────────────────────────────────


# NEW: Tests for the LoguruHandler class that was added to scrapy/utils/log.py.
# LoguruHandler is a thin bridge: it receives Python logging records and
# forwards them to loguru so that loguru can handle rotation on its side.
class TestLoguruHandler:
    def test_is_logging_handler(self):
        # NEW: LoguruHandler must subclass logging.Handler so Python's logging
        # machinery can attach it to the root logger like any other handler.
        # Without this inheritance the addHandler() call in
        # install_scrapy_root_handler() would raise a TypeError at runtime.
        handler = LoguruHandler("some_file.log")
        assert isinstance(handler, logging.Handler)

    def test_stores_filename(self):
        # NEW: The filename passed to __init__ must be saved as self.filename.
        # _uninstall_scrapy_root_handler() reads handler.filename to look up
        # the matching loguru sink in _loguru_sink_ids and remove it.
        # If the attribute were missing or wrong the sink would be permanently
        # leaked every time a crawler finishes.
        handler = LoguruHandler("/var/log/scrapy.log")
        assert handler.filename == "/var/log/scrapy.log"

    def test_emit_calls_loguru(self):
        # NEW: emit() must actually forward the record to loguru.
        # We patch loguru_logger.opt (the entry point LoguruHandler uses) and
        # assert it was called when a record is emitted.
        # If emit() silently swallowed records, log rotation would appear to
        # work (no crash) but nothing would be written to the rotating file.
        handler = LoguruHandler("dummy.log")
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello rotation",
            args=(),
            exc_info=None,
        )

        # loguru_logger.opt() returns a logger-like object; mock the full chain:
        # opt(exception=...).log(level, message)
        with mock.patch.object(loguru_logger, "opt") as mock_opt:
            mock_chained = mock.MagicMock()
            mock_opt.return_value = mock_chained
            handler.emit(record)

        mock_opt.assert_called_once()
        mock_chained.log.assert_called_once()
        # The second positional argument to .log() is the formatted message
        assert "hello rotation" in mock_chained.log.call_args[0][1]

    def test_emit_uses_correct_level(self):
        # NEW: emit() maps Python level names to loguru level names so that
        # severity is preserved in the rotating file.
        # We check every standard level: if the mapping were wrong (e.g. every
        # record logged as DEBUG) the log file would be misleading.
        handler = LoguruHandler("dummy.log")
        handler.setFormatter(logging.Formatter("%(message)s"))

        for py_level, expected_loguru_level in [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]:
            record = logging.LogRecord(
                name="test",
                level=py_level,
                pathname="",
                lineno=0,
                msg="msg",
                args=(),
                exc_info=None,
            )
            with mock.patch.object(loguru_logger, "opt") as mock_opt:
                mock_chained = mock.MagicMock()
                mock_opt.return_value = mock_chained
                handler.emit(record)

            # First positional arg to .log() is the level string
            actual_level = mock_chained.log.call_args[0][0]
            assert actual_level == expected_loguru_level, (
                f"Expected loguru level {expected_loguru_level!r} "
                f"for Python level {py_level}, got {actual_level!r}"
            )


# ── NEW: TestLogRotation ──────────────────────────────────────────────────────


# NEW: Tests for _install_loguru_sink() and the rotation branch of _get_handler().
# These cover the "wiring" between Scrapy settings and loguru.
class TestLogRotation:
    def teardown_method(self):
        # NEW: Every test that touches the real root handler or loguru sinks
        # must clean up afterwards, otherwise state leaks into other tests.
        # This is a known pitfall in Scrapy's suite (see issue #6996):
        # an installed handler from one test can cause another test's log
        # assertions to count extra records.
        #
        # We call the normal uninstall first (handles the root handler and
        # removes the matching sink from _loguru_sink_ids), then do a
        # belt-and-suspenders sweep of _loguru_sink_ids for anything left over
        # (e.g. sinks installed directly via _install_loguru_sink in a test
        # that never called install_scrapy_root_handler).
        _uninstall_scrapy_root_handler()
        for filename, sink_id in list(_loguru_sink_ids.items()):
            with contextlib.suppress(ValueError):
                loguru_logger.remove(sink_id)  # already gone, that is fine
            del _loguru_sink_ids[filename]

    def test_rotation_actually_writes_to_file(self):
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            install_scrapy_root_handler(
                _settings_with_file(path, {"LOG_FILE_ROTATE": "100 MB"})
            )
            # Write through Python's root logger — the full real path
            logging.getLogger("scrapy.test").warning("end to end rotation test")

            # enqueue=True means we must let the background thread flush.
            # Uninstalling removes the sink and joins the background thread,
            # guaranteeing all queued writes are on disk before we read the file.
            _uninstall_scrapy_root_handler()

            with Path(path).open(encoding="utf-8") as fh:
                contents = fh.read()
            assert "end to end rotation test" in contents
        finally:
            # If the test failed before _uninstall_scrapy_root_handler() ran,
            # the sink still holds the file open. Remove it explicitly so that
            # os.unlink() succeeds on Windows.
            if path in _loguru_sink_ids:
                loguru_logger.remove(_loguru_sink_ids.pop(path))
            Path(path).unlink()

    # ── _get_handler: which handler type is returned? ──────────────────────

    def test_get_handler_no_rotation_returns_file_handler(self):
        # NEW: When LOG_FILE is set but LOG_FILE_ROTATE is absent, _get_handler()
        # must return a plain FileHandler — the behaviour that existed before
        # this feature and must remain unchanged for users who don't want rotation.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            settings = _settings_with_file(path)  # no LOG_FILE_ROTATE key
            handler = _get_handler(settings)
            assert isinstance(handler, logging.FileHandler)
            assert not isinstance(handler, LoguruHandler)
        finally:
            handler.close()
            Path(path).unlink()

    def test_get_handler_with_rotation_returns_loguru_handler(self):
        # NEW: When both LOG_FILE and LOG_FILE_ROTATE are set, _get_handler()
        # must return a LoguruHandler instead of a FileHandler.
        # This is the core branch added by the feature: loguru needs to own the
        # file handle so it can rotate it.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            settings = _settings_with_file(path, {"LOG_FILE_ROTATE": "10 MB"})
            handler = _get_handler(settings)
            assert isinstance(handler, LoguruHandler)
        finally:
            handler.close()
            # Must remove the loguru sink before os.unlink on Windows —
            # enqueue=True keeps a background thread with the file handle open.
            if path in _loguru_sink_ids:
                loguru_logger.remove(_loguru_sink_ids.pop(path))
            Path(path).unlink()

    def test_get_handler_no_file_returns_stream_handler(self):
        # NEW: When LOG_FILE is not set at all, _get_handler() must still
        # return a StreamHandler (console output).
        # This is a regression guard: the rotation changes must not accidentally
        # break the default stdout logging path.

        settings = Settings(
            {
                "LOG_FILE": None,
                "LOG_ENABLED": True,
                "LOG_LEVEL": "DEBUG",
                "LOG_FORMAT": "%(message)s",
                "LOG_DATEFORMAT": "%Y-%m-%d",
                "LOG_SHORT_NAMES": False,
            }
        )
        handler = _get_handler(settings)
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, LoguruHandler)

    # ── _install_loguru_sink: sink registration ────────────────────────────

    def test_install_loguru_sink_registers_in_dict(self):
        # NEW: After _install_loguru_sink() runs, the log file path must appear
        # as a key in _loguru_sink_ids with an integer value (the sink id).
        # _uninstall_scrapy_root_handler() looks up the path in this dict to
        # know which loguru sink to remove; a missing entry means a leaked sink.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            settings = _settings_with_file(path, {"LOG_FILE_ROTATE": "10 MB"})
            _install_loguru_sink(settings)
            assert path in _loguru_sink_ids
            assert isinstance(_loguru_sink_ids[path], int)
        finally:
            if path in _loguru_sink_ids:
                loguru_logger.remove(_loguru_sink_ids.pop(path))
            Path(path).unlink()

    def test_install_loguru_sink_replaces_existing_sink_for_same_file(self):
        # NEW: Calling _install_loguru_sink() twice for the same file must not
        # register two sinks. If it did, every log message would be written
        # twice to that file — a silent duplication bug.
        # We verify the sink id changes (old one removed, new one created) and
        # that there is still exactly one entry for that path.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            settings = _settings_with_file(path, {"LOG_FILE_ROTATE": "10 MB"})
            _install_loguru_sink(settings)
            first_id = _loguru_sink_ids[path]

            _install_loguru_sink(settings)
            second_id = _loguru_sink_ids[path]

            assert second_id != first_id  # old sink replaced, not kept
            assert list(_loguru_sink_ids.keys()).count(path) == 1  # still one entry
        finally:
            if path in _loguru_sink_ids:
                loguru_logger.remove(_loguru_sink_ids.pop(path))
            Path(path).unlink()

    def test_install_loguru_sink_two_different_files_coexist(self):
        # NEW: When two crawlers run in the same process, each writing to a
        # different log file, their sinks must coexist independently in
        # _loguru_sink_ids. Installing a sink for file B must not evict the
        # sink for file A.
        with (
            tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f1,
            tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f2,
        ):
            path1, path2 = f1.name, f2.name
        try:
            _install_loguru_sink(
                _settings_with_file(path1, {"LOG_FILE_ROTATE": "10 MB"})
            )
            _install_loguru_sink(
                _settings_with_file(path2, {"LOG_FILE_ROTATE": "10 MB"})
            )

            assert path1 in _loguru_sink_ids
            assert path2 in _loguru_sink_ids
            # Each file has its own distinct sink
            assert _loguru_sink_ids[path1] != _loguru_sink_ids[path2]
        finally:
            for path in (path1, path2):
                if path in _loguru_sink_ids:
                    loguru_logger.remove(_loguru_sink_ids.pop(path))
                Path(path).unlink()

    # ── _uninstall_scrapy_root_handler: cleanup ────────────────────────────

    def test_uninstall_removes_loguru_sink(self):
        # NEW: When the root handler is a LoguruHandler and we call
        # _uninstall_scrapy_root_handler(), the matching entry must be removed
        # from _loguru_sink_ids.
        # Without this, every finished crawl in a long-running process
        # (e.g. Scrapyd) would accumulate orphaned sinks that keep writing to
        # the file even after the crawl is done.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            install_scrapy_root_handler(
                _settings_with_file(path, {"LOG_FILE_ROTATE": "10 MB"})
            )
            assert path in _loguru_sink_ids  # sanity-check: sink was registered

            _uninstall_scrapy_root_handler()

            assert path not in _loguru_sink_ids  # sink entry must be gone
        finally:
            Path(path).unlink()

    def test_uninstall_removes_root_handler(self):
        # NEW: After _uninstall_scrapy_root_handler() the root logger must have
        # no LoguruHandler attached. This is the original contract of _uninstall
        # (which existed before this feature) and must still hold for the new
        # handler type.
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            install_scrapy_root_handler(
                _settings_with_file(path, {"LOG_FILE_ROTATE": "10 MB"})
            )
            assert any(isinstance(h, LoguruHandler) for h in logging.root.handlers)

            _uninstall_scrapy_root_handler()

            assert not any(isinstance(h, LoguruHandler) for h in logging.root.handlers)
        finally:
            Path(path).unlink()

    def test_uninstall_is_idempotent(self):
        # NEW: Calling _uninstall_scrapy_root_handler() when nothing is installed,
        # or calling it twice in a row, must not raise any exception.
        # Scrapy calls _uninstall at the very start of install_scrapy_root_handler()
        # as a safety measure; if it crashed on a no-op call it would break every
        # subsequent crawl in the process.
        _uninstall_scrapy_root_handler()  # nothing installed yet — must not raise
        _uninstall_scrapy_root_handler()  # second call — must also not raise
