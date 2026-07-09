//! LOGOFF handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::LogoffResponse;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::ntstatus;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    _body: &[u8],
) -> HandlerResponse {
    if hdr.session_id == 0 {
        return HandlerResponse::err(ntstatus::STATUS_USER_SESSION_DELETED);
    }
    conn.close_session(hdr.session_id).await;
    let mut buf = Vec::new();
    LogoffResponse::default()
        .write_to(&mut buf)
        .expect("encode");
    HandlerResponse::ok(buf)
}
