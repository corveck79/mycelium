//! IOCTL handler — handles FSCTL_VALIDATE_NEGOTIATE_INFO; everything else
//! returns NOT_SUPPORTED.

use std::sync::Arc;

use crate::proto::header::Smb2Header;
use crate::proto::messages::{Fsctl, IoctlRequest, IoctlResponse};

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::handlers::negotiate::{NEGOTIATE_CAPABILITIES, NEGOTIATE_SECURITY_MODE};
use crate::ntstatus;
use crate::server::ServerState;

pub async fn handle(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    _hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match IoctlRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };

    match req.fsctl() {
        Fsctl::ValidateNegotiateInfo => {
            // Build VALIDATE_NEGOTIATE_INFO_RESPONSE per MS-SMB2 §2.2.32.6:
            // Capabilities (4) | Guid (16) | SecurityMode (2) | Dialect (2) = 24 bytes.
            let dialect = conn.dialect.read().await.map(|d| d.as_u16()).unwrap_or(0);
            let mut out = Vec::with_capacity(24);
            out.extend_from_slice(&NEGOTIATE_CAPABILITIES.to_le_bytes());
            out.extend_from_slice(server.config.server_guid.as_bytes());
            out.extend_from_slice(&NEGOTIATE_SECURITY_MODE.to_le_bytes());
            out.extend_from_slice(&dialect.to_le_bytes());

            let resp = IoctlResponse {
                structure_size: 49,
                reserved: 0,
                ctl_code: req.ctl_code,
                file_id: req.file_id,
                input_offset: 0,
                input_count: 0,
                output_offset: 0x70,
                output_count: out.len() as u32,
                flags: 0,
                reserved2: 0,
                output: out,
            };
            let mut buf = Vec::new();
            resp.write_to(&mut buf).expect("IOCTL response encodes");
            HandlerResponse::ok(buf)
        }
        Fsctl::DfsGetReferrals | Fsctl::DfsGetReferralsEx => {
            HandlerResponse::err(ntstatus::STATUS_FS_DRIVER_REQUIRED)
        }
        _ => HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED),
    }
}
