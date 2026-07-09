//! NEGOTIATE handler.

use std::sync::Arc;

use crate::proto::auth::spnego::encode_init_response;
use crate::proto::crypto::SigningAlgo;
use crate::proto::header::Smb2Header;
use crate::proto::messages::{
    Dialect, NegotiateContext, NegotiateRequest, NegotiateResponse, PreauthIntegrityCapabilities,
    SigningCapabilities,
};
use tracing::info;
use uuid::Uuid;

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::ntstatus;
use crate::server::ServerState;
use crate::utils::{fill_random, now_filetime};

// MS-SMB2 §2.2.4 SecurityMode bits. Keep SIGNING_REQUIRED clear: anonymous
// Linux cifs mounts do not send enough NTLM material for the server to derive
// matching SMB3 signing keys.
pub(crate) const NEGOTIATE_SECURITY_MODE: u16 = 0x0001;

const CAP_DFS: u32 = 0x0000_0001;
const CAP_LEASING: u32 = 0x0000_0002;
const CAP_LARGE_MTU: u32 = 0x0000_0004;
pub(crate) const NEGOTIATE_CAPABILITIES: u32 = CAP_DFS | CAP_LEASING | CAP_LARGE_MTU;

pub async fn handle(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    _hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match NegotiateRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };

    // Pick the highest dialect we support that the client offered.
    const SUPPORTED: &[u16] = &[0x0202, 0x0210, 0x0300, 0x0302, 0x0311];
    let mut chosen: Option<u16> = None;
    for &d in &req.dialects {
        if SUPPORTED.contains(&d) {
            chosen = match chosen {
                None => Some(d),
                Some(prev) if d > prev => Some(d),
                Some(prev) => Some(prev),
            };
        }
    }
    let chosen = match chosen {
        Some(d) => d,
        None => return HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED),
    };
    let dialect = match Dialect::from_u16(chosen) {
        Some(dialect) => dialect,
        None => return HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED),
    };
    *conn.dialect.write().await = Some(dialect);
    *conn.client_guid.write().await = Uuid::from_bytes(req.client_guid);
    *conn.signing_algo.write().await = match dialect {
        Dialect::Smb202 | Dialect::Smb210 => SigningAlgo::HmacSha256,
        _ => SigningAlgo::AesCmac,
    };

    // Build SPNEGO security blob (mech-list-only, advertising NTLMSSP).
    let security_blob = encode_init_response();
    let security_buffer_offset: u16 = 64 + 64; // SMB2 header + fixed NEG response (64 bytes)
    let security_buffer_length: u16 = security_blob.len() as u16;

    // For 3.1.1 build negotiate contexts.
    let mut contexts_bytes: Vec<u8> = Vec::new();
    let mut context_count: u16 = 0;
    let mut negotiate_context_offset: u32 = 0;

    if dialect == Dialect::Smb311 {
        // PREAUTH_INTEGRITY_CAPABILITIES
        let mut salt = [0u8; 32];
        fill_random(&mut salt);
        let preauth_caps = PreauthIntegrityCapabilities {
            hash_algorithm_count: 1,
            salt_length: 32,
            hash_algorithms: vec![PreauthIntegrityCapabilities::HASH_SHA512],
            salt: salt.to_vec(),
        };
        let preauth_data = {
            use binrw::BinWrite;
            let mut c = std::io::Cursor::new(Vec::new());
            BinWrite::write(&preauth_caps, &mut c).expect("preauth negotiate context encodes");
            c.into_inner()
        };
        let preauth_ctx = NegotiateContext {
            context_type: NegotiateContext::TYPE_PREAUTH_INTEGRITY,
            data_length: preauth_data.len() as u16,
            reserved: 0,
            data: preauth_data,
        };

        // SIGNING_CAPABILITIES — advertise AES-CMAC.
        let signing_caps = SigningCapabilities {
            signing_algorithm_count: 1,
            signing_algorithms: vec![SigningCapabilities::ALGORITHM_AES_CMAC],
        };
        let signing_data = {
            use binrw::BinWrite;
            let mut c = std::io::Cursor::new(Vec::new());
            BinWrite::write(&signing_caps, &mut c).expect("signing negotiate context encodes");
            c.into_inner()
        };
        let signing_ctx = NegotiateContext {
            context_type: NegotiateContext::TYPE_SIGNING,
            data_length: signing_data.len() as u16,
            reserved: 0,
            data: signing_data,
        };

        let ctxs = vec![preauth_ctx, signing_ctx];
        if let Err(e) = NegotiateContext::encode_list(&ctxs, &mut contexts_bytes) {
            tracing::error!(error = %e, "encode_list failed");
            return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
        }
        context_count = ctxs.len() as u16;

        // The contexts go after security buffer, 8-byte aligned.
        let post_security = security_buffer_offset as u32 + security_buffer_length as u32;
        // Round up to next multiple of 8 from the start of the SMB2 header.
        negotiate_context_offset = (post_security + 7) & !7;
    }

    let max_read_size = *conn.max_read_size.read().await;
    let max_write_size = *conn.max_write_size.read().await;
    let max_transact_size = max_read_size; // common practice

    let resp = NegotiateResponse {
        structure_size: 65,
        security_mode: NEGOTIATE_SECURITY_MODE,
        dialect_revision: chosen,
        negotiate_context_count_or_reserved: context_count,
        server_guid: *server.config.server_guid.as_bytes(),
        capabilities: NEGOTIATE_CAPABILITIES,
        max_transact_size,
        max_read_size,
        max_write_size,
        system_time: now_filetime(),
        server_start_time: server.server_start_filetime,
        security_buffer_offset,
        security_buffer_length,
        negotiate_context_offset_or_reserved2: negotiate_context_offset,
        security_buffer: security_blob,
    };

    let mut body_out = Vec::new();
    if let Err(e) = resp.write_to(&mut body_out) {
        tracing::error!(error = %e, "encode NEGOTIATE response");
        return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
    }
    // Append padding to align contexts at `negotiate_context_offset`.
    if dialect == Dialect::Smb311 && context_count > 0 {
        let cur = 64 + body_out.len() as u32; // header + body so far
        if cur < negotiate_context_offset {
            let pad = (negotiate_context_offset - cur) as usize;
            body_out.extend(std::iter::repeat_n(0u8, pad));
        }
        body_out.extend_from_slice(&contexts_bytes);
    }
    info!(?dialect, "NEGOTIATE complete");
    let mut hr = HandlerResponse::ok(body_out);
    hr.skip_signing = true;
    hr
}

/// Build the SMB2 NEGOTIATE response sent in reply to an SMB1 multi-protocol
/// NEGOTIATE_REQUEST that listed an SMB2 dialect (MS-SMB2 §3.3.5.3.1).
///
/// We do NOT commit the connection dialect here — the client will follow up
/// with a real SMB2 NEGOTIATE which goes through [`handle`]. This response
/// only tells the client "yes, I speak SMB2; send me an SMB2 NEGOTIATE next".
pub async fn multi_protocol_response(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    chosen: u16,
) -> HandlerResponse {
    let security_blob = encode_init_response();
    let security_buffer_offset: u16 = 64 + 64;
    let security_buffer_length: u16 = security_blob.len() as u16;
    let max_read_size = *conn.max_read_size.read().await;
    let max_write_size = *conn.max_write_size.read().await;
    let max_transact_size = max_read_size;

    let resp = NegotiateResponse {
        structure_size: 65,
        security_mode: NEGOTIATE_SECURITY_MODE,
        dialect_revision: chosen,
        negotiate_context_count_or_reserved: 0,
        server_guid: *server.config.server_guid.as_bytes(),
        capabilities: 0,
        max_transact_size,
        max_read_size,
        max_write_size,
        system_time: now_filetime(),
        server_start_time: server.server_start_filetime,
        security_buffer_offset,
        security_buffer_length,
        negotiate_context_offset_or_reserved2: 0,
        security_buffer: security_blob,
    };

    let mut body_out = Vec::new();
    if let Err(e) = resp.write_to(&mut body_out) {
        tracing::error!(error = %e, "encode multi-protocol NEGOTIATE response");
        return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
    }
    info!(
        chosen = %format_args!("0x{chosen:04X}"),
        "SMB1 multi-protocol -> SMB2"
    );
    let mut hr = HandlerResponse::ok(body_out);
    hr.skip_signing = true;
    hr
}
