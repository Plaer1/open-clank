use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

struct Bridge {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Bridge {
    fn start(dir: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_copal-bridge"))
            .env("COPAL_DATA_DIR", dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("start Copal bridge");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Self {
            child,
            stdin,
            stdout,
        }
    }

    fn call(&mut self, op: &str, args: Value) -> Value {
        writeln!(self.stdin, "{}", json!({"id":"test", "op":op, "args":args})).unwrap();
        self.stdin.flush().unwrap();
        let mut line = String::new();
        self.stdout.read_line(&mut line).unwrap();
        let response: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(response["ok"], true, "bridge error: {response}");
        response["result"].clone()
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn setup() -> (PathBuf, Value, Value) {
    let dir = std::env::temp_dir().join(format!("copal-managed-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    let mut bridge = Bridge::start(&dir);
    let source = bridge.call("create", json!({"owner":"alice", "workspace_id":"course", "kind":"markdown", "name":"source.md", "content":"rev-1"}));
    let target = bridge.call("create", json!({"owner":"alice", "workspace_id":"course", "kind":"markdown", "name":"target.md", "content":"before"}));
    (dir, source["doc"].clone(), target["doc"].clone())
}

fn progress(source: &Value, target: &Value) -> Value {
    json!({
        "owner":"alice", "workspace_id":"course", "action_id":"progress", "actor_id":"learner", "guards":[{"owner":"alice","workspace_id":"course","id":source["id"],"revision":{"kind":"copalHead","value":source["head"]}}],
        "operations":[{"kind":"write","owner":"alice","workspace_id":"course","id":target["id"],"revision":{"kind":"copalHead","value":target["head"]},"content":"done"}]
    })
}

#[test]
fn independent_bridge_processes_recheck_reset_orders_and_restart_receipt() {
    let (dir, source, target) = setup();
    let mut progress_bridge = Bridge::start(&dir);
    let applied = progress_bridge.call("commit_guarded", progress(&source, &target));
    assert_eq!(applied["outcome"], "applied");
    drop(progress_bridge);
    let mut reset_bridge = Bridge::start(&dir);
    let reset = reset_bridge.call("write", json!({"owner":"alice","workspace_id":"course","id":source["id"],"base":source["head"],"content":"rev-reset"}));
    assert_eq!(reset["outcome"], "committed");
    drop(reset_bridge);
    let mut restarted = Bridge::start(&dir);
    let replay = restarted.call("commit_guarded", progress(&source, &target));
    assert_eq!(replay["outcome"], "applied");
    drop(restarted);
    let _ = fs::remove_dir_all(&dir);

    let (dir, source, target) = setup();
    let mut reset_first = Bridge::start(&dir);
    let reset = reset_first.call("write", json!({"owner":"alice","workspace_id":"course","id":source["id"],"base":source["head"],"content":"rev-reset"}));
    assert_eq!(reset["outcome"], "committed");
    drop(reset_first);
    let mut progress_second = Bridge::start(&dir);
    let conflict = progress_second.call("commit_guarded", progress(&source, &target));
    assert_eq!(conflict["outcome"], "conflict");
    assert_eq!(conflict["resource"]["id"], source["id"]);
    let target_after = progress_second.call(
        "get",
        json!({"owner":"alice","workspace_id":"course","id":target["id"]}),
    );
    assert_eq!(target_after["text"], "before");
    let _ = fs::remove_dir_all(&dir);
}
