//! OPLOCK_BREAK handler — acknowledge breaks without granting oplocks.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::FileId;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    _conn: &Arc<Connection>,
    _hdr: &Smb2Header,
    _body: &[u8],
) -> HandlerResponse {
    // Echo back the same shape as the notification — structure_size=24, level=0.
    let mut buf = Vec::new();
    buf.extend_from_slice(&24u16.to_le_bytes()); // structure_size
    buf.push(0); // OplockLevel
    buf.push(0); // Reserved
    buf.extend_from_slice(&0u32.to_le_bytes()); // Reserved2
    buf.extend_from_slice(&FileId::any().persistent.to_le_bytes());
    buf.extend_from_slice(&FileId::any().volatile.to_le_bytes());
    HandlerResponse::ok(buf)
}
