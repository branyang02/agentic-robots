//! Explicit device adapters; both use Linux V4L2 for RGB capture.
use crate::frame::{CameraType, Config};
use anyhow::{Result, ensure};

pub fn open(c: &Config) -> Result<v4l::Device> {
    match c.camera_type {
        CameraType::Usb => usb(&c.device),
        CameraType::Realsense => realsense_rgb(&c.device),
    }
}

fn usb(path: &str) -> Result<v4l::Device> {
    let device = v4l::Device::with_path(path)?;
    let caps = device.query_caps()?;
    ensure!(
        caps.bus.starts_with("usb"),
        "expected a USB video device, found {}",
        caps.bus
    );
    Ok(device)
}

fn realsense_rgb(path: &str) -> Result<v4l::Device> {
    let device = usb(path)?;
    validate_realsense(&device.query_caps()?.card)?;
    // Negotiating YUYV below rejects depth, IR, and metadata nodes. No librealsense
    // process competes for this RGB interface, and no depth stream is enabled.
    Ok(device)
}

fn validate_realsense(card: &str) -> Result<()> {
    ensure!(
        card.to_ascii_lowercase().contains("realsense"),
        "type=realsense requires a RealSense RGB device; found {card}"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn realsense_identity_is_checked_without_opening_hardware() {
        assert!(validate_realsense("Intel(R) RealSense(TM) Depth Camera RGB").is_ok());
        assert!(validate_realsense("USB global shutter camera").is_err());
    }
}
