# CLAUDE.md

## Project Overview

This repository contains the **telegram-ai-responder** project.

Technology stack:
- Python 3.14
- asyncio
- Pyrogram
- Telegram Bot API (aiohttp)
- Gemini API
- Groq API
- Render deployment

---

# Language Policy (Язык общения)

- **ALWAYS communicate, explain, and output responses in Russian (Всегда отвечать и объяснять строго на русском языке).**

---

# Token Usage Policy (Highest Priority)

Minimize token consumption at all times.

Rules:
- NEVER scan the entire repository unless explicitly requested.
- Read ONLY files directly related to the current task.
- Reuse already-read context instead of reopening files.
- Do not inspect unrelated modules.
- Avoid recursive searches when a targeted lookup is sufficient.
- Prefer filename-specific lookups over repository-wide searches.
- Do not summarize large files unless requested.
- Stop reading once enough information has been gathered.

---

# Strict Ignore Rules

Never intentionally read, index, search, or inspect the following:

.git/
.venv/
node_modules/
build/
dist/
.cache/
.pytest_cache/
pycache/
MEDIA_TMP_DIR/
*.pyc


---

# Architecture Rules

The architecture source of truth is:
1. `docs/architecture.md`
2. `docs/rules.md`

Rules:
- Read `docs/architecture.md` BEFORE making major architectural or structural changes.
- Read `docs/rules.md` whenever implementation rules may affect the task.
- Do NOT scan the entire `docs/` directory.
- Do NOT access any external Obsidian vault.
- Never read files outside the repository.

---

# Response Style

Always be concise and reply in Russian.

Do NOT include:
- greetings / introductions
- motivational text
- explanations unless requested
- long reasoning

Prefer:
- ready-to-use code
- minimal diffs
- exact terminal commands
- direct Russian answers

---

# Code Editing & Safety Principles

- Make the smallest correct change.
- Avoid unrelated refactoring or formatting edits.
- Preserve backward compatibility.
- Ensure all Python code is asyncio-native.
