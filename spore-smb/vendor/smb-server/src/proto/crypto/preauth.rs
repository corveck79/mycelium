//! SMB 3.1.1 pre-auth integrity (MS-SMB2 §3.1.4.4.1, §3.3.5.4).
//!
//! A running SHA-512 hash, initialized to all zeros, that absorbs SMB 3.1.1
//! preauth messages (transport prefix excluded). Connection state uses this for
//! NEGOTIATE; each SESSION_SETUP exchange forks its own instance. Per spec:
//!
//! ```text
//! PreauthIntegrityHashValue =
//!     SHA-512(PreauthIntegrityHashValue || RequestOrResponse)
//! ```

use sha2::{Digest, Sha512};

/// Running SMB 3.1.1 preauth integrity hash.
#[derive(Debug, Clone)]
pub struct PreauthIntegrity {
    hash: [u8; 64],
}

impl Default for PreauthIntegrity {
    fn default() -> Self {
        Self::new()
    }
}

impl PreauthIntegrity {
    /// Create a fresh state, hash initialized to all zeros.
    pub fn new() -> Self {
        Self { hash: [0u8; 64] }
    }

    /// Absorb a frame's bytes (excluding the 4-byte Direct-TCP transport
    /// prefix). Updates `hash` in place.
    pub fn update(&mut self, frame: &[u8]) {
        let mut hasher = Sha512::new();
        hasher.update(self.hash);
        hasher.update(frame);
        let out = hasher.finalize();
        self.hash.copy_from_slice(&out);
    }

    /// Take a copy of the current hash. Used as the KDF context for session
    /// keys at SESSION_SETUP completion.
    pub fn snapshot(&self) -> [u8; 64] {
        self.hash
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha512};

    #[test]
    fn new_starts_at_zero() {
        let p = PreauthIntegrity::new();
        assert_eq!(p.snapshot(), [0u8; 64]);
    }

    #[test]
    fn default_starts_at_zero() {
        let p = PreauthIntegrity::default();
        assert_eq!(p.snapshot(), [0u8; 64]);
    }

    /// Two-step chain matches the literal spec formula.
    #[test]
    fn chain_two_buffers_matches_precomputed() {
        let mut p = PreauthIntegrity::new();

        let buf1 = b"NEGOTIATE_REQUEST_FIXTURE";
        let buf2 = b"NEGOTIATE_RESPONSE_FIXTURE";
        p.update(buf1);
        p.update(buf2);

        // Precomputed using Python:
        //   h = bytes(64)
        //   h = sha512(h + buf1).digest()
        //   h = sha512(h + buf2).digest()
        let expected = hex::decode(
            "62deb17d9d07d155b7c634dbfec3ac10c32b80981d925333499a6fbd168d0ee3\
             4d29b093a185529fd927ade8d851c8e8b0d9b55608c7674e4d3e8d438343c95c",
        )
        .unwrap();
        assert_eq!(p.snapshot().as_slice(), expected.as_slice());
    }

    /// Chained call equivalence: explicit SHA-512(prev || frame) on the side
    /// must match what `update` produces internally.
    #[test]
    fn update_equals_manual_sha512() {
        let buf = b"SOME_FRAME_BYTES_HERE_0123456789";

        let mut p = PreauthIntegrity::new();
        p.update(buf);

        let mut hasher = Sha512::new();
        hasher.update([0u8; 64]);
        hasher.update(buf);
        let manual = hasher.finalize();

        assert_eq!(p.snapshot().as_slice(), manual.as_slice());
    }

    /// Snapshot must not be aliased — modifying state after snapshot must not
    /// affect the snapshot already taken.
    #[test]
    fn snapshot_is_a_copy() {
        let mut p = PreauthIntegrity::new();
        p.update(b"first");
        let snap = p.snapshot();
        p.update(b"second");
        assert_ne!(p.snapshot(), snap);
    }
}
