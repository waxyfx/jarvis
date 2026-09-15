"""A small rule-based personality layer that cannot rewrite the facts.

The complete original reply is retained, byte for byte. Character is an optional
short preface, never a technical claim. Protected turns pass through unchanged.
History consists only of a few enum ids; no utterance, emotional inference or
user identity is stored by the provider. The caller owns one history per session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from atlas_shared.enums import Language


class Mode(StrEnum):
    PROFESSIONAL = "professional"
    JARVIS = "jarvis"
    PERSONAL = "personal"
    CUSTOM = "custom"


class Address(StrEnum):
    AUTO = "auto"
    NONE = "none"
    SIR = "sir"


class StyleConfig(BaseModel):
    """Explicit settings, not extracted from user/model text.

    Unknown dials are rejected. Sarcasm/profanity and generative rewriting are
    deliberately not implemented by this first conservative provider.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Mode = Mode.JARVIS
    address: Address = Address.AUTO
    #: Number of undecorated replies before the same preface may be used again.
    cooldown_turns: int = Field(default=2, ge=1, le=8, strict=True)


class Preface(StrEnum):
    PLAIN = "plain"
    ADDRESS = "address"
    PERSONAL = "personal"


@dataclass(frozen=True, slots=True)
class StyleHistory:
    """Bounded presentation ids only; immutable and safe to discard on restart."""

    recent: tuple[Preface, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.recent, tuple) or len(self.recent) > 8:
            raise ValueError("history must be a tuple of at most eight preface ids")
        if any(not isinstance(item, Preface) for item in self.recent):
            raise ValueError("history stores preface ids, not user text")

    def after(self, preface: Preface) -> StyleHistory:
        return StyleHistory((*self.recent, preface)[-8:])


@dataclass(frozen=True, slots=True)
class ReplySnapshot:
    """Presentation values only. No ToolCall, Session, callback or policy object."""

    text: str
    language: Language
    #: Conservative by default: only trusted application code can opt in after
    #: examining the actual turn outcomes. This flag never authorises a tool.
    protected: bool = True


@dataclass(frozen=True, slots=True)
class Presentation:
    text: str
    history: StyleHistory
    preface: Preface


@runtime_checkable
class PersonalityProvider(Protocol):
    def present(
        self, reply: ReplySnapshot, *, config: StyleConfig, history: StyleHistory
    ) -> Presentation: ...


#: The address goes at the end, not the front.
#:
#: Two reasons, and the second is the one that decided it. "Сэр, Свободно 42 ГБ."
#: is wrong Russian — a capital letter after a comma — and there is no reliable
#: way to lowercase the next word, which may be "VS Code" or "Chrome". And the
#: rest of the assistant already speaks this way: every proactive notification
#: ends "..., сэр." Two halves of one assistant addressing the owner in two
#: different shapes is the kind of seam a person notices without being able to
#: say why.
_ADDRESS = {Language.RU: ", сэр", Language.EN: ", sir"}
_PERSONAL = {Language.RU: "Без лишних церемоний: ", Language.EN: "Without the ceremony: "}
_EXISTING_ADDRESS = re.compile(r"\b(?:sir|сэр)\b", re.IGNORECASE)


#: Sentence-ending punctuation the address has to slip in front of, so that
#: "Готово!" becomes "Готово, сэр!" rather than "Готово!, сэр".
_ENDING = ".!?…"


def _addressed(text: str, address: str) -> str:
    stripped = text.rstrip()
    if stripped and stripped[-1] in _ENDING:
        return f"{stripped[:-1].rstrip()}{address}{stripped[-1]}"
    return f"{stripped}{address}."


class RuleBasedPersonality:
    """Stateless, deterministic and offline. One prefix at most; no paraphrase."""

    def present(
        self,
        reply: ReplySnapshot,
        *,
        config: StyleConfig,
        history: StyleHistory,
    ) -> Presentation:
        preface = self._choose(reply, config, history)
        if preface is Preface.ADDRESS:
            text = _addressed(reply.text, _ADDRESS[reply.language])
        elif preface is Preface.PERSONAL:
            text = _PERSONAL[reply.language] + reply.text
        else:
            text = reply.text
        return Presentation(text, history.after(preface), preface)

    @staticmethod
    def _choose(reply: ReplySnapshot, config: StyleConfig, history: StyleHistory) -> Preface:
        # Preserve formatting and empty answers; don't put conversational words
        # in front of code, JSON, tables or lists. Long explanations don't need
        # an opener either. Kazakh is intentionally pass-through for this version.
        if (
            reply.protected
            or config.mode is Mode.PROFESSIONAL
            or reply.language not in _ADDRESS
            or not reply.text.strip()
            or len(reply.text) > 600
            or "\n" in reply.text
            or reply.text.lstrip().startswith(("`", "{", "[", "#", "|", "-", "*", ">"))
            or _EXISTING_ADDRESS.search(reply.text)
        ):
            return Preface.PLAIN

        if config.address is Address.SIR or (
            config.address is Address.AUTO and config.mode is Mode.JARVIS
        ):
            candidate = Preface.ADDRESS
        elif config.mode is Mode.PERSONAL and len(reply.text) >= 80:
            # A short command deserves a short reply, without a longer preamble.
            candidate = Preface.PERSONAL
        else:
            candidate = Preface.PLAIN

        # Never decorate adjacent turns, even after a mode/address change.
        if history.recent and history.recent[-1] is not Preface.PLAIN:
            return Preface.PLAIN
        if candidate in history.recent[-config.cooldown_turns :]:
            return Preface.PLAIN
        return candidate
