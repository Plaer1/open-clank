import pytest
from types import SimpleNamespace

from routes import skills_routes


def test_effectful_or_unknown_skill_tools_require_manual_verification():
    assert "bash" in skills_routes._skill_test_unsafe_tools("Run `bash` and send_email")
    assert "send_email" in skills_routes._skill_test_unsafe_tools("Run `bash` and send_email")
    assert "mcp__*" in skills_routes._skill_test_unsafe_tools("Call mcp__remote__mutate")
    assert skills_routes._skill_test_unsafe_tools("Use read_file then grep") == []


@pytest.mark.asyncio
async def test_skill_judge_uses_managed_route_identity(monkeypatch):
    seen = {}
    route = SimpleNamespace(
        model_route_id="route-judge",
        provider_grant_id="grant-judge",
    )

    async def complete_text(**kwargs):
        seen.update(kwargs)
        return "pass"

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", complete_text)

    result = await skills_routes._skill_auxiliary_call(
        "skill_run_judge",
        [{"role": "user", "content": "grade this"}],
        owner="alice",
        timeout=5,
        route=route,
        root_operation_id="turn-judge",
        temperature=0.1,
        max_tokens=64,
    )

    assert result == "pass"
    assert seen["owner"] == "alice"
    assert seen["purpose"] == "utility"
    assert seen["model_route_id"] == "route-judge"
    assert seen["grant_id"] == "grant-judge"
    assert seen["root_operation_id"] == "turn-judge"
    assert seen["max_output_tokens"] == 64
    assert "url" not in seen
    assert "headers" not in seen


@pytest.mark.asyncio
async def test_infrastructure_failure_is_not_sent_to_semantic_judge(monkeypatch):
    judged = []
    streamed = {}

    async def failed_stream(target, *args, **kwargs):
        streamed["target"] = target
        streamed["envelope"] = kwargs.get("turn_envelope")
        yield 'event: error\ndata: {"code":"SUPERVISOR_UNAVAILABLE","error":"down"}\n\n'
        yield "data: [DONE]\n\n"

    async def judge(*args, **kwargs):
        judged.append(True)
        return {"verdict": "fail"}

    monkeypatch.setattr("src.model_dispatch.stream_agent_target", failed_stream)
    monkeypatch.setattr(skills_routes, "_eval_skill_run", judge)
    route = SimpleNamespace(
        model_route_id="route-1",
        provider_grant_id="grant-1",
        provider_model_id="model",
        connection_id="connection-1",
        runtime_model="connection-1/model",
        capabilities={"tools": True},
    )

    _transcript, verdict = await skills_routes._run_skill_test_once(
        "---\nname: read-only\n---\nUse read_file.",
        "Inspect fixture.txt",
        route,
        "alice",
        root_operation_id="turn-skill-test",
    )

    assert verdict["verdict"] == "manual_verification_required"
    assert judged == []
    assert streamed["target"].transport == "acp"
    assert streamed["target"].endpoint_id == "connection-1"
    assert streamed["target"].provider_id == "connection-1"
    assert streamed["target"].headers == {}
    assert streamed["envelope"]["provider_grant_id"] == "grant-1"
    assert streamed["envelope"]["root_operation_id"] == "turn-skill-test"
