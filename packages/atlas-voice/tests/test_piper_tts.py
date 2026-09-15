"""Which voice speaks which language.

Rendering itself is exercised through the session and end-to-end tests, which
use real Piper output. What is checked here is the choice — cheap, and the place
a new language silently falls through to English.
"""

from __future__ import annotations

from atlas_shared.enums import Language
from atlas_voice.engines.piper_tts import ACKNOWLEDGEMENTS, VoiceChoice


class TestKazakh:
    """The owner's tracker already has Kazakh in it.

    A Kazakh sentence read by the Russian voice is worse than either language
    done properly: the phonology is different enough that it comes out as
    Russian-accented nonsense.
    """

    def test_kazakh_has_its_own_voice(self) -> None:
        choice = VoiceChoice()

        assert choice.for_language(Language.KK) == "kk_KZ-issai-high"
        assert choice.for_language(Language.KK) != choice.for_language(Language.RU)

    def test_every_language_the_project_knows_has_a_voice(self) -> None:
        """A language in the enum with no voice would fall through to English
        silently, which is the failure this catches."""
        choice = VoiceChoice()

        assert {choice.for_language(language) for language in Language} == {
            "en_GB-alan-medium",
            "ru_RU-dmitri-medium",
            "kk_KZ-issai-high",
        }

    def test_the_wake_acknowledgement_exists_in_kazakh(self) -> None:
        """Said on every wake. In the wrong language it is the first thing the
        owner hears go wrong."""
        assert Language.KK in ACKNOWLEDGEMENTS
        assert ACKNOWLEDGEMENTS[Language.KK][0] == "Иә, мырза?"
