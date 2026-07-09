//! Per-connection writer task: serializes responses, applies signing, and
//! frames the bytes onto the wire.

use crate::proto::framing::encode_frame;
use tokio::io::{AsyncWriteExt, WriteHalf};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tracing::{debug, error};

/// One packet of bytes to send. Already includes the final SMB2 header +
/// body, *with signing already applied if required*.
pub type FramePayload = Vec<u8>;

/// Writer-task channel size: large enough that a slow remote rarely backs up
/// the dispatcher.
pub const WRITER_CHANNEL: usize = 64;

pub async fn writer_task(mut writer: WriteHalf<TcpStream>, mut rx: mpsc::Receiver<FramePayload>) {
    while let Some(payload) = rx.recv().await {
        let mut out = Vec::with_capacity(payload.len() + 4);
        encode_frame(&payload, &mut out);
        if let Err(e) = writer.write_all(&out).await {
            error!(error = %e, "writer task: socket write failed");
            return;
        }
        debug!(len = out.len(), "wrote frame");
    }
    // Channel closed — flush and bail.
    if let Err(e) = writer.shutdown().await {
        debug!(error = %e, "writer shutdown error (best-effort)");
    }
}
