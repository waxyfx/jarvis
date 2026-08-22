"""The voice enrollment window.

Tkinter, because it ships with Python: no extra dependency, no download, no
licence to read. The window is small and does one thing, so the widget toolkit
is not where the value is.

What it is for: the owner opens it, reads twelve short phrases, and ends up with
a voice profile. Everything that can go wrong while recording — too quiet, too
loud, clipped, cut off — is caught *while they are still sitting there* and said
in words they can act on, because the alternative is a profile that quietly
stops recognising them a fortnight later.

The window owns no logic. Judging takes, averaging embeddings, spotting a
recording that disagrees with the others and deciding whether the profile is any
good all live in :mod:`atlas_voice.enrollment`, which is tested without a
microphone. This file starts a recording, draws a level meter, and shows what
the session decided.

**Nothing here reaches the network.** The audio, the embedding and the profile
stay on this machine; the recordings are deleted once the profile exists.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np

from atlas_shared.enums import Language
from atlas_voice.audio import SAMPLE_RATE
from atlas_voice.capture import Microphone, list_input_devices
from atlas_voice.engines.speaker import SherpaSpeaker
from atlas_voice.enrollment import EnrollmentSession, Prompt, TakeVerdict
from atlas_voice.profile import Protector, VoiceProfileStore
from atlas_voice.providers import VoiceEngineError

__all__ = ["EnrollmentWindow", "run_enrollment"]

#: How long each phrase gets. Long enough for the longest line at an unhurried
#: pace; the take is trimmed to what was actually said.
RECORD_SECONDS = 5.0
#: The meter redraws this often. Fast enough to feel live, slow enough that
#: tkinter is not the bottleneck.
METER_MS = 50


@dataclass
class _Recorded:
    samples: np.ndarray
    verdict: TakeVerdict


class EnrollmentWindow:
    """The wizard. Construct with a speaker engine and a profile store."""

    def __init__(
        self,
        *,
        speaker: SherpaSpeaker,
        store: VoiceProfileStore,
        device: int | None = None,
    ) -> None:
        self._speaker = speaker
        self._store = store
        self._device = device
        self._session: EnrollmentSession | None = None
        self._script: tuple[Prompt, ...] = ()
        self._index = 0
        self._results: queue.Queue[_Recorded | Exception] = queue.Queue()
        self._level = 0.0
        self._recording = False

        self._root = tk.Tk()
        self._root.title("JARVIS — Voice & Identity")
        self._root.geometry("620x420")
        self._root.minsize(560, 400)
        self._build()
        self._refresh_profile()

    # ------------------------------------------------------------------ chrome

    def _build(self) -> None:
        outer = ttk.Frame(self._root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="My Voice", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        self._profile_label = ttk.Label(outer, text="", foreground="#555")
        self._profile_label.pack(anchor="w", pady=(2, 12))

        self._phrase = tk.StringVar(value="Press Enroll my voice to begin.")
        phrase_box = ttk.Frame(outer, relief="groove", padding=14)
        phrase_box.pack(fill="x")
        ttk.Label(
            phrase_box,
            textvariable=self._phrase,
            font=("Segoe UI", 13),
            wraplength=540,
            justify="left",
        ).pack(anchor="w")

        self._hint = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self._hint, foreground="#b00", wraplength=560).pack(
            anchor="w", pady=(8, 4)
        )

        ttk.Label(outer, text="Microphone level").pack(anchor="w", pady=(10, 2))
        self._meter = ttk.Progressbar(outer, maximum=100, length=560)
        self._meter.pack(fill="x")

        self._progress_text = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self._progress_text).pack(anchor="w", pady=(10, 2))
        self._progress = ttk.Progressbar(outer, maximum=100, length=560)
        self._progress.pack(fill="x")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(18, 0))
        self._enroll_button = ttk.Button(buttons, text="Enroll my voice", command=self._start)
        self._enroll_button.pack(side="left")
        self._record_button = ttk.Button(
            buttons, text="Record phrase", command=self._record, state="disabled"
        )
        self._record_button.pack(side="left", padx=6)
        self._retry_button = ttk.Button(
            buttons, text="Try again", command=self._record, state="disabled"
        )
        self._retry_button.pack(side="left")

        lower = ttk.Frame(outer)
        lower.pack(fill="x", pady=(10, 0))
        self._test_button = ttk.Button(lower, text="Test my voice", command=self._test)
        self._test_button.pack(side="left")
        ttk.Button(lower, text="Re-enroll", command=self._start).pack(side="left", padx=6)
        self._delete_button = ttk.Button(lower, text="Delete voice profile", command=self._delete)
        self._delete_button.pack(side="left")

        self._status = tk.StringVar(value=self._device_summary())
        ttk.Label(outer, textvariable=self._status, foreground="#555").pack(
            anchor="w", pady=(14, 0)
        )

    def _device_summary(self) -> str:
        try:
            devices = list_input_devices()
        except VoiceEngineError as error:
            return str(error)
        if not devices:
            return "No microphone was found."
        chosen = next(
            (d for d in devices if d.index == self._device),
            next((d for d in devices if d.is_default), devices[0]),
        )
        return f"Microphone: {chosen.name}"

    def _refresh_profile(self) -> None:
        profile = self._store.load()
        if profile is None:
            self._profile_label.config(text="No voice profile yet.")
            self._test_button.config(state="disabled")
            self._delete_button.config(state="disabled")
            return
        # Coverage is shown next to cohesion because on its own cohesion
        # misleads: a narrow profile scores highest of all.
        covers = ", ".join(profile.covers) if profile.covers else "one way of speaking only"
        self._profile_label.config(
            text=(
                f"Profile: {profile.phrases} phrases, {profile.quality} "
                f"(cohesion {profile.cohesion:.2f}; heard {covers}), "
                f"created {profile.created_at}"
            )
        )
        self._test_button.config(state="normal")
        self._delete_button.config(state="normal")

    # ----------------------------------------------------------------- flow

    def _start(self) -> None:
        if self._store.exists() and not messagebox.askyesno(
            "Re-enroll",
            "This replaces the existing voice profile. Continue?",
            parent=self._root,
        ):
            return

        self._speaker.forget()
        self._session = EnrollmentSession(embed=self._speaker, store=self._store)
        self._script = self._session.script()
        self._index = 0
        self._hint.set("")
        self._enroll_button.config(state="disabled")
        self._record_button.config(state="normal")
        self._retry_button.config(state="disabled")
        self._show_phrase()

    def _show_phrase(self) -> None:
        assert self._session is not None
        if self._index >= len(self._script):
            self._finish()
            return

        prompt = self._script[self._index]
        tag = "English" if prompt.language is Language.EN else "Русский"
        # The instruction goes above the line, not below it: people start
        # reading the moment they see words, and by then it is too late to
        # be told how.
        lead = f"{prompt.hint}\n\n" if prompt.hint else ""
        self._phrase.set(f"{lead}{tag}:\n\n{prompt.text}")
        done = self._session.collected
        total = self._session.phrase_count
        self._progress_text.set(f"Recorded {done} of {total}")
        self._progress.config(value=100 * done / total)

    def _record(self) -> None:
        if self._recording or self._session is None:
            return
        self._recording = True
        self._hint.set("")
        self._record_button.config(state="disabled")
        self._retry_button.config(state="disabled")
        # Rebuilt from the phrase rather than appended to whatever is on
        # screen: appending stacked a second Recording line on every retry.
        self._show_phrase()
        self._phrase.set(self._phrase.get() + "\n\n● Recording…")

        threading.Thread(target=self._capture, daemon=True).start()
        self._root.after(METER_MS, self._tick)

    def _capture(self) -> None:
        """Runs off the UI thread: tkinter must keep drawing the meter."""
        try:
            microphone = Microphone(device=self._device)
            collected: list[np.ndarray] = []
            wanted = int(RECORD_SECONDS * SAMPLE_RATE)
            gathered = 0
            for frame in microphone.frames():
                collected.append(frame.samples)
                gathered += len(frame.samples)
                self._level = float(np.abs(frame.samples).max(initial=0.0))
                if gathered >= wanted:
                    break
            microphone.stop()

            samples = (
                np.concatenate(collected)[:wanted] if collected else np.zeros(0, dtype=np.float32)
            )
            assert self._session is not None
            self._results.put(_Recorded(samples, self._session.judge(samples)))
        except Exception as error:
            self._results.put(error)

    def _tick(self) -> None:
        self._meter.config(value=min(100.0, self._level * 140))
        try:
            outcome = self._results.get_nowait()
        except queue.Empty:
            if self._recording:
                self._root.after(METER_MS, self._tick)
            return

        self._recording = False
        self._level = 0.0
        self._meter.config(value=0)

        if isinstance(outcome, Exception):
            self._hint.set(f"The microphone failed: {outcome}")
            self._retry_button.config(state="normal")
            return

        self._handle(outcome)

    def _handle(self, recorded: _Recorded) -> None:
        assert self._session is not None
        if not recorded.verdict.accepted:
            self._hint.set(recorded.verdict.advice or recorded.verdict.reason)
            self._retry_button.config(state="normal")
            self._show_phrase()
            return

        prompt = self._script[self._index]
        verdict = self._session.add(recorded.samples, phrase=prompt.text, manner=prompt.manner)
        if not verdict.accepted:
            self._hint.set(verdict.advice or verdict.reason)
            self._retry_button.config(state="normal")
            self._show_phrase()
            return

        self._index += 1
        self._record_button.config(state="normal")
        self._show_phrase()

    def _finish(self) -> None:
        assert self._session is not None
        try:
            profile = self._session.finish()
        except VoiceEngineError as error:
            self._hint.set(str(error))
            self._enroll_button.config(state="normal")
            return

        self._speaker.forget()
        self._record_button.config(state="disabled")
        self._retry_button.config(state="disabled")
        self._enroll_button.config(state="normal")
        self._phrase.set(
            "Done, sir.\n\n"
            f"{profile.phrases} phrases used, quality {profile.quality} "
            f"(cohesion {profile.cohesion:.2f}).\n\n"
            "The recordings have been deleted; only the voice profile remains, "
            "encrypted on this machine.\n\n"
            "Press Test my voice to check it."
        )
        self._progress.config(value=100)
        self._refresh_profile()

    # ---------------------------------------------------------------- actions

    def _test(self) -> None:
        if self._recording:
            return
        self._phrase.set("Say anything at all — a sentence is plenty.\n\n● Recording…")
        self._hint.set("")
        self._recording = True

        def capture() -> None:
            try:
                microphone = Microphone(device=self._device)
                samples = microphone.record(4.0)
                self._results.put(_Recorded(samples, TakeVerdict(True)))
            except Exception as error:
                self._results.put(error)

        threading.Thread(target=capture, daemon=True).start()
        self._root.after(METER_MS, self._tick_test)

    def _tick_test(self) -> None:
        self._meter.config(value=min(100.0, self._level * 140))
        try:
            outcome = self._results.get_nowait()
        except queue.Empty:
            if self._recording:
                self._root.after(METER_MS, self._tick_test)
            return

        self._recording = False
        self._meter.config(value=0)
        if isinstance(outcome, Exception):
            self._hint.set(f"The microphone failed: {outcome}")
            return

        try:
            self._speaker.forget()
            result = self._speaker.verify(outcome.samples)
        except VoiceEngineError as error:
            self._hint.set(str(error))
            return

        verdict = "recognised" if result.accepted else "not recognised"
        self._phrase.set(
            f"You were {verdict}, sir.\n\n"
            f"Similarity {result.score:.2f}, threshold {result.threshold:.2f}.\n\n"
            "This decides whose speech JARVIS listens to. It is not a password: "
            "anything that changes the system still needs its confirmation."
        )

    def _delete(self) -> None:
        if not messagebox.askyesno(
            "Delete voice profile",
            "Delete the stored voice profile? JARVIS will stop recognising your voice "
            "until you enrol again.",
            parent=self._root,
        ):
            return
        self._store.delete()
        self._speaker.forget()
        self._phrase.set("The voice profile has been deleted.")
        self._hint.set("")
        self._progress.config(value=0)
        self._progress_text.set("")
        self._refresh_profile()

    def run(self) -> None:
        self._root.mainloop()


def run_enrollment(*, models_dir: Path, state_dir: Path, device: int | None = None) -> int:
    """Open the window. Returns a process exit code."""
    model = models_dir / "speaker" / "eres2net_base_sv.onnx"
    store = VoiceProfileStore(state_dir / "voice_profile.bin", protector=_dpapi_protector())

    try:
        speaker = SherpaSpeaker(model, store=store)
    except VoiceEngineError as error:
        print(f"Voice enrollment cannot start: {error}")
        return 1

    EnrollmentWindow(speaker=speaker, store=store, device=device).run()
    return 0


def _dpapi_protector() -> Protector:
    """The same user-scoped protection the device key already uses.

    Imported here rather than at module scope so the window can be reasoned
    about — and imported — on a machine without pywin32.
    """
    from atlas_agent.identity import _dpapi_protect, _dpapi_unprotect

    return (_dpapi_protect, _dpapi_unprotect)
