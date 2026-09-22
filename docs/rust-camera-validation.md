# Rust camera service validation — 2026-09-21

Rust is the only rollout camera backend. `robot-record` starts its camera workers
without a backend-selection flag. The FFmpeg rollout pipeline, preview-file clock
checks, and live composite/remux path have been removed. Standalone setup/viewing
tools and saved-media inspection still use FFmpeg.

Raw reports and media referenced below are local validation artifacts, not files
included in the repository.

## Software verification

- **276 Python tests passed; one optional model-agent test skipped.** The full suite
  now uses Rust workers for all recording integration/E2E tests. Legacy backend
  selector and preview-publication tests were removed or replaced with worker tests.
- **Six Rust unit tests passed**, covering image dimensions/conversion,
  JPEG/YUYV/RGB colors in PNGs and video, RealSense identity validation, malformed
  frames, and actual disk-full errors.
- Rust formatting, type/borrow checking, Clippy, Python Ruff lint/format checks,
  and `git diff --check` passed.

Worker and HTTP/CLI/MCP integration checks cover:

- New synthetic content on successive requests, immutable image files, and no
  continuous PNG output. A stalled source errors instead of returning cached data.
- Deliberately slowed encoding skips frames while observations, task completion,
  and playable recordings still succeed.
- Worker crashes/disconnections and partial startup failures are contained and
  cleaned up; healthy cameras continue supplying images.
- Images are requested after a deliberately slow robot-feedback check.
- Concurrent simulated actions return three native MCP images and measured
  feedback. Concurrent observations, finish/review, and repeated rollouts work.
- Camera and logging failures preserve action outcomes and controller access.
  Recorder shutdown leaves the independent simulated motor controller running.

Automated tests use synthetic cameras and simulated arms; test HTTP processes
cannot open CAN sockets. No model-driven evaluation was launched.

## Manual check of the default command

The default `robot-record` command passed end-to-end validation with three physical
cameras and **simulated arms** whose processes cannot open CAN sockets:

| Camera | Configuration |
|---|---|
| Left USB wrist | 1920×1200, 5 FPS |
| Top RealSense RGB | 1920×1080, 8 FPS |
| Right USB wrist | 1920×1200, 5 FPS |

Two complete rollouts produced **six playable MP4s**, decoded without errors at
the configured dimensions. All observations succeeded. Eight concurrent-pair
action responses included three full-resolution native MCP images and measured
simulated-arm feedback. The main recording lasted about 24 seconds, followed by
a second short rollout. The actual motor controller was not restarted or used for
motion; temporary camera workers exited after validation.

Report and repeatable manual harness:

- `outputs/rust-only-validation/e2e-high-20260921-180429/report.json`
- `outputs/rust-only-validation/whole_system_e2e.py`
- `outputs/rust-only-validation/python-tests.log`

Before removing the backend selector, the same native workers also passed original
30 FPS modes, high-resolution modes, and swapped-camera-role checks: six rollouts
produced 18 playable videos. Those reports remain under
`outputs/camera-refactor-validation/`.

## Scope and limitations

New-frame delivery is tested with changing synthetic pixel content, including
post-feedback requests and stalled sources. There is **no measured exposure-age
bound**: device/driver buffering can add latency. No shared camera clock or exact
video/event alignment is provided. Playback timing remains internal to encoding.

No physical arm movement or model-driven evaluation is claimed for this update.
These results verify recording, observations, and integration; they do not validate
physical dynamics. See [requirements](camera-service-plan.md) and
[operation](rust-cameras.md).
