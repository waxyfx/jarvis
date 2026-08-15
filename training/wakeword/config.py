"""Every parameter of the "Atlas" wake-word training run, in one place.

Kept in the repository because a model you cannot rebuild is a liability: when
the false-accept rate turns out to be wrong in six months, the question will be
*what was it trained on*, and the answer has to be readable rather than
remembered.

The audio datasets and the 16 GB of precomputed negative features are **not**
kept — they are downloadable, they are not ours, and they would dwarf the
repository. `fetch.py` reproduces them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS = REPO_ROOT / ".models"
WORK = REPO_ROOT / ".training" / "wakeword"

# Shared feature stack — the same two graphs the runtime uses. Training against
# different feature extraction than inference is the classic way to produce a
# model that scores beautifully and fails in the room.
MELSPECTROGRAM = MODELS / "oww" / "melspectrogram.onnx"
EMBEDDING = MODELS / "oww" / "embedding_model.onnx"

SAMPLE_RATE = 16_000
#: 16 embeddings of 96 values: exactly what the classifier reads, and therefore
#: exactly what one training sample is.
CLF_WINDOW = 16
EMBEDDING_SIZE = 96
#: Samples that yield precisely CLF_WINDOW embeddings. The melspectrogram graph
#: emits n/160 - 3 frames; 16 embeddings need 76 + 15*8 = 196 of them.
CLIP_SAMPLES = (196 + 3) * 160  # 31_840, just under two seconds


@dataclass(frozen=True)
class Voices:
    """Which synthetic speakers produce the positive examples.

    English gets a genuinely multi-speaker model. Russian has only four
    single-speaker voices published, so the Russian half of the training set is
    thinner in speaker identity and leans harder on prosody and augmentation.
    That imbalance is a known weakness of this run and the reason the acceptance
    test reports «Атлас» separately from "Atlas" rather than averaging them.
    """

    english_multispeaker: str = "en_US-libritts_r-medium"
    english_speaker_count: int = 904
    russian: tuple[str, ...] = (
        "ru_RU-dmitri-medium",
        "ru_RU-denis-medium",
        "ru_RU-irina-medium",
        "ru_RU-ruslan-medium",
    )


@dataclass(frozen=True)
class Phrases:
    """What counts as the wake word, and what must never be mistaken for it.

    The wake word is **Jarvis / Джарвис**. The earlier run used "Atlas", and one
    of the reasons it was replaced is preserved in
    ``metrics/atlas-experiment/README.md``: «атлас» is an ordinary Russian noun,
    so «географический атлас» fired at 1.00 and no amount of training could have
    fixed it. «Джарвис» is not a word in either language, which removes that
    class of collision entirely.
    """

    #: Said alone, as a person summoning an assistant does. This stays the main
    #: case and keeps the larger share of the positives.
    positive_en: tuple[str, ...] = ("Jarvis", "Jarvis.", "Jarvis?", "Hey Jarvis", "OK Jarvis")
    positive_ru: tuple[str, ...] = (
        "Джарвис",
        "Джарвис.",
        "Джарвис?",
        "Эй, Джарвис",
        "Окей, Джарвис",
    )

    #: The wake word with a command attached, which is how it is usually said
    #: once someone is used to the assistant.
    #:
    #: v1 had none of these, and paid for it. Every positive was a short word
    #: alone in silence, so *being a short word alone in silence* became part of
    #: what the model recognised. Measured: «Дарвин» said alone scored 0.998,
    #: and «Дарвин был натуралистом» scored 0.0000 — the same word, and the only
    #: difference is whether anything surrounded it.
    contextual_en: tuple[str, ...] = (
        "Jarvis, open Chrome",
        "Jarvis, what time is it?",
        "Hey Jarvis, what time is it?",
        "Jarvis, close Notepad",
        "Jarvis, show me memory usage",
        "Jarvis, are you there?",
        "Hey Jarvis, open the second document",
        "Jarvis, how much disk space is left?",
        "So I asked Jarvis to do it",
        "Jarvis, never mind",
    )
    contextual_ru: tuple[str, ...] = (
        "Джарвис, открой Chrome",
        "Джарвис, который час?",
        "Джарвис, закрой блокнот",
        "Джарвис, покажи использование памяти",
        "Эй, Джарвис, ты здесь?",
        "Джарвис, сколько осталось места на диске",
        "Джарвис, открой второй документ",
        "Я попросил Джарвиса это сделать",
        "Джарвис, отбой",
        "Джарвис, повтори ещё раз",
    )

    @property
    def spoken_en(self) -> str:
        """The bare word, for anything that needs to say it once.

        The acceptance test used to hard-code "Atlas" in five places. Against a
        model trained on a different word that reports 0% recall in every
        positive group — three hours of pipeline measuring nothing, and a result
        that looks like a catastrophic model failure rather than a stale string.
        """
        return self.positive_en[0]

    @property
    def spoken_ru(self) -> str:
        return self.positive_ru[0]

    #: Near-misses. These are the expensive negatives: a model that has never
    #: heard "at last" will happily fire on it, and no amount of unrelated
    #: podcast audio teaches it otherwise. «атлас» as an ordinary noun matters
    #: especially — a Russian speaker says it in normal conversation.
    #: Words alone teach the model a word; sentences teach it that the word is
    #: unremarkable in ordinary speech, at conversational speed and prosody. A
    #: name is most often misheard *inside* a sentence, not in isolation, so the
    #: list carries both.
    #: Extended in v2 from the model's own false positives rather than from
    #: guesswork. "Jargon" scored 0.998, "starve us" 0.998 and "Jervis" 0.755
    #: against v1, so their families are covered properly here. Guessing which
    #: words collide is much worse than asking the model which ones did.
    hard_negative_en: tuple[str, ...] = (
        # Bare near-misses.
        "Travis",
        "Davis",
        "Harvey",
        "Jervis",
        "Service",
        "Jargon",
        "Carve",
        "Harvest",
        # The v1 false positives, and their neighbours.
        "Jarring",
        "Jarred",
        "Charging",
        "Garden",
        "Guarding",
        "Carbon",
        "Pardon",
        "Bargain",
        "Starve",
        "Starving",
        "Carving",
        "Marvin",
        "Marvel",
        "Harbour",
        "Java",
        "Jarvis Cocker sang it",
        # The same sounds inside ordinary sentences.
        "Java is running on the server",
        "The service is available again",
        "Travis said he would call back",
        "Davis is joining us later",
        "The car is parked outside",
        "As far as I can tell, nothing changed",
        "Starve us of detail and we guess",
        "Harvey asked about the harvest",
        "That is a lot of jargon for one page",
        "Could you carve out an hour tomorrow",
        "The garden needs weeding this weekend",
        "I beg your pardon, what was that?",
        "Marvin is guarding the door",
        "That was quite a bargain",
        "The carbon figures came in low",
    )

    #: Ordinary short words, unrelated to the wake word phonetically. Their only
    #: job is to make "a short word alone in silence" uninformative — the shape
    #: v1 learned instead of the word. Against twenty-four thousand isolated
    #: positives, v1 had about a dozen isolated negatives; the imbalance was the
    #: defect.
    isolated_en: tuple[str, ...] = (
        "House",
        "Table",
        "Water",
        "Light",
        "Night",
        "Morning",
        "Friend",
        "Hand",
        "Book",
        "City",
        "Time",
        "Music",
        "Coffee",
        "Window",
        "Door",
        "Chair",
        "Phone",
        "Money",
        "Winter",
        "Summer",
        "Answer",
        "Question",
        "Number",
        "Picture",
        "Letter",
        "Market",
        "Doctor",
        "Teacher",
        "Mother",
        "Father",
        "Yellow",
        "Purple",
        "Simple",
        "Better",
        "Under",
        "Over",
        "After",
        "Never",
        "Always",
        "Maybe",
        "Really",
        "Almost",
        "Enough",
        "Perfect",
        "Thank you",
        "Excuse me",
        "Of course",
        "All right",
        "One moment",
    )
    #: Ordinary conversation, phonetically unrelated to the wake word. Two jobs.
    #: It teaches that everyday speech is not a summons — and it balances clip
    #: length, which the isolated words alone would skew.
    #:
    #: Measured on a v2 smoke run before this list existed: positives had a
    #: median duration of 1.09 s (English) and 1.43 s (Russian) against 0.49–0.80 s
    #: for every negative group. Adding contextual positives had fixed "positives
    #: are always short" by creating "positives are always long". Duration must
    #: not be a cue in either direction.
    generic_en: tuple[str, ...] = (
        "Could you open the second document and check the totals please",
        "I was thinking we should leave before the traffic gets bad",
        "The meeting has been moved to Thursday afternoon",
        "There is a package waiting downstairs for you",
        "It rained all morning and then cleared up",
        "Let me know when you have finished reading it",
        "The train arrives at half past seven",
        "We should probably order something to eat",
        "I left my keys on the kitchen table again",
        "That film was longer than I expected",
    )
    generic_ru: tuple[str, ...] = (
        "Мне кажется, нам стоит выехать пораньше, пока нет пробок",
        "Совещание перенесли на четверг, и это всех устраивает",
        "Внизу тебя ждёт посылка",
        "Дождь шёл всё утро, а потом прояснилось",
        "Дай знать, когда закончишь читать",
        "Поезд приходит в половине восьмого",
        "Наверное, стоит заказать что-нибудь поесть",
        "Я снова забыл ключи на кухонном столе",
        "Фильм оказался длиннее, чем я думал",
        "Завтра обещали хорошую погоду",
    )

    isolated_ru: tuple[str, ...] = (
        "Дом",
        "Стол",
        "Вода",
        "Свет",
        "Ночь",
        "Утро",
        "Друг",
        "Рука",
        "Книга",
        "Город",
        "Время",
        "Музыка",
        "Кофе",
        "Окно",
        "Дверь",
        "Стул",
        "Телефон",
        "Деньги",
        "Зима",
        "Лето",
        "Ответ",
        "Вопрос",
        "Номер",
        "Картина",
        "Письмо",
        "Рынок",
        "Доктор",
        "Учитель",
        "Мама",
        "Папа",
        "Жёлтый",
        "Синий",
        "Простой",
        "Лучше",
        "Под",
        "Над",
        "После",
        "Никогда",
        "Всегда",
        "Может",
        "Правда",
        "Почти",
        "Хватит",
        "Отлично",
        "Спасибо",
        "Извините",
        "Конечно",
        "Хорошо",
        "Минуту",
        "Погоди",
    )
    #: The Russian list is longer because Russian carries the one genuinely
    #: dangerous neighbour: «сервис» is /sʲerˈvʲis/ against /dʒarˈvʲis/, sharing
    #: the stressed vowel and the whole «-рвис» ending. Everything else here is
    #: a softer collision on «джа-», «-ар-» or «-ис».
    hard_negative_ru: tuple[str, ...] = (
        # Bare near-misses. Every one of these fired against v1 at 0.93–0.9997
        # *while already being in its training set* — which is why v2 attacks
        # the shape rather than simply repeating the list.
        "Сервис",
        "Сервиз",
        "Дарвин",
        "Джаз",
        "Джазовый",
        "Жарит",
        "Шарит",
        "Шарф",
        "Париж",
        "Барвинок",
        "Нарвис",
        "Гарвард",
        # Neighbours of the words that fired.
        "Джазмен",
        "Джанго",
        "Джарра",
        "Жарко",
        "Жаркое",
        "Шарик",
        "Шарить",
        "Шарнир",
        "Дарвинизм",
        "Гарнир",
        "Карвинг",
        "Марвин",
        "Арбуз",
        "Барвиха",
        "Тарелка",
        "Фарватер",
        # The same sounds inside ordinary sentences.
        "В сервисе произошла ошибка",
        "Сервис снова доступен",
        "Отнеси часы в сервис",
        "Дарвин писал об этом подробно",
        "Дарвин был натуралистом",
        "Он поставил на стол сервиз",
        "Мне нравится джазовый концерт",
        "Она жарит картошку на кухне",
        "Он шарит в этой теме",
        "Я забыл шарф в Париже",
        "В Париже было дождливо",
        "Гарвардский курс начинается осенью",
        "Джаз играл до самого утра",
        "Шарф висел на вешалке",
        "Дарвин описал этот вид",
        "На гарнир возьму рис",
        "Марвин ждал у входа",
    )


@dataclass(frozen=True)
class Synthesis:
    """How much the synthetic voices are varied.

    Piper's own defaults produce one careful reading per speaker, and a model
    trained on careful readings hears a careful reading. Real summoning is
    faster, flatter and often half-swallowed.
    """

    #: Speaking rate. Above 1 is slower; the range covers a hurried "Atlas!"
    #: through a deliberate one.
    length_scale: tuple[float, float] = (0.75, 1.35)
    #: Piper's prosody randomness, widened from its 0.667 default.
    noise_scale: tuple[float, float] = (0.4, 0.9)
    #: Phoneme duration randomness, widened from its 0.8 default.
    noise_w: tuple[float, float] = (0.4, 1.1)

    positives_en: int = 11_000
    positives_ru: int = 11_000
    #: How many positives are the bare word rather than a whole command. Calling
    #: the assistant by name alone stays the main case, so it keeps the majority
    #: — but not the whole set, which is what taught v1 to recognise isolation.
    bare_fraction: float = 0.6

    hard_negatives_en: int = 5_000
    #: Russian carries more of the collisions and more of the v1 failures.
    hard_negatives_ru: int = 8_000
    #: Ordinary short words, to make "short word alone" uninformative.
    isolated_en: int = 4_000
    isolated_ru: int = 5_000
    #: Ordinary sentences, to keep clip length uninformative in both directions.
    generic_en: int = 3_000
    generic_ru: int = 4_000


@dataclass(frozen=True)
class Background:
    """Negatives made of the augmentation itself, with no word in them.

    This class exists because v1 did not have it. Every noise clip and every
    room used to make a positive sound realistic appeared *only* inside
    positives, and never in the five million published negatives. Within the
    subpopulation of "audio containing this noise corpus", positives dominated —
    so the model learned that the noise corpus was evidence for the wake word,
    and fired 14 985 times an hour on plain street recordings.

    The rule that follows, and which :mod:`preflight` now enforces:

        **Every augmentation applied to a positive must also appear, in
        comparable proportion, in audio labelled negative.**

    Otherwise the augmentation stops being a nuisance the model must see past
    and becomes a feature it can use.
    """

    #: Windows of background with no speech at all. Roughly a third of the
    #: positive count, which is enough for the noise to be uninformative
    #: without swamping the near-misses.
    count: int = 18_000
    #: How that count is split. Digital silence is included because a
    #: microphone in a quiet room is the single most common thing ATLAS will
    #: ever hear, and "never fires on nothing" should be trained, not assumed.
    silence_fraction: float = 0.10
    quiet_noise_fraction: float = 0.20
    #: The rest is the recorded corpus at ordinary levels, half of it
    #: reverberated with the same rooms the positives use.


@dataclass(frozen=True)
class Augmentation:
    """Making synthetic speech survive a real room.

    A clip straight out of a synthesiser is anechoic, evenly loud, and centred
    in the frame. A microphone two metres away in a room with a fan hears none
    of those things.
    """

    #: Fraction of clips convolved with a measured room impulse response.
    reverb_probability: float = 0.6
    #: Fraction mixed with recorded noise, at signal-to-noise ratios spanning
    #: a quiet study through a busy street.
    noise_probability: float = 0.8
    snr_db: tuple[float, float] = (0.0, 25.0)
    #: Overall level, so loudness is never a cue the model can lean on.
    gain_db: tuple[float, float] = (-22.0, -3.0)
    #: Where the word sits inside the two-second window. The classifier should
    #: fire as the word completes, so the end is jittered near — but not at —
    #: the window's end.
    word_end_fraction: tuple[float, float] = (0.72, 0.98)


@dataclass(frozen=True)
class Training:
    batch_size: int = 1024
    epochs: int = 12
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    #: Negatives vastly outnumber positives, which is correct — the world is
    #: mostly not the wake word — but unweighted training then learns to answer
    #: "no" and stop. Positives are oversampled per batch instead of reweighting
    #: the loss, so every batch contains real gradient from both classes.
    positive_fraction: float = 0.25
    #: How many of the 5-million-odd ACAV100M windows to use. All of them is
    #: the point of the full run.
    negative_windows: int | None = None
    seed: int = 20260815

    #: The operating points reported by the acceptance test. Not one "best"
    #: threshold: the trade-off is the user's to make, and it cannot be made
    #: from a single number.
    thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99)


@dataclass(frozen=True)
class Config:
    voices: Voices = field(default_factory=Voices)
    phrases: Phrases = field(default_factory=Phrases)
    synthesis: Synthesis = field(default_factory=Synthesis)
    augmentation: Augmentation = field(default_factory=Augmentation)
    background: Background = field(default_factory=Background)
    training: Training = field(default_factory=Training)

    @property
    def piper_dir(self) -> Path:
        return MODELS / "piper"

    @property
    def rir_dir(self) -> Path:
        return WORK / "rir"

    @property
    def noise_dir(self) -> Path:
        return WORK / "noise"

    @property
    def negative_features(self) -> Path:
        return WORK / "acav100m_features.npy"

    @property
    def validation_features(self) -> Path:
        return WORK / "validation_features.npy"

    @property
    def clips_dir(self) -> Path:
        return WORK / "clips"

    @property
    def features_dir(self) -> Path:
        return WORK / "features"

    #: Bumped per training run, so a new run cannot quietly overwrite the model
    #: an earlier acceptance report describes. metrics/jarvis-v1/ documents v1.
    version: str = "v2"

    @property
    def output_model(self) -> Path:
        return MODELS / "oww" / f"jarvis_{self.version}.onnx"

    @property
    def metrics_dir(self) -> Path:
        return Path(__file__).resolve().parent / "metrics"


CONFIG = Config()
