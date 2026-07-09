//! Cross-stack write-path integration test over anonymous SESSION_SETUP:
//! CREATE with write access, WRITE bytes, READ them back, and confirm the
//! `LocalFsBackend` file contents on disk.

mod common;

use common::{
    STATUS_SUCCESS, anonymous_session_setup, build_header, negotiate, parse_response_header,
    read_frame, tree_connect, utf16le, write_frame,
};
use smb_server::LocalFsBackend;
use smb_server::wire::header::Command;
use smb_server::wire::messages::{
    CloseRequest, CloseResponse, CreateRequest, CreateResponse, ReadRequest, ReadResponse,
    WriteRequest, WriteResponse,
};
use smb_server::{Share, SmbServer};
use tempfile::tempdir;
use tokio::net::TcpStream;

#[tokio::test]
async fn end_to_end_anon_write_then_read_localfs() {
    // 1. Empty temp dir — the test creates `out.txt` from scratch via the
    //    SMB CREATE/WRITE dispatch path.
    let td = tempdir().expect("tempdir");
    let backend = LocalFsBackend::new(td.path()).expect("open root");

    let server = SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .user("alice", "password")
        .share(Share::new("share", backend).public()) // see module-level note
        .netbios_name("TESTSERVER")
        .build()
        .expect("build");

    server.bind().await.expect("bind");
    let addr = server.local_addr().await.expect("addr");
    let handle = tokio::spawn(async move { server.serve().await });
    tokio::task::yield_now().await;

    let mut s = TcpStream::connect(addr).await.expect("connect");

    let _ = negotiate(&mut s).await;
    let session_id = anonymous_session_setup(&mut s).await;
    let tree_id = tree_connect(&mut s, "\\\\127.0.0.1\\share", session_id, 3).await;

    // ---- CREATE out.txt with write+create-new intent --------------------
    let name_u16 = utf16le("out.txt");
    let cr_req = CreateRequest {
        structure_size: 57,
        security_flags: 0,
        requested_oplock_level: 0,
        impersonation_level: 2,
        smb_create_flags: 0,
        reserved: 0,
        // FILE_GENERIC_READ | FILE_GENERIC_WRITE.
        desired_access: 0x0012_0089 | 0x0012_0116,
        file_attributes: 0,
        share_access: 0x0000_0007,
        // FILE_OVERWRITE_IF — create or overwrite.
        create_disposition: 5,
        create_options: 0,
        name_offset: 0x78,
        name_length: name_u16.len() as u16,
        create_contexts_offset: 0,
        create_contexts_length: 0,
        name: name_u16,
        create_contexts: vec![],
    };
    let mut body = Vec::new();
    cr_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Create, 4, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(
        rh.channel_sequence_status, STATUS_SUCCESS,
        "CREATE failed with status {:#010x}",
        rh.channel_sequence_status
    );
    let cr_resp = CreateResponse::parse(rb).expect("parse create resp");
    let file_id = cr_resp.file_id;

    // ---- WRITE the bytes ------------------------------------------------
    let payload = b"written-from-smb";
    let wr_req = WriteRequest {
        structure_size: 49,
        data_offset: WriteRequest::STANDARD_DATA_OFFSET,
        length: payload.len() as u32,
        offset: 0,
        file_id,
        channel: 0,
        remaining_bytes: 0,
        write_channel_info_offset: 0,
        write_channel_info_length: 0,
        flags: 0,
        data: payload.to_vec(),
    };
    let mut body = Vec::new();
    wr_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Write, 5, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(
        rh.channel_sequence_status, STATUS_SUCCESS,
        "WRITE failed with status {:#010x}",
        rh.channel_sequence_status
    );
    let wr_resp = WriteResponse::parse(rb).expect("parse write resp");
    assert_eq!(wr_resp.count as usize, payload.len());

    // ---- READ back to confirm the bytes landed --------------------------
    let rd_req = ReadRequest {
        structure_size: 49,
        padding: ReadResponse::STANDARD_DATA_OFFSET,
        flags: 0,
        length: 64,
        offset: 0,
        file_id,
        minimum_count: 0,
        channel: 0,
        remaining_bytes: 0,
        read_channel_info_offset: 0,
        read_channel_info_length: 0,
        buffer: vec![0],
    };
    let mut body = Vec::new();
    rd_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Read, 6, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let rd_resp = ReadResponse::parse(rb).expect("parse read resp");
    assert_eq!(rd_resp.data.as_slice(), payload);

    // ---- CLOSE ----------------------------------------------------------
    let cl_req = CloseRequest {
        structure_size: 24,
        flags: 0,
        reserved: 0,
        file_id,
    };
    let mut body = Vec::new();
    cl_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Close, 7, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;
    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let _ = CloseResponse::parse(rb).expect("parse close resp");

    // 2. Final cross-check: the file exists on disk with the right bytes.
    let on_disk = std::fs::read(td.path().join("out.txt")).expect("file on disk");
    assert_eq!(on_disk, payload);

    drop(s);
    handle.abort();
}
