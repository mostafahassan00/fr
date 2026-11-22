"""The language the program speaks: Arabic (the window's default) or English.

Every text the user sees is written once in both languages, where it is used:

    tr("Start", "ابدأ")

The command line stays in English (its output is read by the build tests); the
window switches to the language saved in settings.json (Arabic unless changed).
"""
from __future__ import annotations

LANGUAGES = {"ar": "العربي", "en": "English"}
_lang = "en"


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGUAGES else "ar"


def language() -> str:
    return _lang


def rtl() -> bool:
    """Right to left: the window is laid out from the right in Arabic."""
    return _lang == "ar"


def tr(en: str, ar: str) -> str:
    return ar if _lang == "ar" else en


def person_label(label: str) -> str:
    """Show the automatic names (Unknown_0007, New_person_02) in the window's language."""
    if _lang == "ar":
        if label.startswith("Unknown_"):
            return "مجهول_" + label[len("Unknown_"):]
        if label.startswith("New_person_"):
            return "شخص_جديد_" + label[len("New_person_"):]
    return label
