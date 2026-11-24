"""Provider-control regressions for authenticated local connections."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_CONTROL = (_ROOT / "static" / "js" / "providerControl.js").read_text(
    encoding="utf-8"
)
_ADMIN = (_ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")


def test_connection_creation_never_accepts_a_credential():
    create = _CONTROL.split("function renderCreateConnection()", 1)[1].split(
        "function accountHealthBadge", 1
    )[0]
    connection_request = create.split("connection = await request('/connections'", 1)[1].split(
        "created = true", 1
    )[0]
    assert "url: enteredUrl || null" in connection_request
    assert "api_key" not in connection_request
    assert "body: { label: `Account ${accountsFor(connection.id).length + 1}`, api_key: enteredSecret }" in create


def test_quick_add_keeps_compatible_http_servers_in_the_local_lane():
    create = _CONTROL.split("function renderCreateConnection()", 1)[1].split(
        "function accountHealthBadge", 1
    )[0]
    assert "data-provider-add-mode" in create
    assert "addCard('local', localFamilies)" in create
    assert "addCard('remote', remoteFamilies)" in create
    assert "if (isLocal && kinds.includes('local')) return 'local'" in create
    assert "methods.find(method => method.type === 'none')" in create
    assert "secret.required = usesSecret" in create
    assert "http://localhost:11434" in create


def test_api_key_account_is_masked_write_only_and_cleared():
    account = _CONTROL.split("function renderAddAccount", 1)[1].split(
        "function oauthPanel", 1
    )[0]
    assert "type: 'password'" in account
    assert "body: { label: label.value.trim(), api_key: value }" in account
    assert account.count("secret.value = ''") >= 3
    assert "Write-only" in account


def test_retired_local_endpoint_form_is_not_driven_by_admin_javascript():
    assert "adm-epLocalApiKey" not in _ADMIN
    assert "/api/model-endpoints" not in _ADMIN
