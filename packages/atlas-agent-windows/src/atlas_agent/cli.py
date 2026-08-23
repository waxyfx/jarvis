"""Console entry point: ``atlas-agent``.

Commands map to what an operator actually does: enrol the machine, see what
state it is in, run it, work the kill switch, and manage autostart.

Note what is missing: there is no command, flag or API that *disables* the kill
switch from anywhere but this machine. ``safe-mode off`` runs locally, by a
person at the keyboard, and that is the only way out.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from atlas_agent import autostart
from atlas_agent.backend import BackendClient, BackendError
from atlas_agent.config import AgentSettings, _state_dir, get_agent_settings
from atlas_agent.identity import IdentityStore, IdentityStoreError
from atlas_agent.logging import configure_logging, get_logger
from atlas_agent.monitor import ActivityMonitor
from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_agent.safety.paths import PathGuard
from atlas_agent.transport import AgentTransport, FatalTransportError
from atlas_agent.tray import DEFAULT_HOTKEY_LABEL, GlobalHotkey, TrayApplication
from atlas_shared.auth import normalise_pairing_code
from atlas_shared.tools.manifest import RiskContext
from atlas_voice.profile import VoiceProfileStore

log = get_logger(__name__)


def _store(settings: AgentSettings) -> IdentityStore:
    return IdentityStore(settings.identity_path, allow_plaintext=settings.allow_plaintext_key)


def _controller(settings: AgentSettings) -> SafeModeController:
    return SafeModeController(settings.mode_state_path)


# ------------------------------------------------------------------ commands


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
    print("Server key pinned; commands signed by any other key will be refused.")
    return 0


async def _status(settings: AgentSettings) -> int:
    store = _store(settings)
    identity = store.load()
    controller = _controller(settings)

    print(f"Backend:    {settings.backend_url}")
    print(f"Identity:   {store.path}")
    if identity is None:
        print("State:      not paired")
    elif not identity.is_enrolled:
        print("State:      key generated but not enrolled")
    else:
        print(f"State:      paired as {identity.device_id}")
        if not identity.can_accept_commands:
            print("            ⚠ no pinned server key — re-pair before commands will run")

    mode = controller.current
    print(f"Mode:       {mode.mode.value} ({mode.reason})")
    print(f"Monitoring: {'enabled' if settings.monitor_enabled else 'disabled'}")
    print(f"File roots: {', '.join(settings.allowed_file_roots) or '(none configured)'}")

    autostart_state = autostart.status()
    print(f"Autostart:  {'installed' if autostart_state.installed else 'not installed'}")

    try:
        backend_state = await BackendClient(settings).pairing_status()
        print(f"Reachable:  yes (devices registered: {backend_state['devices']})")
    except BackendError as exc:
        print(f"Reachable:  no ({exc})")
        # Not an error condition: SAFE MODE and local controls work offline.
    return 0


def _safe_mode(settings: AgentSettings, action: str) -> int:
    controller = _controller(settings)

    if action == "on":
        change = controller.enter_safe_mode(
            "engaged from the command line", ModeChangeSource.LOCAL_CLI
        )
        print(f"SAFE MODE engaged at {change.at.isoformat()}.")
        print("Only low-risk local reads will run. Cloud vision is disabled.")
        return 0

    if action == "off":
        change = controller.leave_safe_mode(ModeChangeSource.LOCAL_CLI)
        print(f"SAFE MODE released at {change.at.isoformat()}.")
        return 0

    current = controller.current
    print(f"Mode:   {current.mode.value}")
    print(f"Reason: {current.reason}")
    print(f"Source: {current.source.value}")
    print(f"Since:  {current.at.isoformat()}")
    print(f"State:  {settings.mode_state_path}")
    return 0


def _autostart(action: str) -> int:
    if action == "install":
        state = autostart.install()
        print(f"Autostart installed: {state.detail}")
        print("Runs at logon, in your session, with limited privileges (no administrator rights).")
        return 0
    if action == "uninstall":
        autostart.uninstall()
        print("Autostart removed.")
        return 0

    state = autostart.status()
    print(f"Autostart: {'installed' if state.installed else 'not installed'}")
    if state.installed:
        print(f"           {state.detail}")
    return 0


async def _run(
    settings: AgentSettings,
    *,
    with_tray: bool,
    with_voice: bool = False,
    input_device: int | None = None,
    output_device: int | None = None,
) -> int:
    store = _store(settings)
    identity = store.load()
    if identity is None or not identity.is_enrolled:
        print("Not paired. Run: atlas-agent pair --code XXXX-XXXX")
        return 1

    controller = _controller(settings)
    if controller.is_safe:
        log.warning("agent_starting_in_safe_mode", reason=controller.current.reason)

    # Set below if voice is running, so the engine can show Executing rather
    # than Thinking while a program actually opens.
    on_activity: Callable[[bool], None] | None = None

    def report_activity(running: bool) -> None:
        if on_activity is not None:
            on_activity(running)

    runner = ToolRunner(
        safe_mode=controller,
        path_guard=PathGuard(
            settings.allowed_file_roots, extra_denied=settings.denied_path_patterns
        ),
        risk_context=RiskContext(
            allowed_roots=settings.allowed_file_roots,
            executable_roots=settings.allowed_executable_roots,
        ),
        on_activity=report_activity,
    )
    monitor = ActivityMonitor(settings)
    transport = AgentTransport(
        settings, identity, runner=runner, safe_mode=controller, monitor=monitor
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        loop.call_soon_threadsafe(stop.set)

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, stop.set)
        except NotImplementedError:
            # Windows event loops do not support add_signal_handler; the
            # KeyboardInterrupt path in main() covers Ctrl+C there.
            signal.signal(signal_number, lambda *_: stop.set())

    tray = TrayApplication(safe_mode=controller, monitor=monitor, on_quit=request_stop)

    hotkey: GlobalHotkey | None = None
    if settings.enable_hotkey:

        def engage() -> None:
            # Local, immediate, and independent of the network: this is what
            # makes it a kill switch rather than a request.
            controller.toggle(ModeChangeSource.LOCAL_HOTKEY)
            log.info("hotkey_pressed", mode=controller.mode.value)
            tray.refresh()

        hotkey = GlobalHotkey(engage)
        if hotkey.start():
            log.info("kill_switch_ready", hotkey=DEFAULT_HOTKEY_LABEL)

    tray_thread: threading.Thread | None = None
    if with_tray and settings.enable_tray and tray.available():
        tray_thread = threading.Thread(target=tray.run, name="atlas-tray", daemon=True)
        tray_thread.start()

    voice_task: asyncio.Task[None] | None = None
    if with_voice:
        # In this process, not a second one. The voice path and the tool path
        # are the same device: two processes would mean two connections for one
        # identity, and the backend displaces the older -- so whichever started
        # first would quietly stop working.
        from atlas_agent.voice_runtime import VoiceModels, build_runtime

        models = VoiceModels(root=Path(__file__).resolve().parents[4] / ".models")
        absent = models.missing()
        if absent:
            print("Voice models are missing. Run scripts/fetch_voice_models.ps1")
            for item in absent:
                print(f"  - {item}")
            return 1

        print("Loading the voice models. This takes a moment.")
        runtime = await build_runtime(
            settings=settings,
            identity=identity,
            store=_voice_store(),
            models=models,
            input_device=input_device,
            output_device=output_device,
            on_event=lambda event: log.info("voice", kind=event.kind, detail=event.detail),
        )
        on_activity = lambda running: (  # noqa: E731 - one line, one purpose
            runtime.session.note_executing() if running else runtime.session.note_executed()
        )
        runtime.session.states.observe(lambda transition: print(f"  [{transition.current.value}]"))
        voice_task = asyncio.create_task(runtime.run(stop=stop))
        print('Listening. Say "Jarvis".')

    try:
        await transport.run(stop=stop)
    except FatalTransportError as exc:
        log.error("agent_stopped", reason=str(exc))
        return 2
    finally:
        if voice_task is not None:
            voice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await voice_task
        if hotkey is not None:
            hotkey.stop()
        tray.stop()
        if tray_thread is not None:
            tray_thread.join(timeout=3.0)
    return 0


def _voice_store() -> VoiceProfileStore:
    """The enrolled profile, under the same DPAPI the device key uses."""
    from atlas_agent.voice_ui import _dpapi_protector

    return VoiceProfileStore(_state_dir() / "voice_profile.bin", protector=_dpapi_protector())


# ---------------------------------------------------------------------- main


def _enroll_voice(args: argparse.Namespace) -> int:
    """Open the voice enrollment window, or list the microphones.

    Imported lazily: the window pulls in tkinter and the speaker model, and
    `atlas-agent status` should not pay for either.
    """
    from pathlib import Path

    from atlas_agent.config import _state_dir
    from atlas_voice.capture import list_input_devices
    from atlas_voice.providers import VoiceEngineError

    if args.list_devices:
        # The Windows console defaults to a legacy code page, and device names
        # are full of Cyrillic here. Without this the listing is mojibake.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        try:
            devices = list_input_devices()
        except VoiceEngineError as error:
            print(error)
            return 1
        for device in devices:
            marker = " (default)" if device.is_default else ""
            name = " ".join(device.name.split())[:60]
            print(f"  {device.index:3}  {name}{marker}")
        return 0

    from atlas_agent.voice_ui import run_enrollment

    models = Path(__file__).resolve().parents[4] / ".models"
    return run_enrollment(models_dir=models, state_dir=_state_dir(), device=args.device)


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-agent", description="ATLAS Windows Agent.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    pair = subcommands.add_parser("pair", help="enrol this machine using a pairing code")
    pair.add_argument("--code", required=True, help="pairing code, e.g. 4F2K-9X1M")

    subcommands.add_parser("status", help="show identity, mode, autostart and reachability")

    run = subcommands.add_parser("run", help="connect and stay connected")
    run.add_argument("--no-tray", action="store_true", help="run headless, without the tray icon")
    run.add_argument(
        "--voice", action="store_true", help="listen for the wake word and answer aloud"
    )
    run.add_argument("--input-device", type=int, default=None, help="microphone index")
    run.add_argument("--output-device", type=int, default=None, help="speaker index")

    safe = subcommands.add_parser("safe-mode", help="local kill switch")
    safe.add_argument("action", choices=["on", "off", "status"])

    auto = subcommands.add_parser("autostart", help="start the agent at logon")
    auto.add_argument("action", choices=["install", "uninstall", "status"])

    voice = subcommands.add_parser(
        "enroll-voice", help="open the window that registers your voice with JARVIS"
    )
    voice.add_argument(
        "--device", type=int, default=None, help="input device index; the default is used otherwise"
    )
    voice.add_argument("--list-devices", action="store_true", help="print the microphones and exit")

    args = parser.parse_args()
    settings = get_agent_settings()
    configure_logging(level=settings.log_level)

    try:
        if args.command == "pair":
            sys.exit(asyncio.run(_pair(settings, args.code)))
        elif args.command == "enroll-voice":
            sys.exit(_enroll_voice(args))
        elif args.command == "status":
            sys.exit(asyncio.run(_status(settings)))
        elif args.command == "safe-mode":
            sys.exit(_safe_mode(settings, args.action))
        elif args.command == "autostart":
            sys.exit(_autostart(args.action))
        else:
            sys.exit(
                asyncio.run(
                    _run(
                        settings,
                        with_tray=not args.no_tray,
                        with_voice=args.voice,
                        input_device=args.input_device,
                        output_device=args.output_device,
                    )
                )
            )
    except KeyboardInterrupt:
        sys.exit(130)
    except (BackendError, IdentityStoreError, autostart.AutostartError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
