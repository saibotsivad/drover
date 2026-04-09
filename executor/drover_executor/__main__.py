"""CLI entry point: ``python -m drover_executor``."""

import argparse
import asyncio
import logging

from drover_executor.agent import Agent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drover executor — guest agent for micro-containers",
    )
    parser.add_argument(
        "--socket",
        default="/run/orchestrator.sock",
        help="Path to the orchestrator Unix socket (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=2.0,
        help="Seconds between heartbeats (default: %(default)s)",
    )
    parser.add_argument(
        "--max-concurrent-commands",
        type=int,
        default=None,
        help="Max concurrent commands (default: unlimited)",
    )
    parser.add_argument(
        "--no-auto-heartbeat",
        action="store_true",
        help="Disable automatic heartbeats",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level (default: %(default)s)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=__import__("sys").stderr,
    )

    agent = Agent(
        socket_path=args.socket,
        heartbeat_interval=args.heartbeat_interval,
        max_concurrent_commands=args.max_concurrent_commands,
        auto_heartbeat=not args.no_auto_heartbeat,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
