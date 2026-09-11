# Cartesian tracking validation — September 11, 2026

The Cartesian feature remains under physical validation. Software path feasibility
and a completed command are not evidence that the real tool follows a straight path.
Tool positions below are FK of measured joints, not external pose measurements.

## Endpoint feedback fix

Previously the bridge checked the final target immediately after writing it.
A simulated controller applying commands on its next update reproduced a false
endpoint failure: a 2 cm / 0.2 s path stopped with 1.818 mm error even though every
preceding sample was followed exactly. The instantaneous feedback in the original
FakeArm concealed this timing defect.

Cartesian completion now waits for feedback newer than the final command. It
holds that endpoint for up to 150 ms to meet the same 1 mm / 0.5 degree tolerance.
Stop requests, feedback health and the 3 degree joint guard remain active during
this settling period. Moving-path checks are unchanged. The response reports
`path.settling_s` separately from the requested motion duration. This does not add
an approach, retry, compensation offset, or another motion segment.

Validation: 256 software tests passed, one optional Codex E2E test skipped. New
simulated tests cover delayed feedback, persistent endpoint error, stale feedback,
and interruption while settling. These tests do not validate physical dynamics.

## Hardware iteration

The controller was replaced only after verifying both sessions were released.
No gains, gravity compensation, or moving tracking tolerances were changed.

- A zero-displacement Cartesian hold completed on hardware, reporting 20.07 ms
  settling time. This validates the endpoint feedback path, not a physical lift.
- The staged transition to the bent pose completed.
- A 1 cm upward Cartesian motion stopped during execution at 1.060 mm tracking
  error. The endpoint timing fix therefore does not resolve the moving-path issue.
- For the same 30 degree elbow target, approaching from below settled at 28.491
  degrees and approaching from above settled at 30.523 degrees. Observations were
  taken about 14 seconds after the actions, with near-zero reported velocity.
  Motor effort was 8.403 Nm from below and 5.450 Nm from above; corresponding
  gravity feedforward was 6.362 and 6.219 Nm.
- Repeating the lift from the above-target state stopped at 1.066 mm. In this case
  resetting the command target to measured position increases position-control
  torque instead of removing it. The previously observed torque drop is therefore
  not a sufficient explanation of the tracking failure.

The 2.033 degree difference between the settled positions is evidence of
approach-dependent behavior, consistent with friction/hysteresis. It does not
isolate friction from every load-model or drive-level effect. At this posture,
1 mm is equivalent to about 0.10 degree of isolated elbow error.

The measured-position handoff remains unchanged. Blindly preserving the previous
position error as a fixed torque offset is not justified by these direction-dependent
results. Further control characterization is needed before selecting compensation;
loosening a guard alone would not improve actual tracking. No i2rt implementation
bug has been established.

Local evidence: `outputs/endpoint-hardware/`, including per-action trajectories,
feedback and camera references. Video: `outputs/bent-posture-recordings/20260911-134426-19180083/rollout.mp4`.

Both arms completed the staged return. The recording was finished and reviewed,
then two fresh healthy stationary neutral readings were checked 200 ms apart.
Final joint angles in degrees were left [-0.426, 0.688, 0.667, -1.279, 0.382, -0.645]
and right [0.339, 0.273, 0.535, -1.912, 0.710, 0.776]. Both sessions were released;
final status contained no connected arms or faults. External power was not switched off.
