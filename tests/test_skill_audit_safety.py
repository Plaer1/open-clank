import pytest

from routes import skills_routes


def test_effectful_or_unknown_skill_tools_require_manual_verification():
    assert "bash" in skills_routes._skill_test_unsafe_tools("Run `bash` and send_email")
    assert "send_email" in skills_routes._skill_test_unsafe_tools("Run `bash` and send_email")
    assert "mcp__*" in skills_routes._skill_test_unsafe_tools("Call mcp__remote__mutate")
    assert skills_routes._skill_test_unsafe_tools("Use read_file then grep") == []


@pytest.mark.asyncio
async def test_infrastructure_failure_is_not_sent_to_semantic_judge(monkeypatch):
    judged = []

    async def failed_stream(*args, **kwargs):
        yield 'event: error\ndata: {"code":"SUPERVISOR_UNAVAILABLE","error":"down"}\n\n'
        yield "data: [DONE]\n\n"

    async def judge(*args, **kwargs):
        judged.append(True)
        return {"verdict": "fail"}

    monkeypatch.setattr(skills_routes, "_configured_endpoint_id", lambda *args: "endpoint-1")
    monkeypatch.setattr("src.model_dispatch.stream_agent_target", failed_stream)
    monkeypatch.setattr(skills_routes, "_eval_skill_run", judge)

    _transcript, verdict = await skills_routes._run_skill_test_once(
        "---\nname: read-only\n---\nUse read_file.",
        "Inspect fixture.txt",
        "http://example.test/v1/chat/completions",
        "model",
        {},
        "alice",
    )

    assert verdict["verdict"] == "manual_verification_required"
    assert judged == []
