use std::fs;

use tempfile::TempDir;

use super::prepare_isolated_agent_home;

#[test]
fn isolated_agent_home_is_restartable_with_runtime_marker() {
    let parent = TempDir::new().expect("temp parent");
    let agent_home = parent.path().join("codex-home");

    prepare_isolated_agent_home(&agent_home).expect("empty home should initialize");
    prepare_isolated_agent_home(&agent_home).expect("marked home should be restartable");
    assert_eq!(
        fs::read_to_string(agent_home.join(".ai-platform-runtime-home"))
            .expect("marker should exist"),
        "ai-platform-agent-home/v1\n"
    );
}

#[test]
fn isolated_agent_home_rejects_foreign_or_forbidden_state() {
    let foreign = TempDir::new().expect("foreign home");
    fs::write(foreign.path().join("foreign.txt"), "data").expect("fixture write");
    assert!(prepare_isolated_agent_home(foreign.path()).is_err());

    let marked = TempDir::new().expect("marked home");
    prepare_isolated_agent_home(marked.path()).expect("empty home should initialize");
    fs::write(marked.path().join("AGENTS.md"), "untrusted").expect("fixture write");
    assert!(prepare_isolated_agent_home(marked.path()).is_err());
}
