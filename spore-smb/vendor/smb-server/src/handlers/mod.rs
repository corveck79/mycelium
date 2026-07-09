//! Per-command handlers.
//!
//! Each function here builds a `HandlerResponse` for a specific SMB2 command.
//! Handlers receive the parsed request header and a slice of the body bytes;
//! they return either a successful body or `HandlerResponse::err(ntstatus)`.

use std::sync::Arc;

use crate::proto::header::{Command, Smb2Header};

use crate::conn::state::Connection;
use crate::dispatch::HandlerResponse;
use crate::ntstatus;
use crate::server::ServerState;

mod change_notify;
mod close;
mod create;
mod echo;
mod flush;
mod ioctl;
mod lock;
mod logoff;
pub(crate) mod negotiate;
mod oplock_break;
mod query_directory;
mod query_info;
mod read;
mod session_setup;
mod set_info;
pub(crate) mod shared;
mod tree_connect;
mod tree_disconnect;
mod write;

/// Top-level command router.
pub async fn dispatch_command(
    server: &Arc<ServerState>,
    conn: &Arc<Connection>,
    hdr: &Smb2Header,
    body: &[u8],
) -> HandlerResponse {
    match hdr.command {
        Command::Negotiate => negotiate::handle(server, conn, hdr, body).await,
        Command::SessionSetup => session_setup::handle(server, conn, hdr, body).await,
        Command::Logoff => logoff::handle(server, conn, hdr, body).await,
        Command::TreeConnect => tree_connect::handle(server, conn, hdr, body).await,
        Command::TreeDisconnect => tree_disconnect::handle(server, conn, hdr, body).await,
        Command::Create => create::handle(server, conn, hdr, body).await,
        Command::Close => close::handle(server, conn, hdr, body).await,
        Command::Flush => flush::handle(server, conn, hdr, body).await,
        Command::Read => read::handle(server, conn, hdr, body).await,
        Command::Write => write::handle(server, conn, hdr, body).await,
        Command::Lock => lock::handle(server, conn, hdr, body).await,
        Command::Ioctl => ioctl::handle(server, conn, hdr, body).await,
        Command::Echo => echo::handle(server, conn, hdr, body).await,
        Command::QueryDirectory => query_directory::handle(server, conn, hdr, body).await,
        Command::ChangeNotify => change_notify::handle(server, conn, hdr, body).await,
        Command::QueryInfo => query_info::handle(server, conn, hdr, body).await,
        Command::SetInfo => set_info::handle(server, conn, hdr, body).await,
        Command::OplockBreak => oplock_break::handle(server, conn, hdr, body).await,
        Command::Cancel => HandlerResponse::err(ntstatus::STATUS_INVALID_PARAMETER),
    }
}
