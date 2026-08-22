"""Turning what the recogniser heard into what the user meant.

Whisper assigns one language per utterance, and that is the whole problem with
code-switching. «Открой VS Code and start my project» is Russian by majority, so
the English inside it gets decoded through Russian phonotactics and comes back
transliterated: «вс код», «вэс коуд», «старт май проджект».

The tool layer then fails to match an application name that was, acoustically,
said perfectly clearly. The fix is not a better model — it is a small table of
the things this assistant actually knows how to open, applied after decoding.

Two rules keep the table honest:

* it only ever maps onto names the **tool catalogue already knows**, so it
  cannot invent a target;
* it matches on word boundaries, so «шарф» is never rewritten because it shares
  letters with something.

Everything here is deterministic and cheap. Nothing about it is a security
control: a wrong substitution produces a wrong tool argument, which the Policy
Engine and the path guard still judge exactly as they would any other.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

__all__ = ["Normaliser", "TranscriptRules"]


def _fold(text: str) -> str:
    """Lowercase, strip accents, and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass(frozen=True)
class TranscriptRules:
    """What the recogniser tends to hear, and what it should have been.

    Keys are folded on construction, so the table can be written the way a
    person would say it.
    """

    #: Application and product names, in the spellings Whisper produces when it
    #: decodes them through Russian. Measured from real transcripts rather than
    #: imagined: guessing at misrecognitions produces a table that never fires.
    aliases: dict[str, str] = field(
        default_factory=lambda: {
            # VS Code
            "вс код": "VS Code",
            "вэс код": "VS Code",
            "вс коуд": "VS Code",
            "ви эс код": "VS Code",
            "вискод": "VS Code",
            "вс-код": "VS Code",
            # Chrome
            "хром": "Chrome",
            "кром": "Chrome",
            "хроум": "Chrome",
            # Notepad
            "блокнот": "Notepad",
            "нотпад": "Notepad",
            "ноутпад": "Notepad",
            # Others the catalogue can plausibly be asked for
            "телеграм": "Telegram",
            "телеграмм": "Telegram",
            "повершелл": "PowerShell",
            "пауэршелл": "PowerShell",
            "проводник": "Explorer",
            "калькулятор": "Calculator",
            "спотифай": "Spotify",
            "дискорд": "Discord",
        }
    )

    #: Filler the recogniser emits that carries no instruction.
    drop: tuple[str, ...] = ("ээ", "эм", "мм", "uh", "um", "erm")

    def folded_aliases(self) -> dict[str, str]:
        return {_fold(key): value for key, value in self.aliases.items()}


class Normaliser:
    """Rewrites a transcript, and can say what it changed."""

    def __init__(
        self, rules: TranscriptRules | None = None, *, known_names: frozenset[str] | None = None
    ) -> None:
        """
        ``known_names`` is the set of things the tool layer can actually act on,
        folded. When given, a rule whose target is not in it is dropped at
        construction — a table that can rewrite text into a name nothing
        recognises is worse than no table, because the failure moves from "did
        not understand" to "understood something that does not exist".
        """
        self._rules = rules or TranscriptRules()
        aliases = self._rules.folded_aliases()
        if known_names is not None:
            allowed = {_fold(name) for name in known_names}
            aliases = {heard: meant for heard, meant in aliases.items() if _fold(meant) in allowed}
        # Longest first, so «ви эс код» wins over any shorter overlapping rule.
        self._aliases = dict(sorted(aliases.items(), key=lambda kv: -len(kv[0])))
        self._patterns = {
            heard: re.compile(rf"(?<!\w){re.escape(heard)}(?!\w)", re.IGNORECASE)
            for heard in self._aliases
        }
        self._filler = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(word) for word in self._rules.drop) + r")(?!\w)",
            re.IGNORECASE,
        )

    def apply(self, text: str) -> tuple[str, list[str]]:
        """Return the rewritten text and a list of the substitutions made."""
        applied: list[str] = []
        folded = _fold(text)
        result = text

        for heard, meant in self._aliases.items():
            if self._patterns[heard].search(folded) is None:
                continue
            # Rewrite against the folded form to find positions, but substitute
            # in the original so casing and punctuation elsewhere survive.
            pattern = re.compile(rf"(?<!\w){re.escape(heard)}(?!\w)", re.IGNORECASE)
            rewritten = pattern.sub(meant, result)
            if rewritten != result:
                applied.append(f"{heard} -> {meant}")
                result = rewritten
            folded = _fold(result)

        result = self._filler.sub("", result)
        result = re.sub(r"\s+([,.!?])", r"\1", result)
        return re.sub(r"\s+", " ", result).strip(), applied
