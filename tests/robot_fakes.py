"""Deterministic command/feedback doubles; they do not model physical dynamics."""

import numpy as np


class FakeArm:
    def __init__(self, *args, **kwargs):
        self.q = np.zeros(6)
        self.commands = []
        self.closed = False
        self.healthy = True
        self.temperature = 25
        self.velocity = np.zeros(6)
        self.feedback_age = 0
        self.frozen = False
        self.opening = 1.0
        self.gripper_commands = []

    def read(self):
        return {
            "joints_rad": self.q.tolist(),
            "velocity_rad_s": self.velocity.tolist(),
            "temperature_c": self.temperature,
            "feedback_age_s": self.feedback_age,
            "healthy": self.healthy,
            "gripper_opening": self.opening,
            "command_count": len(self.commands),
        }

    def command(self, q):
        self.commands.append(q.copy())
        if not self.frozen:
            self.q = q.copy()

    def close(self):
        self.closed = True

    def command_gripper(self, opening):
        self.gripper_commands.append(opening)
        if not self.frozen:
            self.opening = opening


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


class TrackingSlipArm(FakeArm):
    """One simulated feedback excursion on the left arm's first changed joint command."""

    def __init__(self, side, **kwargs):
        super().__init__()
        self.slip_pending = side == "left"

    def command(self, q):
        slip = self.slip_pending and np.max(abs(q - self.q)) > 1e-6
        super().command(q)
        if slip:
            self.q[0] += 0.07
            self.slip_pending = False


class ReturnSlipArm(FakeArm):
    """One software tracking fault during the left arm's return toward neutral."""

    def __init__(self, side, **kwargs):
        super().__init__()
        self.slip_pending = side == "left"

    def command(self, q):
        slip = self.slip_pending and self.q[0] > 0.05 and q[0] < self.q[0] - 1e-6
        super().command(q)
        if slip:
            self.q[0] += 0.07
            self.slip_pending = False
