"""Model transport tests: fake API, simulated state, no hardware or paid requests."""

import asyncio
import base64
import copy
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
import uvicorn
from PIL import Image

from agentic_robots.feedback import ActionFeedback
from agentic_robots.model_proxy import create_app, observation_message
from tests.robot_fakes import FakeArm


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed = True


def sample(tmp_path, index=0):
    images = {}
    for role, size in [("top", (1920, 1080)), ("left", (1920, 1200)), ("right", (1920, 1200))]:
        path = tmp_path / f"{role}-{index}.png"
        Image.new("RGB", size, (index, 30, 40)).save(path)
        images[role] = {"path": str(path)}
    arms = {side: FakeArm().read() for side in ("left", "right")}
    arms["left"]["joints_rad"][0] = index / 100
    return ActionFeedback().measured({"images": images, "arms": arms, "errors": {}, "faults": {}})


def test_compact_state_and_original_camera_bytes(tmp_path):
    observation = sample(tmp_path)
    original = copy.deepcopy(observation)
    message, audit = observation_message(observation)
    assert observation == original
    assert audit["state"]["arms"]["left"]["velocity_rad_s"] == [0] * 6
    for role, block in zip(("top", "left", "right"), message["content"][2::2], strict=True):
        data = base64.b64decode(block["image_url"].split(",", 1)[1])
        assert data == Path(observation["images"][role]["path"]).read_bytes()
        assert Image.open(io.BytesIO(data)).size == (
            audit["images"][role]["width"],
            audit["images"][role]["height"],
        )
    assert "feedback_age_s" not in message["content"][0]["text"]
    assert audit["state"]["arms"]["right"]["ee_pose"]["frame"] == "right_base"


def test_unavailable_evidence_is_explicit_and_not_current(tmp_path):
    observation = sample(tmp_path)
    observation["arms"]["left"]["feedback_age_s"] = 10
    observation["images"].pop("right")
    _, audit = observation_message(observation)
    assert audit["state"]["arms"]["left"] is None
    assert audit["state"]["arms"]["right"] is not None
    assert {"arm:left", "camera:right"} <= audit["state"]["errors"].keys()


def test_every_request_refreshes_preserves_input_and_compaction(tmp_path):
    async def run():
        count, requests, streams = 0, [], []

        async def observe():
            nonlocal count
            count += 1
            return sample(tmp_path, count)

        async def upstream(request):
            assert request.headers["authorization"] == "Bearer upstream-secret"
            requests.append(json.loads(request.content))
            stream = BytesStream(b'data: {"type":"response.completed"}\n\n')
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as target:
            app = create_app(
                observe=observe,
                client=target,
                upstream="http://test/v1",
                api_key="upstream-secret",
                token="local-secret",
                audit_dir=tmp_path / "audit",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client:
                headers = {"authorization": "Bearer local-secret"}
                inputs = [{"type": "function_call_output", "call_id": "call_1", "output": "done"}]
                body = {
                    "model": "test",
                    "input": inputs,
                    "stream": True,
                    "previous_response_id": "resp_1",
                }
                for _ in range(2):
                    response = await client.post("/v1/responses", json=body, headers=headers)
                    assert response.status_code == 200
                assert count == 2
                for request in requests:
                    assert request["input"][:-1] == inputs
                    assert request["previous_response_id"] == "resp_1"
                    assert len(request["input"][-1]["content"]) == 7
                assert requests[0]["input"][-1] != requests[1]["input"][-1]
                await client.post("/v1/responses/compact", json=body, headers=headers)
                assert count == 2 and requests[-1] == body
                app.state.inject = False  # Initialization must never access the robot.
                await client.post("/v1/responses", json=body, headers=headers)
                assert count == 2 and requests[-1] == body
                denied = await client.post("/v1/responses", json=body)
                assert denied.status_code == 401 and len(requests) == 4
        assert all(stream.closed for stream in streams)
        records = list((tmp_path / "audit").glob("*.json"))
        assert len(records) == 2
        assert all("secret" not in path.read_text() for path in records)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["observation", "rate_limit", "connect", "stream"])
def test_failures_do_not_retry_requests_or_invent_observations(tmp_path, failure):
    async def run():
        calls = []

        async def observe():
            if failure == "observation":
                raise OSError("Recorder offline")
            return sample(tmp_path)

        class BrokenStream(BytesStream):
            async def __aiter__(self):
                yield b"data: partial\n\n"
                raise httpx.ReadError("Connection lost")

        stream = BrokenStream(b"") if failure == "stream" else BytesStream(b'{"error":"busy"}')

        async def upstream(request):
            calls.append(json.loads(request.content))
            if failure == "connect":
                raise httpx.ConnectError("Unavailable")
            return httpx.Response(429 if failure == "rate_limit" else 200, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as target:
            app = create_app(
                observe=observe,
                client=target,
                upstream="http://test/v1",
                api_key="secret",
                token="local",
                audit_dir=tmp_path / "audit",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client:

                async def request():
                    return await client.post(
                        "/v1/responses",
                        json={"input": "task"},
                        headers={"authorization": "Bearer local"},
                    )

                if failure == "stream":
                    with pytest.raises(httpx.ReadError):
                        await request()
                else:
                    response = await request()
                    assert response.status_code == {"connect": 502, "rate_limit": 429}.get(
                        failure, 200
                    )
        assert len(calls) == 1
        if failure == "observation":
            message = calls[0]["input"][-1]
            assert len(message["content"]) == 1
            assert "Recorder offline" in message["content"][0]["text"]
        if failure != "connect":
            assert stream.closed

    asyncio.run(run())


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            assert thread.is_alive()
            time.sleep(0.02)
        assert server.started
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def events(output, number):
    response = {
        "id": f"resp_{number}",
        "object": "response",
        "status": "completed",
        "output": output,
    }
    values = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        }
    ]
    for index, item in enumerate(output):
        values += [
            {"type": "response.output_item.added", "output_index": index, "item": item},
            {"type": "response.output_item.done", "output_index": index, "item": item},
        ]
    values += [{"type": "response.completed", "response": response}]
    return "".join(f"data: {json.dumps(value)}\n\n" for value in values).encode()


def test_client_disconnect_closes_upstream_stream(tmp_path):
    closed = threading.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            await asyncio.sleep(30)

        async def aclose(self):
            closed.set()

    async def observe():
        return sample(tmp_path)

    async def upstream(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=WaitingStream()
        )

    target = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        observe=observe,
        client=target,
        upstream="http://fake/v1",
        api_key="fake",
        token="probe",
        audit_dir=tmp_path / "audit",
    )
    with serve(app) as url:
        with httpx.Client() as client:
            with client.stream(
                "POST",
                url + "/responses",
                json={"input": "test"},
                headers={"authorization": "Bearer probe"},
            ) as response:
                assert next(response.iter_raw()) == b"data: first\n\n"
        assert closed.wait(3), "Disconnect must release the upstream model connection"


@pytest.mark.e2e
def test_installed_codex_cli_tool_round_trip_through_adapter(tmp_path):
    codex = shutil.which("codex") or "/usr/lib/chatgpt/resources/codex"
    if not os.path.isfile(codex):
        pytest.skip("Codex CLI is not installed")
    captured = []

    async def observe():
        return sample(tmp_path, len(captured) + 1)

    async def upstream(request):
        payload = json.loads(request.content)
        captured.append(payload)
        (tmp_path / f"request-{len(captured)}.json").write_text(json.dumps(payload))
        if len(captured) == 1:
            output = [
                {
                    "type": "function_call",
                    "id": "fc_probe",
                    "call_id": "call_probe",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {"cmd": "printf observation-probe", "yield_time_ms": 1000}
                    ),
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "id": "msg_done",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "Transport verified.", "annotations": []}
                    ],
                }
            ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream(events(output, len(captured))),
        )

    target = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        observe=observe,
        client=target,
        upstream="http://fake/v1",
        api_key="fake",
        token="probe",
        audit_dir=tmp_path / "audit",
    )
    with serve(app) as url:
        command = [
            codex,
            "exec",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(tmp_path),
            "-m",
            "gpt-6-astra",
            "--json",
            "-c",
            'model_provider="observation_test"',
            "-c",
            f'model_providers.observation_test={{name="test",base_url="{url}",env_key="ROBOT_MODEL_TOKEN",wire_api="responses",supports_websockets=false}}',
            "-c",
            'forced_login_method="api"',
            "-c",
            'approval_policy="never"',
            "Run the observation probe and finish.",
        ]
        result = subprocess.run(
            command,
            env={**os.environ, "ROBOT_MODEL_TOKEN": "probe"},
            capture_output=True,
            text=True,
            timeout=90,
        )
    (tmp_path / "cli.stdout").write_text(result.stdout)
    (tmp_path / "cli.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stderr + result.stdout
    assert len(captured) == 2
    outputs = [i for i in captured[1]["input"] if i.get("type") == "function_call_output"]
    assert any("observation-probe" in item["output"] for item in outputs)
    assert captured[0]["input"][-1] != captured[1]["input"][-1]
    assert "Transport verified." in result.stdout
