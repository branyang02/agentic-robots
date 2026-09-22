use crate::{
    frame::{Config, Frame, Pixels},
    record::Recorder,
};
use anyhow::{Context, Result, ensure};
use std::{
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
use v4l::{
    buffer::{Flags, Type},
    io::traits::CaptureStream,
    video::Capture,
};

#[derive(Default)]
pub struct State {
    pub latest: Option<Frame>,
    pub generation: u64,
    pub capture_error: Option<String>,
    pub recording_error: Option<String>,
    pub stopped: bool,
}
pub type Shared = Arc<(Mutex<State>, Condvar)>;

pub fn run(c: Config, shared: Shared, stop: Arc<AtomicBool>) {
    let result = if c.format == "synthetic" {
        synthetic(&c, &shared, &stop)
    } else {
        hardware(&c, &shared, &stop)
    };
    let mut state = shared.0.lock().unwrap();
    if let Err(error) = result {
        state.capture_error = Some(format!("{error:#}"));
    }
    state.stopped = true;
    shared.1.notify_all();
}

fn publish(frame: Frame, writer: &Recorder, shared: &Shared) {
    {
        let mut s = shared.0.lock().unwrap();
        s.generation += 1;
        s.latest = Some(frame.clone());
        shared.1.notify_all();
    }
    let can_record = shared.0.lock().unwrap().recording_error.is_none();
    if can_record && let Err(error) = writer.push(&frame) {
        shared.0.lock().unwrap().recording_error = Some(format!("{error:#}"));
    }
}

fn finish(writer: &Recorder, shared: &Shared) {
    if let Err(error) = writer.finish() {
        shared.0.lock().unwrap().recording_error = Some(format!("{error:#}"));
    }
}

fn synthetic(c: &Config, shared: &Shared, stop: &AtomicBool) -> Result<()> {
    let writer = Recorder::new(c, Pixels::Rgb)?;
    let test = c.test.as_ref().unwrap();
    let mut sequence: u64 = 0;
    let mut next = Instant::now();
    let mut disconnected = false;
    while !stop.load(Ordering::Relaxed) {
        if test.fail_after_frames.is_some_and(|n| sequence >= n) {
            disconnected = true;
            break;
        }
        if test.stall_after_frames.is_some_and(|n| sequence >= n) {
            thread::sleep(Duration::from_millis(10));
            continue;
        }
        let now = Instant::now();
        if now < next {
            thread::sleep((next - now).min(Duration::from_millis(10)));
            continue;
        }
        let mut data = test.color.repeat(c.width as usize * c.height as usize);
        // Binary frame identifier in the first pixels makes content independently testable.
        for (i, b) in sequence.to_le_bytes().iter().enumerate() {
            if i * 3 < data.len() {
                data[i * 3] = *b;
            }
        }
        publish(
            Frame {
                data: Arc::from(data),
                pixels: Pixels::Rgb,
                width: c.width,
                height: c.height,
            },
            &writer,
            shared,
        );
        sequence += 1;
        // Avoid a catch-up burst after scheduler delays.
        next = Instant::now() + Duration::from_nanos(c.period_ns());
    }
    finish(&writer, shared);
    ensure!(!disconnected, "synthetic source disconnected");
    Ok(())
}

fn hardware(c: &Config, shared: &Shared, stop: &AtomicBool) -> Result<()> {
    let device = crate::source::open(c).context("open camera")?;
    let fourcc = v4l::FourCC::new(if c.format == "mjpeg" {
        b"MJPG"
    } else {
        b"YUYV"
    });
    let format = device
        .set_format(&v4l::Format::new(c.width, c.height, fourcc))
        .context("set camera format")?;
    ensure!(
        format.width == c.width && format.height == c.height && format.fourcc == fourcc,
        "camera substituted an unsupported resolution/format: {format}"
    );
    let parameters = device
        .set_params(&v4l::video::capture::Parameters::new(v4l::Fraction::new(
            1000,
            (c.fps * 1000.0).round() as u32,
        )))
        .context("set camera FPS")?;
    let actual_fps = parameters.interval.denominator as f64 / parameters.interval.numerator as f64;
    ensure!(
        (actual_fps / c.fps - 1.0).abs() < 0.001,
        "camera substituted FPS: {actual_fps}"
    );
    let pixels = if c.format == "mjpeg" {
        Pixels::Jpeg
    } else {
        let rec709 = matches!(format.colorspace, v4l::format::Colorspace::Rec709);
        ensure!(
            matches!(
                format.colorspace,
                v4l::format::Colorspace::Default
                    | v4l::format::Colorspace::SMPTE170M
                    | v4l::format::Colorspace::Rec709
                    | v4l::format::Colorspace::SRGB
                    | v4l::format::Colorspace::JPEG
            ),
            "unsupported YUYV colorspace: {}",
            format.colorspace
        );
        let full_range = matches!(format.quantization, v4l::format::Quantization::FullRange)
            || (matches!(format.quantization, v4l::format::Quantization::Default)
                && matches!(format.colorspace, v4l::format::Colorspace::JPEG));
        ensure!(!rec709 || !full_range, "full-range Rec709 is not supported");
        Pixels::Yuyv { rec709, full_range }
    };
    let mut stream = v4l::io::mmap::Stream::with_buffers(&device, Type::VideoCapture, 4)
        .context("allocate camera buffers")?;
    // Allow cold-start latency. After the first frame, poll outside next(): v4l
    // 0.14 next() queues the previous buffer, so retrying it after a dequeue
    // timeout would queue an already queued buffer and fail with EINVAL.
    stream.set_timeout(Duration::from_secs(2));
    let writer = Recorder::new(c, pixels)?;
    // Stabilize USB clocks/auto-exposure before admitting frames to a rollout.
    // Python does not start the task until all cameras report ready.
    let warmup_until = Instant::now() + Duration::from_secs(3);
    let mut streaming = false;
    let result = (|| -> Result<()> {
        while !stop.load(Ordering::Relaxed) {
            if streaming && device.handle().poll(libc::POLLIN, 100)? == 0 {
                continue;
            }
            let (bytes, meta) = stream.next().context("dequeue camera buffer")?;
            streaming = true;
            let used = meta.bytesused as usize;
            if Instant::now() < warmup_until
                || meta.flags.contains(Flags::ERROR)
                || used == 0
                || used > bytes.len()
            {
                continue;
            }
            let data = if matches!(pixels, Pixels::Yuyv { .. }) {
                let row = c.width as usize * 2;
                let stride = (format.stride as usize).max(row);
                if used < stride * (c.height as usize - 1) + row {
                    continue;
                }
                bytes[..used]
                    .chunks(stride)
                    .take(c.height as usize)
                    .flat_map(|r| r[..row].iter().copied())
                    .collect::<Vec<_>>()
            } else {
                bytes[..used].to_vec()
            };
            publish(
                Frame {
                    data: Arc::from(data),
                    pixels,
                    width: c.width,
                    height: c.height,
                },
                &writer,
                shared,
            );
        }
        Ok(())
    })();
    finish(&writer, shared);
    result
}
