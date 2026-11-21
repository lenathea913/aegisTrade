"""Alert bridge command line.

    python -m alert_bridge          # poll Prometheus every prometheus.alert_poll_seconds
    python -m alert_bridge --once   # one poll, then exit

Delivery uses the same ``build_alerter`` as the risk gate and the runner, so Telegram
credentials are read in exactly one place and an unconfigured stack still records every
alert in the structured log.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence

from aegis_alerting import build_alerter
from aegis_config import load_settings
from aegis_observability import configure_logging, get_logger
from aegis_observability.heartbeat import touch_heartbeat
from alert_bridge.bridge import AlertBridge

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade Prometheus alert bridge")
    parser.add_argument("--once", action="store_true", help="poll once, then exit")
    parser.add_argument(
        "--heartbeat-file",
        default=None,
        help="touch this file each poll so Docker can healthcheck the bridge",
    )
    args = parser.parse_args(argv)
    heartbeat_path: str | None = args.heartbeat_file

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.alert_bridge")
    bridge = AlertBridge(
        prometheus_url=settings.prometheus.url,
        alerter=build_alerter(settings.alerting),
    )
    logger.info(
        "alert_bridge_started",
        prometheus_url=settings.prometheus.url,
        poll_seconds=settings.prometheus.alert_poll_seconds,
        alerting_enabled=settings.alerting.enabled,
        channels=list(settings.alerting.channels),
    )
    try:
        while True:
            forwarded = bridge.poll()
            if forwarded:
                # The alert itself already went to the alerter; this row is the local
                # proof that the bridge did its job, with the names involved.
                logger.warning(
                    "alert_bridge_forwarded",
                    count=len(forwarded),
                    titles=[alert.title for alert in forwarded],
                )
            if heartbeat_path is not None:
                touch_heartbeat(heartbeat_path)
            if args.once:
                return 0
            time.sleep(settings.prometheus.alert_poll_seconds)
    except KeyboardInterrupt:
        logger.info("alert_bridge_stopped")
    finally:
        bridge.close()
    return 0
