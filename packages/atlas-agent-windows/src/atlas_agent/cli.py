"""Console entry point: ``atlas-agent``.

Three commands, matching the three things an operator does: enrol the machine
once, check what state it is in, and run it.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from atlas_agent.backend import BackendClient, BackendError
from atlas_agent.config import AgentSettings, get_agent_settings
from atlas_agent.identity import IdentityStore, IdentityStoreError
from atlas_agent.logging import configure_logging, get_logger
from atlas_agent.transport import AgentTransport, FatalTransportError
from atlas_shared.auth import normalise_pairing_code

log = get_logger(__name__)


def _store(settings: AgentSettings) -> IdentityStore:
    return IdentityStore(settings.identity_path, allow_plaintext=settings.allow_plaintext_key)


async def _pair(settings: AgentSettings, code: str) -> int:
    store = _store(settings)

    existing = store.load()
    if existing is not None and existing.is_enrolled:
        print(
            f"Already paired as device {existing.device_id}.\n"
            f"To re-pair, delete {store.path} first — this machine will get a new identity."
        )
        return 1

    identity = existing or store.create()
    enrolled = await BackendClient(settings).enrol(identity, normalise_pairing_code(code))
    store.save(enrolled)

    print(f"Paired. Device id: {enrolled.device_id}")
    print(f"Identity stored at: {store.path}")
    return 0


async def _status(settings: AgentSettings) -> int:
    store = _store(settings)
    identity = store.load()

    print(f"Backend:  {settings.backend_url}")
    print(f"Identity: {store.path}")
    if identity is None:
        print("State:    not paired")
    elif not identity.is_enrolled:
        print("State:    key generated but not enrolled")
    else:
        print(f"State:    paired as {identity.device_id}")

    try:
        status = await BackendClient(settings).pairing_status()
        print(f"Reachable: yes (devices registered: {status['devices']})")
    except BackendError as exc:
        print(f"Reachable: no ({exc})")
        return 1
    return 0


async def _run(settings: AgentSettings) -> int:
    store = _store(settings)
    identity = store.load()
    if identity is None or not identity.is_enrolled:
        print("Not paired. Run: atlas-agent pair --code XXXX-XXXX")
        return 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except NotImplementedError:
            # Windows event loops do not support add_signal_handler; the
            # KeyboardInterrupt path below covers Ctrl+C there.
            signal.signal(signal_number, lambda *_: stop.set())

    transport = AgentTransport(settings, identity)
    try:
        await transport.run(stop=stop)
    except FatalTransportError as exc:
        log.error("agent_stopped", reason=str(exc))
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-agent", description="ATLAS Windows Agent.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    pair = subcommands.add_parser("pair", help="enrol this machine using a pairing code")
    pair.add_argument("--code", required=True, help="pairing code, e.g. 4F2K-9X1M")

    subcommands.add_parser("status", help="show identity and backend reachability")
    subcommands.add_parser("run", help="connect and stay connected")

    args = parser.parse_args()
    settings = get_agent_settings()
    configure_logging(level=settings.log_level)

    try:
        if args.command == "pair":
            code = args.code
            sys.exit(asyncio.run(_pair(settings, code)))
        elif args.command == "status":
            sys.exit(asyncio.run(_status(settings)))
        else:
            sys.exit(asyncio.run(_run(settings)))
    except KeyboardInterrupt:
        sys.exit(130)
    except (BackendError, IdentityStoreError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
