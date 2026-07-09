//! TREE_DISCONNECT handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::TreeDisconnectResponse;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::handlers::shared::lookup_session;
use crate::ntstatus;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    _body: &[u8],
) -> HandlerResponse {
    let tid = match hdr.tree_id() {
        Some(t) => t,
        None => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };

    if lookup_session(conn, hdr.session_id).await.is_err() {
        return HandlerResponse::err(ntstatus::STATUS_USER_SESSION_DELETED);
    }
    if !conn.close_tree(hdr.session_id, tid).await {
        return HandlerResponse::err(ntstatus::STATUS_NETWORK_NAME_DELETED);
    }
    let mut buf = Vec::new();
    TreeDisconnectResponse::default()
        .write_to(&mut buf)
        .expect("encode");
    HandlerResponse::ok(buf)
}
