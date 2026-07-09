use std::sync::Arc;

use super::memfs::MemFsBackend;
use crate::conn::state::{Connection, Session, TreeConnect};
use crate::server::ConfigError;
use crate::{Access, Identity, Share, ShareMode, SmbServer};

fn test_server() -> SmbServer {
    SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .user("alice", "password")
        .share(
            Share::new("home", MemFsBackend::new().with_file("seed.txt", b""))
                .user("alice", Access::ReadWrite),
        )
        .build()
        .expect("build")
}

fn public_server() -> SmbServer {
    SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .share(Share::new("public", MemFsBackend::new()).public())
        .build()
        .expect("build")
}

async fn register_session(
    server: &SmbServer,
    identity: Identity,
    share_name: &str,
) -> Arc<Connection> {
    let state = server.state();
    let conn = Arc::new(Connection::new(
        state.config.server_guid,
        state.config.max_read_size,
        state.config.max_write_size,
    ));
    state.active_connections.register(&conn).await;

    let session = Session::new(1, identity, [0; 16], [0; 16], false, None);
    let session = Arc::new(tokio::sync::RwLock::new(session));
    let share = state.find_share(share_name).await.expect("share");
    let tree = Arc::new(tokio::sync::RwLock::new(TreeConnect::new(
        1,
        share,
        Access::ReadWrite,
    )));
    {
        let sess = session.read().await;
        sess.trees.write().await.insert(1, tree);
    }
    conn.sessions.write().await.insert(1, session);
    conn
}

async fn register_alice_session(server: &SmbServer) -> Arc<Connection> {
    register_session(
        server,
        Identity::User {
            user: "alice".to_string(),
            domain: String::new(),
        },
        "home",
    )
    .await
}

#[tokio::test]
async fn config_handle_adds_users_and_shares() {
    let server = SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .build()
        .expect("build");
    let config = server.config_handle();

    config.add_user("bob", "password").await.expect("add user");
    config
        .add_share(Share::new("media", MemFsBackend::new()).user("bob", Access::Read))
        .await
        .expect("add share");

    let state = server.state();
    assert!(state.lookup_user("bob").await.is_some());
    assert!(state.find_share("media").await.is_some());
}

#[tokio::test]
async fn removing_user_revokes_active_sessions() {
    let server = test_server();
    let conn = register_alice_session(&server).await;

    server
        .config_handle()
        .remove_user("alice")
        .await
        .expect("remove user");

    assert!(server.state().lookup_user("alice").await.is_none());
    assert!(conn.sessions.read().await.is_empty());
}

#[tokio::test]
async fn removing_share_revokes_active_trees() {
    let server = test_server();
    let conn = register_alice_session(&server).await;

    server
        .config_handle()
        .remove_share("home")
        .await
        .expect("remove share");

    assert!(server.state().find_share("home").await.is_none());
    let sessions = conn.sessions.read().await;
    let session = sessions.get(&1).expect("session remains").read().await;
    assert!(session.trees.read().await.is_empty());
}

#[tokio::test]
async fn revoking_user_from_share_revokes_only_that_tree() {
    let server = test_server();
    let conn = register_alice_session(&server).await;

    server
        .config_handle()
        .revoke_share_user("home", "alice")
        .await
        .expect("revoke user share");

    assert!(conn.sessions.read().await.contains_key(&1));
    let sessions = conn.sessions.read().await;
    let session = sessions.get(&1).expect("session remains").read().await;
    assert!(session.trees.read().await.is_empty());
}

#[tokio::test]
async fn changing_share_mode_revokes_active_trees() {
    let server = public_server();
    let conn = register_session(&server, Identity::Anonymous, "public").await;

    server
        .config_handle()
        .set_share_mode("public", ShareMode::PublicReadOnly)
        .await
        .expect("set mode");

    let sessions = conn.sessions.read().await;
    let session = sessions.get(&1).expect("session remains").read().await;
    assert!(session.trees.read().await.is_empty());
}

#[tokio::test]
async fn public_share_cannot_mix_explicit_users() {
    let server = SmbServer::builder()
        .listen("127.0.0.1:0".parse().unwrap())
        .share(Share::new("public", MemFsBackend::new()).public())
        .build()
        .expect("build");

    let config = server.config_handle();
    config
        .add_user("alice", "password")
        .await
        .expect("add user");

    let err = config
        .grant_share_user("public", "alice", Access::Read)
        .await
        .expect_err("grant should fail");

    assert_eq!(err, ConfigError::PublicMixedWithUsers("public".to_string()));
}
