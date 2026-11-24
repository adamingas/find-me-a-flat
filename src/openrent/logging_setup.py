"""Configure standard Python logging for container stdout."""

import logging
import sys
import time

from dbos import DBOS


class WorkflowContext(logging.Filter):
    def filter(self, record):
        record.workflow_id = DBOS.workflow_id or "-"
        record.step_id = DBOS.step_id or "-"
        return True


def configure_logging(*, quiet=False):
    logger = logging.getLogger("openrent")
    logger.setLevel(logging.WARNING if quiet else logging.INFO)
    logger.propagate = False
    for existing in logger.handlers[:]:
        if existing.name == "openrent-stdout":
            logger.removeHandler(existing)
            existing.close()
    handler = logging.StreamHandler(sys.stdout)
    handler.name = "openrent-stdout"
    handler.addFilter(WorkflowContext())
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s workflow=%(workflow_id)s step=%(step_id)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
