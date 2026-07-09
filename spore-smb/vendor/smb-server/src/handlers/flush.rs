//! FLUSH handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::{FileId, FlushRequest, FlushResponse};

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::handlers::shared::{lookup_open, lookup_session_tree};
use crate::ntstatus;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match FlushRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };
    let fid = FileId::new(req.file_id_persistent, req.file_id_volatile);
    let tree_arc = match lookup_session_tree(conn, hdr).await {
        Ok(t) => t,
        Err(s) => return HandlerResponse::err(s),
    };
    let open_arc = match lookup_open(&tree_arc, fid).await {
        Some(o) => o,
        None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
    };
    let res = {
        let open = open_arc.read().await;
        match open.handle.as_ref() {
            Some(h) => h.flush().await,
            None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
        }
    };
    if let Err(e) = res {
        return HandlerResponse::err(e.to_nt_status());
    }
    let mut buf = Vec::new();
    FlushResponse::default().write_to(&mut buf).expect("encode");
    HandlerResponse::ok(buf)
}
