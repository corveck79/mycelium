//! Per-connection frame reader: pulls bytes off the socket, frames them,
//! hands each frame to the dispatcher.

use std::io;
use std::sync::Arc;

use crate::proto::framing::{FRAME_HEADER_LEN, decode_frame_header};
use tokio::io::{AsyncReadExt, ReadHalf};
use tokio::net::TcpStream;
use tokio::sync::Semaphore;
use tracing::{debug, error};

use crate::conn::state::Connection;
use crate::server::ServerState;

/// Upper bound on requests dispatched concurrently per connection. Bounds
/// task/memory growth if a client pipelines many requests without waiting
/// for responses; once exhausted, the reader stops pulling new frames off
/// the socket until a dispatch finishes and frees a slot -- natural
/// backpressure, TCP just backs up on the wire.
const MAX_IN_FLIGHT_PER_CONN: usize = 32;

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

/// Continuously read frames and dispatch each on its own task, bounded by a
/// per-connection semaphore.
///
/// Per-connection state (sessions/trees/opens) is keyed and guarded by
/// async-safe locks, with ids minted from atomics -- concurrent dispatch is
/// safe without extra coordination. NEGOTIATE and SESSION_SETUP preauth-hash
/// chaining are the one genuinely order-sensitive sequence, but they're
/// naturally serialized by any conformant client (each round waits for the
/// prior response before sending the next) and, for 3.1.1, keyed per
/// session so unrelated sessions can't interleave into the same hash.
/// Responses may complete out of request order -- legitimate per MS-SMB2,
/// correlated by MessageId, and `writer_task` just forwards whatever
/// arrives on the channel.
///
/// This replaces v1's fully sequential await-inline dispatch, which
/// head-of-line-blocked every other in-flight request behind whichever one
/// happened to be slow (e.g. a backend read waiting on a network fetch).
pub async fn reader_task(
    mut reader: ReadHalf<TcpStream>,
    server: Arc<ServerState>,
    conn: Arc<Connection>,
    tx: tokio::sync::mpsc::Sender<crate::conn::writer::FramePayload>,
) -> io::Result<()> {
    let in_flight = Arc::new(Semaphore::new(MAX_IN_FLIGHT_PER_CONN));
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

        let permit = in_flight
            .clone()
            .acquire_owned()
            .await
            .expect("in_flight semaphore is never closed");
        let dispatch_server = server.clone();
        let dispatch_conn = conn.clone();
        let dispatch_tx = tx.clone();
        tokio::spawn(async move {
            let response =
                crate::dispatch::dispatch_frame(&dispatch_server, &dispatch_conn, &frame).await;
            drop(permit);
            if let Some(bytes) = response {
                let _ = dispatch_tx.send(bytes).await;
            }
        });
    }
}
