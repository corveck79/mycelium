//! SESSION_SETUP handler — drives the SPNEGO + NTLMv2 state machine.

use std::sync::Arc;

use crate::proto::auth::ntlm::{Identity, NtlmServer, NtlmTargetInfo, UserCreds};
use crate::proto::auth::spnego::{
    NegState, OID_NTLMSSP, decode_init_token, decode_resp_token, encode_resp_token,
};
use crate::proto::crypto::signing_key_30;
use crate::proto::header::Smb2Header;
use crate::proto::messages::{Dialect, SessionSetupRequest, SessionSetupResponse};
use tracing::{debug, info, warn};

use crate::conn::state::{Connection, Session};
use crate::dispatch::HandlerResponse;
use crate::ntstatus;
use crate::server::ServerState;
use crate::utils::{fill_random, now_filetime};

pub async fn handle(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match SessionSetupRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };

    let blob = req.security_buffer;
    if blob.is_empty() {
        return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
    }
    if tracing::enabled!(tracing::Level::DEBUG) {
        let mut first8 = String::with_capacity(16);
        for b in blob.iter().take(8) {
            use std::fmt::Write as _;
            write!(&mut first8, "{b:02x}").expect("writing to String cannot fail");
        }
        tracing::debug!(
            first8 = %first8,
            len = blob.len(),
            sid = hdr.session_id,
            "session setup blob"
        );
    }

    // Decide which form the security blob takes:
    //   * GSS-API NegTokenInit       — starts with 0x60.
    //   * SPNEGO NegTokenResp        — starts with 0xa1 ([1] context tag).
    //   * Raw NTLMSSP message        — starts with "NTLMSSP\0" (RFC 4178
    //     §4.2.1 lets the client skip SPNEGO once the mech is settled; both
    //     Win11 reauth and Linux cifs.ko use this form).
    const NTLMSSP_MAGIC: &[u8] = b"NTLMSSP\0";
    let inner_token: Vec<u8>;
    let is_first_round: bool;
    let is_raw_ntlmssp: bool;
    if blob.starts_with(NTLMSSP_MAGIC) {
        // Raw NTLMSSP. Decide round by message-type at offset 8.
        let msg_type = if blob.len() >= 12 {
            u32::from_le_bytes([blob[8], blob[9], blob[10], blob[11]])
        } else {
            0
        };
        // 1 = NEGOTIATE (first), 3 = AUTHENTICATE (second). 2 is server-only.
        is_first_round = msg_type == 1;
        is_raw_ntlmssp = true;
        inner_token = blob.to_vec();
    } else if blob[0] == 0x60 {
        // GSS-API outer wrapper — NegTokenInit.
        let init = match decode_init_token(&blob) {
            Ok(t) => t,
            Err(e) => {
                warn!(error = %e, "SPNEGO init decode failed");
                return HandlerResponse::err(ntstatus::STATUS_LOGON_FAILURE);
            }
        };
        if !init.mech_types.iter().any(|m| m == OID_NTLMSSP) {
            return HandlerResponse::err(ntstatus::STATUS_NOT_SUPPORTED);
        }
        inner_token = init.mech_token.unwrap_or_default();
        is_first_round = true;
        is_raw_ntlmssp = false;
    } else {
        // NegTokenResp follow-up.
        let resp = match decode_resp_token(&blob) {
            Ok(r) => r,
            Err(e) => {
                warn!(error = %e, "SPNEGO resp decode failed");
                return HandlerResponse::err(ntstatus::STATUS_LOGON_FAILURE);
            }
        };
        inner_token = resp.response_token.unwrap_or_default();
        is_first_round = false;
        is_raw_ntlmssp = false;
    }

    if is_first_round {
        // Allocate a fresh session id and start the NTLM state machine.
        let new_sid = conn.alloc_session_id();
        let mut server_challenge = [0u8; 8];
        fill_random(&mut server_challenge);
        let netbios = server.config.netbios_name.clone();
        let mut acceptor = NtlmServer::new(
            server_challenge,
            NtlmTargetInfo::new(netbios.clone(), netbios.clone(), netbios, "", ""),
            now_filetime(),
        );

        // Step 1: parse client NEGOTIATE.
        if let Err(e) = acceptor.step1_negotiate(&inner_token) {
            warn!(error = %e, "NTLM step1 failed");
            return HandlerResponse::err(ntstatus::STATUS_LOGON_FAILURE);
        }
        let challenge_blob = acceptor.challenge();
        // Reply form mirrors the request: raw NTLMSSP if the client skipped
        // SPNEGO, else SPNEGO-wrapped.
        let outbound = if is_raw_ntlmssp {
            challenge_blob
        } else {
            encode_resp_token(
                NegState::AcceptIncomplete,
                Some(OID_NTLMSSP),
                Some(&challenge_blob),
                None,
            )
        };

        // Stash the acceptor for the next round; remember the form so the
        // success response can match.
        {
            let mut pa = conn.pending_auths.write().await;
            pa.insert(
                new_sid,
                Arc::new(std::sync::Mutex::new((acceptor, is_raw_ntlmssp))),
            );
        }

        let body_out =
            build_session_setup_response(ntstatus::STATUS_MORE_PROCESSING_REQUIRED, &outbound, 0);
        return HandlerResponse {
            body: body_out,
            status: ntstatus::STATUS_MORE_PROCESSING_REQUIRED,
            override_tree_id: None,
            override_session_id: Some(new_sid),
            skip_signing: true, // no key yet
            take_preauth_snapshot_for_session: None,
        };
    }

    // Follow-up round: look up pending acceptor by session id from header.
    let sid = hdr.session_id;
    if sid == 0 {
        return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER);
    }
    let acceptor_arc = {
        let mut pa = conn.pending_auths.write().await;
        pa.remove(&sid)
    };
    let acceptor_arc = match acceptor_arc {
        Some(a) => a,
        None => return HandlerResponse::err(ntstatus::STATUS_USER_SESSION_DELETED),
    };
    let users = server.users.table.read().await.clone();
    let (outcome, raw_form) = {
        let pair = acceptor_arc
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let (acceptor, raw_form) = (&pair.0, pair.1);
        let lookup = |u: &str, _d: &str| -> Option<UserCreds> { users.get(u).cloned() };
        let outcome = match acceptor.authenticate(&inner_token, lookup) {
            Ok(o) => o,
            Err(e) => {
                info!(error = %e, "NTLM authenticate failed");
                return HandlerResponse::err(ntstatus::STATUS_LOGON_FAILURE);
            }
        };
        (outcome, raw_form)
    };

    // Anonymous gating.
    if matches!(outcome.identity, Identity::Anonymous) && !server.anonymous_allowed().await {
        return HandlerResponse::err(ntstatus::STATUS_LOGON_FAILURE);
    }

    let session_base_key = outcome.session_key;
    let dialect = *conn.dialect.read().await;
    let signing_key = match dialect {
        Some(Dialect::Smb311) => [0u8; 16],
        Some(_) => signing_key_30(&session_base_key),
        None => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };

    let session_flags = if matches!(outcome.identity, Identity::Anonymous) {
        SessionSetupResponse::FLAG_IS_GUEST
    } else {
        0
    };
    let signing_required = false;

    let session = Session::new(
        sid,
        outcome.identity.clone(),
        session_base_key,
        signing_key,
        signing_required,
        None,
    );
    let session_arc = Arc::new(tokio::sync::RwLock::new(session));
    {
        let mut sessions = conn.sessions.write().await;
        sessions.insert(sid, session_arc);
    }

    // Empty buffer for raw NTLMSSP path; SPNEGO accept-completed for SPNEGO.
    let success_buf: Vec<u8> = if raw_form {
        Vec::new()
    } else {
        empty_completed()
    };
    let body_out =
        build_session_setup_response(ntstatus::STATUS_SUCCESS, &success_buf, session_flags);

    let take_snapshot = if dialect == Some(Dialect::Smb311) {
        Some(sid)
    } else {
        None
    };

    info!(?outcome.identity, "session established");

    HandlerResponse {
        body: body_out,
        status: ntstatus::STATUS_SUCCESS,
        override_tree_id: None,
        override_session_id: Some(sid),
        // Anonymous responses are not signed (no key). Signed responses for
        // authenticated sessions get signed by the dispatcher's normal path.
        skip_signing: matches!(outcome.identity, Identity::Anonymous),
        take_preauth_snapshot_for_session: take_snapshot,
    }
}

fn build_session_setup_response(_status: u32, spnego_blob: &[u8], session_flags: u16) -> Vec<u8> {
    let resp = SessionSetupResponse {
        structure_size: 9,
        session_flags,
        security_buffer_offset: 64 + 8, // SMB2 header + fixed prefix
        security_buffer_length: spnego_blob.len() as u16,
        security_buffer: spnego_blob.to_vec(),
    };
    let mut buf = Vec::new();
    resp.write_to(&mut buf)
        .expect("SESSION_SETUP response encodes");
    debug!(len = buf.len(), "SESSION_SETUP response built");
    buf
}

fn empty_completed() -> Vec<u8> {
    encode_resp_token(NegState::AcceptCompleted, None, None, None)
}
