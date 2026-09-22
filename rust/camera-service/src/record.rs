use crate::frame::{Config, Frame, Pixels};
use anyhow::{Context, Result, bail, ensure};
use gstreamer::{self as gst, prelude::*};
use gstreamer_app as app;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

pub struct Recorder {
    pipeline: gst::Pipeline,
    source: app::AppSrc,
    has_video: Arc<AtomicBool>,
    started: std::time::Instant,
    period: u64,
}

impl Recorder {
    pub fn new(c: &Config, pixels: Pixels) -> Result<Self> {
        gst::init()?;
        let decode = if matches!(pixels, Pixels::Jpeg) {
            "jpegdec ! "
        } else {
            ""
        };
        // Appsrc alone is the bounded recording queue. Its push is non-blocking;
        // full queues skip video frames without delaying capture or snapshots.
        let pipeline = gst::parse::launch(&format!(
            "appsrc name=input is-live=true format=time block=false max-buffers=8 max-bytes=0 \
             ! identity name=delay ! {decode}videoconvert ! video/x-raw,format=I420 \
             ! x264enc name=encoder speed-preset=ultrafast tune=zerolatency threads=2 \
                pass=qual quantizer=18 key-int-max=30 \
             ! video/x-h264,stream-format=avc ! mp4mux fragment-duration=1000 \
             ! filesink name=file sync=false"
        ))?
        .downcast::<gst::Pipeline>()
        .map_err(|_| anyhow::anyhow!("not a pipeline"))?;
        pipeline
            .by_name("file")
            .unwrap()
            .set_property("location", c.output.to_str().context("non-UTF8 output")?);
        if let Some(test) = &c.test {
            pipeline
                .by_name("delay")
                .unwrap()
                .set_property("sleep-time", test.encoder_delay_ms * 1000);
        }
        let source = pipeline
            .by_name("input")
            .unwrap()
            .downcast::<app::AppSrc>()
            .unwrap();
        let builder = match pixels {
            Pixels::Jpeg => gst::Caps::builder("image/jpeg"),
            Pixels::Rgb => gst::Caps::builder("video/x-raw").field("format", "RGB"),
            Pixels::Yuyv { rec709, full_range } => gst::Caps::builder("video/x-raw")
                .field("format", "YUY2")
                .field(
                    "colorimetry",
                    if full_range {
                        "jpeg"
                    } else if rec709 {
                        "bt709"
                    } else {
                        "bt601"
                    },
                ),
        };
        source.set_caps(Some(
            &builder
                .field("width", c.width as i32)
                .field("height", c.height as i32)
                .field(
                    "framerate",
                    gst::Fraction::new((c.fps * 1000.0).round() as i32, 1000),
                )
                .build(),
        ));
        let has_video = Arc::new(AtomicBool::new(false));
        let written = has_video.clone();
        pipeline
            .by_name("encoder")
            .unwrap()
            .static_pad("src")
            .unwrap()
            .add_probe(gst::PadProbeType::BUFFER, move |_, _| {
                written.store(true, Ordering::Relaxed);
                gst::PadProbeReturn::Ok
            });
        pipeline.set_state(gst::State::Playing)?;
        Ok(Self {
            pipeline,
            source,
            has_video,
            started: std::time::Instant::now(),
            period: c.period_ns(),
        })
    }

    pub fn push(&self, frame: &Frame) -> Result<()> {
        self.check_error()?;
        if self.source.current_level_buffers() >= 8 {
            return Ok(());
        }
        // Playback timing is local to this encoder; observations carry no timestamps.
        let pts = self.started.elapsed().as_nanos() as u64;
        let mut buffer = gst::Buffer::from_slice(frame.data.clone());
        {
            let b = buffer.get_mut().unwrap();
            b.set_pts(gst::ClockTime::from_nseconds(pts));
            b.set_duration(gst::ClockTime::from_nseconds(self.period));
        }
        self.source.push_buffer(buffer)?;
        Ok(())
    }

    fn check_error(&self) -> Result<()> {
        while let Some(msg) = self.pipeline.bus().unwrap().pop() {
            if let gst::MessageView::Error(e) = msg.view() {
                bail!("recording: {} ({:?})", e.error(), e.debug());
            }
        }
        Ok(())
    }

    pub fn finish(&self) -> Result<()> {
        self.check_error()?;
        self.source.end_of_stream()?;
        let bus = self.pipeline.bus().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while std::time::Instant::now() < deadline {
            if let Some(msg) = bus.timed_pop(gst::ClockTime::from_mseconds(100)) {
                match msg.view() {
                    gst::MessageView::Eos(_) => {
                        ensure!(
                            self.has_video.load(Ordering::Relaxed),
                            "no video frames recorded"
                        );
                        return Ok(());
                    }
                    gst::MessageView::Error(e) => bail!("recording finalization: {}", e.error()),
                    _ => (),
                }
            }
        }
        bail!("recording finalization timed out")
    }
}

impl Drop for Recorder {
    fn drop(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Null);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::TestSource;
    use std::{path::PathBuf, thread, time::Duration};

    #[test]
    fn disk_full_is_reported_as_an_encoder_error() {
        let config = Config {
            camera_type: crate::frame::CameraType::Usb,
            device: "synthetic".into(),
            format: "synthetic".into(),
            width: 32,
            height: 32,
            fps: 30.0,
            output: PathBuf::from("/dev/full"),
            test: Some(TestSource::default()),
        };
        // Bypass CLI overwrite checks only inside this test to exercise a real ENOSPC sink.
        let recorder = Recorder::new(&config, Pixels::Rgb).unwrap();
        let frame = Frame {
            data: Arc::from(vec![255; 32 * 32 * 3]),
            pixels: Pixels::Rgb,
            width: 32,
            height: 32,
        };
        let mut failed = false;
        for _ in 0..100 {
            if recorder.push(&frame).is_err() {
                failed = true;
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            failed || recorder.finish().is_err(),
            "disk full must not finalize successfully"
        );
    }

    #[test]
    fn jpeg_yuyv_and_rgb_keep_matching_colors_in_png_and_video() {
        let directory = std::env::temp_dir().join(format!(
            "camera-colors-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let rgb = [255_u8, 0, 0].repeat(32 * 32);
        let mut jpeg = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, 95)
            .encode(&rgb, 32, 32, image::ExtendedColorType::Rgb8)
            .unwrap();
        for (name, pixels, data) in [
            ("rgb", Pixels::Rgb, rgb),
            ("jpeg", Pixels::Jpeg, jpeg),
            (
                "yuyv",
                Pixels::Yuyv {
                    rec709: false,
                    full_range: false,
                },
                [81, 90, 81, 240].repeat(32 * 16),
            ),
        ] {
            let config = Config {
                camera_type: crate::frame::CameraType::Usb,
                device: "synthetic".into(),
                format: "synthetic".into(),
                width: 32,
                height: 32,
                fps: 30.0,
                output: directory.join(format!("{name}.mp4")),
                test: Some(TestSource::default()),
            };
            let recorder = Recorder::new(&config, pixels).unwrap();
            let frame = Frame {
                data: Arc::from(data),
                pixels,
                width: 32,
                height: 32,
            };
            recorder.push(&frame).unwrap();
            recorder.finish().unwrap();
            let png = image::load_from_memory(&frame.png().unwrap())
                .unwrap()
                .to_rgb8();
            let decoded = std::process::Command::new("ffmpeg")
                .args(["-v", "error", "-i"])
                .arg(&config.output)
                .args(["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                .output()
                .unwrap();
            assert!(
                decoded.status.success(),
                "{}",
                String::from_utf8_lossy(&decoded.stderr)
            );
            assert_eq!(decoded.stdout.len(), 32 * 32 * 3);
            for pixel in [png.get_pixel(0, 0).0.as_slice(), &decoded.stdout[..3]] {
                assert!(
                    pixel[0] >= 245 && pixel[1] <= 6 && pixel[2] <= 6,
                    "{name}: {pixel:?}"
                );
            }
        }
        std::fs::remove_dir_all(directory).unwrap();
    }
}
