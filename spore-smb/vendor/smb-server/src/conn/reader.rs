//! Per-connection frame reader: pulls bytes off the socket, frames them,
//! hands each frame to the dispatcher.

use std::io;
use std::sync::Arc;

use crate::proto::framing::{FRAME_HEADER_LEN, decode_frame_header};
use tokio::io::{AsyncReadExt, ReadHalf};
use tokio::net::TcpStream;
use tracing::{debug, error};

use crate::conn::state::Connection;
use crate::server::ServerState;

/// Read one frame's payload (without the 4-byte length prefix).
///
/// Returns `Ok(None)` on a clean EOF, `Ok(Some(bytes))` on a complete frame,
/// `Err` on partial/garbled data.
pub async fn read_one_frame(reader: &mut ReadHalf<TcpStream>) -> io::Result<Option<Vec<u8>>> {
    let mut hdr = [0u8; FRAME_HEADER_LEN];
    match reader.read_exact(&mut hdr).await {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    let len = match decode_frame_header(&hdr) {
        Ok(n) => n,
        Err(e) => {
            return Err(io::Error::new(io::ErrorKind::InvalidData, e.to_string()));
        }
    };
    let mut payload = vec![0u8; len as usize];
    reader.read_exact(&mut payload).await?;
    Ok(Some(payload))
}

/// Continuously read frames; for each, await `dispatch_one`'s response and
/// route it to the writer.
///
/// Sequential dispatch keeps v1 simple and matches the spec's "single writer
/// task / per-frame dispatch" pattern. We process one frame at a time per
/// connection in v1 — a follow-up can spawn dispatch tasks if a workload
/// proves to need credit-window concurrency.
pub async fn reader_task(
    mut reader: ReadHalf<TcpStream>,
    server: Arc<ServerState>,
    conn: Arc<Connection>,
    tx: tokio::sync::mpsc::Sender<crate::conn::writer::FramePayload>,
) -> io::Result<()> {
    loop {
        let frame = match read_one_frame(&mut reader).await {
            Ok(Some(b)) => b,
            Ok(None) => {
                debug!("client closed connection");
                return Ok(());
            }
            Err(e) => {
                error!(error = %e, "frame read error");
                return Err(e);
            }
        };
        // Check shutdown after every frame.
        if server
            .shutting_down
            .load(std::sync::atomic::Ordering::Acquire)
        {
            debug!("server shutting down; dropping connection");
            return Ok(());
        }
        // The dispatcher is async but we await it inline — order-preserving and
        // good enough for v1.
        let response = crate::dispatch::dispatch_frame(&server, &conn, &frame).await;
        if let Some(bytes) = response
            && tx.send(bytes).await.is_err()
        {
            debug!("writer channel closed; reader exiting");
            return Ok(());
        }
    }
}
