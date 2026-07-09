//! ECHO handler.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::EchoResponse;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::server::ServerState;

pub async fn handle(
    _server: &Arc<ServerState>,
    _conn: &Arc<Connection>,
    _hdr: &Smb2Header,
    _body: &[u8],
) -> HandlerResponse {
    let mut buf = Vec::new();
    EchoResponse::default().write_to(&mut buf).expect("encode");
    HandlerResponse::ok(buf)
}
