# Simplified camera service validation — 2026-09-21

Validation was run from `codex/rust-camera-service` in `agentic-robots-rust-cameras`.
Rust remains opt-in with `--camera-backend rust`; the FFmpeg backend is retained
for rollback. Raw reports and media referenced below are local validation artifacts,
not files included in the repository.

## Contract being tested

Record three MP4s and return newly delivered frames on request. Skip damaged
buffers or video frames when encoding falls behind. These gaps do not fail the
rollout. Return image paths without capture timestamps or frame/drop statistics.
A stalled image request times out; real device, worker, and file errors remain
visible. See [requirements](camera-service-plan.md) and [operation](rust-cameras.md).

## Software verification

Rust formatting, type/borrow checking, Clippy, and six unit tests passed. They
cover image dimensions/conversion, JPEG/YUYV/RGB colors in PNGs and video,
RealSense identity validation, malformed frames, and actual disk-full errors.

Python Ruff lint/format checks passed. The complete suite reported **281 passed,
1 skipped**. The skip is the separately opt-in Codex agent workflow; no model-driven
evaluation was launched. Worker and HTTP/CLI/MCP integration checks cover:

- New synthetic content on successive requests, immutable observation files,
  and no continuous PNG output.
- A stalled source returns an error instead of the cached image.
- Deliberately slowed encoding skips frames while images, task completion, and
  playable recordings still succeed.
- Worker crashes/disconnections and partial startup failures are contained and
  cleaned up; healthy cameras continue supplying images.
- Images are requested after a deliberately slow robot-feedback check.
- Concurrent simulated actions return three native MCP images and measured
  feedback. Concurrent observations, finish/review, and repeated rollouts work.

Automated tests use synthetic cameras and simulated arms; test HTTP processes
cannot open CAN sockets. Full-suite results and manual checks are recorded below.

## Manual camera verification

Artifacts and the repeatable manual harness are in
`outputs/camera-refactor-validation/`. The harness runs real cameras through the
Python recorder, Rust workers, CLI, and native MCP with **simulated arms** whose
processes cannot open CAN sockets. It decodes every MP4 and verifies image/video
dimensions. The actual motor controller is not restarted or used for motion.

The original profile passed two complete rollouts: both USB wrists at
1280×720/30 FPS and RealSense RGB at 640×480/30 FPS. All six videos decoded without
errors and contained the configured image dimensions. Returned camera images
were also visually inspected.

The high-resolution profile also passed two rollouts: USB wrists at
1920×1200/5 FPS and RealSense RGB at 1920×1080/8 FPS. The longer recording
contains about 94 seconds per camera, followed by a second short rollout. All
six videos decoded without errors, every requested image returned, and native
MCP image blocks retained the configured dimensions. High-resolution images
from all three views were visually inspected.

The final build also passed two high-resolution rollouts with RealSense assigned
as `left` and a USB camera assigned as `top`, confirming camera type is independent
of view. All six videos decoded correctly. Across the three configurations,
**six rollouts produced 18 playable videos**.

A final four-test integration repeat passed after removing the inherited FFmpeg
clock description from Rust manifests. Formatting/lint checks and `git diff --check`
also passed.

Exact manual reports:

- `outputs/camera-refactor-validation/e2e-original-20260921-174719/report.json`
- `outputs/camera-refactor-validation/e2e-high-20260921-174828/report.json`
- `outputs/camera-refactor-validation/e2e-swap-20260921-175116/report.json`

## Scope and limitations

New-frame delivery is tested with changing synthetic pixel content, including
post-feedback requests and stalled sources. There is **no measured exposure-age
bound**: device/driver buffering can add latency. No shared camera clock or exact
video/event alignment is provided. Playback timing remains internal to encoding.

No new physical arm movement or model-driven evaluation is claimed for this
refactor. Earlier implementation checks included real arm movement, a 30-minute
stationary recording, and camera-role replacement; their original reports remain
in `outputs/rust-camera-validation/`. Those used the previous timestamp/lossless
contract and do not establish optical latency for this implementation. Both real
arm sessions were released before this refactor's camera checks.
