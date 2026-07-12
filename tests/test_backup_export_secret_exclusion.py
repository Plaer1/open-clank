from routes.backup_routes import _sanitize_export


def test_ordinary_export_removes_secret_fields_without_dropping_token_limits():
    removed = []
    clean = _sanitize_export(
        {
            "brave_api_key": "secret-value",
            "nested": {
                "smtp_password": "secret-value",
                "access_token": "secret-value",
                "research_max_tokens": 16384,
            },
            "keybinds": {"search": "ctrl+k"},
        },
        removed,
    )

    assert clean == {
        "nested": {"research_max_tokens": 16384},
        "keybinds": {"search": "ctrl+k"},
    }
    assert set(removed) == {
        "brave_api_key",
        "nested.smtp_password",
        "nested.access_token",
    }
