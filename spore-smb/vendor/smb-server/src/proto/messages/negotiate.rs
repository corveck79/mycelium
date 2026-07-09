//! NEGOTIATE Request/Response (MS-SMB2 §2.2.3 / §2.2.4) including the SMB
//! 3.1.1 negotiate-context machinery from §2.2.3.1.x and §2.2.4.x.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

// ---------------------------------------------------------------------------
// Dialect
// ---------------------------------------------------------------------------

/// SMB2 dialect revision codes (MS-SMB2 §2.2.3 — DialectRevision).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u16)]
pub enum Dialect {
    Smb202 = 0x0202,
    Smb210 = 0x0210,
    Smb300 = 0x0300,
    Smb302 = 0x0302,
    Smb311 = 0x0311,
    /// Sent by SMB 2.0.2/2.1 clients via SMB1 negotiate; we accept it as a
    /// signal to multi-protocol-negotiate. Value 0x02FF.
    Smb2Wildcard = 0x02FF,
}

impl Dialect {
    pub fn from_u16(v: u16) -> Option<Self> {
        Some(match v {
            0x0202 => Self::Smb202,
            0x0210 => Self::Smb210,
            0x0300 => Self::Smb300,
            0x0302 => Self::Smb302,
            0x0311 => Self::Smb311,
            0x02FF => Self::Smb2Wildcard,
            _ => return None,
        })
    }

    pub const fn as_u16(self) -> u16 {
        self as u16
    }
}

// ---------------------------------------------------------------------------
// Negotiate request
// ---------------------------------------------------------------------------

/// MS-SMB2 §2.2.3 NEGOTIATE Request.
///
/// `dialects` is a sequence of u16 little-endian dialect codes; for SMB 3.1.1
/// the trailing `negotiate_context_list` carries variable-length contexts at
/// `negotiate_context_offset`.
///
/// Note on parsing: we deliberately don't try to read `negotiate_context_list`
/// here automatically, because its position is given by an absolute offset
/// from the *start of the SMB2 header*, not from the start of this body.
/// The server crate decodes this body, then if `dialects` includes 3.1.1 it
/// resolves `negotiate_context_offset` against the original packet buffer
/// and parses the contexts via [`NegotiateContext::parse_list`].
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NegotiateRequest {
    pub structure_size: u16,
    pub dialect_count: u16,
    pub security_mode: u16,
    pub reserved: u16,
    pub capabilities: u32,
    pub client_guid: [u8; 16],
    /// 3.1.1: NegotiateContextOffset. 2.x/3.0/3.0.2: ClientStartTime.
    pub negotiate_context_offset_or_client_start_time: u64,
    #[br(count = dialect_count as usize)]
    pub dialects: Vec<u16>,
}

impl NegotiateRequest {
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
// Negotiate response
// ---------------------------------------------------------------------------

/// MS-SMB2 §2.2.4 NEGOTIATE Response.
///
/// The trailing `security_buffer` and (3.1.1) `negotiate_context_list` are
/// referenced by absolute offsets from the start of the SMB2 header. This
/// struct encodes the *fixed* portion plus a `security_buffer` that we treat
/// as a length-counted blob immediately following the fixed portion (the
/// common server layout). For 3.1.1 contexts, the server crate writes the
/// fixed portion via [`NegotiateResponse::write_to`], then appends 8-byte-
/// aligned negotiate contexts and patches `negotiate_context_offset` to the
/// post-padding offset.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NegotiateResponse {
    pub structure_size: u16,
    pub security_mode: u16,
    pub dialect_revision: u16,
    /// 3.1.1: NegotiateContextCount. 2.x/3.0/3.0.2: Reserved.
    pub negotiate_context_count_or_reserved: u16,
    pub server_guid: [u8; 16],
    pub capabilities: u32,
    pub max_transact_size: u32,
    pub max_read_size: u32,
    pub max_write_size: u32,
    /// 100ns ticks since 1601-01-01 UTC.
    pub system_time: u64,
    pub server_start_time: u64,
    pub security_buffer_offset: u16,
    pub security_buffer_length: u16,
    /// 3.1.1: NegotiateContextOffset. 2.x/3.0/3.0.2: Reserved2.
    pub negotiate_context_offset_or_reserved2: u32,
    #[br(count = security_buffer_length as usize)]
    pub security_buffer: Vec<u8>,
}

impl NegotiateResponse {
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
// Negotiate contexts (SMB 3.1.1)
// ---------------------------------------------------------------------------

/// MS-SMB2 §2.2.3.1 / §2.2.4.x — NEGOTIATE_CONTEXT generic header.
///
/// Contexts are 8-byte-aligned in the chain (the trailing padding is between
/// contexts; see §2.2.3.1 "Each NEGOTIATE_CONTEXT MUST be 8-byte aligned").
/// `parse_list` / `encode_list` handle the alignment.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NegotiateContext {
    pub context_type: u16,
    pub data_length: u16,
    pub reserved: u32,
    #[br(count = data_length as usize)]
    pub data: Vec<u8>,
}

impl NegotiateContext {
    pub const TYPE_PREAUTH_INTEGRITY: u16 = 0x0001;
    pub const TYPE_ENCRYPTION: u16 = 0x0002;
    pub const TYPE_COMPRESSION: u16 = 0x0003;
    pub const TYPE_NETNAME_NEGOTIATE: u16 = 0x0005;
    pub const TYPE_TRANSPORT_CAPS: u16 = 0x0006;
    pub const TYPE_RDMA_TRANSFORM: u16 = 0x0007;
    pub const TYPE_SIGNING: u16 = 0x0008;

    /// Parse a chain of negotiate contexts from `buf`. The chain is a series
    /// of (8-byte-aligned) [`NegotiateContext`] entries. `count` comes from
    /// the parent message's `NegotiateContextCount`.
    pub fn parse_list(mut buf: &[u8], count: u16) -> ProtoResult<Vec<NegotiateContext>> {
        let mut out = Vec::with_capacity(count as usize);
        let mut consumed_total = 0usize;
        for _ in 0..count {
            // Pad to 8-byte alignment relative to the start of the list.
            let pad = (8 - (consumed_total % 8)) % 8;
            if pad > 0 {
                if buf.len() < pad {
                    return Err(crate::proto::error::ProtoError::Malformed(
                        "negotiate context alignment underflow",
                    ));
                }
                buf = &buf[pad..];
                consumed_total += pad;
            }
            let mut c = Cursor::new(buf);
            let ctx = NegotiateContext::read(&mut c)?;
            let consumed = c.position() as usize;
            buf = &buf[consumed..];
            consumed_total += consumed;
            out.push(ctx);
        }
        Ok(out)
    }

    /// Encode a chain of negotiate contexts into `out`, inserting 8-byte
    /// padding between entries.
    pub fn encode_list(list: &[NegotiateContext], out: &mut Vec<u8>) -> ProtoResult<()> {
        let start = out.len();
        for (i, ctx) in list.iter().enumerate() {
            if i > 0 {
                let pad = (8 - ((out.len() - start) % 8)) % 8;
                out.extend(std::iter::repeat_n(0u8, pad));
            }
            let mut c = Cursor::new(Vec::new());
            BinWrite::write(ctx, &mut c)?;
            out.extend_from_slice(&c.into_inner());
        }
        Ok(())
    }
}

/// Parsed payload of a known [`NegotiateContext`] type. Convenience wrapper —
/// the wire form is always [`NegotiateContext`]; this enum is for callers who
/// prefer typed access.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NegotiateContextData {
    PreauthIntegrity(PreauthIntegrityCapabilities),
    Encryption(EncryptionCapabilities),
    Signing(SigningCapabilities),
    /// Unknown / unhandled context — preserve raw bytes for round-tripping.
    Other {
        context_type: u16,
        data: Vec<u8>,
    },
}

/// MS-SMB2 §2.2.3.1.1 / §2.2.4.1 SMB2_PREAUTH_INTEGRITY_CAPABILITIES.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreauthIntegrityCapabilities {
    pub hash_algorithm_count: u16,
    pub salt_length: u16,
    #[br(count = hash_algorithm_count as usize)]
    pub hash_algorithms: Vec<u16>,
    #[br(count = salt_length as usize)]
    pub salt: Vec<u8>,
}

impl PreauthIntegrityCapabilities {
    /// Hash algorithm: SHA-512 (the only one defined in MS-SMB2 §2.2.3.1.1).
    pub const HASH_SHA512: u16 = 0x0001;
}

/// MS-SMB2 §2.2.3.1.2 / §2.2.4.2 SMB2_ENCRYPTION_CAPABILITIES.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncryptionCapabilities {
    pub cipher_count: u16,
    #[br(count = cipher_count as usize)]
    pub ciphers: Vec<u16>,
}

impl EncryptionCapabilities {
    pub const CIPHER_AES_128_CCM: u16 = 0x0001;
    pub const CIPHER_AES_128_GCM: u16 = 0x0002;
    pub const CIPHER_AES_256_CCM: u16 = 0x0003;
    pub const CIPHER_AES_256_GCM: u16 = 0x0004;
}

/// MS-SMB2 §2.2.3.1.7 / §2.2.4.7 SMB2_SIGNING_CAPABILITIES.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SigningCapabilities {
    pub signing_algorithm_count: u16,
    #[br(count = signing_algorithm_count as usize)]
    pub signing_algorithms: Vec<u16>,
}

impl SigningCapabilities {
    pub const ALGORITHM_HMAC_SHA256: u16 = 0x0000;
    pub const ALGORITHM_AES_CMAC: u16 = 0x0001;
    pub const ALGORITHM_AES_GMAC: u16 = 0x0002;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn negotiate_request_round_trips() {
        let req = NegotiateRequest {
            structure_size: 36,
            dialect_count: 5,
            security_mode: 0x0001, // signing enabled
            reserved: 0,
            capabilities: 0x0000_007F,
            client_guid: [0xAB; 16],
            negotiate_context_offset_or_client_start_time: 0x0000_0070_0000_0000,
            dialects: vec![0x0202, 0x0210, 0x0300, 0x0302, 0x0311],
        };
        let mut buf = Vec::new();
        req.write_to(&mut buf).unwrap();
        let decoded = NegotiateRequest::parse(&buf).unwrap();
        assert_eq!(decoded, req);
    }

    #[test]
    fn negotiate_response_round_trips() {
        let resp = NegotiateResponse {
            structure_size: 65,
            security_mode: 0x0003,
            dialect_revision: Dialect::Smb311.as_u16(),
            negotiate_context_count_or_reserved: 3,
            server_guid: [0xCD; 16],
            capabilities: 0x0000_007F,
            max_transact_size: 0x0010_0000,
            max_read_size: 0x0010_0000,
            max_write_size: 0x0010_0000,
            system_time: 0x01D9_1234_5678_9ABC,
            server_start_time: 0,
            security_buffer_offset: 0x80,
            security_buffer_length: 8,
            negotiate_context_offset_or_reserved2: 0x100,
            security_buffer: vec![1, 2, 3, 4, 5, 6, 7, 8],
        };
        let mut buf = Vec::new();
        resp.write_to(&mut buf).unwrap();
        let decoded = NegotiateResponse::parse(&buf).unwrap();
        assert_eq!(decoded, resp);
    }

    #[test]
    fn dialect_round_trips() {
        for d in [
            Dialect::Smb202,
            Dialect::Smb210,
            Dialect::Smb300,
            Dialect::Smb302,
            Dialect::Smb311,
            Dialect::Smb2Wildcard,
        ] {
            assert_eq!(Dialect::from_u16(d.as_u16()), Some(d));
        }
        assert_eq!(Dialect::from_u16(0xBEEF), None);
    }

    #[test]
    fn preauth_caps_round_trips() {
        let p = PreauthIntegrityCapabilities {
            hash_algorithm_count: 1,
            salt_length: 32,
            hash_algorithms: vec![PreauthIntegrityCapabilities::HASH_SHA512],
            salt: vec![0xAA; 32],
        };
        let mut buf = Vec::new();
        let mut c = Cursor::new(&mut buf);
        BinWrite::write(&p, &mut c).unwrap();
        let decoded = PreauthIntegrityCapabilities::read(&mut Cursor::new(&buf)).unwrap();
        assert_eq!(decoded, p);
    }

    #[test]
    fn negotiate_context_list_round_trips() {
        let list = vec![
            NegotiateContext {
                context_type: NegotiateContext::TYPE_PREAUTH_INTEGRITY,
                data_length: 6,
                reserved: 0,
                data: vec![0x01, 0x00, 0x20, 0x00, 0x01, 0x00],
            },
            NegotiateContext {
                context_type: NegotiateContext::TYPE_ENCRYPTION,
                data_length: 4,
                reserved: 0,
                data: vec![0x02, 0x00, 0x02, 0x00],
            },
            NegotiateContext {
                context_type: NegotiateContext::TYPE_SIGNING,
                data_length: 4,
                reserved: 0,
                data: vec![0x01, 0x00, 0x01, 0x00],
            },
        ];
        let mut buf = Vec::new();
        NegotiateContext::encode_list(&list, &mut buf).unwrap();
        let parsed = NegotiateContext::parse_list(&buf, 3).unwrap();
        assert_eq!(parsed, list);
    }
}
