from html import escape
from html.parser import HTMLParser

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext


router = Router()


def _length(value):
    return len(value.encode("utf-16-le")) // 2


class _PageParser(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=False)
        self.limit = limit
        self.pages = []
        self.parts = []
        self.size = 0
        self.stack = []

    def _closing(self):
        return "".join(f"</{tag}>" for tag, _ in reversed(self.stack))

    def _flush(self):
        if self.parts:
            self.pages.append("".join(self.parts) + self._closing())
        self.parts = [start for _, start in self.stack]
        self.size = sum(_length(start) for start in self.parts)

    def _append(self, value, extra=0):
        size = _length(value)
        if self.size + size + _length(self._closing()) + extra > self.limit:
            self._flush()
        if self.size + size + _length(self._closing()) + extra > self.limit:
            raise ValueError("HTML tag exceeds message size")
        self.parts.append(value)
        self.size += size

    def handle_starttag(self, tag, attrs):
        start = self.get_starttag_text()
        self._append(start, _length(f"</{tag}>"))
        self.stack.append((tag, start))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("unbalanced HTML")
        self.stack.pop()
        self._append(f"</{tag}>")

    def handle_data(self, data):
        for char in data:
            self._append(escape(char, quote=False))

    def handle_entityref(self, name):
        self._append(f"&{name};")

    def handle_charref(self, name):
        self._append(f"&#{name};")


def split_html(text: str, limit: int = 3500) -> list[str]:
    if _length(text) <= limit:
        return [text]
    parser = _PageParser(limit)
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError("unbalanced HTML")
    if parser.parts:
        parser.pages.append("".join(parser.parts))
    return parser.pages


async def display_pages(message, state, pages):
    await state.update_data(screen_pages=pages)
    await _display_page(message, pages, 0)


async def _display_page(message, pages, index):
    page = pages[index]
    markup = types.InlineKeyboardMarkup.model_validate(page["markup"])
    if len(pages) > 1:
        navigation = []
        if index:
            navigation.append(types.InlineKeyboardButton(text="←", callback_data=f"screen_page:{index - 1}"))
        navigation.append(
            types.InlineKeyboardButton(text=f"{index + 1}/{len(pages)}", callback_data=f"screen_page:{index}")
        )
        if index + 1 < len(pages):
            navigation.append(types.InlineKeyboardButton(text="→", callback_data=f"screen_page:{index + 1}"))
        markup.inline_keyboard.append(navigation)
    if message.content_type == types.ContentType.PHOTO:
        await message.delete()
        await message.answer(page["text"], reply_markup=markup, parse_mode="HTML")
        return
    try:
        await message.edit_text(page["text"], reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data.startswith("screen_page:"))
async def change_page(callback: types.CallbackQuery, state: FSMContext):
    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    pages = (await state.get_data()).get("screen_pages") or []
    if 0 <= index < len(pages):
        await _display_page(callback.message, pages, index)
    await callback.answer()
