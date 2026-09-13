from html.parser import HTMLParser
from string import Formatter
from urllib.parse import urlsplit

import yaml

from panel_core.resources import BOT_TEXTS_DEFAULTS, read_data_text


def text_defaults():
    return yaml.safe_load(read_data_text(BOT_TEXTS_DEFAULTS)) or {}


def template_variables(key, defaults=None):
    defaults = text_defaults() if defaults is None else defaults
    fields = set()
    for template in (defaults.get(key) or {}).values():
        for _, field, _, _ in Formatter().parse(template):
            if field is not None:
                fields.add(field)
    return sorted(fields)


class _TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        allowed = {
            "b",
            "strong",
            "i",
            "em",
            "u",
            "ins",
            "s",
            "strike",
            "del",
            "code",
            "pre",
            "a",
            "span",
            "tg-spoiler",
            "blockquote",
        }
        if tag not in allowed:
            raise ValueError("Unsupported HTML tag")
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise ValueError("Duplicate HTML attribute")
        if tag == "a":
            if set(attributes) != {"href"} or urlsplit(attributes["href"] or "").scheme not in {"http", "https", "tg"}:
                raise ValueError("Links require an http, https or tg URL")
        elif tag == "span":
            if attributes != {"class": "tg-spoiler"}:
                raise ValueError("Only tg-spoiler spans are supported")
        elif tag == "code" and attributes:
            if (
                set(attributes) != {"class"}
                or not (attributes["class"] or "").startswith("language-")
                or not self.stack
                or self.stack[-1] != "pre"
            ):
                raise ValueError("Code language requires a pre tag")
        elif tag == "blockquote" and attributes:
            if set(attributes) != {"expandable"}:
                raise ValueError("Unsupported blockquote attribute")
        elif attributes:
            raise ValueError("Unsupported HTML attribute")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("HTML tags must be balanced")

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Self-closing HTML tags are unsupported")

    def handle_data(self, data):
        if "<" in data or ">" in data:
            raise ValueError("Escape literal angle brackets")

    def handle_entityref(self, name):
        if name not in {"lt", "gt", "amp", "quot"}:
            raise ValueError("Unsupported HTML entity")

    def handle_charref(self, name):
        try:
            value = int(name[1:], 16) if name.lower().startswith("x") else int(name)
            if not 0 < value <= 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError
        except ValueError:
            raise ValueError("Invalid HTML character reference") from None

    def handle_comment(self, data):
        raise ValueError("HTML comments are unsupported")

    def handle_decl(self, decl):
        raise ValueError("HTML declarations are unsupported")

    def unknown_decl(self, data):
        raise ValueError("HTML declarations are unsupported")

    def handle_pi(self, data):
        raise ValueError("HTML processing instructions are unsupported")


def validate_bot_text(key, text, defaults=None):
    if not text.strip() or len(text.encode("utf-16-le")) // 2 > 3500:
        raise ValueError("Text must contain between 1 and 3500 UTF-16 characters")
    allowed = set(template_variables(key, defaults))
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None and (field not in allowed or not field.isidentifier() or spec or conversion):
            raise ValueError("Use only the listed template variables without formatting")
    parser = _TelegramHTML()
    try:
        parser.feed(text)
        parser.close()
    except AssertionError:
        raise ValueError("Invalid HTML declaration") from None
    if parser.stack:
        raise ValueError("HTML tags must be balanced")
