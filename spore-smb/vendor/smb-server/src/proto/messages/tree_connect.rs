//! TREE_CONNECT Request/Response (MS-SMB2 §2.2.9 / §2.2.10).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

/// SMB2_TREE_CONNECT_REQUEST (MS-SMB2 §2.2.9).
///
/// `path` is UTF-16LE. The wire format gives `PathOffset` (from the start of
/// the SMB2 header) and `PathLength`; we encode/decode the path immediately
/// following the fixed prefix. The 3.1.1 tree-connect-context machinery
/// (extension `flags`, `path_offset`/`path_length` interpretation) is
/// preserved on the wire and the server crate inspects `flags` if needed.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeConnectRequest {
    pub structure_size: u16,
    /// 3.1.1: flags. 2.x/3.0/3.0.2: reserved.
    pub flags: u16,
    pub path_offset: u16,
    pub path_length: u16,
    /// UTF-16LE share path bytes (e.g. `\\server\share`).
    #[br(count = path_length as usize)]
    pub path: Vec<u8>,
}

impl TreeConnectRequest {
    /// Flag: SMB2_TREE_CONNECT_FLAG_CLUSTER_RECONNECT (3.1.1).
    pub const FLAG_CLUSTER_RECONNECT: u16 = 0x0001;
    /// Flag: SMB2_TREE_CONNECT_FLAG_REDIRECT_TO_OWNER (3.1.1).
    pub const FLAG_REDIRECT_TO_OWNER: u16 = 0x0002;
    /// Flag: SMB2_TREE_CONNECT_FLAG_EXTENSION_PRESENT (3.1.1).
    pub const FLAG_EXTENSION_PRESENT: u16 = 0x0004;

    /// Decode the UTF-16LE share path into a `String`. Returns `None` if the
    /// stored bytes are not an even length (malformed UTF-16LE).
    pub fn path_str(&self) -> Option<String> {
        if !self.path.len().is_multiple_of(2) {
            return None;
        }
        let units: Vec<u16> = self
            .path
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

/// SMB2_TREE_CONNECT_RESPONSE (MS-SMB2 §2.2.10).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeConnectResponse {
    pub structure_size: u16,
    pub share_type: u8,
    pub reserved: u8,
    pub share_flags: u32,
    pub capabilities: u32,
    pub maximal_access: u32,
}

impl TreeConnectResponse {
    /// Share type: SMB2_SHARE_TYPE_DISK.
    pub const SHARE_TYPE_DISK: u8 = 0x01;
    pub const SHARE_TYPE_PIPE: u8 = 0x02;
    pub const SHARE_TYPE_PRINT: u8 = 0x03;

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

#[cfg(test)]
mod tests {
    use super::*;

    fn utf16le(s: &str) -> Vec<u8> {
        s.encode_utf16().flat_map(u16::to_le_bytes).collect()
    }

    #[test]
    fn request_round_trips() {
        let path = utf16le(r"\\server\share");
        let r = TreeConnectRequest {
            structure_size: 9,
            flags: 0,
            path_offset: 0x48,
            path_length: path.len() as u16,
            path,
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        let decoded = TreeConnectRequest::parse(&buf).unwrap();
        assert_eq!(decoded, r);
        assert_eq!(decoded.path_str().unwrap(), r"\\server\share");
    }

    #[test]
    fn response_round_trips() {
        let r = TreeConnectResponse {
            structure_size: 16,
            share_type: TreeConnectResponse::SHARE_TYPE_DISK,
            reserved: 0,
            share_flags: 0,
            capabilities: 0,
            maximal_access: 0x001F_01FF,
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(TreeConnectResponse::parse(&buf).unwrap(), r);
    }
}
