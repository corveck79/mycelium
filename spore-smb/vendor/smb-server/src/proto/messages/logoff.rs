//! LOGOFF Request/Response (MS-SMB2 §2.2.7 / §2.2.8).

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use crate::proto::error::ProtoResult;

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogoffRequest {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for LogoffRequest {
    fn default() -> Self {
        Self {
            structure_size: 4,
            reserved: 0,
        }
    }
}

#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogoffResponse {
    pub structure_size: u16,
    pub reserved: u16,
}

impl Default for LogoffResponse {
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

impl_codec!(LogoffRequest);
impl_codec!(LogoffResponse);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        let r = LogoffRequest::default();
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(LogoffRequest::parse(&buf).unwrap(), r);

        let r = LogoffResponse::default();
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(LogoffResponse::parse(&buf).unwrap(), r);
    }
}
