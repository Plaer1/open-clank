def test_agent_error_sse_parser_keeps_typed_recovery_fields():
    from routes.chat_routes import _agent_error_from_sse

    frame = (
        "event: error\n"
        'data: {"code":"MODEL_CAPABILITY_UNKNOWN","error":"Certify tools.",'
        '"status":409,"actions":["certify_or_decline_tools"],'
        '"details":{"endpoint_id":"ep","model_id":"model"}}\n\n'
    )
    assert _agent_error_from_sse(frame) == {
        "code": "MODEL_CAPABILITY_UNKNOWN",
        "error": "Certify tools.",
        "status": 409,
        "actions": ["certify_or_decline_tools"],
        "details": {"endpoint_id": "ep", "model_id": "model"},
    }
