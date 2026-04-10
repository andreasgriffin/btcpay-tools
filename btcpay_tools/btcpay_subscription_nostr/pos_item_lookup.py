from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests


@dataclass(frozen=True)
class BtcpayPosItemData:
    pos_url: str
    item_id: str
    title: str | None
    price_text: str | None
    buy_button_text: str | None
    form_action_url: str | None
    is_free: bool


@dataclass
class _ParsedPosItem:
    card_id: str | None = None
    choice_key: str | None = None
    title_parts: list[str] = field(default_factory=list)
    price_parts: list[str] = field(default_factory=list)
    button_parts: list[str] = field(default_factory=list)
    form_action: str | None = None
    is_free: bool = False

    def item_id(self) -> str | None:
        if self.choice_key:
            return self.choice_key
        if self.card_id and self.card_id.startswith("card_"):
            return self.card_id.removeprefix("card_")
        return None

    def title(self) -> str | None:
        return _join_text_parts(self.title_parts)

    def price_text(self) -> str | None:
        return _join_text_parts(self.price_parts)

    def buy_button_text(self) -> str | None:
        return _join_text_parts(self.button_parts)


def _join_text_parts(parts: list[str]) -> str | None:
    normalized = " ".join(part for part in parts if part)
    if normalized:
        return normalized
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


class _PosPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._capture_depth: int | None = None
        self._capture_tag: str | None = None
        self._capture_target: str | None = None
        self._current_item: _ParsedPosItem | None = None
        self._current_item_depth: int | None = None
        self.items: list[_ParsedPosItem] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._stack.append(tag)
        depth = len(self._stack)
        attr_map = {key: value for key, value in attrs}
        classes = _classes(attr_map)

        if tag == "div" and "posItem" in classes:
            self._current_item = _ParsedPosItem(card_id=attr_map.get("id"))
            self._current_item_depth = depth
            self._clear_capture()
            return

        if self._current_item is None:
            return

        if tag == "form":
            self._current_item.form_action = attr_map.get("action")

        if tag == "input" and attr_map.get("name") == "choiceKey":
            self._current_item.choice_key = attr_map.get("value")

        if tag == "h5" and "card-title" in classes:
            self._start_capture(depth, tag, "title")
            return

        if tag == "span" and "fw-semibold" in classes:
            if "badge" in classes and "text-bg-info" in classes:
                self._current_item.is_free = True
            self._start_capture(depth, tag, "price")
            return

        if tag == "button" and attr_map.get("type") == "submit":
            self._start_capture(depth, tag, "button")

    def handle_data(self, data: str) -> None:
        if self._current_item is None or self._capture_target is None:
            return
        text = _normalize_text(data)
        if not text:
            return
        if self._capture_target == "title":
            self._current_item.title_parts.append(text)
            return
        if self._capture_target == "price":
            self._current_item.price_parts.append(text)
            return
        if self._capture_target == "button":
            self._current_item.button_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        depth = len(self._stack)
        if (
            self._current_item is not None
            and self._current_item_depth is not None
            and tag == "div"
            and depth == self._current_item_depth
        ):
            self.items.append(self._current_item)
            self._current_item = None
            self._current_item_depth = None
            self._clear_capture()

        if (
            self._capture_depth is not None
            and self._capture_tag == tag
            and self._capture_depth == depth
        ):
            self._clear_capture()

        if self._stack:
            self._stack.pop()

    def _start_capture(self, depth: int, tag: str, target: str) -> None:
        self._capture_depth = depth
        self._capture_tag = tag
        self._capture_target = target

    def _clear_capture(self) -> None:
        self._capture_depth = None
        self._capture_tag = None
        self._capture_target = None


def _classes(attrs: dict[str, str | None]) -> set[str]:
    raw = attrs.get("class") or ""
    return {part for part in raw.split() if part}


class BtcpayPosItemLookup:
    def __init__(
        self,
        timeout_seconds: int = 20,
        proxy_dict: dict[str, str] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.proxy_dict = proxy_dict
        self.session = requests.Session()
        if proxy_dict:
            self.session.proxies.update(proxy_dict)

    def fetch(
        self,
        pos_url: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, BtcpayPosItemData]:
        proxies = proxy_dict if proxy_dict is not None else self.proxy_dict
        try:
            response = self.session.get(
                pos_url,
                timeout=self.timeout_seconds,
                proxies=proxies,
            )
        except requests.RequestException:
            return {}
        if response.status_code >= 400:
            return {}
        return self.parse_items(response.url, response.text)

    @staticmethod
    def parse_items(
        pos_url: str,
        html: str,
    ) -> dict[str, BtcpayPosItemData]:
        try:
            parser = _PosPageParser()
            parser.feed(html)
            parser.close()
        except Exception:
            return {}

        items: dict[str, BtcpayPosItemData] = {}
        for parsed_item in parser.items:
            item_id = parsed_item.item_id()
            if item_id is None:
                continue
            items[item_id] = BtcpayPosItemData(
                pos_url=pos_url,
                item_id=item_id,
                title=parsed_item.title(),
                price_text=parsed_item.price_text(),
                buy_button_text=parsed_item.buy_button_text(),
                form_action_url=_absolute_form_action(pos_url, parsed_item.form_action),
                is_free=parsed_item.is_free,
            )
        return items


def _absolute_form_action(pos_url: str, form_action: str | None) -> str | None:
    if not form_action:
        return None
    return urllib.parse.urljoin(pos_url, form_action)
