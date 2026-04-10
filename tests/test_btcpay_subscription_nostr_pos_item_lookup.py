from unittest.mock import Mock, patch

import requests

from btcpay_tools.btcpay_subscription_nostr.pos_item_lookup import (
    BtcpayPosItemLookup,
)


def test_lookup_fetches_items_without_language_specific_parsing() -> None:
    html = """
    <div id="card_monatlich" class="col posItem posItem--displayed">
        <div class="tile card">
            <div class="card-body d-flex flex-column gap-2 mb-auto">
                <h5 class="card-title m-0">Monatliches Abo</h5>
                <div class="d-flex gap-2 align-items-center">
                    <span class="fw-semibold badge text-bg-info">Kostenlos</span>
                </div>
            </div>
            <div class="card-footer">
                <form method="post" autocomplete="off" action="/apps/app123/pos">
                    <input type="hidden" name="choiceKey" value="monatlich" />
                    <button class="btn btn-primary w-100" type="submit">
                        Jetzt kaufen
                    </button>
                </form>
            </div>
        </div>
    </div>
    """
    response = Mock(
        status_code=200,
        text=html,
        url="https://example.com/apps/app123/pos?lang=de",
    )
    lookup = BtcpayPosItemLookup()

    with patch.object(lookup.session, "get", return_value=response):
        items = lookup.fetch("https://example.com/apps/app123/pos")

    item = items["monatlich"]
    assert item.pos_url == "https://example.com/apps/app123/pos?lang=de"
    assert item.item_id == "monatlich"
    assert item.title == "Monatliches Abo"
    assert item.price_text == "Kostenlos"
    assert item.buy_button_text == "Jetzt kaufen"
    assert item.form_action_url == "https://example.com/apps/app123/pos"
    assert item.is_free is True


def test_parse_items_returns_all_items_by_item_id() -> None:
    html = """
    <div id="card_monatlich" class="col posItem posItem--displayed">
        <form method="post" action="/apps/app123/pos">
            <h5 class="card-title m-0">Monatliches Abo</h5>
            <span class="fw-semibold badge text-bg-info">Kostenlos</span>
            <input type="hidden" name="choiceKey" value="monatlich" />
            <button type="submit">Jetzt kaufen</button>
        </form>
    </div>
    <div id="card_business-plan" class="col posItem posItem--displayed">
        <form method="post" action="/apps/app123/pos">
            <h5 class="card-title m-0">Business Plan</h5>
            <span class="fw-semibold">10,00 EUR</span>
            <input type="hidden" name="choiceKey" value="business-plan" />
            <button type="submit">Jetzt kaufen</button>
        </form>
    </div>
    """

    items = BtcpayPosItemLookup.parse_items(
        "https://example.com/apps/app123/pos?lang=de",
        html,
    )

    assert sorted(items) == ["business-plan", "monatlich"]
    assert items["monatlich"].title == "Monatliches Abo"
    assert items["monatlich"].is_free is True
    assert items["business-plan"].price_text == "10,00 EUR"
    assert items["business-plan"].is_free is False


def test_lookup_uses_card_id_as_item_id_fallback() -> None:
    lookup = BtcpayPosItemLookup()
    response = Mock(
        status_code=200,
        text='<div id="card_known" class="col posItem"></div>',
        url="https://example.com/apps/app123/pos",
    )

    with patch.object(lookup.session, "get", return_value=response):
        items = lookup.fetch("https://example.com/apps/app123/pos")

    assert sorted(items) == ["known"]
    assert items["known"].item_id == "known"


def test_lookup_uses_proxy_override() -> None:
    lookup = BtcpayPosItemLookup(
        proxy_dict={"https": "http://default-proxy.local:8443"},
    )
    response = Mock(
        status_code=200,
        text="""
        <div id="card_paid" class="col posItem">
            <form action="/apps/app123/pos">
                <input type="hidden" name="choiceKey" value="paid" />
            </form>
        </div>
        """,
        url="https://example.com/apps/app123/pos",
    )
    override_proxy = {"https": "http://override-proxy.local:8443"}

    with patch.object(lookup.session, "get", return_value=response) as mocked_get:
        items = lookup.fetch(
            "https://example.com/apps/app123/pos",
            proxy_dict=override_proxy,
        )

    assert mocked_get.call_args.kwargs["proxies"] == override_proxy
    assert "paid" in items


def test_lookup_returns_empty_mapping_on_http_error() -> None:
    lookup = BtcpayPosItemLookup()
    response = Mock(
        status_code=500,
        text="server error",
        url="https://example.com/apps/app123/pos",
    )

    with patch.object(lookup.session, "get", return_value=response):
        items = lookup.fetch("https://example.com/apps/app123/pos")

    assert items == {}


def test_lookup_returns_empty_mapping_on_request_exception() -> None:
    lookup = BtcpayPosItemLookup()

    with patch.object(
        lookup.session,
        "get",
        side_effect=requests.RequestException("network down"),
    ):
        items = lookup.fetch("https://example.com/apps/app123/pos")

    assert items == {}
