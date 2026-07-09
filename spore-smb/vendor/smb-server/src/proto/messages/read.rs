//! READ Request/Response (MS-SMB2 §2.2.19 / §2.2.20).
//!
//! ## Data buffer offsets
//!
//! Both the READ request `ReadChannelInfoOffset` and the READ response
//! `DataOffset` are measured from the **start of the SMB2 header**, not from
//! the start of this structure (MS-SMB2 §2.2.20 explicitly: "DataOffset (1
//! byte): The offset, in bytes, from the beginning of the SMB2 header to the
//! data being read"). When constructing a response, the server crate must
//! compute `DataOffset = SMB2_HEADER_LEN + offset_within_body_of_data`.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// SMB2_READ_REQUEST (MS-SMB2 §2.2.19).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadRequest {
    pub structure_size: u16,
    pub padding: u8,
    /// 3.0+ flags (`SMB2_READFLAG_*`); reserved on 2.x.
    pub flags: u8,
    pub length: u32,
    pub offset: u64,
    pub file_id: FileId,
    pub minimum_count: u32,
    pub channel: u32,
    pub remaining_bytes: u32,
    pub read_channel_info_offset: u16,
    pub read_channel_info_length: u16,
    /// MS-SMB2: "If ReadChannelInfoOffset and ReadChannelInfoLength are both
    /// 0, the client MUST set this field to a single 0 byte." We follow that
    /// — at least one byte of buffer is required on the wire.
    #[br(count = if read_channel_info_length == 0 { 1 } else { read_channel_info_length as usize })]
    pub buffer: Vec<u8>,
}

impl ReadRequest {
    /// Flag: SMB2_READFLAG_READ_UNBUFFERED (3.0.2+).
    pub const FLAG_READ_UNBUFFERED: u8 = 0x01;
    /// Flag: SMB2_READFLAG_REQUEST_COMPRESSED (3.1.1+).
    pub const FLAG_REQUEST_COMPRESSED: u8 = 0x02;

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

/// SMB2_READ_RESPONSE (MS-SMB2 §2.2.20).
///
/// `data_offset` is from the start of the SMB2 header. Use
/// [`ReadResponse::standard_data_offset`] for the canonical "data immediately
/// after the fixed prefix" layout.
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadResponse {
    pub structure_size: u16,
    pub data_offset: u8,
    pub reserved: u8,
    pub data_length: u32,
    pub data_remaining: u32,
    /// 3.x: `Flags`. 2.x: reserved.
    pub flags: u32,
    #[br(count = data_length as usize)]
    pub data: Vec<u8>,
}

impl ReadResponse {
    /// Canonical `DataOffset` value when the data buffer immediately follows
    /// the fixed 16-byte response prefix and the SMB2 header (64 + 16 = 80).
    ///
    /// Most servers (ksmbd, Samba) emit 0x50 = 80 here.
    pub const STANDARD_DATA_OFFSET: u8 = 0x50;

    pub const fn standard_data_offset() -> u8 {
        Self::STANDARD_DATA_OFFSET
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
        let r = ReadRequest {
            structure_size: 49,
            padding: 0x50,
            flags: 0,
            length: 0x1000,
            offset: 0x2000,
            file_id: FileId::new(0xAAAA, 0xBBBB),
            minimum_count: 1,
            channel: 0,
            remaining_bytes: 0,
            read_channel_info_offset: 0,
            read_channel_info_length: 0,
            buffer: vec![0],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(ReadRequest::parse(&buf).unwrap(), r);
    }

    #[test]
    fn response_round_trips() {
        let r = ReadResponse {
            structure_size: 17,
            data_offset: ReadResponse::STANDARD_DATA_OFFSET,
            reserved: 0,
            data_length: 5,
            data_remaining: 0,
            flags: 0,
            data: vec![1, 2, 3, 4, 5],
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(ReadResponse::parse(&buf).unwrap(), r);
    }
}
