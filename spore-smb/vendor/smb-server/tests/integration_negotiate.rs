//! Integration test: drive a real `SmbServer` over a TCP loopback through a
//! NEGOTIATE → SESSION_SETUP (anonymous) → TREE_CONNECT → CREATE → READ flow.
//!
//! We hand-craft the request bytes since we don't depend on a Rust SMB client
//! crate.

mod common;

use common::{
    STATUS_SUCCESS, anonymous_session_setup, build_header, negotiate, parse_response_header,
    read_frame, tree_connect, utf16le, write_frame,
};

use smb_server::wire::header::Command;
use smb_server::wire::messages::{
    CreateRequest, CreateResponse, FileId, Fsctl, IoctlRequest, IoctlResponse, ReadRequest,
    ReadResponse,
};
use smb_server::{LocalFsBackend, Share, SmbServer};
use tempfile::tempdir;
use tokio::net::TcpStream;

#[tokio::test]
async fn end_to_end_anon_read() {
    // 1. Build a server with one public share and one in-memory file.
    let td = tempdir().expect("tempdir");
    std::fs::write(td.path().join("hello.txt"), b"hello world\n").expect("write hello.txt");
    let backend = LocalFsBackend::new(td.path()).expect("open root");
    let server = SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .share(Share::new("downloads", backend).public())
        .netbios_name("TESTSERVER")
        .build()
        .expect("build");

    server.bind().await.expect("bind");
    let addr = server.local_addr().await.expect("addr");

    // Spawn the server.
    let handle = tokio::spawn(async move { server.serve().await });

    // Tiny grace period.
    tokio::task::yield_now().await;

    let mut s = TcpStream::connect(addr).await.expect("connect");

    let neg_resp = negotiate(&mut s).await;
    let session_id = anonymous_session_setup(&mut s).await;
    let tree_id = tree_connect(&mut s, "\\\\TESTSERVER\\downloads", session_id, 3).await;

    // ---- FSCTL_VALIDATE_NEGOTIATE_INFO mirrors NEGOTIATE ----------------
    let ioctl_req = IoctlRequest {
        structure_size: 57,
        reserved: 0,
        ctl_code: Fsctl::VALIDATE_NEGOTIATE_INFO,
        file_id: FileId::any(),
        input_offset: 0,
        input_count: 0,
        max_input_response: 0,
        output_offset: 0,
        output_count: 0,
        max_output_response: 24,
        flags: IoctlRequest::FLAG_IS_FSCTL,
        reserved2: 0,
        input: vec![],
    };
    let mut body = Vec::new();
    ioctl_req.write_to(&mut body).expect("write");
    let hdr = build_header(Command::Ioctl, 4, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;

    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Ioctl);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let ioctl_resp = IoctlResponse::parse(rb).expect("parse ioctl resp");
    assert_eq!(ioctl_resp.output.len(), 24);
    let validate_security_mode = u16::from_le_bytes([ioctl_resp.output[20], ioctl_resp.output[21]]);
    assert_eq!(validate_security_mode, neg_resp.security_mode);

    // ---- CREATE hello.txt ------------------------------------------------
    let name_u16 = utf16le("hello.txt");
    let cr_req = CreateRequest {
        structure_size: 57,
        security_flags: 0,
        requested_oplock_level: 0,
        impersonation_level: 2,
        smb_create_flags: 0,
        reserved: 0,
        desired_access: 0x0012_0089, // FILE_GENERIC_READ
        file_attributes: 0,
        share_access: 0x0000_0007, // FILE_SHARE_READ|WRITE|DELETE
        create_disposition: 1,     // FILE_OPEN
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
    let hdr = build_header(Command::Create, 5, session_id, tree_id);
    write_frame(&mut s, &hdr, &body).await;

    let resp = read_frame(&mut s).await;
    let (rh, rb) = parse_response_header(&resp);
    assert_eq!(rh.command, Command::Create);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let cr_resp = CreateResponse::parse(rb).expect("parse create resp");
    let file_id = cr_resp.file_id;
    assert_eq!(cr_resp.end_of_file, 12); // "hello world\n"

    // ---- READ ------------------------------------------------------------
    let rd_req = ReadRequest {
        structure_size: 49,
        padding: ReadResponse::STANDARD_DATA_OFFSET,
        flags: 0,
        length: 32,
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
    assert_eq!(rh.command, Command::Read);
    assert_eq!(rh.channel_sequence_status, STATUS_SUCCESS);
    let rd_resp = ReadResponse::parse(rb).expect("parse read resp");
    assert_eq!(rd_resp.data, b"hello world\n");

    drop(s);
    // The server keeps accepting; abort the spawned task so the test
    // process exits cleanly.
    handle.abort();
}
