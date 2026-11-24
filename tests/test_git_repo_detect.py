import os
import tempfile

from src.openclank.git_repo_detect import detect_repo


def root():
    return os.path.realpath(tempfile.mkdtemp())


def test_repository_non_repo_nested_and_worktree():
    r = root()
    os.makedirs(os.path.join(r, "plain", "sub")); os.makedirs(os.path.join(r, "repo", ".git")); os.makedirs(os.path.join(r, "repo", "src", "deep"))
    assert detect_repo(os.path.join(r, "plain", "sub"), r) == {"state": "none"}
    assert detect_repo(os.path.join(r, "repo"), r) == {"state": "repository", "kind": "repository", "root_relative": "."}
    assert detect_repo(os.path.join(r, "repo", "src", "deep"), r) == {"state": "repository", "kind": "repository", "root_relative": "../.."}
    # nested repo: the nearest .git wins
    os.makedirs(os.path.join(r, "repo", "vendor", "inner", ".git"))
    assert detect_repo(os.path.join(r, "repo", "vendor", "inner"), r)["root_relative"] == "."
    assert detect_repo(os.path.join(r, "repo", "vendor"), r)["root_relative"] == ".."
    # a folder inside an agent worktree: .git is a file "gitdir: ..."
    os.makedirs(os.path.join(r, "wt", "pkg"))
    with open(os.path.join(r, "wt", ".git"), "w") as f: f.write("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert detect_repo(os.path.join(r, "wt", "pkg"), r) == {"state": "repository", "kind": "worktree", "root_relative": ".."}


def test_never_walks_above_the_location_root_and_ignores_bad_markers():
    outer = root(); os.makedirs(os.path.join(outer, ".git")); inner = os.path.join(outer, "loc"); os.makedirs(inner)
    assert detect_repo(inner, inner) == {"state": "none"}, "a repository above the registered location is not reported"
    assert detect_repo(outer, inner)["state"] == "unavailable"
    junk = os.path.join(inner, "junk"); os.makedirs(junk)
    with open(os.path.join(junk, ".git"), "w") as f: f.write("not a gitdir\n")
    assert detect_repo(junk, inner) == {"state": "none"}
    link = os.path.join(inner, "link"); os.makedirs(link); os.symlink(outer, os.path.join(link, ".git"))
    assert detect_repo(link, inner) == {"state": "none"}
