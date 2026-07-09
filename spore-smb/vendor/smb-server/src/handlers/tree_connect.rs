//! TREE_CONNECT handler — share lookup + authorization.

use std::sync::Arc;

use crate::proto::auth::ntlm::Identity;
use crate::proto::header::Smb2Header;
use crate::proto::messages::{TreeConnectRequest, TreeConnectResponse};
use tracing::{info, warn};

use crate::builder::Access;
use crate::conn::state::{Connection, TreeConnect};
use crate::dispatch::HandlerResponse;
use crate::handlers::shared::lookup_session;
use crate::ntstatus;
use crate::server::{ServerState, ShareMode};

const SHARE_TYPE_DISK: u8 = 0x01;
const SHARE_TYPE_PIPE: u8 = 0x02;

const FILE_GENERIC_READ: u32 = 0x0012_0089;
const FILE_GENERIC_EXECUTE: u32 = 0x0012_00A0;
const FILE_ALL_ACCESS: u32 = 0x001F_01FF;

pub async fn handle(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    let req = match TreeConnectRequest::parse(body) {
        Ok(r) => r,
        Err(_) => return HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    };
    let path = req.path_str().unwrap_or_default();
    tracing::debug!(%path, "tree connect path");
    let share_name = match extract_share_name(&path) {
        Some(s) => s,
        None => {
            tracing::warn!(%path, "tree connect: empty share name");
            return HandlerResponse::err(ntstatus::STATUS_BAD_NETWORK_NAME);
        }
    };
    tracing::debug!(%share_name, "tree connect lookup");
    let sess_arc = match lookup_session(conn, hdr.session_id).await {
        Ok(s) => s,
        Err(s) => return HandlerResponse::err(s),
    };
    let sess = sess_arc.read().await;
    let identity = sess.identity.clone();
    drop(sess);

    // IPC$: synthetic share. Accept at TREE_CONNECT (Windows always probes
    // it before mounting an actual share); downstream CREATE/IOCTL on it
    // return NotSupported via the no-op backend.
    let share = if share_name.eq_ignore_ascii_case("IPC$") {
        crate::server::ShareBindings::ipc()
    } else {
        match server.find_share(&share_name).await {
            Some(s) => s,
            None => return HandlerResponse::err(ntstatus::STATUS_BAD_NETWORK_NAME),
        }
    };

    // Authorize.
    let acl = share.acl.read().await;
    let granted = match authorize(&acl.mode, &acl.users, &identity) {
        Some(a) => a,
        None => {
            warn!(?identity, share = %share.name, "TREE_CONNECT denied");
            return HandlerResponse::err(ntstatus::STATUS_ACCESS_DENIED);
        }
    };
    drop(acl);
    // Backend cap.
    let granted = if share.backend.capabilities().is_read_only {
        granted.clamp_to(Access::Read)
    } else {
        granted
    };

    let tree_id = sess_arc.read().await.alloc_tree_id();
    let tc = Arc::new(tokio::sync::RwLock::new(TreeConnect::new(
        tree_id,
        share.clone(),
        granted,
    )));
    {
        let sess = sess_arc.read().await;
        let mut trees = sess.trees.write().await;
        trees.insert(tree_id, tc);
    }

    let maximal_access = match granted {
        Access::Read => FILE_GENERIC_READ | FILE_GENERIC_EXECUTE,
        Access::ReadWrite => FILE_ALL_ACCESS,
    };
    let resp = TreeConnectResponse {
        structure_size: 16,
        share_type: if share.is_ipc {
            SHARE_TYPE_PIPE
        } else {
            SHARE_TYPE_DISK
        },
        reserved: 0,
        share_flags: 0,
        capabilities: 0,
        maximal_access,
    };
    let mut buf = Vec::new();
    resp.write_to(&mut buf).expect("encode");
    info!(tree_id, share = %share.name, ?granted, "tree connect");
    let mut hr = HandlerResponse::ok(buf);
    hr.override_tree_id = Some(tree_id);
    hr
}

fn extract_share_name(unc: &str) -> Option<String> {
    // \\server\share or \\server\share\
    let trimmed = unc.trim_end_matches(['\\', '/']);
    let parts: Vec<&str> = trimmed
        .split(['\\', '/'])
        .filter(|s| !s.is_empty())
        .collect();
    parts.last().map(|s| s.to_string())
}

fn authorize(
    mode: &ShareMode,
    users: &std::collections::HashMap<String, Access>,
    identity: &Identity,
) -> Option<Access> {
    match mode {
        ShareMode::Public => Some(Access::ReadWrite),
        ShareMode::PublicReadOnly => Some(Access::Read),
        ShareMode::AuthenticatedOnly => match identity {
            Identity::Anonymous => None,
            Identity::User { user, .. } => users.get(user).copied(),
        },
    }
}
