//! SMB2 fixed 64-byte packet header (sync + async forms).
//!
//! References:
//! * MS-SMB2 §2.2.1   — Common header preamble.
//! * MS-SMB2 §2.2.1.1 — Async form (`Flags & SMB2_FLAGS_ASYNC_COMMAND`).
//! * MS-SMB2 §2.2.1.2 — Sync form.
//!
//! ## Encoding choice
//!
//! The two forms differ only in the 12-byte block at offset 0x18..0x24:
//!
//! * **Sync**:  `ChannelSequence` (u16) + `Reserved` (u16) + `Reserved2` (u32) + `TreeId` (u32)
//!   wait — actually the sync form is: `Reserved` (u32) + `TreeId` (u32) (bytes 0x20..0x28).
//! * **Async**: `AsyncId` (u64) at bytes 0x20..0x28.
//!
//! In *both* forms, bytes 0x10..0x14 are `Status` (or `ChannelSequence + Reserved` on
//! 3.x channel-sequence-aware requests; we treat them as a single u32 named
//! `channel_sequence_status`). Bytes 0x14..0x18 are `Command + CreditReqResp`,
//! 0x18..0x1C are `Flags`, 0x1C..0x20 are `NextCommand`, 0x20..0x28 are `MessageId`.
//! The discriminated 8-byte block lives at 0x28..0x30, followed by the 16-byte
//! `Signature` at 0x30..0x40 — totalling 64 bytes.
//!
//! We model this as a single `Smb2Header` struct with a `tail: HeaderTail` enum
//! that is `Sync { reserved: u32, tree_id: u32 }` or `Async { async_id: u64 }`,
//! discriminated by `Flags & SMB2_FLAGS_ASYNC_COMMAND`. This is the cleanest
//! mapping to the spec — every other field is shared.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::{ProtoError, ProtoResult};

/// SMB2 protocol identifier ("\xfeSMB").
pub const SMB2_MAGIC: [u8; 4] = [0xFE, b'S', b'M', b'B'];

/// Fixed `StructureSize` of the SMB2 header (MS-SMB2 §2.2.1.1/§2.2.1.2).
pub const SMB2_HEADER_STRUCTURE_SIZE: u16 = 64;

/// Total wire size of the SMB2 header.
pub const SMB2_HEADER_LEN: usize = 64;

// ---------------------------------------------------------------------------
// Flags (MS-SMB2 §2.2.1.2 Flags field)
// ---------------------------------------------------------------------------

/// `SMB2_FLAGS_SERVER_TO_REDIR` — set on responses.
pub const SMB2_FLAGS_SERVER_TO_REDIR: u32 = 0x0000_0001;
/// `SMB2_FLAGS_ASYNC_COMMAND` — selects the async header form.
pub const SMB2_FLAGS_ASYNC_COMMAND: u32 = 0x0000_0002;
/// `SMB2_FLAGS_RELATED_OPERATIONS` — compound chain marker.
pub const SMB2_FLAGS_RELATED_OPERATIONS: u32 = 0x0000_0004;
/// `SMB2_FLAGS_SIGNED` — message is signed.
pub const SMB2_FLAGS_SIGNED: u32 = 0x0000_0008;
/// `SMB2_FLAGS_PRIORITY_MASK` — bits 4..6 hold priority (3.1.1+).
pub const SMB2_FLAGS_PRIORITY_MASK: u32 = 0x0000_0070;
/// `SMB2_FLAGS_DFS_OPERATIONS`.
pub const SMB2_FLAGS_DFS_OPERATIONS: u32 = 0x1000_0000;
/// `SMB2_FLAGS_REPLAY_OPERATION`.
pub const SMB2_FLAGS_REPLAY_OPERATION: u32 = 0x2000_0000;

// ---------------------------------------------------------------------------
// Command opcodes (MS-SMB2 §2.2.1.2 Command field)
// ---------------------------------------------------------------------------

/// SMB2 command opcodes (the 19 commands in v1).
#[binrw]
#[brw(little, repr = u16)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Command {
    Negotiate = 0x0000,
    SessionSetup = 0x0001,
    Logoff = 0x0002,
    TreeConnect = 0x0003,
    TreeDisconnect = 0x0004,
    Create = 0x0005,
    Close = 0x0006,
    Flush = 0x0007,
    Read = 0x0008,
    Write = 0x0009,
    Lock = 0x000A,
    Ioctl = 0x000B,
    Cancel = 0x000C,
    Echo = 0x000D,
    QueryDirectory = 0x000E,
    ChangeNotify = 0x000F,
    QueryInfo = 0x0010,
    SetInfo = 0x0011,
    OplockBreak = 0x0012,
}

impl Command {
    /// Raw opcode for diagnostics.
    pub const fn as_u16(self) -> u16 {
        self as u16
    }
}

// ---------------------------------------------------------------------------
// Header struct
// ---------------------------------------------------------------------------

/// The 12-byte tail of the header that differs between sync and async forms.
///
/// The discriminant is `flags & SMB2_FLAGS_ASYNC_COMMAND`. We can't easily use
/// binrw's args+if without making the parent struct generic over the runtime
/// flag value, so the parent reads/writes this manually via `parse` / `write`
/// helpers and we expose a regular Rust enum here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeaderTail {
    /// Sync form: `Reserved (u32)` + `TreeId (u32)` at bytes 0x24..0x2C.
    /// (See note in module docs about offsets.)
    Sync { reserved: u32, tree_id: u32 },
    /// Async form: `AsyncId (u64)` at bytes 0x24..0x2C.
    Async { async_id: u64 },
}

impl HeaderTail {
    /// Default sync tail with `TreeId = 0`.
    pub const fn sync(tree_id: u32) -> Self {
        HeaderTail::Sync {
            reserved: 0,
            tree_id,
        }
    }

    /// Default async tail.
    pub const fn async_(async_id: u64) -> Self {
        HeaderTail::Async { async_id }
    }
}

/// SMB2 fixed 64-byte header.
///
/// On the wire the layout is (offsets in decimal — total 64 bytes):
///
/// | Offset | Size | Field |
/// |-------:|-----:|-------|
/// |   0    |   4  | ProtocolId (`0xFE 'S' 'M' 'B'`) |
/// |   4    |   2  | StructureSize (always 64) |
/// |   6    |   2  | CreditCharge |
/// |   8    |   4  | (Channel)Status |
/// |  12    |   2  | Command |
/// |  14    |   2  | CreditRequest/CreditResponse |
/// |  16    |   4  | Flags |
/// |  20    |   4  | NextCommand |
/// |  24    |   8  | MessageId |
/// |  32    |   8  | Reserved/TreeId (sync) **or** AsyncId (async) |
/// |  40    |   8  | SessionId |
/// |  48    |  16  | Signature |
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Smb2Header {
    pub credit_charge: u16,
    /// Bytes 8..12: in client→server requests on 3.x this can split into
    /// `ChannelSequence(u16)` + `Reserved(u16)`; in server→client responses
    /// it carries `Status` (NTSTATUS). We expose the raw u32 — handlers/
    /// signing code interpret it.
    pub channel_sequence_status: u32,
    pub command: Command,
    /// On requests this is `CreditRequest`; on responses, `CreditResponse`.
    pub credit_request_response: u16,
    pub flags: u32,
    /// Offset to the next header in a compound chain, or 0 for the last.
    pub next_command: u32,
    pub message_id: u64,
    /// Sync: `(reserved, tree_id)`. Async: `async_id`. Discriminated by
    /// `flags & SMB2_FLAGS_ASYNC_COMMAND`.
    pub tail: HeaderTail,
    pub session_id: u64,
    /// 16-byte signature; zeroed on unsigned messages.
    pub signature: [u8; 16],
}

impl Default for Smb2Header {
    fn default() -> Self {
        Self {
            credit_charge: 0,
            channel_sequence_status: 0,
            command: Command::Negotiate,
            credit_request_response: 0,
            flags: 0,
            next_command: 0,
            message_id: 0,
            tail: HeaderTail::sync(0),
            session_id: 0,
            signature: [0u8; 16],
        }
    }
}

impl Smb2Header {
    /// Convenience: is this an async-form header?
    pub fn is_async(&self) -> bool {
        self.flags & SMB2_FLAGS_ASYNC_COMMAND != 0
    }

    /// Convenience: is this a server→client response?
    pub fn is_response(&self) -> bool {
        self.flags & SMB2_FLAGS_SERVER_TO_REDIR != 0
    }

    /// Convenience: tree_id from a sync header (panics if async).
    pub fn tree_id(&self) -> Option<u32> {
        match self.tail {
            HeaderTail::Sync { tree_id, .. } => Some(tree_id),
            HeaderTail::Async { .. } => None,
        }
    }

    /// Convenience: async_id from an async header.
    pub fn async_id(&self) -> Option<u64> {
        match self.tail {
            HeaderTail::Async { async_id } => Some(async_id),
            HeaderTail::Sync { .. } => None,
        }
    }

    /// Parse from a byte slice. Returns the header and the remaining bytes.
    pub fn parse(buf: &[u8]) -> ProtoResult<(Self, &[u8])> {
        if buf.len() < SMB2_HEADER_LEN {
            return Err(ProtoError::Malformed("short SMB2 header"));
        }
        let mut cursor = Cursor::new(&buf[..SMB2_HEADER_LEN]);
        let raw = RawHeader::read(&mut cursor)?;
        if raw.protocol_id != SMB2_MAGIC {
            return Err(ProtoError::Malformed("bad SMB2 magic"));
        }
        if raw.structure_size != SMB2_HEADER_STRUCTURE_SIZE {
            return Err(ProtoError::Malformed("SMB2 header structure_size != 64"));
        }
        let command = match Command::read_le(&mut Cursor::new(raw.command_raw.to_le_bytes())) {
            Ok(c) => c,
            Err(_) => {
                return Err(ProtoError::Malformed("unknown SMB2 command opcode"));
            }
        };
        let tail = if raw.flags & SMB2_FLAGS_ASYNC_COMMAND != 0 {
            HeaderTail::Async {
                async_id: u64::from_le_bytes(raw.tail_bytes),
            }
        } else {
            let reserved = u32::from_le_bytes([
                raw.tail_bytes[0],
                raw.tail_bytes[1],
                raw.tail_bytes[2],
                raw.tail_bytes[3],
            ]);
            let tree_id = u32::from_le_bytes([
                raw.tail_bytes[4],
                raw.tail_bytes[5],
                raw.tail_bytes[6],
                raw.tail_bytes[7],
            ]);
            HeaderTail::Sync { reserved, tree_id }
        };
        Ok((
            Smb2Header {
                credit_charge: raw.credit_charge,
                channel_sequence_status: raw.channel_sequence_status,
                command,
                credit_request_response: raw.credit_request_response,
                flags: raw.flags,
                next_command: raw.next_command,
                message_id: raw.message_id,
                tail,
                session_id: raw.session_id,
                signature: raw.signature,
            },
            &buf[SMB2_HEADER_LEN..],
        ))
    }

    /// Serialize the 64-byte header into `out`.
    pub fn write(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let tail_bytes = match self.tail {
            HeaderTail::Sync { reserved, tree_id } => {
                let mut b = [0u8; 8];
                b[..4].copy_from_slice(&reserved.to_le_bytes());
                b[4..].copy_from_slice(&tree_id.to_le_bytes());
                b
            }
            HeaderTail::Async { async_id } => async_id.to_le_bytes(),
        };
        let raw = RawHeader {
            protocol_id: SMB2_MAGIC,
            structure_size: SMB2_HEADER_STRUCTURE_SIZE,
            credit_charge: self.credit_charge,
            channel_sequence_status: self.channel_sequence_status,
            command_raw: self.command.as_u16(),
            credit_request_response: self.credit_request_response,
            flags: self.flags,
            next_command: self.next_command,
            message_id: self.message_id,
            tail_bytes,
            session_id: self.session_id,
            signature: self.signature,
        };
        let start = out.len();
        let mut cursor = Cursor::new(Vec::with_capacity(SMB2_HEADER_LEN));
        raw.write(&mut cursor)?;
        out.extend_from_slice(&cursor.into_inner());
        debug_assert_eq!(out.len() - start, SMB2_HEADER_LEN);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Internal raw header for binrw plumbing.
// ---------------------------------------------------------------------------

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, Copy)]
struct RawHeader {
    protocol_id: [u8; 4],
    structure_size: u16,
    credit_charge: u16,
    channel_sequence_status: u32,
    command_raw: u16,
    credit_request_response: u16,
    flags: u32,
    next_command: u32,
    message_id: u64,
    tail_bytes: [u8; 8],
    session_id: u64,
    signature: [u8; 16],
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_sync() -> Smb2Header {
        Smb2Header {
            credit_charge: 1,
            channel_sequence_status: 0,
            command: Command::Negotiate,
            credit_request_response: 1,
            flags: 0,
            next_command: 0,
            message_id: 0,
            tail: HeaderTail::Sync {
                reserved: 0,
                tree_id: 0,
            },
            session_id: 0,
            signature: [0u8; 16],
        }
    }

    fn sample_async() -> Smb2Header {
        Smb2Header {
            credit_charge: 4,
            channel_sequence_status: 0,
            command: Command::Read,
            credit_request_response: 1,
            flags: SMB2_FLAGS_ASYNC_COMMAND | SMB2_FLAGS_SERVER_TO_REDIR,
            next_command: 0,
            message_id: 42,
            tail: HeaderTail::Async {
                async_id: 0xDEAD_BEEF_CAFE_F00D,
            },
            session_id: 0x1122_3344_5566_7788,
            signature: [0xAA; 16],
        }
    }

    #[test]
    fn sync_round_trips() {
        let hdr = sample_sync();
        let mut buf = Vec::new();
        hdr.write(&mut buf).unwrap();
        assert_eq!(buf.len(), SMB2_HEADER_LEN);
        // First 4 bytes must be the magic.
        assert_eq!(&buf[..4], &SMB2_MAGIC);
        // StructureSize at offset 4 == 64
        assert_eq!(u16::from_le_bytes([buf[4], buf[5]]), 64);

        let (decoded, rest) = Smb2Header::parse(&buf).unwrap();
        assert!(rest.is_empty());
        assert_eq!(decoded, hdr);
    }

    #[test]
    fn async_round_trips() {
        let hdr = sample_async();
        let mut buf = Vec::new();
        hdr.write(&mut buf).unwrap();
        assert_eq!(buf.len(), SMB2_HEADER_LEN);

        let (decoded, _rest) = Smb2Header::parse(&buf).unwrap();
        assert_eq!(decoded, hdr);
        assert!(decoded.is_async());
        assert!(decoded.is_response());
        assert_eq!(decoded.async_id(), Some(0xDEAD_BEEF_CAFE_F00D));
        assert_eq!(decoded.tree_id(), None);
    }

    #[test]
    fn rejects_bad_magic() {
        let hdr = sample_sync();
        let mut buf = Vec::new();
        hdr.write(&mut buf).unwrap();
        buf[0] = 0xFF;
        let err = Smb2Header::parse(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }

    #[test]
    fn rejects_bad_structure_size() {
        let hdr = sample_sync();
        let mut buf = Vec::new();
        hdr.write(&mut buf).unwrap();
        buf[4] = 0; // wreck the structure_size LE bytes
        buf[5] = 0;
        let err = Smb2Header::parse(&buf).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }

    #[test]
    fn rejects_short_buffer() {
        let err = Smb2Header::parse(&[0u8; 32]).unwrap_err();
        assert!(matches!(err, ProtoError::Malformed(_)));
    }

    #[test]
    fn handcrafted_sync_negotiate_request() {
        // Hand-built Sync NEGOTIATE request header: magic, size=64, no flags,
        // command=0, mid=0, tree_id=0, sid=0, no signature.
        let mut buf = vec![0u8; 64];
        buf[..4].copy_from_slice(&SMB2_MAGIC);
        buf[4..6].copy_from_slice(&64u16.to_le_bytes());
        // command at offset 12 = 0 (NEGOTIATE), already zero
        // everything else zero
        let (hdr, _) = Smb2Header::parse(&buf).unwrap();
        assert_eq!(hdr.command, Command::Negotiate);
        assert!(!hdr.is_async());
        assert_eq!(hdr.tree_id(), Some(0));
    }

    #[test]
    fn command_round_trips_via_binrw() {
        for cmd in [
            Command::Negotiate,
            Command::SessionSetup,
            Command::Logoff,
            Command::TreeConnect,
            Command::TreeDisconnect,
            Command::Create,
            Command::Close,
            Command::Flush,
            Command::Read,
            Command::Write,
            Command::Lock,
            Command::Ioctl,
            Command::Cancel,
            Command::Echo,
            Command::QueryDirectory,
            Command::ChangeNotify,
            Command::QueryInfo,
            Command::SetInfo,
            Command::OplockBreak,
        ] {
            let mut hdr = sample_sync();
            hdr.command = cmd;
            let mut buf = Vec::new();
            hdr.write(&mut buf).unwrap();
            let (decoded, _) = Smb2Header::parse(&buf).unwrap();
            assert_eq!(decoded.command, cmd);
        }
    }
}
