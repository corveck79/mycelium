//! Per-command request/response wire structs.
//!
//! Each SMB2 command (MS-SMB2 §2.2.3 — §2.2.18, §2.2.31, §2.2.37, §2.2.39)
//! gets its own submodule with a `…Request` and `…Response` struct, both
//! `binrw`-driven and round-trip safe.
//!
//! The crate does **not** implement command behavior — it only encodes/decodes
//! the wire bytes. The server crate owns dispatch and state.

pub mod cancel;
pub mod change_notify;
pub mod close;
pub mod create;
pub mod echo;
pub mod error_response;
pub mod flush;
pub mod ioctl;
pub mod lock;
pub mod logoff;
pub mod negotiate;
pub mod oplock_break;
pub mod query_directory;
pub mod query_info;
pub mod read;
pub mod session_setup;
pub mod set_info;
pub mod tree_connect;
pub mod tree_disconnect;
pub mod write;

pub use cancel::CancelRequest;
pub use change_notify::{ChangeNotifyRequest, ChangeNotifyResponse};
pub use close::{CloseRequest, CloseResponse};
pub use create::{
    CreateContext, CreateRequest, CreateResponse, FileId, ImpersonationLevel, OplockLevel,
};
pub use echo::{EchoRequest, EchoResponse};
pub use error_response::{ErrorContext, ErrorResponse};
pub use flush::{FlushRequest, FlushResponse};
pub use ioctl::{Fsctl, IoctlRequest, IoctlResponse};
pub use lock::{LockElement, LockRequest, LockResponse};
pub use logoff::{LogoffRequest, LogoffResponse};
pub use negotiate::{
    Dialect, EncryptionCapabilities, NegotiateContext, NegotiateContextData, NegotiateRequest,
    NegotiateResponse, PreauthIntegrityCapabilities, SigningCapabilities,
};
pub use oplock_break::{OplockBreakAck, OplockBreakNotification};
pub use query_directory::{FileInfoClass, QueryDirectoryRequest, QueryDirectoryResponse};
pub use query_info::{InfoType, QueryInfoRequest, QueryInfoResponse};
pub use read::{ReadRequest, ReadResponse};
pub use session_setup::{SessionSetupRequest, SessionSetupResponse};
pub use set_info::{SetInfoRequest, SetInfoResponse};
pub use tree_connect::{TreeConnectRequest, TreeConnectResponse};
pub use tree_disconnect::{TreeDisconnectRequest, TreeDisconnectResponse};
pub use write::{WriteRequest, WriteResponse};
