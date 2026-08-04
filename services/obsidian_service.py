# -*- coding: utf-8 -*-
"""Obsidian integration: базовый класс и заготовки для работы с .md файлами.

Предназначен для будущего расширения: поиск по заметкам, чтение/запись
файлов в хранилище Obsidian, синхронизация контекста диалогов и т.п.
"""
from __future__ import annotations

import glob
import os
import re
from typing import Any


class ObsidianService:
    """Базовый класс для работы с хранилищем Obsidian (.md файлы)."""

    def __init__(self, vault_path: str = "") -> None:
        self.vault_path = vault_path or os.getenv("OBSIDIAN_VAULT", "")
        self._cache: dict[str, str] = {}

    def list_notes(self, pattern: str = "**/*.md") -> list[str]:
        """Возвращает список .md файлов в хранилище, соответствующих pattern."""
        if not self.vault_path or not os.path.isdir(self.vault_path):
            return []
        return sorted(
            p for p in glob.glob(os.path.join(self.vault_path, pattern), recursive=True)
            if os.path.isfile(p)
        )

    def read_note(self, path: str) -> str:
        """Читает содержимое .md файла. Результат кешируется."""
        if path in self._cache:
            return self._cache[path]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError):
            return ""
        self._cache[path] = content
        return content

    def search_notes(self, query: str, notes: list[str] | None = None) -> list[str]:
        """Ищет `query` (подстрока, без учёта регистра) по указанным заметкам.

        Если notes не передан — ищет по всем .md файлам в хранилище.
        Возвращает список путей к найденным файлам.
        """
        if notes is None:
            notes = self.list_notes()
        q = query.lower()
        found: list[str] = []
        for note in notes:
            content = self.read_note(note)
            if q in content.lower():
                found.append(note)
        return found

    def search_by_regex(self, pattern: str, notes: list[str] | None = None) -> dict[str, list[str]]:
        """Ищет `pattern` (regex) по заметкам.

        Возвращает dict: путь -> список найденных совпадений.
        """
        if notes is None:
            notes = self.list_notes()
        regex = re.compile(pattern, re.IGNORECASE)
        results: dict[str, list[str]] = {}
        for note in notes:
            content = self.read_note(note)
            matches = regex.findall(content)
            if matches:
                results[note] = matches
        return results

    def extract_frontmatter(self, path: str) -> dict[str, Any]:
        """Извлекает YAML frontmatter из .md файла (между --- строками)."""
        content = self.read_note(path)
        fm: dict[str, Any] = {}
        m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return fm
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip()
        return fm

    def clear_cache(self) -> None:
        self._cache.clear()