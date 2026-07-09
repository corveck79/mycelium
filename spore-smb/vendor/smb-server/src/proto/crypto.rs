//! SMB signing, key derivation, pre-auth integrity.
//!
//! Submodules:
//! * [`kdf`]     — SP 800-108 CTR-mode KDF (`SMB2KDF`) and SMB-specific
//!   signing/application key helpers (MS-SMB2 §3.1.4.2).
//! * [`sign`]    — HMAC-SHA-256 (SMB 2.x) and AES-CMAC (SMB 3.x) signing of
//!   SMB2 messages (MS-SMB2 §3.1.4.1).
//! * [`preauth`] — SMB 3.1.1 pre-auth integrity running SHA-512 hash
//!   (MS-SMB2 §3.1.4.4.1, §3.3.5.4).
//!
//! Encryption (AES-CCM/AES-GCM) is intentionally out of scope for v1; see the
//! design spec.

pub mod kdf;
pub mod preauth;
pub mod sign;

pub use kdf::{signing_key_30, signing_key_311};
pub use preauth::PreauthIntegrity;
pub use sign::{SigningAlgo, sign, verify};
