import base64
import json

from src import chatgpt_subscription as subscription


def _jwt(payload):
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{segment}.signature"


def test_account_id_is_extracted_and_sent_as_backend_scope_header():
    token = _jwt({"chatgpt_account_id": "acct_fixture"})

    assert subscription.extract_account_id({"id_token": token}) == "acct_fixture"
    headers = subscription.chatgpt_headers(token)
    assert headers["ChatGPT-Account-Id"] == "acct_fixture"


def test_live_catalog_keeps_visible_subscription_slugs_and_hides_hidden(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {"slug": "new-subscription-model", "priority": 2},
                    {"slug": "hidden-api-model", "visibility": "hidden", "priority": 1},
                ]
            }

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(subscription.httpx, "get", fake_get)
    models = subscription.fetch_available_models("access", account_id="acct_fixture")

    assert models == ["new-subscription-model"]
    assert seen["headers"]["ChatGPT-Account-Id"] == "acct_fixture"
