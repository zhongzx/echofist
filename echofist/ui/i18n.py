from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from typing import Any

import toml


def _flatten_mapping(data: Any, prefix: str = "") -> dict[str, str]:
    if data is None:
        return {}
    if isinstance(data, str):
        return {prefix: data} if prefix else {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten_mapping(v, prefix=key))
        elif isinstance(v, str):
            out[key] = v
    return out


def load_ui_translations(toml_text: str) -> dict[str, dict[str, str]]:
    raw = toml.loads(toml_text)
    translations: dict[str, dict[str, str]] = {}
    for lang, payload in raw.items():
        if lang == "meta":
            continue
        translations[str(lang)] = _flatten_mapping(payload)
    return translations


@dataclass(frozen=True)
class UILocalizer:
    language: str
    translations: dict[str, dict[str, str]]
    default_language: str
    fallback_language: str

    def with_language(self, language: str) -> UILocalizer:
        return UILocalizer(
            language=str(language),
            translations=self.translations,
            default_language=self.default_language,
            fallback_language=self.fallback_language,
        )

    def t(self, key: str, **kwargs: Any) -> str:
        lang = self.translations.get(self.language, {})
        default = self.translations.get(self.default_language, {})
        fallback = self.translations.get(self.fallback_language, {})
        template = lang.get(key) or default.get(key) or fallback.get(key) or key
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except Exception:
            return template


def extract_ui_meta(toml_text: str) -> tuple[str, str, tuple[str, ...]]:
    raw = toml.loads(toml_text)
    meta = raw.get("meta", {})
    default_language = str(meta.get("default_language") or meta.get("default") or "zh")
    fallback_language = str(
        meta.get("fallback_language") or meta.get("fallback") or "en"
    )
    supported_raw = meta.get("supported_languages") or meta.get("supported") or []
    supported: list[str] = []
    if isinstance(supported_raw, list | tuple):
        supported = [str(x) for x in supported_raw]
    return default_language, fallback_language, tuple(supported)


def build_ui_localizer(
    *,
    toml_text: str,
    language: str | None,
) -> tuple[UILocalizer, tuple[str, ...]]:
    default_language, fallback_language, supported = extract_ui_meta(toml_text)
    translations = load_ui_translations(toml_text)
    resolved_language = str(language or default_language)
    if supported and resolved_language not in supported:
        resolved_language = default_language
    return (
        UILocalizer(
            language=resolved_language,
            translations=translations,
            default_language=default_language,
            fallback_language=fallback_language,
        ),
        supported,
    )


def load_pack_text() -> str:
    return (
        resources.files("echofist.ui").joinpath("i18n.toml").read_text(encoding="utf-8")
    )


def get_ui_localizer(
    language: str | None = None,
) -> tuple[UILocalizer, tuple[str, ...]]:
    pack_text = load_pack_text()
    resolved = language or os.environ.get("ECHOFIST_LANG")
    return build_ui_localizer(toml_text=pack_text, language=resolved)
