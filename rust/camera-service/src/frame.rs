use anyhow::{Result, bail, ensure};
use image::{ExtendedColorType, ImageEncoder, ImageFormat, codecs::png};
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Arc};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(rename = "type")]
    pub camera_type: CameraType,
    pub device: String,
    pub format: String,
    pub width: u32,
    pub height: u32,
    pub fps: f64,
    pub output: PathBuf,
    #[serde(default)]
    pub test: Option<TestSource>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum CameraType {
    Usb,
    Realsense,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct TestSource {
    pub color: [u8; 3],
    pub stall_after_frames: Option<u64>,
    pub fail_after_frames: Option<u64>,
    pub encoder_delay_ms: u32,
}

impl Config {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.width > 0 && self.height > 0 && self.width <= 8192 && self.height <= 8192,
            "invalid image dimensions"
        );
        ensure!(
            self.width.is_multiple_of(2) && self.height.is_multiple_of(2),
            "H.264 recording requires even image dimensions"
        );
        ensure!(
            self.fps.is_finite() && self.fps > 0.0 && self.fps <= 120.0,
            "invalid FPS"
        );
        ensure!(self.output.is_absolute(), "output must be an absolute path");
        ensure!(!self.output.exists(), "refusing to overwrite recording");
        match self.format.as_str() {
            "mjpeg" | "yuyv422" => {
                ensure!(
                    self.camera_type != CameraType::Realsense || self.format == "yuyv422",
                    "RealSense RGB requires yuyv422"
                );
                ensure!(self.test.is_none(), "test controls require synthetic input");
                ensure!(
                    std::env::var_os("ROBOT_CAMERA_TEST_ONLY").is_none(),
                    "hardware camera access is forbidden in tests"
                );
            }
            "synthetic" => ensure!(self.test.is_some(), "synthetic settings required"),
            _ => bail!("unsupported camera format"),
        }
        Ok(())
    }

    pub fn period_ns(&self) -> u64 {
        (1e9 / self.fps).round() as u64
    }
}

#[derive(Clone, Copy, Debug)]
pub enum Pixels {
    Jpeg,
    Yuyv { rec709: bool, full_range: bool },
    Rgb,
}

#[derive(Clone)]
pub struct Frame {
    pub data: Arc<[u8]>,
    pub pixels: Pixels,
    pub width: u32,
    pub height: u32,
}

impl Frame {
    pub fn png(&self) -> Result<Vec<u8>> {
        let rgb = match self.pixels {
            Pixels::Jpeg => {
                let decoded = image::load_from_memory_with_format(&self.data, ImageFormat::Jpeg)?;
                ensure!(
                    decoded.width() == self.width && decoded.height() == self.height,
                    "JPEG dimensions disagree with negotiated format"
                );
                decoded.to_rgb8().into_raw()
            }
            Pixels::Rgb => {
                ensure!(
                    self.data.len() == self.width as usize * self.height as usize * 3,
                    "incomplete RGB frame"
                );
                self.data.to_vec()
            }
            Pixels::Yuyv { rec709, full_range } => {
                ensure!(
                    self.data.len() == self.width as usize * self.height as usize * 2,
                    "incomplete YUYV frame"
                );
                yuyv_rgb(&self.data, rec709, full_range)
            }
        };
        let mut bytes = Vec::new();
        png::PngEncoder::new_with_quality(
            &mut bytes,
            png::CompressionType::Fast,
            png::FilterType::Sub,
        )
        .write_image(&rgb, self.width, self.height, ExtendedColorType::Rgb8)?;
        Ok(bytes)
    }
}

fn yuyv_rgb(bytes: &[u8], rec709: bool, full: bool) -> Vec<u8> {
    let (kr, kb) = if rec709 {
        (0.2126_f32, 0.0722_f32)
    } else {
        (0.299, 0.114)
    };
    let kg = 1.0 - kr - kb;
    let mut rgb = Vec::with_capacity(bytes.len() / 2 * 3);
    for p in bytes.as_chunks::<4>().0 {
        let u = (p[1] as f32 - 128.0) * if full { 1.0 } else { 255.0 / 224.0 };
        let v = (p[3] as f32 - 128.0) * if full { 1.0 } else { 255.0 / 224.0 };
        for yy in [p[0], p[2]] {
            let y = if full {
                yy as f32
            } else {
                (yy as f32 - 16.0) * 255.0 / 219.0
            };
            for c in [
                y + 2.0 * (1.0 - kr) * v,
                y - 2.0 * kb * (1.0 - kb) / kg * u - 2.0 * kr * (1.0 - kr) / kg * v,
                y + 2.0 * (1.0 - kb) * u,
            ] {
                rgb.push(c.round().clamp(0.0, 255.0) as u8);
            }
        }
    }
    rgb
}

#[cfg(test)]
mod tests {
    use super::*;
    fn frame() -> Frame {
        Frame {
            data: Arc::from([255, 0, 0, 0, 255, 0]),
            pixels: Pixels::Rgb,
            width: 2,
            height: 1,
        }
    }
    #[test]
    fn png_preserves_dimensions_and_pixels() {
        let decoded = image::load_from_memory(&frame().png().unwrap())
            .unwrap()
            .to_rgb8();
        assert_eq!(decoded.dimensions(), (2, 1));
        assert_eq!(decoded.get_pixel(0, 0).0, [255, 0, 0]);
        assert_eq!(decoded.get_pixel(1, 0).0, [0, 255, 0]);
    }
    #[test]
    fn limited_and_full_range_yuyv() {
        assert_eq!(
            yuyv_rgb(&[16, 128, 235, 128], false, false),
            [0, 0, 0, 255, 255, 255]
        );
        assert_eq!(
            yuyv_rgb(&[0, 128, 255, 128], true, true),
            [0, 0, 0, 255, 255, 255]
        );
        let red = yuyv_rgb(&[81, 90, 81, 240], false, false);
        assert!(red[0] > 250 && red[1] < 3 && red[2] < 3);
    }
    #[test]
    fn bad_frame_size_is_rejected() {
        let mut f = frame();
        f.width = 4;
        assert!(f.png().is_err());
    }
}
