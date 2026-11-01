"""The languages this project can read, and how to greet in them.

One table, three jobs:

1. **Routing.** A language tag maps to the recognition model that reads it:
   `multi` is the PP-OCRv6 model bundled with rapidocr (53 languages — Latin
   script, Japanese, Chinese), and the rest are PP-OCRv5 models downloaded on
   first use (Korean, Arabic, Cyrillic, Devanagari, Greek, Thai, Tamil, Telugu).
2. **Reporting.** `doctor` and `/api/ocr` answer "what can you read?" from this
   list instead of from a hard-coded string in three places.
3. **The boot screen.** A random greeting is picked from the languages that are
   actually readable on this machine, so the splash is a live coverage report:
   it can only greet you in a language the OCR would have read.

Languages that no bundled or downloadable model covers (Hebrew, Bengali,
Georgian, Armenian) stay in the table marked `model=None`. They are not lies —
the Windows recogniser reads Hebrew if that language pack is installed — but
they are excluded from the greeting pool unless Windows covers them.
"""

from __future__ import annotations

from dataclasses import dataclass

# rapidocr recognition groups, cheapest first: `multi` ships with the package,
# everything else is a download. The order is also the order the fallback chain
# tries them in, so the two most common non-Latin screenshots after CJK —
# Korean and Arabic — come before the long tail.
MODELS = (
    "multi",
    "korean",
    "arabic",
    "cyrillic",
    "devanagari",
    "th",
    "el",
    "ta",
    "te",
)


@dataclass(frozen=True, slots=True)
class Language:
    tag: str
    name: str
    hello: str
    model: str | None = None


# `model=None` means: no rapidocr model for this script. Windows can still read
# it when the matching language pack is installed.
LANGUAGES: tuple[Language, ...] = (
    # -- Latin script, all on the bundled v6 model -------------------------
    Language("en", "English", "Hello", "multi"),
    Language("ru", "Русский", "Привет", "cyrillic"),
    Language("es", "Español", "Hola", "multi"),
    Language("fr", "Français", "Bonjour", "multi"),
    Language("de", "Deutsch", "Hallo", "multi"),
    Language("it", "Italiano", "Ciao", "multi"),
    Language("pt", "Português", "Olá", "multi"),
    Language("nl", "Nederlands", "Hallo", "multi"),
    Language("sv", "Svenska", "Hej", "multi"),
    Language("no", "Norsk", "Hei", "multi"),
    Language("da", "Dansk", "Hej", "multi"),
    Language("fi", "Suomi", "Hei", "multi"),
    Language("is", "Íslenska", "Halló", "multi"),
    Language("pl", "Polski", "Cześć", "multi"),
    Language("cs", "Čeština", "Ahoj", "multi"),
    Language("sk", "Slovenčina", "Ahoj", "multi"),
    Language("hu", "Magyar", "Szia", "multi"),
    Language("ro", "Română", "Salut", "multi"),
    Language("hr", "Hrvatski", "Bok", "multi"),
    Language("bs", "Bosanski", "Zdravo", "multi"),
    Language("sl", "Slovenščina", "Živjo", "multi"),
    Language("sq", "Shqip", "Përshëndetje", "multi"),
    Language("et", "Eesti", "Tere", "multi"),
    Language("lv", "Latviešu", "Sveiki", "multi"),
    Language("lt", "Lietuvių", "Labas", "multi"),
    Language("tr", "Türkçe", "Merhaba", "multi"),
    Language("az", "Azərbaycan", "Salam", "multi"),
    Language("uz", "Oʻzbekcha", "Salom", "multi"),
    Language("id", "Bahasa Indonesia", "Halo", "multi"),
    Language("ms", "Bahasa Melayu", "Helo", "multi"),
    Language("tl", "Filipino", "Kumusta", "multi"),
    Language("vi", "Tiếng Việt", "Xin chào", "multi"),
    Language("sw", "Kiswahili", "Habari", "multi"),
    Language("af", "Afrikaans", "Hallo", "multi"),
    Language("ca", "Català", "Hola", "multi"),
    Language("gl", "Galego", "Ola", "multi"),
    Language("eu", "Euskara", "Kaixo", "multi"),
    Language("cy", "Cymraeg", "Helo", "multi"),
    Language("ga", "Gaeilge", "Dia duit", "multi"),
    Language("mt", "Malti", "Bongu", "multi"),
    Language("lb", "Lëtzebuergesch", "Moien", "multi"),
    Language("la", "Latina", "Salve", "multi"),
    Language("mi", "Te Reo Māori", "Kia ora", "multi"),
    # -- Cyrillic ----------------------------------------------------------
    Language("uk", "Українська", "Привіт", "cyrillic"),
    Language("be", "Беларуская", "Прывітанне", "cyrillic"),
    Language("bg", "Български", "Здравей", "cyrillic"),
    Language("sr", "Српски", "Здраво", "cyrillic"),
    Language("mk", "Македонски", "Здраво", "cyrillic"),
    Language("kk", "Қазақша", "Сәлем", "cyrillic"),
    Language("ky", "Кыргызча", "Салам", "cyrillic"),
    Language("tg", "Тоҷикӣ", "Салом", "cyrillic"),
    Language("mn", "Монгол", "Сайн уу", "cyrillic"),
    # -- CJK ---------------------------------------------------------------
    Language("zh", "中文", "你好", "multi"),
    Language("zh-Hant", "繁體中文", "你好", "multi"),
    Language("ja", "日本語", "こんにちは", "multi"),
    Language("ko", "한국어", "안녕하세요", "korean"),
    # -- Arabic script -----------------------------------------------------
    Language("ar", "العربية", "مرحبا", "arabic"),
    Language("fa", "فارسی", "سلام", "arabic"),
    Language("ur", "اردو", "سلام", "arabic"),
    # -- Other scripts -----------------------------------------------------
    Language("el", "Ελληνικά", "Γεια σου", "el"),
    Language("th", "ไทย", "สวัสดี", "th"),
    Language("hi", "हिन्दी", "नमस्ते", "devanagari"),
    Language("mr", "मराठी", "नमस्कार", "devanagari"),
    Language("ne", "नेपाली", "नमस्ते", "devanagari"),
    Language("ta", "தமிழ்", "வணக்கம்", "ta"),
    Language("te", "తెలుగు", "నమస్కారం", "te"),
    # -- Windows-only: readable only when the language pack is installed ---
    Language("he", "עברית", "שלום", None),
    Language("bn", "বাংলা", "নমস্কার", None),
    Language("ka", "ქართული", "გამარჯობა", None),
    Language("hy", "Հայերեն", "Բարև", None),
)

BY_TAG: dict[str, Language] = {language.tag: language for language in LANGUAGES}

# Windows reports language packs as `en-US`, `ru`, `zh-Hans-CN`. The table is
# keyed by the base tag, so the lookup has to fall back from the most specific
# form to the least.
_BASE: dict[str, Language] = {}
for _language in LANGUAGES:
    _BASE.setdefault(_language.tag.lower(), _language)

# Chinese needs more than the prefix walk: `zh-TW` and `zh-HK` are Traditional
# (which has its own recognition model), while `zh-CN` and `zh-SG` are not.
_ALIASES = {
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh-cn": "zh",
    "zh-sg": "zh",
    "zh-hans": "zh",
}


def for_tag(tag: str | None) -> Language | None:
    """`zh-Hans-CN` -> the Chinese entry. `None` when the tag is unknown."""
    if not tag:
        return None
    parts = tag.lower().replace("_", "-").split("-")
    for size in range(len(parts), 0, -1):
        candidate = "-".join(parts[:size])
        alias = _ALIASES.get(candidate)
        if alias is not None:
            return _BASE.get(alias.lower())
        found = _BASE.get(candidate)
        if found is not None:
            return found
    return None


def with_model(model: str) -> list[Language]:
    return [language for language in LANGUAGES if language.model == model]


def greeting_pool(models: set[str]) -> list[Language]:
    """Languages readable right now, for the boot screen.

    `models` is what the OCR layer reports as usable on this machine, so the
    greeting is never in a script that would come back as empty boxes.
    """
    pool = [language for language in LANGUAGES if language.model in models]
    return pool or [BY_TAG["en"]]
