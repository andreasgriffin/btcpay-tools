from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests
import json


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

        # Newer BTCPay POS pages render a Vue template and put the real item
        # data inside: const srvModel = { ... };
        self._script_depth: int | None = None
        self._script_parts: list[str] = []

    def feed(self, data: str) -> None:
        super().feed(data)

        # Prefer the concrete Vue model items when present. The HTML card
        # content only contains placeholders such as {{ item.title }}.
        srv_model_items = self._parse_srv_model_items("".join(self._script_parts))
        if srv_model_items:
            self.items = srv_model_items

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._stack.append(tag)
        depth = len(self._stack)

        if tag == "script":
            self._script_depth = depth

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

        if tag == "button" and attr_map.get("type") in {"submit", "button", None}:
            self._start_capture(depth, tag, "button")

    def handle_data(self, data: str) -> None:
        if self._script_depth is not None:
            self._script_parts.append(data)

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

        if (
            self._script_depth is not None
            and tag == "script"
            and self._script_depth == depth
        ):
            self._script_depth = None

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

    def _parse_srv_model_items(self, script_text: str) -> list[_ParsedPosItem]:
        raw_model = self._extract_js_object_after_marker(
            script_text, "const srvModel ="
        )
        if raw_model is None:
            return []

        try:
            model = json.loads(raw_model)
        except json.JSONDecodeError:
            return []

        app_id = model.get("appId")
        form_action = f"/apps/{app_id}/pos" if app_id else None

        parsed_items: list[_ParsedPosItem] = []

        for item in model.get("items", []):
            item_id = item.get("id")
            title = item.get("title")
            price_formatted = item.get("priceFormatted")
            button_text = item.get("buttonText")

            parsed = _ParsedPosItem(
                card_id=f"card_{item_id}" if item_id else None,
            )

            if title is not None:
                parsed.title_parts.append(str(title))

            if price_formatted is not None:
                parsed.price_parts.append(str(price_formatted))

            if button_text is not None:
                parsed.button_parts.append(str(button_text))

            parsed.choice_key = str(item_id) if item_id is not None else None
            parsed.form_action = form_action
            parsed.is_free = (
                item.get("hasPrice") is False
                or str(price_formatted or "").strip().lower() == "free"
                or str(item.get("price") or "").strip() == "0"
            )

            parsed_items.append(parsed)

        return parsed_items

    def _extract_js_object_after_marker(
        self,
        text: str,
        marker: str,
    ) -> str | None:
        marker_pos = text.find(marker)
        if marker_pos == -1:
            return None

        start = text.find("{", marker_pos)
        if start == -1:
            return None

        depth = 0
        in_string = False
        quote_char: str | None = None
        escape = False

        for pos in range(start, len(text)):
            ch = text[pos]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote_char:
                    in_string = False
                    quote_char = None
                continue

            if ch in {"'", '"'}:
                in_string = True
                quote_char = ch
                continue

            if ch == "{":
                depth += 1
                continue

            if ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : pos + 1]

        return None


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
