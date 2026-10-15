"""Small, deterministic boundaries for Telegram's HTML and size limits."""
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

MESSAGE_LIMIT = 4000  # Leave room below Telegram's 4096 UTF-16 code-unit limit.
CALLBACK_LIMIT = 180


def units(text):
    return len(str(text).encode("utf-16-le", errors="replace")) // 2


def clip(text, limit):
    text = "" if text is None else str(text)
    if units(text) <= limit:
        return text
    out, size = [], 0
    for char in text:
        size += units(char)
        if size > limit - 1:
            break
        out.append(char)
    return "".join(out) + "…"


def plain(text):
    """Decode source entities once; source strings are data, never template HTML."""
    value = html.unescape("" if text is None else str(text))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)


def escape(text, limit=None):
    value = plain(text)
    return html.escape(clip(value, limit) if limit is not None else value, quote=True)


def safe_url(value):
    value = plain(value).strip()
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"https", "http"} or not parts.hostname
                or parts.username or parts.password or any(ch.isspace() for ch in value)
                or re.search(r"[\x00-\x1f\x7f]", value)):
            return None
    except ValueError:
        return None
    return value


def url_button(label, url):
    url = safe_url(url)
    return {"text": clip(plain(label), 60), "url": url} if url else None


def keyboard(*rows):
    result = [[button for button in row if button] for row in rows]
    return {"inline_keyboard": [row for row in result if row]}


def callback(label, data):
    if not 1 <= len(data.encode("utf-8")) <= 64:
        raise ValueError("callback_data_limit")
    return {"text": clip(plain(label), 60), "callback_data": data}


class _Template(HTMLParser):
    """Normalize our templates, retaining balanced formatting across chunks.

    Dynamic fields must still use escape(): allowing a tag here never makes an
    untrusted field safe. Unknown template markup is rendered as literal text.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in {"b", "i", "code", "pre", "u", "s"}:
            opening = f"<{tag}>"
        elif tag == "a" and safe_url(dict(attrs).get("href", "")):
            opening = f'<a href="{html.escape(dict(attrs)["href"], quote=True)}">'
        elif tag == "br":
            self.events.append(("text", "\n"))
            return
        else:
            self.events.append(("text", self.get_starttag_text()))
            return
        self.stack.append(tag)
        self.events.append(("open", (tag, opening)))

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                item = self.stack.pop()
                self.events.append(("close", item))
                if item == tag:
                    break
        else:
            self.events.append(("text", f"</{tag}>"))

    def handle_data(self, data):
        self.events.append(("text", data))

    def finish(self):
        self.close()
        while self.stack:
            self.events.append(("close", self.stack.pop()))


def split_html(template, limit=MESSAGE_LIMIT):
    parser = _Template()
    parser.feed(str(template or ""))
    parser.finish()
    chunks, parts, opened, size = [], [], [], 0

    def flush():
        nonlocal parts, size
        if size:
            chunks.append("".join(parts) + "".join(f"</{tag}>" for tag, _ in reversed(opened)))
        parts = [opening for _, opening in opened]
        size = 0

    for kind, value in parser.events:
        if kind == "open":
            opened.append(value)
            parts.append(value[1])
        elif kind == "close":
            if opened and opened[-1][0] == value:
                opened.pop()
                parts.append(f"</{value}>")
        else:
            for char in value:
                width = units(char)
                if size + width > limit:
                    flush()
                parts.append(html.escape(char, quote=False))
                size += width
    flush()
    return chunks or ["（无内容）"]


def fit_html(template, limit=MESSAGE_LIMIT):
    chunks = split_html(template, limit - 28)
    if len(chunks) == 1:
        return chunks[0]
    return chunks[0] + "\n<i>内容较长，请打开详情或原帖查看。</i>"


def valid_markup(markup):
    if not markup:
        return None
    rows = []
    for row in markup.get("inline_keyboard", []):
        buttons = []
        for button in row:
            label = clip(plain(button.get("text", "")), 60) or "查看"
            data = button.get("callback_data")
            if isinstance(data, str) and 1 <= len(data.encode("utf-8")) <= 64:
                buttons.append({"text": label, "callback_data": data})
            elif safe_url(button.get("url", "")):
                buttons.append({"text": label, "url": safe_url(button["url"])})
        if buttons:
            rows.append(buttons)
    return {"inline_keyboard": rows} if rows else None
