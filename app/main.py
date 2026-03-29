"""Entry point for the EV charger integration."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import traceback

from app.config import ConfigError, ConfigManager
from app.control_loop import ControlLoop
from app.logging_setup import setup_logging
from app.modbus_ev import EVChargerModbusClient
from app.modbus_victron import VictronModbusClient
from app.mqtt_client import MQTTClient

logger = logging.getLogger(__name__)

_RESTART_DELAY_S = 5.0


async def _supervised(coro_factory, name: str, *, _restart_delay: float = _RESTART_DELAY_S) -> None:
    """Run *coro_factory()* in a loop, restarting after unhandled exceptions."""
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Unhandled exception in %s:\n%s", name, traceback.format_exc())
            await asyncio.sleep(_restart_delay)


async def _async_main(config_path: str) -> None:
    """Wire all components and run the event loop."""
    config_manager = ConfigManager(config_path)
    try:
        state = config_manager.load()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    publish_queue: asyncio.Queue = asyncio.Queue()

    victron_client = VictronModbusClient(state)
    ev_client = EVChargerModbusClient(state)
    control_loop = ControlLoop(state, ev_client, publish_queue, config_manager=config_manager)
    mqtt_client = MQTTClient(
        state,
        config_manager,
        publish_queue,
        victron_client=victron_client,
        ev_client=ev_client,
    )

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    tasks = [
        asyncio.create_task(_supervised(victron_client.poll_loop, "victron_client"), name="victron"),
        asyncio.create_task(_supervised(ev_client.poll_loop, "ev_client"), name="ev"),
        asyncio.create_task(_supervised(control_loop.run_loop, "control_loop"), name="control"),
        asyncio.create_task(_supervised(mqtt_client.run_loop, "mqtt_client"), name="mqtt"),
        asyncio.create_task(_supervised(config_manager.flush_loop, "config_flush"), name="flush"),
    ]

    logger.info("All tasks started")

    await shutdown_event.wait()

    logger.info("Shutting down gracefully")
    await mqtt_client.shutdown()

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Shutdown complete")


def main() -> None:
    """Parse CLI args and run the async entry point."""
    parser = argparse.ArgumentParser(description="EV Charger Integration")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    args = parser.parse_args()

    setup_logging()
    asyncio.run(_async_main(args.config))


if __name__ == "__main__":
    main()
