//! OPLOCK_BREAK Notification + Acknowledgement (MS-SMB2 §2.2.23 / §2.2.24).
//!
//! V1 never grants oplocks, so we never *send* a notification, but the
//! handler exists for safety. A client may send an OPLOCK_BREAK ACK before
//! the server has cleared its oplock state in the (rare) edge case during
//! teardown.

use binrw::{BinRead, BinWrite, binrw};
use std::io::Cursor;

use super::create::FileId;
use crate::proto::error::ProtoResult;

/// SMB2_OPLOCK_BREAK_NOTIFICATION (MS-SMB2 §2.2.23.1).
#[binrw]
#[brw(little)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OplockBreakNotification {
    pub structure_size: u16,
    pub oplock_level: u8,
    pub reserved: u8,
    pub reserved2: u32,
    pub file_id: FileId,
}

impl OplockBreakNotification {
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

/// SMB2_OPLOCK_BREAK_ACK (MS-SMB2 §2.2.24.1) — same wire shape as the
/// notification.
pub type OplockBreakAck = OplockBreakNotification;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        let r = OplockBreakNotification {
            structure_size: 24,
            oplock_level: 0,
            reserved: 0,
            reserved2: 0,
            file_id: FileId::new(1, 2),
        };
        let mut buf = Vec::new();
        r.write_to(&mut buf).unwrap();
        assert_eq!(OplockBreakNotification::parse(&buf).unwrap(), r);
    }
}
