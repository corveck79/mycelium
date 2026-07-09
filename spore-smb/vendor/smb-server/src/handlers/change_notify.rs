//! CHANGE_NOTIFY handler — v1 always returns NOT_SUPPORTED.

use std::sync::Arc;

use crate::proto::header::Smb2Header;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::ntstatus;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    _conn: &Arc<Connection>,
    _hdr: &Smb2Header,
    _body: &[u8],
) -> HandlerResponse {
    HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED)
}
