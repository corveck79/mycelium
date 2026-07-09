//! SP 800-108 CTR-mode KDF using HMAC-SHA-256, as required by MS-SMB2 §3.1.4.2.
//!
//! Fixed input fed to the PRF (HMAC-SHA-256) is:
//!
//! ```text
//! i (u32be=1) || Label || 0x00 || Context || L (u32be=128)
//! ```
//!
//! Convention in this crate:
//! * Callers pass `label` and `context` *already including* a trailing `\x00`.
//! * The KDF then **also** emits a single `0x00` separator between `label`
//!   and `context`, so the wire-level input has two consecutive NULs at that
//!   boundary. This matches what real Windows clients require — a single NUL
//!   produces a different signing key and Windows rejects with
//!   `STATUS_ACCESS_DENIED` / event 31013 "signing validation failed".

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// SP 800-108 CTR-mode KDF using HMAC-SHA-256.
///
/// * `key` — the input key (session key, typically 16 bytes).
/// * `label` — the label string with trailing NUL (e.g. `b"SMB2AESCMAC\x00"`).
/// * `context` — the context string with trailing NUL (e.g. `b"SmbSign\x00"`).
///
/// Returns the first 16 bytes of `HMAC-SHA-256(key, fixed_input)` where
/// `fixed_input = [0,0,0,1] || label || 0x00 || context || [0,0,0,0x80]`.
/// The single separator `0x00` between `label` and `context` is required for
/// Windows interop; do not remove.
pub fn smb2_kdf(key: &[u8], label: &[u8], context: &[u8]) -> [u8; 16] {
    let mut mac =
        <HmacSha256 as Mac>::new_from_slice(key).expect("HMAC-SHA-256 accepts keys of any length");

    // i = 1 (big-endian u32)
    mac.update(&[0x00, 0x00, 0x00, 0x01]);
    // Label (including trailing NUL provided by caller)
    mac.update(label);
    // SP 800-108 separator byte between Label and Context (in addition to any
    // trailing NUL the caller already included in `label`).
    mac.update(&[0x00]);
    // Context (including trailing NUL provided by caller, or for 3.1.1 the
    // 64-byte preauth hash)
    mac.update(context);
    // L = 128 bits (big-endian u32)
    mac.update(&[0x00, 0x00, 0x00, 0x80]);

    let full = mac.finalize().into_bytes();
    let mut out = [0u8; 16];
    out.copy_from_slice(&full[..16]);
    out
}

// --- Convenience helpers ---------------------------------------------------

/// Signing key for SMB 3.0 / 3.0.2.
///
/// Label = `"SMB2AESCMAC\x00"`, Context = `"SmbSign\x00"` (MS-SMB2 §3.1.4.2).
pub fn signing_key_30(session_key: &[u8]) -> [u8; 16] {
    smb2_kdf(session_key, b"SMB2AESCMAC\x00", b"SmbSign\x00")
}

/// Signing key for SMB 3.1.1.
///
/// Label = `"SMBSigningKey\x00"`, Context = pre-auth integrity hash
/// (the SHA-512 snapshot taken at SESSION_SETUP completion).
pub fn signing_key_311(session_key: &[u8], preauth_hash: &[u8; 64]) -> [u8; 16] {
    smb2_kdf(session_key, b"SMBSigningKey\x00", preauth_hash)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Determinism / shape sanity: the function always produces 16 bytes and
    /// is reproducible for the same inputs.
    #[test]
    fn smb2_kdf_is_deterministic() {
        let key = [0x11u8; 16];
        let a = smb2_kdf(&key, b"SMB2AESCMAC\x00", b"SmbSign\x00");
        let b = smb2_kdf(&key, b"SMB2AESCMAC\x00", b"SmbSign\x00");
        assert_eq!(a, b);
        assert_eq!(a.len(), 16);
    }

    /// Different label or context → different output.
    #[test]
    fn smb2_kdf_label_and_context_matter() {
        let key = [0x42u8; 16];
        let signing = smb2_kdf(&key, b"SMB2AESCMAC\x00", b"SmbSign\x00");
        let app = smb2_kdf(&key, b"SMB2APP\x00", b"SmbRpc\x00");
        assert_ne!(signing, app);

        let other_ctx = smb2_kdf(&key, b"SMB2AESCMAC\x00", b"OtherCtx\x00");
        assert_ne!(signing, other_ctx);
    }

    /// Known-answer test computed directly from the documented fixed-input
    /// construction. This pins the exact byte layout we feed to HMAC.
    ///
    /// Reference computation (Python):
    /// ```text
    /// import hmac, hashlib
    /// key = bytes(16)  # all zeros
    /// label = b"SMB2AESCMAC\x00"
    /// context = b"SmbSign\x00"
    /// data = b"\x00\x00\x00\x01" + label + b"\x00" + context + b"\x00\x00\x00\x80"
    /// hmac.new(key, data, hashlib.sha256).hexdigest()[:32]
    /// # = "9951088b83220f39d99420419d16d393"
    /// ```
    #[test]
    fn smb2_kdf_known_answer_zero_key_signing_30() {
        let key = [0u8; 16];
        let out = signing_key_30(&key);
        let expected = hex::decode("9951088b83220f39d99420419d16d393").unwrap();
        assert_eq!(out.as_slice(), expected.as_slice());
    }

    /// 3.1.1 derivation differs from 3.0 (different label, 64-byte context).
    #[test]
    fn smb2_kdf_311_differs_from_30() {
        let key = [0u8; 16];
        let preauth = [0u8; 64];
        let k30 = signing_key_30(&key);
        let k311 = signing_key_311(&key, &preauth);
        assert_ne!(k30, k311);
    }

    /// Known-answer test for 3.1.1 with zero key and zero pre-auth hash.
    ///
    /// Reference computation (Python):
    /// ```text
    /// data = b"\x00\x00\x00\x01" + b"SMBSigningKey\x00" + b"\x00" + bytes(64) + b"\x00\x00\x00\x80"
    /// hmac.new(bytes(16), data, hashlib.sha256).hexdigest()[:32]
    /// # = "a06a153e09bd0f34706a5c671acaa37d"
    /// ```
    #[test]
    fn smb2_kdf_known_answer_zero_key_signing_311() {
        let key = [0u8; 16];
        let preauth = [0u8; 64];
        let out = signing_key_311(&key, &preauth);
        let expected = hex::decode("a06a153e09bd0f34706a5c671acaa37d").unwrap();
        assert_eq!(out.as_slice(), expected.as_slice());
    }
}
