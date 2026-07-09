//! SMB2/3 message signing per MS-SMB2 §3.1.4.1.
//!
//! Two algorithms are supported:
//! 1. **HMAC-SHA-256** for SMB 2.0.2 / 2.1 / 3.0 negotiating without 3.x
//!    signing.
//! 2. **AES-CMAC** for SMB 3.0+.
//!
//! Both produce a 16-byte signature that lives at bytes 48..64 of the SMB2
//! header (the `Signature` field, MS-SMB2 §2.2.1.2).
//!
//! Algorithm:
//! 1. Zero out bytes 48..64 of the message.
//! 2. Compute MAC over the **entire** message (header + body).
//! 3. Place the first 16 bytes of MAC at bytes 48..64.

use aes::Aes128;
use cmac::Cmac;
use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::proto::error::{ProtoError, ProtoResult};

type HmacSha256 = Hmac<Sha256>;
type CmacAes128 = Cmac<Aes128>;

/// SMB2 header is 64 bytes; the 16-byte signature field starts at offset 48.
const SIG_OFF: usize = 48;
const SIG_LEN: usize = 16;
const SMB2_HEADER_LEN: usize = 64;

/// Which signing algorithm to use for a given session/dialect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SigningAlgo {
    /// HMAC-SHA-256, used by SMB 2.x.
    HmacSha256,
    /// AES-CMAC over AES-128, used by SMB 3.0+.
    AesCmac,
}

/// Compute the 16-byte MAC over `msg` as if the SMB2 signature field were
/// zeroed, without copying the whole message.
fn compute_mac_zeroed_signature(msg: &[u8], key: &[u8; 16], algo: SigningAlgo) -> [u8; SIG_LEN] {
    let mut out = [0u8; SIG_LEN];
    let zero_signature = [0u8; SIG_LEN];
    let prefix = &msg[..SIG_OFF];
    let suffix = &msg[SIG_OFF + SIG_LEN..];

    match algo {
        SigningAlgo::HmacSha256 => {
            let mut mac = <HmacSha256 as Mac>::new_from_slice(key)
                .expect("HMAC-SHA-256 accepts keys of any length");
            mac.update(prefix);
            mac.update(&zero_signature);
            mac.update(suffix);
            let full = mac.finalize().into_bytes();
            out.copy_from_slice(&full[..SIG_LEN]);
        }
        SigningAlgo::AesCmac => {
            let mut mac = <CmacAes128 as Mac>::new_from_slice(key)
                .expect("AES-128-CMAC requires a 16-byte key, which we have");
            mac.update(prefix);
            mac.update(&zero_signature);
            mac.update(suffix);
            let full = mac.finalize().into_bytes();
            out.copy_from_slice(&full[..SIG_LEN]);
        }
    }
    out
}

/// Compute and embed a signature in `msg`. Mutates `msg` in place.
///
/// The caller is responsible for setting the SMB2 SIGNED flag (`0x00000008`)
/// on the header *before* calling — it is part of the bytes that get MAC'd.
///
/// Errors if `msg` is too short to contain an SMB2 header (< 64 bytes).
pub fn sign(msg: &mut [u8], key: &[u8; 16], algo: SigningAlgo) -> ProtoResult<()> {
    if msg.len() < SMB2_HEADER_LEN {
        return Err(ProtoError::Crypto("message too short to sign"));
    }

    // Compute MAC over the whole message with the signature field treated as
    // zero, then place the MAC into the signature field.
    let mac = compute_mac_zeroed_signature(msg, key, algo);
    msg[SIG_OFF..SIG_OFF + SIG_LEN].copy_from_slice(&mac);

    Ok(())
}

/// Verify the signature in `msg`. Does **not** modify `msg`.
///
/// Uses constant-time comparison. Returns `Ok(())` if the embedded signature
/// matches the freshly computed MAC.
pub fn verify(msg: &[u8], key: &[u8; 16], algo: SigningAlgo) -> ProtoResult<()> {
    if msg.len() < SMB2_HEADER_LEN {
        return Err(ProtoError::Crypto("message too short to verify"));
    }

    // Capture the embedded signature.
    let mut embedded = [0u8; SIG_LEN];
    embedded.copy_from_slice(&msg[SIG_OFF..SIG_OFF + SIG_LEN]);

    let computed = compute_mac_zeroed_signature(msg, key, algo);

    if constant_time_eq(&embedded, &computed) {
        Ok(())
    } else {
        Err(ProtoError::Crypto("signature mismatch"))
    }
}

/// Constant-time comparison of two 16-byte arrays.
#[inline]
fn constant_time_eq(a: &[u8; SIG_LEN], b: &[u8; SIG_LEN]) -> bool {
    let mut diff: u8 = 0;
    for i in 0..SIG_LEN {
        diff |= a[i] ^ b[i];
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a 100-byte message: a plausible 64-byte SMB2 header followed by
    /// 36 bytes of body. The signature region (bytes 48..64) is left zero;
    /// `sign` will overwrite it.
    fn fixture_message() -> Vec<u8> {
        let mut msg = vec![0u8; 100];
        // Magic: 0xFE 'S' 'M' 'B'
        msg[0..4].copy_from_slice(&[0xFE, b'S', b'M', b'B']);
        // StructureSize = 64
        msg[4..6].copy_from_slice(&64u16.to_le_bytes());
        // Pretend ChannelSequence = 0
        msg[6..8].copy_from_slice(&0u16.to_le_bytes());
        // Command = NEGOTIATE (0)
        msg[12..14].copy_from_slice(&0u16.to_le_bytes());
        // Flags: SIGNED (0x00000008)
        msg[16..20].copy_from_slice(&0x0000_0008u32.to_le_bytes());
        // Body filler
        for (i, b) in msg[64..].iter_mut().enumerate() {
            *b = (i as u8).wrapping_mul(7);
        }
        msg
    }

    #[test]
    fn sign_and_verify_hmac_sha256() {
        let key = [0xAAu8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::HmacSha256).expect("sign ok");

        // Signature should now be non-zero (overwhelmingly likely).
        assert_ne!(&msg[SIG_OFF..SIG_OFF + SIG_LEN], &[0u8; 16]);

        verify(&msg, &key, SigningAlgo::HmacSha256).expect("verify ok");
    }

    #[test]
    fn sign_and_verify_aes_cmac() {
        let key = [0x55u8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::AesCmac).expect("sign ok");
        assert_ne!(&msg[SIG_OFF..SIG_OFF + SIG_LEN], &[0u8; 16]);
        verify(&msg, &key, SigningAlgo::AesCmac).expect("verify ok");
    }

    #[test]
    fn tamper_outside_sig_fails_verify_hmac() {
        let key = [0xAAu8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::HmacSha256).expect("sign ok");

        // Flip one body byte.
        msg[80] ^= 0x01;
        let res = verify(&msg, &key, SigningAlgo::HmacSha256);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
    }

    #[test]
    fn tamper_outside_sig_fails_verify_cmac() {
        let key = [0x55u8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::AesCmac).expect("sign ok");

        // Flip a header byte (not in the sig region).
        msg[10] ^= 0xFF;
        let res = verify(&msg, &key, SigningAlgo::AesCmac);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
    }

    #[test]
    fn tamper_signature_fails_verify() {
        let key = [0xAAu8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::HmacSha256).expect("sign ok");
        msg[SIG_OFF] ^= 0x01;
        let res = verify(&msg, &key, SigningAlgo::HmacSha256);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
    }

    #[test]
    fn wrong_key_fails_verify() {
        let key = [0xAAu8; 16];
        let bad_key = [0xBBu8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::HmacSha256).expect("sign ok");
        let res = verify(&msg, &bad_key, SigningAlgo::HmacSha256);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
    }

    #[test]
    fn too_short_message_errors() {
        let mut tiny = [0u8; 10];
        let key = [0u8; 16];
        let res = sign(&mut tiny, &key, SigningAlgo::HmacSha256);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
        let res = verify(&tiny, &key, SigningAlgo::HmacSha256);
        assert!(matches!(res, Err(ProtoError::Crypto(_))));
    }

    #[test]
    fn verify_does_not_mutate_message_hmac_sha256() {
        let key = [0xAAu8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::HmacSha256).expect("sign ok");
        let snapshot = msg.clone();
        verify(&msg, &key, SigningAlgo::HmacSha256).expect("verify ok");
        assert_eq!(msg, snapshot);
    }

    #[test]
    fn verify_does_not_mutate_message_aes_cmac() {
        let key = [0x55u8; 16];
        let mut msg = fixture_message();
        sign(&mut msg, &key, SigningAlgo::AesCmac).expect("sign ok");
        let snapshot = msg.clone();
        verify(&msg, &key, SigningAlgo::AesCmac).expect("verify ok");
        assert_eq!(msg, snapshot);
    }

    #[test]
    fn sign_ignores_existing_signature_bytes() {
        let key = [0xAAu8; 16];
        let mut clean = fixture_message();
        let mut dirty = fixture_message();
        dirty[SIG_OFF..SIG_OFF + SIG_LEN].fill(0xCC);

        sign(&mut clean, &key, SigningAlgo::HmacSha256).expect("sign clean");
        sign(&mut dirty, &key, SigningAlgo::HmacSha256).expect("sign dirty");

        assert_eq!(
            &clean[SIG_OFF..SIG_OFF + SIG_LEN],
            &dirty[SIG_OFF..SIG_OFF + SIG_LEN]
        );
        verify(&dirty, &key, SigningAlgo::HmacSha256).expect("verify dirty");
    }
}
