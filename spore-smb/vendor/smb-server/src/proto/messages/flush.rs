//! FLUSH Request/Response (MS-SMB2 §2.2.17 / §2.2.18).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlushRequest {
    pub structure_size: u16,
    pub reserved1: u16,
    pub reserved2: u32,
    /// Volatile portion of the FileId.
    pub file_id_persistent: u64,
    /// Persistent portion of the FileId.
    pub file_id_volatile: u64,
}

impl FlushRequest {
    pub fn new(persistent: u64, volatile: u64) -> Self {
        Self {
            structure_size: 24,
            reserved1: 0,
            reserved2: 0,
            file_id_persistent: persistent,
            file_id_volatile: volatile,
        }
    }
}

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlushResponse {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for FlushResponse {
    fn default() -> Self {
        Self {
            structure_size: 4,
            reserved: 0,
        }
    }
}

macro_rules! impl_codec {
    ($t:ty) => {
        impl $t {
            pub fn parse(buf: &[u8]) -> ProtoResult<Self> {
                Ok(<Self as BinRead>::read(&mut Cursor::new(buf))?)
            }
            pub fn write_to(&self, out: &mut Vec<u8>) -> ProtoResult<()> {
                let mut c = Cursor::new(Vec::new());
                BinWrite::write(self, &mut c)?;
                out.extend_from_slice(&c.into_inner());
                Ok(())
            }
        }
    };
}

impl_codec!(FlushRequest);
impl_codec!(FlushResponse);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        let r = FlushRequest::new(0x1122_3344_5566_7788, 0xAABB_CCDD_EEFF_0011);
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(buf.len(), 24);
        assert_eq!(FlushRequest::parse(&buf).unwrap(), r);

        let r = FlushResponse::default();
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(FlushResponse::parse(&buf).unwrap(), r);
    }
}
