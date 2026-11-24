//! Pure rules: which values are hidden, which apps are excluded, how long a value may be.
use super::{Info, MAX_VALUE};

/// Applications whose windows are never listed, read or acted on (matched on app name and executable, case-insensitive).
pub const EXCLUDED: &[&str] = &["keepassxc", "keepass", "bitwarden", "1password", "lastpass", "dashlane", "enpass", "proton pass",
    "protonpass", "kwalletmanager", "seahorse", "gnome-keyring", "pass-gui", "nordpass", "roboform", "psono"];
const SECRET_WORDS: &[&str] = &["password", "passwd", "passphrase", "secret", "token", "api key", "apikey", "api_key", "private key", "credential", "pin code"];
pub const HIDDEN: &str = "[hidden]";

pub fn excluded(name: &str, exe: &str) -> bool {
    let (n, e) = (name.to_lowercase(), exe.to_lowercase());
    EXCLUDED.iter().any(|f| n.contains(f) || e.contains(f))
}

fn secret_value(v: &str) -> bool {
    let t = v.trim();
    t.starts_with("-----BEGIN") || t.starts_with("sk-") || t.starts_with("ghp_") || t.starts_with("xox") || t.to_lowercase().starts_with("bearer ")
        || (t.len() >= 32 && !t.contains(' ') && t.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/' || b == b'=' || b == b'-' || b == b'_'))
}

/// True when the node's value must not leave the helper.
pub fn hide_value(i: &Info) -> bool {
    if i.protected { return true; }
    let (role, name) = (i.role.to_lowercase(), i.name.to_lowercase());
    if role.contains("password") || SECRET_WORDS.iter().any(|w| name.contains(w) || i.id.to_lowercase().contains(w)) { return true; }
    i.value.as_deref().is_some_and(secret_value)
}

/// The value a reply may carry: hidden marker, or the value cut to MAX_VALUE characters. Second field: it was hidden.
pub fn shown_value(i: &Info) -> (Option<String>, bool) {
    match &i.value {
        None => (None, false),
        Some(_) if hide_value(i) => (Some(HIDDEN.to_string()), true),
        Some(v) => (Some(v.chars().take(MAX_VALUE).collect()), false),
    }
}

/// Containers that carry no meaning of their own; a rendered tree skips them.
pub fn trivial(i: &Info) -> bool {
    i.name.is_empty() && i.actions.is_empty() && i.value.is_none() && i.id.is_empty()
        && matches!(i.role.as_str(), "panel" | "filler" | "section" | "generic" | "unknown" | "layered pane" | "redundant object" | "viewport")
}

#[cfg(test)]
mod tests {
    use super::*;
    fn info(role: &str, name: &str, value: Option<&str>) -> Info { Info { role: role.into(), name: name.into(), value: value.map(str::to_string), ..Default::default() } }
    #[test] fn excluded_matches_name_or_exe_ignoring_case() {
        assert!(excluded("KeePassXC", "")); assert!(excluded("", "bitwarden-desktop")); assert!(!excluded("Calculator", "kcalc"));
    }
    #[test] fn protected_role_label_and_token_values_are_hidden() {
        let mut p = info("text", "Name", Some("alice")); p.protected = true; assert_eq!(shown_value(&p), (Some(HIDDEN.into()), true));
        assert_eq!(shown_value(&info("password text", "x", Some("hunter2"))), (Some(HIDDEN.into()), true));
        assert_eq!(shown_value(&info("text", "API key", Some("abc"))), (Some(HIDDEN.into()), true));
        assert!(hide_value(&info("text", "Note", Some("sk-live-abcdef"))));
        assert!(hide_value(&info("text", "Note", Some("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2"))));
        assert_eq!(shown_value(&info("text", "Note", Some("hello world"))), (Some("hello world".into()), false));
        assert_eq!(shown_value(&info("text", "Note", None)), (None, false));
    }
    #[test] fn long_values_are_cut_to_the_cap() {
        let long = "a b ".repeat(400);
        let (v, hidden) = shown_value(&info("text", "Note", Some(&long)));
        assert!(!hidden); assert_eq!(v.unwrap().chars().count(), MAX_VALUE);
    }
    #[test] fn trivial_containers_only_when_empty() {
        assert!(trivial(&info("panel", "", None))); assert!(!trivial(&info("panel", "Toolbar", None))); assert!(!trivial(&info("push button", "", None)));
    }
}
