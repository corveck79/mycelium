//! File / FileSystem / Security info-class encoders used by QUERY_INFO,
//! SET_INFO, and QUERY_DIRECTORY.
//!
//! These are byte-for-byte wire encodings per MS-FSCC §2.4 (file info) /
//! §2.5 (filesystem info) / MS-DTYP §2.4 (security descriptor).

use crate::backend::{DirEntry, FileInfo};
use crate::utils::utf16le;

// ---------------------------------------------------------------------------
// File info classes (MS-FSCC §2.4)
// ---------------------------------------------------------------------------

pub const FILE_DIRECTORY_INFORMATION: u8 = 0x01;
pub const FILE_FULL_DIRECTORY_INFORMATION: u8 = 0x02;
pub const FILE_BOTH_DIRECTORY_INFORMATION: u8 = 0x03;
pub const FILE_BASIC_INFORMATION: u8 = 0x04;
pub const FILE_STANDARD_INFORMATION: u8 = 0x05;
pub const FILE_INTERNAL_INFORMATION: u8 = 0x06;
pub const FILE_EA_INFORMATION: u8 = 0x07;
pub const FILE_ACCESS_INFORMATION: u8 = 0x08;
pub const FILE_NAME_INFORMATION: u8 = 0x09;
pub const FILE_NAMES_INFORMATION: u8 = 0x0C;
pub const FILE_POSITION_INFORMATION: u8 = 0x0E;
pub const FILE_FULL_EA_INFORMATION: u8 = 0x0F;
pub const FILE_MODE_INFORMATION: u8 = 0x10;
pub const FILE_ALIGNMENT_INFORMATION: u8 = 0x11;
pub const FILE_ALL_INFORMATION: u8 = 0x12;
pub const FILE_ALLOCATION_INFORMATION: u8 = 0x13;
pub const FILE_END_OF_FILE_INFORMATION: u8 = 0x14;
pub const FILE_STREAM_INFORMATION: u8 = 0x16;
pub const FILE_DISPOSITION_INFORMATION: u8 = 0x0D;
pub const FILE_RENAME_INFORMATION: u8 = 0x0A;
pub const FILE_NETWORK_OPEN_INFORMATION: u8 = 0x22;
pub const FILE_ID_BOTH_DIRECTORY_INFORMATION: u8 = 0x25;
pub const FILE_ID_FULL_DIRECTORY_INFORMATION: u8 = 0x26;

// ---------------------------------------------------------------------------
// FileBasicInformation (MS-FSCC §2.4.7) — 40 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_basic_information(info: &FileInfo) -> Vec<u8> {
    let mut out = Vec::with_capacity(40);
    out.extend_from_slice(&info.creation_time.to_le_bytes());
    out.extend_from_slice(&info.last_access_time.to_le_bytes());
    out.extend_from_slice(&info.last_write_time.to_le_bytes());
    out.extend_from_slice(&info.change_time.to_le_bytes());
    out.extend_from_slice(&info.attributes().to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes()); // Reserved
    out
}

// ---------------------------------------------------------------------------
// FileStandardInformation (MS-FSCC §2.4.41) — 24 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_standard_information(info: &FileInfo) -> Vec<u8> {
    let mut out = Vec::with_capacity(24);
    out.extend_from_slice(&info.allocation_size.to_le_bytes());
    out.extend_from_slice(&info.end_of_file.to_le_bytes());
    out.extend_from_slice(&1u32.to_le_bytes()); // NumberOfLinks = 1
    out.push(0); // DeletePending
    out.push(if info.is_directory { 1 } else { 0 }); // Directory
    out.extend_from_slice(&0u16.to_le_bytes()); // Reserved
    out
}

// ---------------------------------------------------------------------------
// FileInternalInformation (MS-FSCC §2.4.20) — 8 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_internal_information(file_index: u64) -> Vec<u8> {
    file_index.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FileEaInformation (MS-FSCC §2.4.12) — 4 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_ea_information() -> Vec<u8> {
    0u32.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FileAccessInformation (MS-FSCC §2.4.1) — 4 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_access_information(access_mask: u32) -> Vec<u8> {
    access_mask.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FilePositionInformation (MS-FSCC §2.4.32) — 8 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_position_information() -> Vec<u8> {
    0u64.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FileModeInformation (MS-FSCC §2.4.24) — 4 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_mode_information(mode: u32) -> Vec<u8> {
    mode.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FileAlignmentInformation (MS-FSCC §2.4.3) — 4 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_alignment_information() -> Vec<u8> {
    // FILE_BYTE_ALIGNMENT (0) — no alignment requirement.
    0u32.to_le_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// FileNameInformation (MS-FSCC §2.4.27) — 4 bytes + UTF-16LE name
// ---------------------------------------------------------------------------

pub fn encode_file_name_information(name: &str) -> Vec<u8> {
    let n = utf16le(name);
    let mut out = Vec::with_capacity(4 + n.len());
    out.extend_from_slice(&(n.len() as u32).to_le_bytes());
    out.extend_from_slice(&n);
    out
}

// ---------------------------------------------------------------------------
// FileAllInformation (MS-FSCC §2.4.2) — concatenation of basic, standard,
// internal, EA, access, position, mode, alignment, name.
// ---------------------------------------------------------------------------

pub fn encode_file_all_information(info: &FileInfo, file_index: u64, access_mask: u32) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&encode_file_basic_information(info));
    out.extend_from_slice(&encode_file_standard_information(info));
    out.extend_from_slice(&encode_file_internal_information(file_index));
    out.extend_from_slice(&encode_file_ea_information());
    out.extend_from_slice(&encode_file_access_information(access_mask));
    out.extend_from_slice(&encode_file_position_information());
    out.extend_from_slice(&encode_file_mode_information(0));
    out.extend_from_slice(&encode_file_alignment_information());
    out.extend_from_slice(&encode_file_name_information(&info.name));
    // Linux cifs checks FileAllInformation against its struct with
    // FileName[1], so the empty-name root case must still be at least 101
    // bytes.
    if out.len() < 101 {
        out.push(0);
    }
    out
}

// ---------------------------------------------------------------------------
// FileNetworkOpenInformation (MS-FSCC §2.4.30) — 56 bytes
// ---------------------------------------------------------------------------

pub fn encode_file_network_open_information(info: &FileInfo) -> Vec<u8> {
    let mut out = Vec::with_capacity(56);
    out.extend_from_slice(&info.creation_time.to_le_bytes());
    out.extend_from_slice(&info.last_access_time.to_le_bytes());
    out.extend_from_slice(&info.last_write_time.to_le_bytes());
    out.extend_from_slice(&info.change_time.to_le_bytes());
    out.extend_from_slice(&info.allocation_size.to_le_bytes());
    out.extend_from_slice(&info.end_of_file.to_le_bytes());
    out.extend_from_slice(&info.attributes().to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes()); // Reserved
    out
}

// ---------------------------------------------------------------------------
// FileStreamInformation (MS-FSCC §2.4.43) — for non-directories, one default
// stream entry (`::$DATA`); for directories, empty buffer.
// ---------------------------------------------------------------------------

pub fn encode_file_stream_information(info: &FileInfo) -> Vec<u8> {
    if info.is_directory {
        return Vec::new();
    }
    let stream_name = utf16le("::$DATA");
    let stream_name_len = stream_name.len() as u32;
    let mut out = Vec::new();
    out.extend_from_slice(&0u32.to_le_bytes()); // NextEntryOffset = 0
    out.extend_from_slice(&stream_name_len.to_le_bytes()); // StreamNameLength
    out.extend_from_slice(&info.end_of_file.to_le_bytes()); // StreamSize
    out.extend_from_slice(&info.allocation_size.to_le_bytes()); // StreamAllocationSize
    out.extend_from_slice(&stream_name);
    out
}

// ---------------------------------------------------------------------------
// FS info classes (MS-FSCC §2.5)
// ---------------------------------------------------------------------------

pub const FS_VOLUME_INFORMATION: u8 = 0x01;
pub const FS_SIZE_INFORMATION: u8 = 0x03;
pub const FS_DEVICE_INFORMATION: u8 = 0x04;
pub const FS_ATTRIBUTE_INFORMATION: u8 = 0x05;
pub const FS_FULL_SIZE_INFORMATION: u8 = 0x07;

/// FileFsVolumeInformation (MS-FSCC §2.5.9). Volume creation time, serial,
/// label.
pub fn encode_fs_volume_information(creation_time: u64, serial: u32, label: &str) -> Vec<u8> {
    let label_u16 = utf16le(label);
    let mut out = Vec::new();
    out.extend_from_slice(&creation_time.to_le_bytes());
    out.extend_from_slice(&serial.to_le_bytes());
    out.extend_from_slice(&(label_u16.len() as u32).to_le_bytes());
    out.push(0); // SupportsObjects
    out.push(0); // Reserved
    out.extend_from_slice(&label_u16);
    out
}

/// FileFsSizeInformation (MS-FSCC §2.5.7) — 24 bytes.
pub fn encode_fs_size_information(
    total_alloc_units: u64,
    avail_alloc_units: u64,
    sectors_per_unit: u32,
    bytes_per_sector: u32,
) -> Vec<u8> {
    let mut out = Vec::with_capacity(24);
    out.extend_from_slice(&total_alloc_units.to_le_bytes());
    out.extend_from_slice(&avail_alloc_units.to_le_bytes());
    out.extend_from_slice(&sectors_per_unit.to_le_bytes());
    out.extend_from_slice(&bytes_per_sector.to_le_bytes());
    out
}

/// FileFsDeviceInformation (MS-FSCC §2.5.10) — 8 bytes.
pub fn encode_fs_device_information(device_type: u32, characteristics: u32) -> Vec<u8> {
    let mut out = Vec::with_capacity(8);
    out.extend_from_slice(&device_type.to_le_bytes());
    out.extend_from_slice(&characteristics.to_le_bytes());
    out
}

/// FileFsAttributeInformation (MS-FSCC §2.5.1) — variable.
pub fn encode_fs_attribute_information(
    attributes: u32,
    max_component_len: u32,
    fs_name: &str,
) -> Vec<u8> {
    let name_u16 = utf16le(fs_name);
    let mut out = Vec::new();
    out.extend_from_slice(&attributes.to_le_bytes());
    out.extend_from_slice(&max_component_len.to_le_bytes());
    out.extend_from_slice(&(name_u16.len() as u32).to_le_bytes());
    out.extend_from_slice(&name_u16);
    out
}

/// FileFsFullSizeInformation (MS-FSCC §2.5.4) — 32 bytes.
pub fn encode_fs_full_size_information(
    total_alloc_units: u64,
    caller_avail_alloc_units: u64,
    actual_avail_alloc_units: u64,
    sectors_per_unit: u32,
    bytes_per_sector: u32,
) -> Vec<u8> {
    let mut out = Vec::with_capacity(32);
    out.extend_from_slice(&total_alloc_units.to_le_bytes());
    out.extend_from_slice(&caller_avail_alloc_units.to_le_bytes());
    out.extend_from_slice(&actual_avail_alloc_units.to_le_bytes());
    out.extend_from_slice(&sectors_per_unit.to_le_bytes());
    out.extend_from_slice(&bytes_per_sector.to_le_bytes());
    out
}

// ---------------------------------------------------------------------------
// Minimal SECURITY_DESCRIPTOR with owner=Everyone, DACL=Everyone allowed.
// ---------------------------------------------------------------------------

/// Build a minimal absolute-form SECURITY_DESCRIPTOR per MS-DTYP §2.4.6.
///
/// Owner = Everyone (S-1-1-0). No group. DACL = single Allow ACE granting
/// `0x001F_01FF` (FILE_ALL_ACCESS) to Everyone. Self-relative format so it
/// embeds cleanly in the QUERY_INFO buffer.
pub fn encode_minimal_security_descriptor() -> Vec<u8> {
    // SID Everyone (S-1-1-0): 1, 1, [0,0,0,0,0,1], [0,0,0,0]
    // Total length: 1 (Revision) + 1 (SubAuthorityCount=1) + 6 (Identifier) + 4 (subauth) = 12
    let everyone: Vec<u8> = vec![
        0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
    ];

    // Build ACE: AccessAllowedAce
    //   Header: 4 bytes (Type=0, Flags=0, Size)
    //   Mask: 4 bytes
    //   Sid: variable
    let mut ace = Vec::new();
    ace.push(0x00); // ACCESS_ALLOWED_ACE_TYPE
    ace.push(0x00); // AceFlags
    let ace_size: u16 = (4 + 4 + everyone.len()) as u16;
    ace.extend_from_slice(&ace_size.to_le_bytes());
    ace.extend_from_slice(&0x001F_01FFu32.to_le_bytes()); // FILE_ALL_ACCESS
    ace.extend_from_slice(&everyone);

    // ACL: Revision (1), Sbz1 (1), AclSize (2), AceCount (2), Sbz2 (2), then ACEs.
    let acl_size: u16 = (8 + ace.len()) as u16;
    let mut dacl = Vec::new();
    dacl.push(0x02); // Revision = ACL_REVISION
    dacl.push(0x00); // Sbz1
    dacl.extend_from_slice(&acl_size.to_le_bytes());
    dacl.extend_from_slice(&1u16.to_le_bytes()); // AceCount
    dacl.extend_from_slice(&0u16.to_le_bytes()); // Sbz2
    dacl.extend_from_slice(&ace);

    // SECURITY_DESCRIPTOR (self-relative):
    //   Revision (1), Sbz1 (1), Control (2),
    //   OwnerOffset (4), GroupOffset (4), SaclOffset (4), DaclOffset (4)
    //   Then concatenated entities.
    const SE_DACL_PRESENT: u16 = 0x0004;
    const SE_SELF_RELATIVE: u16 = 0x8000;
    let mut sd = Vec::new();
    sd.push(0x01); // Revision = SECURITY_DESCRIPTOR_REVISION
    sd.push(0x00); // Sbz1
    sd.extend_from_slice(&(SE_DACL_PRESENT | SE_SELF_RELATIVE).to_le_bytes());
    let header_len: u32 = 20;
    let owner_off = header_len;
    let group_off = 0u32;
    let sacl_off = 0u32;
    let dacl_off = owner_off + everyone.len() as u32;
    sd.extend_from_slice(&owner_off.to_le_bytes());
    sd.extend_from_slice(&group_off.to_le_bytes());
    sd.extend_from_slice(&sacl_off.to_le_bytes());
    sd.extend_from_slice(&dacl_off.to_le_bytes());
    sd.extend_from_slice(&everyone);
    sd.extend_from_slice(&dacl);
    sd
}

// ---------------------------------------------------------------------------
// Directory information classes (MS-FSCC §2.4.{8,14,17,30,31})
// ---------------------------------------------------------------------------

/// Encode a single FileBothDirectoryInformation entry. Returns the encoded
/// bytes. The caller patches `NextEntryOffset` for chained entries.
pub fn encode_dir_entry(class: u8, entry: &DirEntry, file_index: u64) -> Vec<u8> {
    let info = &entry.info;
    let name_u16 = utf16le(&info.name);
    match class {
        FILE_DIRECTORY_INFORMATION => {
            // 64 bytes fixed + name
            let mut out = Vec::new();
            write_dir_entry_prefix(&mut out, info, file_index, name_u16.len());
            out.extend_from_slice(&name_u16);
            out
        }
        FILE_FULL_DIRECTORY_INFORMATION => {
            let mut out = Vec::new();
            write_dir_entry_prefix(&mut out, info, file_index, name_u16.len());
            out.extend_from_slice(&0u32.to_le_bytes()); // EaSize
            out.extend_from_slice(&name_u16);
            out
        }
        FILE_BOTH_DIRECTORY_INFORMATION => {
            let mut out = Vec::new();
            write_dir_entry_prefix(&mut out, info, file_index, name_u16.len());
            out.extend_from_slice(&0u32.to_le_bytes()); // EaSize
            out.push(0); // ShortNameLength
            out.push(0); // Reserved1
            // ShortName: 24 bytes (12 UTF-16 chars).
            out.extend_from_slice(&[0u8; 24]);
            out.extend_from_slice(&name_u16);
            out
        }
        FILE_ID_BOTH_DIRECTORY_INFORMATION => {
            let mut out = Vec::new();
            write_dir_entry_prefix(&mut out, info, file_index, name_u16.len());
            out.extend_from_slice(&0u32.to_le_bytes()); // EaSize
            out.push(0); // ShortNameLength
            out.push(0); // Reserved1
            out.extend_from_slice(&[0u8; 24]); // ShortName
            out.extend_from_slice(&0u16.to_le_bytes()); // Reserved2
            out.extend_from_slice(&file_index.to_le_bytes()); // FileId
            out.extend_from_slice(&name_u16);
            out
        }
        FILE_ID_FULL_DIRECTORY_INFORMATION => {
            let mut out = Vec::new();
            write_dir_entry_prefix(&mut out, info, file_index, name_u16.len());
            out.extend_from_slice(&0u32.to_le_bytes()); // EaSize
            out.extend_from_slice(&0u32.to_le_bytes()); // Reserved
            out.extend_from_slice(&file_index.to_le_bytes()); // FileId
            out.extend_from_slice(&name_u16);
            out
        }
        FILE_NAMES_INFORMATION => {
            let mut out = Vec::new();
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&(file_index as u32).to_le_bytes());
            out.extend_from_slice(&(name_u16.len() as u32).to_le_bytes());
            out.extend_from_slice(&name_u16);
            out
        }
        _ => Vec::new(),
    }
}

fn write_dir_entry_prefix(out: &mut Vec<u8>, info: &FileInfo, file_index: u64, name_len: usize) {
    out.extend_from_slice(&0u32.to_le_bytes()); // NextEntryOffset (patched later)
    out.extend_from_slice(&(file_index as u32).to_le_bytes()); // FileIndex
    out.extend_from_slice(&info.creation_time.to_le_bytes());
    out.extend_from_slice(&info.last_access_time.to_le_bytes());
    out.extend_from_slice(&info.last_write_time.to_le_bytes());
    out.extend_from_slice(&info.change_time.to_le_bytes());
    out.extend_from_slice(&info.end_of_file.to_le_bytes());
    out.extend_from_slice(&info.allocation_size.to_le_bytes());
    out.extend_from_slice(&info.attributes().to_le_bytes());
    out.extend_from_slice(&(name_len as u32).to_le_bytes());
}

/// Round up `n` to the next multiple of 8.
pub fn align8(n: usize) -> usize {
    (n + 7) & !7
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake_info() -> FileInfo {
        FileInfo {
            name: "file.txt".to_string(),
            end_of_file: 100,
            allocation_size: 100,
            creation_time: 0x01D9_0000_0000_0000,
            last_access_time: 0x01D9_0000_0000_0000,
            last_write_time: 0x01D9_0000_0000_0000,
            change_time: 0x01D9_0000_0000_0000,
            is_directory: false,
            file_index: 1,
        }
    }

    #[test]
    fn basic_information_is_40_bytes() {
        let bytes = encode_file_basic_information(&fake_info());
        assert_eq!(bytes.len(), 40);
    }

    #[test]
    fn standard_information_is_24_bytes() {
        let bytes = encode_file_standard_information(&fake_info());
        assert_eq!(bytes.len(), 24);
    }

    #[test]
    fn network_open_information_is_56_bytes() {
        let bytes = encode_file_network_open_information(&fake_info());
        assert_eq!(bytes.len(), 56);
    }

    #[test]
    fn file_all_information_empty_name_keeps_linux_minimum_size() {
        let mut info = fake_info();
        info.name.clear();
        let bytes = encode_file_all_information(&info, 1, 0x001F_01FF);
        assert_eq!(bytes.len(), 101);
    }

    #[test]
    fn security_descriptor_is_self_relative() {
        let sd = encode_minimal_security_descriptor();
        // Revision=1, then Control bits 8000 set => self-relative.
        assert_eq!(sd[0], 0x01);
        let control = u16::from_le_bytes([sd[2], sd[3]]);
        assert!(control & 0x8000 != 0);
    }
}
