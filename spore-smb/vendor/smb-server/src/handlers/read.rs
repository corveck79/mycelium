//! READ handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::{ReadRequest, ReadResponse};

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
    let req = match ReadRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };
    let max_read = *conn.max_read_size.read().await;
    if req.length > max_read {
        return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
    }
    let tree_arc = match lookup_session_tree(conn, hdr).await {
        Ok(t) => t,
        Err(s) => return HandlerResponse::err(s),
    };
    let open_arc = match lookup_open(&tree_arc, req.file_id).await {
        Some(o) => o,
        None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
    };
    let result = {
        let open = open_arc.read().await;
        match open.handle.as_ref() {
            Some(h) => h.read(req.offset, req.length).await,
            None => return HandlerResponse::err(ntstatus::STATUS_FILE_CLOSED),
        }
    };
    let bytes = match result {
        Ok(b) => b,
        Err(e) => return HandlerResponse::err(e.to_nt_status()),
    };
    if bytes.is_empty() && req.length > 0 {
        return HandlerResponse::err(ntstatus::STATUS_END_OF_FILE);
    }
    let resp = ReadResponse {
        structure_size: 17,
        data_offset: ReadResponse::STANDARD_DATA_OFFSET,
        reserved: 0,
        data_length: bytes.len() as u32,
        data_remaining: 0,
        flags: 0,
        data: bytes.to_vec(),
    };
    let mut buf = Vec::new();
    resp.write_to(&mut buf).expect("encode");
    HandlerResponse::ok(buf)
}
