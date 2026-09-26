"""Attach recorder observations to Codex's HTTP model requests; never sends motion."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx2 as httpx
from anyio import CancelScope
from PIL import Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from agentic_robots.bridge import Bridge, vector


def observation_message(observation):
    """Return compact model input and an auditable record, without resizing images."""
    state = {
        "arms": {},
        "faults": observation.get("faults", {}),
        "errors": dict(observation.get("errors", {})),
        "recording_phase": observation.get("recording", {}).get("task", {}).get("phase"),
    }
    for side in ("left", "right"):
        arm = observation.get("arms", {}).get(side)
        try:
            if arm is None:
                raise ValueError("Arm session is not connected")
            Bridge.healthy(arm)
            state["arms"][side] = {
                "joints_rad": vector(arm["joints_rad"], 6).tolist(),
                "velocity_rad_s": vector(arm["velocity_rad_s"], 6).tolist(),
                "gripper_opening": float(vector([arm["gripper_opening"]], 1)[0]),
                "ee_pose": arm.get("ee_pose"),
            }
        except Exception as exc:
            state["arms"][side] = None
            state["errors"][f"arm:{side}"] = str(exc)
    content, images = [], {}
    for role in ("top", "left", "right"):
        try:
            record = observation.get("images", {}).get(role)
            if record is None:
                raise ValueError("No current camera image")
            path = Path(record["path"])
            data = path.read_bytes()
            with Image.open(path) as image:
                mime, size = Image.MIME[image.format], image.size
            images[role] = {
                "path": str(path),
                "width": size[0],
                "height": size[1],
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            content.extend(
                [
                    {"type": "input_text", "text": f"{role} camera"},
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                        "detail": "high",
                    },
                ]
            )
        except Exception as exc:
            state["errors"][f"camera:{role}"] = str(exc)
    content.insert(
        0,
        {
            "type": "input_text",
            "text": "Current robot observation, collected before this model request. "
            "Unavailable evidence is not current state.\n" + json.dumps(state, allow_nan=False),
        },
    )
    return {"role": "user", "content": content}, {"state": state, "images": images}


def create_app(*, observe, client, upstream, api_key, token, audit_dir):
    """A single-run, loopback-only relay. Caller owns client and service lifecycle."""
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    sequence = 0

    async def forward(request: Request):
        nonlocal sequence
        audit_path = None
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"error": "Invalid local model token"}, status_code=401)
        if request.headers.get("content-encoding", "identity") != "identity":
            return JSONResponse({"error": "Request compression is unsupported"}, status_code=415)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a model request object")
            if request.url.path == "/v1/responses" and app.state.inject:
                try:
                    observation = await observe()
                except Exception as exc:
                    observation = {"errors": {"observation": str(exc)}}
                message, audit = observation_message(observation)
                inputs = payload.get("input", [])
                if isinstance(inputs, str):
                    inputs = [{"role": "user", "content": inputs}]
                if not isinstance(inputs, list):
                    raise ValueError("Expected a list or text model input")
                payload["input"] = [*inputs, message]
                sequence += 1
                audit["prepared_unix"] = time.time()
                audit_path = audit_dir / f"{sequence:06d}.json"
                # No keys, headers, base64, or unrelated conversation content in the audit.
                audit_path.write_text(json.dumps(audit, indent=2, allow_nan=False) + "\n")
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse(
                {"error": f"Cannot prepare model observation: {exc}"}, status_code=400
            )
        headers = {
            k: v
            for k, v in request.headers.items()
            if k
            not in {"host", "content-length", "authorization", "connection", "content-encoding"}
        }
        headers["authorization"] = f"Bearer {api_key}"
        try:
            outgoing = client.build_request(
                "POST",
                upstream.rstrip("/") + request.url.path.removeprefix("/v1"),
                json=payload,
                headers=headers,
            )
            response = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "Upstream model connection failed"}, status_code=502)

        if audit_path is not None:
            audit.update(
                model_http_status=response.status_code,
                model_request_id=response.headers.get("x-request-id"),
            )
            try:
                audit_path.write_text(json.dumps(audit, indent=2, allow_nan=False) + "\n")
            except OSError:
                await response.aclose()
                return JSONResponse(
                    {"error": "Cannot save model observation receipt"}, status_code=500
                )

        async def stream():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                with CancelScope(shield=True):
                    await response.aclose()

        return StreamingResponse(
            stream(),
            status_code=response.status_code,
            headers={
                k: v
                for k, v in response.headers.items()
                if k not in {"connection", "transfer-encoding", "content-length"}
            },
        )

    app = Starlette(
        routes=[
            Route("/v1/responses", forward, methods=["POST"]),
            Route("/v1/responses/compact", forward, methods=["POST"]),
        ]
    )
    app.state.inject = True
    return app
