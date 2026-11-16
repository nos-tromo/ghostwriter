"""The backend's logging: what the standard-library loggers of its dependencies may write."""

import logging

import pytest

from ghostwriter.log_cfg import setup_logger


@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_the_http_loggers_are_pinned_to_warning(name: str, caplog: pytest.LogCaptureFixture) -> None:
    """Below WARNING nothing is emitted: httpx logs each request URL at INFO, and a view URL carries a file name."""
    logging.getLogger(name).setLevel(logging.NOTSET)
    setup_logger()
    pinned = logging.getLogger(name)
    assert pinned.level == logging.WARNING
    with caplog.at_level(logging.DEBUG):
        pinned.info("GET http://workbench.test/files/view?path=/workspace/x/sales.xlsx")
        logging.getLogger(f"{name}.child").debug("synthetic")
    assert caplog.records == []
