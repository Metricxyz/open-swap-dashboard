import logging

from dashboard import logging_config


def test_logging_writes_and_rotates_file(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_config, "MAX_LOG_BYTES", 300)
    monkeypatch.setattr(logging_config, "LOG_BACKUP_COUNT", 2)
    log_path = logging_config.configure_logging(tmp_path)
    logger = logging.getLogger("dashboard.rotation-test")

    for index in range(20):
        logger.info("rotation record=%s payload=%s", index, "x" * 80)
    for handler in logging.getLogger("dashboard").handlers:
        handler.flush()

    assert log_path.exists()
    assert log_path.with_suffix(".log.1").exists()
    assert len(list(log_path.parent.glob("open-metric-dashboard.log*"))) == 3
