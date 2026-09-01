"""Telegram bot (aiogram 3) — mirrors the web UI for the linked user (§8).

Runs as its own process (`python -m app.bot`), shares the database and the
`app/services/*` layer directly — it is not an HTTP client of its own API.
"""
