"""Shared bot chrome: the help text, the persistent reply keyboard, /-menu commands.

Kept dependency-light (aiogram types only) so ``app/services/tglink.py`` can
reuse it for the post-linking confirmation message.
"""

from __future__ import annotations

from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

HELP = (
    "DocsClassifier — бот\n\n"
    "🔎 /find <запрос> — поиск по всем доступным доменам\n"
    "    пример: /find договор #контрагент type:pdf 2024 ocr:yes\n"
    "📥 /inbox — обработать инбокс (расставить теги)\n"
    "📦 /sets — наборы документов и архивы\n"
    "🗂 /domains — домены: выбрать текущий, создать, участники\n"
    "📎 просто пришлите файл или архив — попадёт в инбокс текущего домена\n"
)

ROOT_COMMANDS = [
    BotCommand(command="find", description="Поиск по документам"),
    BotCommand(command="inbox", description="Обработать инбокс"),
    BotCommand(command="sets", description="Наборы документов"),
    BotCommand(command="domains", description="Домены"),
    BotCommand(command="help", description="Справка"),
]


def root_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/find"), KeyboardButton(text="/inbox")],
            [KeyboardButton(text="/sets"), KeyboardButton(text="/domains")],
            [KeyboardButton(text="/help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="команда или запрос для /find",
    )
