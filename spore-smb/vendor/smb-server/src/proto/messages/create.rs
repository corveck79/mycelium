//! CREATE Request/Response (MS-SMB2 §2.2.13 / §2.2.14).
//!
//! `create_contexts` is a chained sequence of `SMB2_CREATE_CONTEXT` records
//! (MS-SMB2 §2.2.13.2). Each record has `Next` (offset to the next entry,
//! relative to the start of *this* entry; 0 marks the last), a name + data
//! pair, and 8-byte alignment.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::{ProtoError, ProtoResult};

/// SMB2 FileId — opaque 16 bytes (volatile + persistent).
///
/// MS-SMB2 §2.2.14.1. We expose both halves; the server uses identical values
/// for both since durable handles are out of scope (spec §2 in the v1 design).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct FileId {
    pub persistent: u64,
    pub volatile: u64,
}

impl FileId {
    pub const fn new(persistent: u64, volatile: u64) -> Self {
        Self {
            persistent,
            volatile,
        }
    }

    /// MS-SMB2: the "any" FileId is `0xFFFF…FFFF`.
    pub const fn any() -> Self {
        Self {
            persistent: u64::MAX,
            volatile: u64::MAX,
        }
    }
}

/// MS-SMB2 §2.2.13 CREATE Request — fixed prefix.
///
/// Variable-length tail: the file `name` (UTF-16LE) and `create_contexts`
/// blob, each at absolute offsets from the start of the SMB2 header. We hold
/// them as length-counted byte buffers immediately following the fixed
/// portion. The server crate parses contexts with [`CreateContext::parse_chain`].
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateRequest {
    pub structure_size: u16,
    pub security_flags: u8,
    pub requested_oplock_level: u8,
    pub impersonation_level: u32,
    pub smb_create_flags: u64,
    pub reserved: u64,
    pub desired_access: u32,
    pub file_attributes: u32,
    pub share_access: u32,
    pub create_disposition: u32,
    pub create_options: u32,
    pub name_offset: u16,
    pub name_length: u16,
    pub create_contexts_offset: u32,
    pub create_contexts_length: u32,
    /// UTF-16LE filename.
    #[br(count = name_length as usize)]
    pub name: Vec<u8>,
    /// Raw create-contexts chain bytes; parse with
    /// [`CreateContext::parse_chain`].
    #[br(count = create_contexts_length as usize)]
    pub create_contexts: Vec<u8>,
}

impl CreateRequest {
    /// Decode the UTF-16LE filename.
    pub fn name_str(&self) -> Option<String> {
        if !self.name.len().is_multiple_of(2) {
            return None;
        }
        let units: Vec<u16> = self
            .name
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        Some(String::from_utf16_lossy(&units))
    }

    pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
        Ok(Self::read(&mut Cursor::new(buf))?)
    }
    pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let mut c = Cursor::new(Vec::new());
        BinWrite::write(self, &mut c)?;
        out.extend_from_slice(&c.into_inner());
        Ok(())
    }
}

/// MS-SMB2 §2.2.14 CREATE Response.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateResponse {
    pub structure_size: u16,
    pub oplock_level: u8,
    pub flags: u8,
    pub create_action: u32,
    pub creation_time: u64,
    pub last_access_time: u64,
    pub last_write_time: u64,
    pub change_time: u64,
    pub allocation_size: u64,
    pub end_of_file: u64,
    pub file_attributes: u32,
    pub reserved2: u32,
    pub file_id: FileId,
    pub create_contexts_offset: u32,
    pub create_contexts_length: u32,
    #[br(count = create_contexts_length as usize)]
    pub create_contexts: Vec<u8>,
}

impl CreateResponse {
    pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
        Ok(Self::read(&mut Cursor::new(buf))?)
    }
    pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
        let mut c = Cursor::new(Vec::new());
        BinWrite::write(self, &mut c)?;
        out.extend_from_slice(&c.into_inner());
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Create contexts (MS-SMB2 §2.2.13.2)
// ---------------------------------------------------------------------------

/// Generic SMB2_CREATE_CONTEXT envelope.
///
/// Per MS-SMB2 §2.2.13.2 each entry has:
/// * `Next` — offset (bytes) from the start of *this* entry to the start of
///   the next entry in the chain, or 0 for the last entry.
/// * `NameOffset`/`NameLength` — name (typically a 4-byte ASCII tag) at an
///   offset relative to the entry start.
/// * `Reserved` — 2 bytes.
/// * `DataOffset`/`DataLength` — payload at an offset relative to the entry
///   start.
///
/// We model the entry as `name` + `data` byte vectors plus the raw flags. The
/// chain reader / writer below handles `Next` and 8-byte alignment between
/// entries.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CreateContext {
    pub name: Vec<u8>,
    pub data: Vec<u8>,
}

impl CreateContext {
    // Well-known names (MS-SMB2 §2.2.13.2 table). 4-byte ASCII tags.
    pub const NAME_EXTA: &'static [u8; 4] = b"ExtA"; // SMB2_CREATE_EA_BUFFER
    pub const NAME_SECD: &'static [u8; 4] = b"SecD"; // SMB2_CREATE_SD_BUFFER
    pub const NAME_DHNQ: &'static [u8; 4] = b"DHnQ"; // DURABLE_HANDLE_REQUEST
    pub const NAME_DHNC: &'static [u8; 4] = b"DHnC"; // DURABLE_HANDLE_RECONNECT
    pub const NAME_ALSI: &'static [u8; 4] = b"AlSi"; // ALLOCATION_SIZE
    pub const NAME_MXAC: &'static [u8; 4] = b"MxAc"; // QUERY_MAXIMAL_ACCESS
    pub const NAME_TWRP: &'static [u8; 4] = b"TWrp"; // TIMEWARP_TOKEN
    pub const NAME_QFID: &'static [u8; 4] = b"QFid"; // QUERY_ON_DISK_ID
    pub const NAME_RQLS: &'static [u8; 4] = b"RqLs"; // REQUEST_LEASE
    pub const NAME_DH2Q: &'static [u8; 4] = b"DH2Q"; // DURABLE_HANDLE_REQUEST_V2
    pub const NAME_DH2C: &'static [u8; 4] = b"DH2C"; // DURABLE_HANDLE_RECONNECT_V2

    /// Parse a chain of create-contexts from the raw chain bytes.
    ///
    /// The chain is empty if `chain.is_empty()`. Otherwise we walk `Next`
    /// offsets until we hit a zero terminator, validating bounds at each step.
    pub fn parse_chain(chain: &[u8]) -> ProtoResult<Vec<CreateContext>> {
        let mut out = Vec::new();
        if chain.is_empty() {
            return Ok(out);
        }
        let mut cursor_off = 0usize;
        loop {
            let entry = &chain
                .get(cursor_off..)
                .ok_or(ProtoError::Malformed("create context out of range"))?;
            if entry.len() < 16 {
                return Err(ProtoError::Malformed("create context too short"));
            }
            let next = u32::from_le_bytes([entry[0], entry[1], entry[2], entry[3]]) as usize;
            let name_offset = u16::from_le_bytes([entry[4], entry[5]]) as usize;
            let name_length = u16::from_le_bytes([entry[6], entry[7]]) as usize;
            // entry[8..10] = reserved
            let data_offset = u16::from_le_bytes([entry[10], entry[11]]) as usize;
            let data_length =
                u32::from_le_bytes([entry[12], entry[13], entry[14], entry[15]]) as usize;

            let name = entry
                .get(name_offset..name_offset + name_length)
                .ok_or(ProtoError::Malformed("create context name out of range"))?
                .to_vec();
            let data = if data_length == 0 {
                Vec::new()
            } else {
                entry
                    .get(data_offset..data_offset + data_length)
                    .ok_or(ProtoError::Malformed("create context data out of range"))?
                    .to_vec()
            };
            out.push(CreateContext { name, data });

            if next == 0 {
                break;
            }
            cursor_off = cursor_off
                .checked_add(next)
                .ok_or(ProtoError::Malformed("create context next overflow"))?;
        }
        Ok(out)
    }

    /// Encode a chain of create-contexts into `out`. Inserts `Next` offsets
    /// and 8-byte alignment padding between entries.
    pub fn encode_chain(list: &[CreateContext], out: &mut Vec<u8>) -> ProtoResult<()> {
        if list.is_empty() {
            return Ok(());
        }
        // We build the chain in a scratch buffer, then copy. Each entry is:
        //   16-byte header + name + (pad to 8) + data + (pad to 8 if not last)
        // The `Next` of every entry except the last is the size from this
        // entry's start to the next entry's start.
        let mut scratch: Vec<u8> = Vec::new();
        let mut entry_starts: Vec<usize> = Vec::with_capacity(list.len());

        for (i, ctx) in list.iter().enumerate() {
            // Pad to 8-byte boundary before each entry (except possibly first
            // — but contexts must be 8-byte aligned, and the chain itself is
            // anchored at an 8-aligned offset by the server).
            while !scratch.len().is_multiple_of(8) {
                scratch.push(0);
            }
            entry_starts.push(scratch.len());

            // Reserve 16 bytes for the header; will fill in once we know
            // the actual offsets.
            let header_pos = scratch.len();
            scratch.extend_from_slice(&[0u8; 16]);

            // Name immediately follows the header.
            let name_offset_rel = (scratch.len() - header_pos) as u16;
            scratch.extend_from_slice(&ctx.name);
            // Pad to 8 before data.
            while !(scratch.len() - header_pos).is_multiple_of(8) {
                scratch.push(0);
            }
            let data_offset_rel = (scratch.len() - header_pos) as u16;
            scratch.extend_from_slice(&ctx.data);

            // Now backfill the header bytes (Next is patched after the loop).
            let hdr = &mut scratch[header_pos..header_pos + 16];
            hdr[0..4].copy_from_slice(&0u32.to_le_bytes()); // Next, fixed up below
            hdr[4..6].copy_from_slice(&name_offset_rel.to_le_bytes());
            hdr[6..8].copy_from_slice(&(ctx.name.len() as u16).to_le_bytes());
            hdr[8..10].copy_from_slice(&0u16.to_le_bytes()); // Reserved
            hdr[10..12].copy_from_slice(&data_offset_rel.to_le_bytes());
            hdr[12..16].copy_from_slice(&(ctx.data.len() as u32).to_le_bytes());

            // For non-last, pad the trailing data area to 8 so the next
            // entry starts aligned.
            if i + 1 < list.len() {
                while !scratch.len().is_multiple_of(8) {
                    scratch.push(0);
                }
            }
        }

        // Patch `Next` offsets.
        for i in 0..(entry_starts.len() - 1) {
            let this = entry_starts[i];
            let next = entry_starts[i + 1];
            let delta = (next - this) as u32;
            scratch[this..this + 4].copy_from_slice(&delta.to_le_bytes());
        }
        // Last entry's Next stays 0.

        out.extend_from_slice(&scratch);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Helper enums (oplock level, impersonation level)
// ---------------------------------------------------------------------------

/// MS-SMB2 §2.2.13 RequestedOplockLevel / §2.2.14 OplockLevel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum OplockLevel {
    None = 0x00,
    Ii = 0x01,
    Exclusive = 0x08,
    Batch = 0x09,
    Lease = 0xFF,
}

impl OplockLevel {
    pub fn from_u8(v: u8) -> Option<Self> {
        Some(match v {
            0x00 => Self::None,
            0x01 => Self::Ii,
            0x08 => Self::Exclusive,
            0x09 => Self::Batch,
            0xFF => Self::Lease,
            _ => return None,
        })
    }
}

/// MS-SMB2 §2.2.13 ImpersonationLevel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u32)]
pub enum ImpersonationLevel {
    Anonymous = 0x0000_0000,
    Identification = 0x0000_0001,
    Impersonation = 0x0000_0002,
    Delegate = 0x0000_0003,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn utf16le(s: &str) -> Vec<u8> {
        s.encode_utf16().flat_map(u16::to_le_bytes).collect()
    }

    #[test]
    fn request_round_trips() {
        let name = utf16le("dir\\file.txt");
        let r = CreateRequest {
            structure_size: 57,
            security_flags: 0,
            requested_oplock_level: 0,
            impersonation_level: ImpersonationLevel::Impersonation as u32,
            smb_create_flags: 0,
            reserved: 0,
            desired_access: 0x0012_0089,
            file_attributes: 0,
            share_access: 0x0000_0007,
            create_disposition: 1,
            create_options: 0x0000_0040,
            name_offset: 0x78,
            name_length: name.len() as u16,
            create_contexts_offset: 0,
            create_contexts_length: 0,
            name,
            create_contexts: vec![],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = CreateRequest::parse(&buf).unwrap();
        assert_eq!(decoded, r);
        assert_eq!(decoded.name_str().unwrap(), "dir\\file.txt");
    }

    #[test]
    fn response_round_trips() {
        let r = CreateResponse {
            structure_size: 89,
            oplock_level: 0,
            flags: 0,
            create_action: 1,
            creation_time: 0x01D9_0000_0000_0000,
            last_access_time: 0x01D9_0000_0000_0000,
            last_write_time: 0x01D9_0000_0000_0000,
            change_time: 0x01D9_0000_0000_0000,
            allocation_size: 0x1000,
            end_of_file: 0x800,
            file_attributes: 0x0020,
            reserved2: 0,
            file_id: FileId::new(0x1234, 0x5678),
            create_contexts_offset: 0,
            create_contexts_length: 0,
            create_contexts: vec![],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = CreateResponse::parse(&buf).unwrap();
        assert_eq!(decoded, r);
    }

    #[test]
    fn create_context_chain_round_trips_single() {
        let ctxs = vec![CreateContext {
            name: b"MxAc".to_vec(),
            data: vec![],
        }];
        let mut buf = Vec::new();
        CreateContext::encode_chain(&ctxs, &mut buf).unwrap();
        let decoded = CreateContext::parse_chain(&buf).unwrap();
        assert_eq!(decoded, ctxs);
    }

    #[test]
    fn create_context_chain_round_trips_multi() {
        let ctxs = vec![
            CreateContext {
                name: b"DHnQ".to_vec(),
                data: vec![0u8; 16],
            },
            CreateContext {
                name: b"MxAc".to_vec(),
                data: vec![],
            },
            CreateContext {
                name: b"QFid".to_vec(),
                data: vec![0xAA; 32],
            },
        ];
        let mut buf = Vec::new();
        CreateContext::encode_chain(&ctxs, &mut buf).unwrap();
        let decoded = CreateContext::parse_chain(&buf).unwrap();
        assert_eq!(decoded, ctxs);
    }

    #[test]
    fn empty_chain_round_trips() {
        let ctxs: Vec<CreateContext> = vec![];
        let mut buf = Vec::new();
        CreateContext::encode_chain(&ctxs, &mut buf).unwrap();
        assert!(buf.is_empty());
        let decoded = CreateContext::parse_chain(&buf).unwrap();
        assert!(decoded.is_empty());
    }
}
