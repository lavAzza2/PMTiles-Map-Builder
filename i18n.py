"""Small dependency-free localization helper for THS2 Map Builder."""

from __future__ import annotations

import ctypes
import json
import locale
import os
from pathlib import Path


SUPPORTED_LANGUAGES = ("ru", "en")
_language = "en"


def detect_system_language() -> str:
    """Use the Windows display locale when available, with a portable fallback."""
    locale_name = ""
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                locale_name = buffer.value
        except Exception:
            pass
    if not locale_name:
        locale_name = locale.getlocale()[0] or ""
    return "ru" if locale_name.lower().startswith("ru") else "en"


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA", "")).expanduser()
    if not str(base) or str(base) == ".":
        base = Path.home() / ".config"
    return base / "THS2 Map Builder" / "settings.json"


def load_settings() -> dict[str, object]:
    try:
        payload = json.loads(settings_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(settings: dict[str, object]) -> None:
    """Atomically save preferences so an interrupted write cannot corrupt them."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_language() -> str:
    value = load_settings().get("language")
    if value in SUPPORTED_LANGUAGES:
        return str(value)
    return detect_system_language()


def save_language(language: str) -> None:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    settings = load_settings()
    settings["language"] = language
    save_settings(settings)


def set_language(language: str) -> None:
    global _language
    _language = language if language in SUPPORTED_LANGUAGES else "en"


def get_language() -> str:
    return _language


def tr(russian: str, english: str) -> str:
    return russian if _language == "ru" else english


_language = load_language()
