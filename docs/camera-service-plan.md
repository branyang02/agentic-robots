# Camera service requirements

The service has two jobs: record the rollout and supply images on request.

1. **Three videos:** record `left.mp4`, `top.mp4`, and `right.mp4` continuously
   during each rollout, at each camera's configured resolution and frame rate.
   Finalize playable files on finish. Dropped or damaged frames are acceptable;
   skip them without failing the rollout or tracking/reporting drop counts.
2. **Images on request:** each Rust worker waits for the next valid frame delivered
   after its snapshot request, encodes one full-resolution PNG, and returns its
   path. Save these immutable images for the run history. Do not export PNGs
   continuously. Post-action requests use the same mechanism after execution.
3. **Simple failure handling:** a snapshot has a two-second deadline. Return a
   concise error if no new frame arrives or image creation fails; never silently
   return the cached frame. Camera disconnection, worker crashes, and actual
   encoder/file errors remain visible. One failed worker must not stop the others.
4. **Minimal interface:** no capture timestamps, age/skew calculations, frame IDs,
   frame/drop counters, or lossless-recording checks. Video playback timing remains
   internal. A newly delivered frame is not a guarantee of exposure after the
   request: device/driver buffering is not measured or bounded by this service.

One process owns each camera, keeps a latest-frame slot in RAM, and feeds a bounded
recording queue. Snapshot encoding and video encoding run independently of capture.
Configure each view with JSON containing `type`, `path`, `width`, `height`, `fps`,
and optionally `format`. Separate USB and RealSense RGB adapters share the worker
interface; either type can serve any view. Python retains the robot tools and task
lifecycle. No motion logic or video composition belongs in this module.

## Verification

- **Unit:** configuration, image conversion, malformed frames, encoder/file errors.
- **Worker integration:** successive images contain new synthetic content; a
  stalled camera times out; a slow encoder skips video frames while images and
  completion still succeed; disconnects/crashes are isolated; files remain playable.
- **Whole-system E2E:** CLI and native MCP, three image blocks after actions,
  measured feedback, concurrent requests/actions, finish/review, and repeated
  rollouts. Use synthetic cameras and CAN-forbidden simulated arms in tests.
- **Manual real-camera check:** repeat the whole-system flow with all three physical
  cameras at original and high-resolution modes. Inspect images, decode all three
  videos, and check dimensions and usable duration. Report this separately from
  software tests; simulated arms do not validate physical dynamics.

Keep Rust opt-in until migration is approved. No optical-latency or lossless-video
acceptance threshold is required by this contract.
