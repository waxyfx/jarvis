"""Delivering something the backend decided to say.

The backend decides *whether* to speak; this decides *how*, on the machine where
the person actually is. Three rules, and all three are about not being the
application everyone mutes.

**It is never said twice.** A reconnect can redeliver, and the same sentence
twice in one minute is worse than not saying it at all — so delivered ids are
remembered.

**SAFE MODE silences the voice, not the notification.** The kill switch means
stop acting, and a voice in the room at an unexpected moment is the most
intrusive thing this system does. The text still appears, because suppressing a
reminder outright loses information the owner may want; it simply waits to be
looked at.

**Speech is best-effort.** The voice stack may not be loaded, the speakers may
be busy with an answer to something the owner actually asked. A notification
that could not be spoken is shown and logged, never retried into the middle of
a conversation.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable

from atlas_agent.logging import get_logger
from atlas_agent.safety.mode import SafeModeController
from atlas_shared.enums import Language, NotificationPriority
from atlas_shared.protocol.messages import Notify

__all__ = ["NotificationDelivery"]

log = get_logger(__name__)

#: How many delivered ids to remember. Enough for a day of notifications many
#: times over, bounded so a long uptime cannot grow it without limit.
_REMEMBERED = 256


class NotificationDelivery:
    """Says it, shows it, or explains in the log why it did neither."""

    def __init__(
        self,
        *,
        speak: Callable[[str, Language], Awaitable[None]] | None = None,
        show: Callable[[str, str], None] | None = None,
        safe_mode: SafeModeController | None = None,
        language: Language = Language.RU,
    ) -> None:
        self._speak = speak
        self._show = show
        self._safe_mode = safe_mode
        self._language = language
        self._delivered: OrderedDict[str, None] = OrderedDict()

    async def deliver(self, notification: Notify) -> bool:
        """Deliver one. Returns whether anything reached the owner at all."""
        if self._already_delivered(notification.notification_id):
            log.info("notification_duplicate", kind=notification.kind.value)
            return False

        shown = self._display(notification)
        spoken = await self._say(notification)

        log.info(
            "notification_delivered",
            kind=notification.kind.value,
            priority=notification.priority.value,
            spoken=spoken,
            shown=shown,
        )
        return shown or spoken

    # ------------------------------------------------------------- internals

    def _already_delivered(self, notification_id: str) -> bool:
        if notification_id in self._delivered:
            return True
        self._delivered[notification_id] = None
        while len(self._delivered) > _REMEMBERED:
            self._delivered.popitem(last=False)
        return False

    def _display(self, notification: Notify) -> bool:
        if self._show is None:
            return False
        try:
            self._show(notification.title, notification.body)
        except Exception:
            # The tray icon is a nicety. A failure there must not stop the
            # spoken half, which is the half the owner asked for.
            log.warning("notification_display_failed", kind=notification.kind.value)
            return False
        return True

    async def _say(self, notification: Notify) -> bool:
        if not notification.speak or notification.priority is NotificationPriority.LOW:
            return False
        if self._speak is None:
            return False
        if self._safe_mode is not None and self._safe_mode.is_safe:
            log.info("notification_not_spoken", reason="safe mode", kind=notification.kind.value)
            return False

        try:
            await self._speak(notification.body, self._language)
        except Exception:
            log.warning("notification_speech_failed", kind=notification.kind.value)
            return False
        return True
