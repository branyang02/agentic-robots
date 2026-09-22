"""Persistence/neutral contracts with simulated arms and a fake Codex conversation."""

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest

from agentic_robots.bridge import Action
from agentic_robots.recording import RecordingBridge, Rollout
from agentic_robots.task import Review, continuation, neutral_feedback
from scripts import robot_record
from tests.robot_fakes import FakeArm
from tests.test_robot_record import cameras, ready_fake


def state():
    return {"arms": {side: FakeArm().read() for side in ("left", "right")}, "faults": {}}


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    feedback = state()
    upstream = Mock(
        side_effect=lambda _, tool, __: (
            {"status": "completed"} if tool == "execute" else copy.deepcopy(feedback)
        )
    )
    monkeypatch.setattr("agentic_robots.recording.upstream_call", upstream)
    monkeypatch.setattr(Rollout, "start", ready_fake)

    def finish(r):
        r.state = "finished"
        r.save_manifest()
        return r.status()

    monkeypatch.setattr(Rollout, "finish", finish)
    bridge = RecordingBridge(output_root=tmp_path, cameras=cameras, upstream="mock-only")
    assert bridge.recording("start", "Arrange shoes")["ready"]
    return bridge, feedback, upstream


@pytest.mark.parametrize("problem", ["away", "moving", "stale", "missing", "fault", "nan"])
def test_finish_checks_measured_arms_and_preserves_active_attempt(attempt, problem):
    bridge, feedback, _ = attempt
    arm = feedback["arms"]["left"]
    if problem == "away":
        arm["joints_rad"][2] = 0.3
    elif problem == "moving":
        arm["velocity_rad_s"][1] = 0.1
    elif problem == "stale":
        arm["feedback_age_s"] = 1
    elif problem == "missing":
        del feedback["arms"]["left"]
    elif problem == "fault":
        feedback["faults"]["left"] = {"code": "tracking_error", "recoverable": True}
    else:
        arm["joints_rad"][0] = float("nan")
    result = bridge.recording("finish")
    assert result["error"]["code"] == "neutral_unverified"
    assert "left" in result["error"]["details"]["problems"]
    assert bridge.status()["ready"]
    assert bridge.status()["task"]["phase"] == "active"
    for worker in bridge.rollout.workers.values():
        worker.close.assert_not_called()


def test_finish_then_review_then_retry_links_correction(attempt):
    bridge, _, upstream = attempt
    previous = bridge.rollout.output
    assert bridge.recording("finish")["task"]["phase"] == "review"
    assert bridge.recording("start", "same task")["error"]["code"] == "attempt_unreviewed"
    result = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert result["error"]["code"] == "recording_unavailable"
    assert all(call.args[1] == "session" for call in upstream.call_args_list)
    decision = Review(
        outcome="retry",
        summary="Shoe slipped",
        evidence=["Video 00:12: heel slipped"],
        correction="Use the other arm to support the heel",
    )
    assert bridge.recording("review", review=decision)["task"]["phase"] == "retry"
    saved = json.loads((previous / "manifest.json").read_text())
    assert saved["task"]["review"]["correction"] == decision.correction
    next_attempt = bridge.recording("start", "Arrange shoes with heel support")
    assert next_attempt["task"]["previous_attempt"] == str(previous)
    assert next_attempt["task"]["correction"] == decision.correction


def test_return_declaration_never_moves_and_video_failure_does_not_trap_return(
    attempt, monkeypatch
):
    bridge, _, upstream = attempt
    bridge.rollout.workers["top"].call.side_effect = RuntimeError("Camera worker exited")
    bridge.rollout.error = "encoder stopped"
    monkeypatch.setattr(bridge.rollout, "event", Mock(side_effect=OSError("disk full")))
    monkeypatch.setattr(bridge.rollout, "save_manifest", Mock(side_effect=OSError("disk full")))
    assert bridge.recording("return", "Task done; return both arms")["task"]["phase"] == "returning"
    upstream.assert_not_called()
    bridge.session("recover", arm="left")
    assert upstream.call_args.args[2]["operation"] == "recover"
    bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert sum(c.args[1] == "execute" for c in upstream.call_args_list) == 1
    assert upstream.call_args.args[1:] == ("session", {"operation": "status"})


def test_recoverable_latch_cannot_be_declared_unavailable_control(attempt):
    bridge, feedback, _ = attempt
    feedback["faults"]["left"] = {"code": "tracking_error", "recoverable": True}
    decision = Review(
        outcome="needs_intervention",
        summary="Return stopped",
        evidence=["Tracking error"],
        constraint="Return stopped",
        intervention="control_unavailable",
    )
    assert bridge.recording("review", review=decision)["error"]["code"] == "control_still_available"
    assert bridge.status()["ready"]
    feedback["arms"]["left"]["healthy"] = False
    assert bridge.recording("review", review=decision)["task"]["phase"] == "needs_intervention"
    assert not neutral_feedback(feedback)["verified"]


@pytest.mark.parametrize("outcome", ["retry", "blocked", "needs_intervention"])
def test_decisions_require_a_correction_or_specific_constraint(outcome):
    with pytest.raises(ValueError):
        Review(outcome=outcome, summary="Stopped", evidence=["frame 1"])


@pytest.mark.parametrize("kind", ["active", "interrupted", "failed", "old", "terminal", "stop"])
def test_watcher_does_not_resume_working_stopped_failed_or_completed_tasks(kind):
    status = {"task": {"phase": "active"}}
    page = {"thread": {"status": {"type": "idle"}}, "turns": [{"id": "new", "status": "completed"}]}
    if kind == "active":
        page["thread"]["status"]["type"] = "active"
    elif kind in {"interrupted", "failed"}:
        page["turns"][0]["status"] = kind
    elif kind == "old":
        page["turns"][0]["id"] = "old"
    elif kind == "stop":
        status["task"]["watch_paused"] = True
    else:
        status["task"]["phase"] = "success"
    assert continuation(status, page, "old") is None


def test_watcher_resumes_same_agent_once_with_feedback_without_commanding(attempt, monkeypatch):
    bridge, feedback, upstream = attempt
    feedback["arms"]["left"]["joints_rad"][1] = 0.5
    bridge.binding = {"thread_id": "same-agent", "after_turn_id": "init"}
    page = {
        "thread": {"id": "same-agent", "status": {"type": "idle"}},
        "turns": [{"id": "early-end", "status": "completed"}],
    }
    sent = []

    async def app_call(client, thread, tool, arguments):
        assert thread == "same-agent"
        if tool == "read_thread":
            return page
        sent.append(arguments)
        return {}

    monkeypatch.setattr(robot_record, "app_call", app_call)
    asyncio.run(robot_record.watch_once(bridge, object()))
    asyncio.run(robot_record.watch_once(bridge, object()))
    assert len(sent) == 1 and sent[0]["threadId"] == "same-agent"
    assert "0.5" in sent[0]["prompt"] and "still active" in sent[0]["prompt"]
    assert all(call.args[1] == "session" for call in upstream.call_args_list)


def test_watcher_rechecks_when_user_starts_another_turn(attempt, monkeypatch):
    bridge, _, _ = attempt
    bridge.binding = {"thread_id": "same-agent", "after_turn_id": "init"}
    reads = []

    async def app_call(client, thread, tool, arguments):
        assert tool == "read_thread", "Must not send into an active user turn"
        reads.append(tool)
        return {
            "thread": {"id": thread, "status": {"type": "idle" if len(reads) == 1 else "active"}},
            "turns": [{"id": "early-end", "status": "completed"}],
        }

    monkeypatch.setattr(robot_record, "app_call", app_call)
    asyncio.run(robot_record.watch_once(bridge, object()))
    assert len(reads) == 2


def test_uncertain_continuation_is_not_resent_and_wrong_thread_is_rejected(attempt, monkeypatch):
    bridge, _, _ = attempt
    bridge.binding = {"thread_id": "same-agent", "after_turn_id": "init"}
    sent = []
    page = {
        "thread": {"id": "different", "status": {"type": "idle"}},
        "turns": [{"id": "early-end", "status": "completed"}],
    }

    async def app_call(client, thread, tool, arguments):
        if tool == "read_thread":
            return page
        sent.append(arguments)
        raise TimeoutError("Unknown delivery")

    monkeypatch.setattr(robot_record, "app_call", app_call)
    with pytest.raises(ValueError, match="different conversation"):
        asyncio.run(robot_record.watch_once(bridge, object()))
    assert not sent
    page["thread"]["id"] = "same-agent"
    with pytest.raises(TimeoutError):
        asyncio.run(robot_record.watch_once(bridge, object()))
    asyncio.run(robot_record.watch_once(bridge, object()))
    assert len(sent) == 1 and bridge.binding["delivery_unknown"]


def test_failed_capture_start_can_be_repaired_before_any_motion(attempt):
    bridge, _, upstream = attempt
    bridge.rollout.state = "failed"
    bridge.rollout.workers["top"].call.side_effect = RuntimeError("Camera worker exited")
    assert bridge.recording("start", "Retry camera setup")["ready"]
    upstream.assert_not_called()
