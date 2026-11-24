"""Serve pending and control requests against a disposable real provider store."""
import asyncio
import json
import sys
from pathlib import Path

# Reuse the joined-chain test's synthetic worker, admission barrier and callbacks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_cross_model_joined_chain as chain


def status(env):
    with env.engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?",
                                         (env.bound["bindingID"],)).scalar_one()
        coordinator = json.loads(row)
        health = [list(row) for row in connection.exec_driver_sql("SELECT * FROM provider_account_health ORDER BY account_id")]
        return {"state": coordinator["state"], "last": (coordinator.get("active_attempt") or {}).get("last_control"), "health": health}


async def serve():
    env = await chain._setup("provider-host", policy_on=True)
    try:
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            method, params = request["method"], request["params"]
            try:
                if method == "test/status":
                    value = status(env)
                elif method == "test/stale-account":
                    with env.engine.begin() as connection:
                        connection.exec_driver_sql("UPDATE provider_accounts SET credential_version=credential_version+1 WHERE id=?",
                                                   (env.bound["accountID"],))
                    value = {}
                elif method == chain.P + "account/bind":
                    if any(params[key] != env.bound[key] for key in ("connectionID", "modelRouteID", "actorID", "executionRevision")):
                        raise ValueError("unexpected fixture binding")
                    value = env.bound
                elif method == "_openclank/session/v1/turn/start":
                    if params != env.identity:
                        raise ValueError("unexpected fixture turn")
                    value = {"accepted": True, "replayed": False, "originalIngressRequired": False, **params}
                else:
                    value = await env.callbacks.dispatch(method, params)
                response = {"ok": True, "value": value}
            except Exception as error:
                response = {"ok": False, "error": type(error).__name__}
            print(json.dumps(response), flush=True)
    finally:
        await chain._teardown(env)


if __name__ == "__main__":
    asyncio.run(serve())
