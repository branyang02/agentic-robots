mod capture;
mod frame;
mod record;
mod source;

use anyhow::{Context, Result, bail, ensure};
use capture::{Shared, State};
use frame::Config;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, BufRead, Write},
    path::PathBuf,
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    id: u64,
    operation: String,
    path: Option<PathBuf>,
}

fn status(shared: &Shared) -> Value {
    let state = shared.0.lock().unwrap();
    json!({
        "ready": !state.stopped && state.capture_error.is_none() && state.latest.is_some(),
        "capture_error": state.capture_error,
        "recording_error": state.recording_error,
        "stopped": state.stopped,
    })
}

fn snapshot(shared: &Shared, request: &Request) -> Result<Value> {
    let path = request.path.as_ref().context("snapshot path required")?;
    ensure!(
        path.is_absolute() && !path.exists(),
        "snapshot needs a new absolute path"
    );
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut s = shared.0.lock().unwrap();
    let generation = s.generation;
    let frame = loop {
        if let Some(error) = &s.capture_error {
            bail!("capture failed: {error}");
        }
        ensure!(!s.stopped, "capture is stopped");
        if let Some(f) = &s.latest
            && s.generation > generation
        {
            break f.clone();
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        ensure!(
            !remaining.is_zero(),
            "timed out waiting for a new camera frame"
        );
        s = shared.1.wait_timeout(s, remaining).unwrap().0;
    };
    drop(s); // Never hold capture's lock during encoding or disk I/O.
    let png = frame.png()?;
    ensure!(
        Instant::now() < deadline,
        "snapshot timed out while encoding"
    );
    let temporary = path.with_extension(format!("{}.tmp", request.id));
    let result = (|| -> Result<Value> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&png)?;
        drop(file);
        ensure!(
            Instant::now() < deadline,
            "snapshot timed out while writing"
        );
        fs::rename(&temporary, path)?;
        Ok(json!({"path": path}))
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn serve() -> Result<()> {
    let path = std::env::args()
        .nth(1)
        .context("usage: robot-camera-service CONFIG.json")?;
    let config: Config = serde_json::from_slice(&fs::read(path)?)?;
    config.validate()?;
    let shared = Arc::new((Mutex::new(State::default()), Condvar::new()));
    let stop = Arc::new(AtomicBool::new(false));
    let worker_config = config.clone();
    let worker_shared = shared.clone();
    let worker_stop = stop.clone();
    let mut worker = Some(thread::spawn(move || {
        capture::run(worker_config, worker_shared, worker_stop)
    }));
    let mut out = io::stdout().lock();
    writeln!(
        out,
        "{}",
        json!({"protocol_version": 1, "pid": std::process::id()})
    )?;
    out.flush()?;
    let result = (|| -> Result<()> {
        for line in io::stdin().lock().lines() {
            let line = line?;
            let decoded = serde_json::from_str::<Request>(&line);
            let id = decoded.as_ref().map(|r| r.id).ok();
            let stopping = decoded.as_ref().is_ok_and(|r| r.operation == "stop");
            let result =
                decoded
                    .map_err(anyhow::Error::from)
                    .and_then(|r| match r.operation.as_str() {
                        "status" => Ok(status(&shared)),
                        "snapshot" => snapshot(&shared, &r),
                        "stop" => {
                            stop.store(true, Ordering::Relaxed);
                            worker
                                .take()
                                .unwrap()
                                .join()
                                .map_err(|_| anyhow::anyhow!("capture thread panicked"))?;
                            Ok(status(&shared))
                        }
                        _ => bail!("unknown operation"),
                    });
            let response = match result {
                Ok(value) => json!({"id": id, "ok": true, "result": value}),
                Err(error) => json!({"id": id, "ok": false, "error": format!("{error:#}")}),
            };
            writeln!(out, "{response}")?;
            out.flush()?;
            if stopping {
                break;
            }
        }
        Ok(())
    })();
    // Parent EOF/disconnect also finalizes files. No motor devices are owned here.
    stop.store(true, Ordering::Relaxed);
    if let Some(worker) = worker {
        let _ = worker.join();
    }
    result
}

fn main() {
    if let Err(error) = serve() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}
