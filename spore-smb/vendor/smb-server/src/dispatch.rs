//! Per-frame dispatch: parse header, route to handler, sign response, encode.

use std::sync::Arc;

use crate::proto::auth::ntlm::Identity;
use crate::proto::crypto::{PreauthIntegrity, sign};
use crate::proto::header::{
    Command, HeaderTail, SMB2_FLAGS_ASYNC_COMMAND, SMB2_FLAGS_RELATED_OPERATIONS,
    SMB2_FLAGS_SERVER_TO_REDIR, SMB2_FLAGS_SIGNED, SMB2_HEADER_LEN, Smb2Header,
};
use crate::proto::messages::ErrorResponse;
use tracing::{Instrument, debug, debug_span, error, warn};

use crate::conn::state::Connection;
use crate::handlers;
use crate::ntstatus;
use crate::server::ServerState;

/// Result of a handler: a complete (unsigned) response payload + the NTSTATUS
/// to set in the header. The dispatcher patches the header, applies signing
/// (if required), and ships the bytes.
pub struct HandlerResponse {
    /// Bytes after the SMB2 header — the body. The handler owns body
    /// construction.
    pub body: Vec<u8>,
    /// NTSTATUS for the response header.
    pub status: u32,
    /// Optional override for `tree_id` on the response header (e.g.
    /// TREE_CONNECT returns the freshly minted tree id).
    pub override_tree_id: Option<u32>,
    /// Optional override for `session_id` on the response header (e.g.
    /// SESSION_SETUP returns the freshly minted session id).
    pub override_session_id: Option<u64>,
    /// If true, the dispatcher will not sign the response. Used for
    /// pre-session-setup messages where no key exists yet.
    pub skip_signing: bool,
    /// If set, take the per-session 3.1.1 preauth snapshot after hashing the
    /// SESSION_SETUP request but before hashing the response. Set by
    /// SESSION_SETUP on the round that produces STATUS_SUCCESS, so the
    /// session's KDF context can use the snapshot.
    pub take_preauth_snapshot_for_session: Option<u64>,
}

impl HandlerResponse {
    pub fn ok(body: Vec<u8>) -> Self {
        Self {
            body,
            status: ntstatus::STATUS_SUCCESS,
            override_tree_id: None,
            override_session_id: None,
            skip_signing: false,
            take_preauth_snapshot_for_session: None,
        }
    }

    pub fn err(status: u32) -> Self {
        let er = ErrorResponse::status(status);
        let mut buf = Vec::new();
        er.write_to(&mut buf).expect("error response encodes");
        Self {
            body: buf,
            status,
            override_tree_id: None,
            override_session_id: None,
            skip_signing: false,
            take_preauth_snapshot_for_session: None,
        }
    }
}

/// Top-level frame dispatch. Returns the bytes to push into the writer
/// channel, or `None` if the request elicits no response (CANCEL).
pub async fn dispatch_frame(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    frame: &[u8],
) -> Option<Vec<u8>> {
    // SMB1 multi-protocol bootstrap (MS-SMB2 §3.3.5.3.1). The only SMB1 we
    // accept: a NEGOTIATE_REQUEST listing "SMB 2.???" or "SMB 2.002".
    // Reply with an SMB2 NEGOTIATE response and the client follows up with
    // a real SMB2 NEGOTIATE.
    if let Some(bytes) = handle_smb1_multi_protocol(server, conn, frame).await {
        return Some(bytes);
    }
    if frame.len() < SMB2_HEADER_LEN {
        warn!(len = frame.len(), "frame too short for SMB2 header");
        return None;
    }

    let mut sub_offset = 0;
    let mut responses = Vec::new();
    let mut prev_session_id = 0;
    let mut prev_tree_id = 0;
    let mut prev_create_file_id = None;

    while sub_offset < frame.len() {
        let available = &frame[sub_offset..];
        if available.len() < SMB2_HEADER_LEN {
            warn!(remaining = available.len(), "compound tail too short");
            return None;
        }

        let (mut req_hdr, _) = match Smb2Header::parse(available) {
            Ok(p) => p,
            Err(e) => {
                warn!(error = %e, "failed to parse compound sub-header");
                return None;
            }
        };

        let next = req_hdr.next_command as usize;
        let sub_len = if next == 0 {
            available.len()
        } else if next < SMB2_HEADER_LEN || next > available.len() {
            warn!(
                next,
                remaining = available.len(),
                "invalid compound NextCommand"
            );
            return None;
        } else {
            next
        };

        let mut sub_frame = available[..sub_len].to_vec();
        if req_hdr.flags & SMB2_FLAGS_RELATED_OPERATIONS != 0 {
            inherit_related_context(
                &mut sub_frame,
                &mut req_hdr,
                prev_session_id,
                prev_tree_id,
                prev_create_file_id,
            );
        }

        prev_session_id = req_hdr.session_id;
        prev_tree_id = req_hdr.tree_id().unwrap_or(0);

        if let Some(response) = dispatch_one(server, conn, &sub_frame).await {
            if req_hdr.command == Command::Create {
                prev_create_file_id = capture_create_file_id(&response);
            }
            responses.push(response);
        }

        if next == 0 {
            break;
        }
        sub_offset += next;
    }

    if responses.is_empty() {
        return None;
    }

    Some(stitch_responses(conn, responses).await)
}

fn inherit_related_context(
    sub_frame: &mut [u8],
    req_hdr: &mut Smb2Header,
    prev_session_id: u64,
    prev_tree_id: u32,
    prev_create_file_id: Option<[u8; 16]>,
) {
    if read_u64(sub_frame, 0x28) == u64::MAX {
        sub_frame[0x28..0x30].copy_from_slice(&prev_session_id.to_le_bytes());
        req_hdr.session_id = prev_session_id;
    }

    if read_u32(sub_frame, 0x24) == u32::MAX {
        sub_frame[0x24..0x28].copy_from_slice(&prev_tree_id.to_le_bytes());
        if let HeaderTail::Sync { reserved, .. } = req_hdr.tail {
            req_hdr.tail = HeaderTail::Sync {
                reserved,
                tree_id: prev_tree_id,
            };
        }
    }

    let Some(file_id) = prev_create_file_id else {
        return;
    };
    let Some(body_offset) = file_id_body_offset(req_hdr.command) else {
        return;
    };
    let offset = SMB2_HEADER_LEN + body_offset;
    if offset + 16 <= sub_frame.len()
        && read_u64(sub_frame, offset) == u64::MAX
        && read_u64(sub_frame, offset + 8) == u64::MAX
    {
        sub_frame[offset..offset + 16].copy_from_slice(&file_id);
    }
}

fn file_id_body_offset(command: Command) -> Option<usize> {
    match command {
        Command::Close
        | Command::Flush
        | Command::Lock
        | Command::Ioctl
        | Command::QueryDirectory
        | Command::ChangeNotify
        | Command::OplockBreak => Some(8),
        Command::Read | Command::Write => Some(16),
        Command::QueryInfo => Some(24),
        Command::SetInfo => Some(16),
        _ => None,
    }
}

fn capture_create_file_id(response: &[u8]) -> Option<[u8; 16]> {
    if response.len() < SMB2_HEADER_LEN + 80 || read_u32(response, 0x08) != ntstatus::STATUS_SUCCESS
    {
        return None;
    }

    let mut file_id = [0u8; 16];
    let offset = SMB2_HEADER_LEN + 64;
    file_id.copy_from_slice(&response[offset..offset + 16]);
    Some(file_id)
}

async fn stitch_responses(conn: &Arc<Connection>, responses: Vec<Vec<u8>>) -> Vec<u8> {
    let mut out = Vec::new();
    let mut ranges = Vec::with_capacity(responses.len());
    let response_count = responses.len();

    for (index, mut response) in responses.into_iter().enumerate() {
        let start = out.len();
        let actual_len = response.len();
        if index + 1 < response_count {
            let next = align_8(actual_len);
            response[0x14..0x18].copy_from_slice(&(next as u32).to_le_bytes());
        }
        out.extend_from_slice(&response);
        ranges.push((start, actual_len));

        if index + 1 < response_count {
            out.resize(start + align_8(actual_len), 0);
        }
    }

    let algo = *conn.signing_algo.read().await;
    for (start, len) in ranges {
        let flags = read_u32(&out, start + 0x10);
        if flags & SMB2_FLAGS_SIGNED == 0 {
            continue;
        }

        let session_id = read_u64(&out, start + 0x28);
        let key = {
            let sessions = conn.sessions.read().await;
            sessions.get(&session_id).cloned()
        };
        let Some(session) = key else {
            continue;
        };
        let session = session.read().await;
        if matches!(session.identity, Identity::Anonymous) {
            continue;
        }
        let signing_key = session.signing_key;
        drop(session);

        if let Err(e) = sign(&mut out[start..start + len], &signing_key, algo) {
            error!(error = %e, "failed to sign compound response");
        }
    }

    out
}

const fn align_8(n: usize) -> usize {
    (n + 7) & !7
}

fn read_u32(buf: &[u8], offset: usize) -> u32 {
    let mut bytes = [0u8; 4];
    bytes.copy_from_slice(&buf[offset..offset + 4]);
    u32::from_le_bytes(bytes)
}

fn read_u64(buf: &[u8], offset: usize) -> u64 {
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&buf[offset..offset + 8]);
    u64::from_le_bytes(bytes)
}

async fn dispatch_one(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    frame: &[u8],
) -> Option<Vec<u8>> {
    let (req_hdr, body_bytes) = match Smb2Header::parse(frame) {
        Ok(p) => p,
        Err(e) => {
            warn!(error = %e, "failed to parse header");
            return None;
        }
    };

    let cmd = req_hdr.command;
    let mid = req_hdr.message_id;
    let sid = req_hdr.session_id;
    let tid = req_hdr.tree_id().unwrap_or(0);

    let span = debug_span!("dispatch", cmd = ?cmd, mid, sid, tid);
    async move {
        debug!("dispatch start");

        // Verify signature on incoming request (when applicable).
        if let Err(status) = verify_request_signature(server, conn, &req_hdr, frame).await {
            return Some(build_response_bytes(conn, &req_hdr, HandlerResponse::err(status)).await);
        }

        // CANCEL is fire-and-forget — no response.
        if cmd == Command::Cancel {
            debug!("CANCEL received; no response");
            return None;
        }

        let dialect = *conn.dialect.read().await;
        let mut session_preauth = None;

        // 3.1.1 preauth is connection-scoped for NEGOTIATE, then per
        // SESSION_SETUP authentication exchange.
        if cmd == Command::Negotiate {
            let mut p = conn
                .preauth
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            p.update(frame);
        } else if cmd == Command::SessionSetup
            && dialect == Some(crate::proto::messages::Dialect::Smb311)
        {
            let mut p = take_session_preauth(conn, req_hdr.session_id).await;
            p.update(frame);
            session_preauth = Some(p);
        }

        let resp = handlers::dispatch_command(server, conn, &req_hdr, body_bytes).await;

        // If the handler asked for a preauth snapshot (3.1.1), take it now.
        if let Some(sid) = resp.take_preauth_snapshot_for_session {
            let snap = session_preauth
                .as_ref()
                .expect("SMB 3.1.1 SessionSetup snapshot requires per-session preauth")
                .snapshot();
            // Stash on the session — the handler already created it.
            let sessions = conn.sessions.read().await;
            if let Some(sess_arc) = sessions.get(&sid) {
                let mut sess = sess_arc.write().await;
                sess.preauth_snapshot = Some(snap);
                // For 3.1.1, recompute signing key now that we have the snapshot.
                let dialect = *conn.dialect.read().await;
                if dialect == Some(crate::proto::messages::Dialect::Smb311) {
                    sess.signing_key =
                        crate::proto::crypto::signing_key_311(&sess.session_base_key, &snap);
                }
            }
        }

        let bytes = build_response_bytes(conn, &req_hdr, resp).await;

        if cmd == Command::Negotiate {
            let mut p = conn
                .preauth
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            p.update(&bytes);
        } else if cmd == Command::SessionSetup
            && dialect == Some(crate::proto::messages::Dialect::Smb311)
        {
            if read_u32(&bytes, 0x08) == ntstatus::STATUS_MORE_PROCESSING_REQUIRED {
                if let Some(mut p) = session_preauth {
                    p.update(&bytes);
                    let sid = read_u64(&bytes, 0x28);
                    conn.session_preauth.write().await.insert(sid, p);
                }
            } else {
                conn.session_preauth
                    .write()
                    .await
                    .remove(&req_hdr.session_id);
            }
        }

        Some(bytes)
    }
    .instrument(span)
    .await
}

async fn take_session_preauth(conn: &Arc<Connection>, session_id: u64) -> PreauthIntegrity {
    if session_id != 0
        && let Some(preauth) = conn.session_preauth.write().await.remove(&session_id)
    {
        return preauth;
    }

    conn.preauth
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone()
}

async fn verify_request_signature(
    _server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    frame: &[u8],
) -> Result<(), u32> {
    if hdr.command == Command::Negotiate {
        return Ok(());
    }
    if hdr.session_id == 0 {
        return Ok(());
    }
    let sessions = conn.sessions.read().await;
    let sess_arc = match sessions.get(&hdr.session_id) {
        Some(s) => s.clone(),
        None => {
            // Unknown session.
            if hdr.flags & SMB2_FLAGS_SIGNED == 0 {
                return Ok(());
            }
            return Err(ntstatus::STATUS_USER_SESSION_DELETED);
        }
    };
    drop(sessions);

    if hdr.flags & SMB2_FLAGS_SIGNED != 0 {
        let sess = sess_arc.read().await;
        if matches!(sess.identity, Identity::Anonymous) {
            return Ok(());
        }
        let key = sess.signing_key;
        drop(sess);
        let algo = *conn.signing_algo.read().await;
        if let Err(e) = crate::proto::crypto::verify(frame, &key, algo) {
            warn!(error = %e, "request signature verification failed");
            return Err(ntstatus::STATUS_ACCESS_DENIED);
        }
    } else if hdr.command != Command::SessionSetup {
        let sess = sess_arc.read().await;
        let need = sess.signing_required && !matches!(sess.identity, Identity::Anonymous);
        drop(sess);
        if need {
            warn!(?hdr.command, "missing required signature on request");
            return Err(ntstatus::STATUS_ACCESS_DENIED);
        }
    }
    Ok(())
}

/// Build the final on-the-wire bytes: header + body, with signing applied
/// when the session has a key.
async fn build_response_bytes(
    conn: &Arc<Connection>,
    req_hdr: &Smb2Header,
    handler_resp: HandlerResponse,
) -> Vec<u8> {
    let mut hdr = *req_hdr;
    hdr.flags |= SMB2_FLAGS_SERVER_TO_REDIR;
    hdr.flags &= !SMB2_FLAGS_ASYNC_COMMAND;
    hdr.next_command = 0;
    hdr.channel_sequence_status = handler_resp.status;
    hdr.tail = HeaderTail::sync(
        handler_resp
            .override_tree_id
            .unwrap_or_else(|| req_hdr.tree_id().unwrap_or(0)),
    );
    if let Some(sid) = handler_resp.override_session_id {
        hdr.session_id = sid;
    }
    hdr.signature = [0u8; 16];

    let request_was_signed = req_hdr.flags & SMB2_FLAGS_SIGNED != 0;
    // MS-SMB2 §3.3.5.5.3 step 12: SessionSetup SUCCESS must be signed for
    // non-anon/non-guest sessions even though the request cannot be signed yet.
    let is_session_setup_success =
        req_hdr.command == Command::SessionSetup && handler_resp.status == ntstatus::STATUS_SUCCESS;
    let mut should_sign = false;
    let mut key = [0u8; 16];
    let algo = *conn.signing_algo.read().await;
    if !handler_resp.skip_signing
        && hdr.session_id != 0
        && (request_was_signed || is_session_setup_success)
    {
        let sessions = conn.sessions.read().await;
        if let Some(sess_arc) = sessions.get(&hdr.session_id) {
            let sess = sess_arc.read().await;
            let is_anon = matches!(sess.identity, Identity::Anonymous);
            let is_guest_response = is_session_setup_success
                && handler_resp.body.len() >= 4
                && (handler_resp.body[2] & 0x01) != 0;
            if !is_anon && !is_guest_response && sess.signing_key != [0u8; 16] {
                key = sess.signing_key;
                should_sign = true;
            }
        }
    }
    if should_sign {
        hdr.flags |= SMB2_FLAGS_SIGNED;
    } else {
        hdr.flags &= !SMB2_FLAGS_SIGNED;
    }
    let mut out = Vec::with_capacity(SMB2_HEADER_LEN + handler_resp.body.len());
    if let Err(e) = hdr.write(&mut out) {
        error!(error = %e, "failed to encode response header");
        return Vec::new();
    }
    out.extend_from_slice(&handler_resp.body);

    if should_sign && let Err(e) = sign(&mut out, &key, algo) {
        error!(error = %e, "failed to sign response");
    }
    out
}

/// Detect and answer an SMB1 multi-protocol NEGOTIATE_REQUEST.
///
/// SMB1 frame layout for the request we accept:
/// * `[0..4]`  — magic `0xFF 'S' 'M' 'B'`
/// * `[4]`     — command (0x72 = SMB_COM_NEGOTIATE)
/// * `[5..32]` — rest of SMB1 header (status, flags, pid, tid, mid …)
/// * `[32]`    — `WordCount` (0 for NEGOTIATE)
/// * `[33..35]`— `ByteCount` (u16 LE)
/// * `[35..]`  — dialect strings, each `0x02 <ASCII> 0x00`.
///
/// Returns `Some(reply_bytes)` only for a SMB1 NEGOTIATE that lists at least
/// one SMB2 dialect we recognise; otherwise `None` so the caller can fall
/// through to the normal SMB2 path.
async fn handle_smb1_multi_protocol(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    frame: &[u8],
) -> Option<Vec<u8>> {
    if frame.len() < 35 || frame[0..4] != [0xFF, b'S', b'M', b'B'] || frame[4] != 0x72 {
        return None;
    }
    let body_start = 33; // 32-byte header + 1-byte WordCount(=0)
    let byte_count = u16::from_le_bytes([frame[body_start], frame[body_start + 1]]) as usize;
    let blob_start = body_start + 2;
    let blob_end = (blob_start + byte_count).min(frame.len());
    let blob = &frame[blob_start..blob_end];

    let mut wants_wildcard = false;
    let mut wants_smb202 = false;
    let mut i = 0;
    while i < blob.len() {
        if blob[i] != 0x02 {
            break;
        }
        i += 1;
        let nul = match blob[i..].iter().position(|&b| b == 0) {
            Some(p) => p,
            None => break,
        };
        let s = std::str::from_utf8(&blob[i..i + nul]).unwrap_or("");
        match s {
            "SMB 2.???" => wants_wildcard = true,
            "SMB 2.002" => wants_smb202 = true,
            _ => {}
        }
        i += nul + 1;
    }

    let chosen = if wants_wildcard {
        crate::proto::messages::Dialect::Smb2Wildcard.as_u16()
    } else if wants_smb202 {
        crate::proto::messages::Dialect::Smb202.as_u16()
    } else {
        return None;
    };

    debug!(
        chosen = %format_args!("0x{chosen:04X}"),
        "SMB1 multi-protocol negotiate"
    );

    // Synthesize a request header so build_response_bytes can mint the
    // SERVER_TO_REDIR response. Per MS-SMB2 §3.3.5.3.1 the response uses
    // message_id=0, tree_id=0xFFFF, session_id=0.
    let req_hdr = Smb2Header {
        command: Command::Negotiate,
        message_id: 0,
        session_id: 0,
        tail: HeaderTail::Sync {
            reserved: 0,
            tree_id: 0xFFFF,
        },
        ..Default::default()
    };
    let resp = handlers::negotiate::multi_protocol_response(server, conn, chosen).await;
    Some(build_response_bytes(conn, &req_hdr, resp).await)
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn test_conn() -> Arc<Connection> {
        Arc::new(Connection::new(Uuid::nil(), 1024 * 1024, 1024 * 1024))
    }

    fn negotiated_preauth() -> PreauthIntegrity {
        let mut preauth = PreauthIntegrity::new();
        preauth.update(b"negotiate request");
        preauth.update(b"negotiate response");
        preauth
    }

    #[tokio::test]
    async fn new_session_setup_preauth_starts_from_negotiate_base() {
        let conn = test_conn();
        let base = negotiated_preauth();
        *conn.preauth.lock().expect("preauth lock") = base.clone();

        let mut first_session = take_session_preauth(&conn, 0).await;
        first_session.update(b"session one request");
        first_session.update(b"session one response");
        conn.session_preauth.write().await.insert(1, first_session);

        let mut second_session = take_session_preauth(&conn, 0).await;
        second_session.update(b"session two request");

        let mut expected = base.clone();
        expected.update(b"session two request");

        let mut polluted = base;
        polluted.update(b"session one request");
        polluted.update(b"session one response");
        polluted.update(b"session two request");

        assert_eq!(second_session.snapshot(), expected.snapshot());
        assert_ne!(second_session.snapshot(), polluted.snapshot());
    }

    #[tokio::test]
    async fn followup_session_setup_consumes_stored_session_preauth() {
        let conn = test_conn();
        let mut stored = negotiated_preauth();
        stored.update(b"session setup request");
        stored.update(b"session setup more-processing response");
        let expected = stored.snapshot();
        conn.session_preauth.write().await.insert(7, stored);

        let got = take_session_preauth(&conn, 7).await;

        assert_eq!(got.snapshot(), expected);
        assert!(!conn.session_preauth.read().await.contains_key(&7));
    }
}
