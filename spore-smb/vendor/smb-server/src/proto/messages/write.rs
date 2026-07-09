//! WRITE Request/Response (MS-SMB2 §2.2.21 / §2.2.22).
//!
//! ## Data buffer offsets
//!
//! `DataOffset` is from the **start of the SMB2 header**, not from the start
//! of this structure (MS-SMB2 §2.2.21). The canonical layout puts the data
//! immediately after the fixed 48-byte prefix, giving 64 + 48 = 112 = 0x70.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// SMB2_WRITE_REQUEST (MS-SMB2 §2.2.21).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteRequest {
    pub structure_size: u16,
    pub data_offset: u16,
    pub length: u32,
    pub offset: u64,
    pub file_id: FileId,
    pub channel: u32,
    pub remaining_bytes: u32,
    pub write_channel_info_offset: u16,
    pub write_channel_info_length: u16,
    pub flags: u32,
    /// MS-SMB2: at least 1 byte of payload buffer is required on the wire
    /// even when length=0.
    #[br(count = if length == 0 { 1 } else { length as usize })]
    pub data: Vec<u8>,
}

impl WriteRequest {
    /// Canonical `DataOffset` placing the data buffer immediately after the
    /// fixed 48-byte WRITE prefix: 64 (SMB2 header) + 48 = 112 = 0x70.
    pub const STANDARD_DATA_OFFSET: u16 = 0x70;
    /// Flag: SMB2_WRITEFLAG_WRITE_THROUGH.
    pub const FLAG_WRITE_THROUGH: u32 = 0x0000_0001;
    /// Flag: SMB2_WRITEFLAG_WRITE_UNBUFFERED (3.0.2+).
    pub const FLAG_WRITE_UNBUFFERED: u32 = 0x0000_0002;

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

/// SMB2_WRITE_RESPONSE (MS-SMB2 §2.2.22).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct WriteResponse {
    pub structure_size: u16,
    pub reserved: u16,
    pub count: u32,
    pub remaining: u32,
    pub write_channel_info_offset: u16,
    pub write_channel_info_length: u16,
}

impl WriteResponse {
    pub fn new(count: u32) -> Self {
        Self {
            structure_size: 17,
            reserved: 0,
            count,
            remaining: 0,
            write_channel_info_offset: 0,
            write_channel_info_length: 0,
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_round_trips() {
        let r = WriteRequest {
            structure_size: 49,
            data_offset: WriteRequest::STANDARD_DATA_OFFSET,
            length: 4,
            offset: 0x100,
            file_id: FileId::new(0xAA, 0xBB),
            channel: 0,
            remaining_bytes: 0,
            write_channel_info_offset: 0,
            write_channel_info_length: 0,
            flags: 0,
            data: vec![1, 2, 3, 4],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(WriteRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = WriteResponse::new(0x1000);
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(WriteResponse::parse(&buf).unwrap(), r);
    }
}
