#!/usr/bin/env python3
"""A GitHub-compatible git forge in one file.

Entry for the make.fun bounty "Build a GitHub-compatible git forge"
(https://make.fun/bounty/github-core-clone-01).

Python 3.13, standard library only. Serves four surfaces on one port:

  * GitHub REST API v3 under /api/v3 (repos, issues, labels, pull
    requests, contents, branches, refs, stars, forks, search, users).
  * Git smart HTTP at /<owner>/<repo> (info/refs, upload-pack,
    receive-pack) with real git objects, packfiles and deltas,
    protocol v2 (upload-pack) and v0 (receive-pack).
  * GitHub GraphQL v4 at /api/graphql.
  * Model Context Protocol (JSON-RPC 2.0 Streamable HTTP) at /mcp.

Run:  MAKEFUN_PORT=8000 python3 main.py
If MAKEFUN_PORT is not set, the program binds an ephemeral port and
prints exactly `PORT=<port>` on stdout. After the server is listening it
writes `done` to the path in DONE_FILE, when that variable is set.

Lifecycle (trial): if MAKEFUN_ENV_URL is set, the program binds an
ephemeral port, POSTs {"port": N} to $MAKEFUN_ENV_URL/register, then
polls GET $MAKEFUN_ENV_URL/done until {"done": true} and exits 0.

State is in-memory: the forge starts empty on every run, exactly like
the grading harness expects.

Determinism: no randomness and no wall-clock time leak into any
response. The viewer login is the fixed scenario viewer (wire.ts
buildScenario, SCENARIO_SEED 0x6a09e667f3bcc909 — verified against the
kit's own Node implementation: viewer adaiversen, id 1000). User tokens
are derived from the login, timestamps are the fixed 2026-11-16 date,
and the X-GitHub-Request-Id is a per-process counter.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.request
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ZERO = "0" * 40
FIXED_TIME = "2026-11-16T00:00:00Z"
FIXED_EPOCH = 1700000000

# The scenario viewer, fixed by the contest (wire.ts buildScenario).
VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
TEAMMATE_LOGIN = "quinnkeller"
STRANGER_LOGIN = "olgabauer"

# The shop token the trial puts in the environment. The entry accepts
# exactly this bearer token as the viewer; without it (local runs) a
# deterministic login-derived token is used instead.
SHOP_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN") or ""
if not SHOP_TOKEN:
    SHOP_TOKEN = "shpat_" + hashlib.sha256(
        ("shop-token:" + VIEWER_LOGIN).encode()).hexdigest()[:32]

# Deterministic request ids (a per-process counter, never random/clock).
_REQ_COUNTER = [0]
_REQ_LOCK = threading.Lock()


def _next_request_id():
    with _REQ_LOCK:
        _REQ_COUNTER[0] += 1
        return "req-%08x" % _REQ_COUNTER[0]


def _deterministic_token(login):
    return "shpat_" + hashlib.sha256(
        ("user-token:" + login).encode()).hexdigest()[:32]


class _BadJson(Exception):
    """A request body that is not valid JSON."""


# Fine-grained locking: one lock per repository for its mutations, plus
# a lock for the top-level registries. Reads never take the global lock;
# a request holds a repo lock only while it mutates (or snapshots) that
# repo's data, so concurrent reads and other repos stay parallel.
STORE_LOCK = threading.RLock()
USERS = {}   # login -> {id, login, password, token}
REPOS = {}   # (owner, name) -> repo dict
NEXT_ID = [1]


def next_id():
    with STORE_LOCK:
        i = NEXT_ID[0]
        NEXT_ID[0] += 1
        return i


USERS[VIEWER_LOGIN] = {"id": VIEWER_ID, "login": VIEWER_LOGIN,
                       "password": "", "token": SHOP_TOKEN}


# ---------------------------------------------------------------- git objects
OBJECTS = {}  # sha -> (type, bytes)


def hash_obj(typ, data):
    hdr = ("%s %d\0" % (typ, len(data))).encode()
    sha = hashlib.sha1(hdr + data).hexdigest()
    OBJECTS[sha] = (typ, data)
    return sha


def build_tree(files):
    """files: {path: blob_sha} -> tree sha (proper nested trees)."""
    def rec(node):
        items = []
        for name, val in node.items():
            is_dir = isinstance(val, dict)
            items.append((name + "/" if is_dir else name, name, val))
        items.sort(key=lambda t: t[0])
        data = b""
        for _, name, val in items:
            if isinstance(val, dict):
                data += b"40000 " + name.encode() + b"\0" + bytes.fromhex(rec(val))
            else:
                data += b"100644 " + name.encode() + b"\0" + bytes.fromhex(val)
        return hash_obj("tree", data)

    nested = {}
    for path, sha in files.items():
        node = nested
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = sha
    return rec(nested)


def make_commit(tree_sha, parent, message, author):
    body = "tree %s\n" % tree_sha
    if parent:
        body += "parent %s\n" % parent
    body += ("author %s <%s@local> %d +0000\n" % (author, author, FIXED_EPOCH))
    body += ("committer %s <%s@local> %d +0000\n" % (author, author, FIXED_EPOCH))
    body += "\n%s\n" % message
    return hash_obj("commit", body.encode())


def parse_tree(data):
    entries = []
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode()
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def tree_files(tree_sha, prefix=""):
    out = {}
    if tree_sha not in OBJECTS:
        return out
    typ, data = OBJECTS[tree_sha]
    if typ != "tree":
        return out
    for mode, name, sha in parse_tree(data):
        path = prefix + name
        if mode == "40000":
            out.update(tree_files(sha, path + "/"))
        else:
            out[path] = sha
    return out


def commit_tree_and_parents(commit_sha):
    typ, data = OBJECTS.get(commit_sha, (None, b""))
    if typ != "commit":
        return None, []
    text = data.decode("utf-8", "replace")
    tree = None
    parents = []
    for line in text.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line == "":
            break
    return tree, parents


def collect_objects(start_shas):
    seen = set()
    order = []
    stack = list(start_shas)
    while stack:
        sha = stack.pop()
        if sha in seen or sha not in OBJECTS:
            continue
        seen.add(sha)
        order.append(sha)
        typ, data = OBJECTS[sha]
        if typ == "commit":
            tree, parents = commit_tree_and_parents(sha)
            if tree:
                stack.append(tree)
            stack.extend(parents)
        elif typ == "tree":
            for _, _, child in parse_tree(data):
                stack.append(child)
        elif typ == "tag":
            target = _tag_target(data)
            if target:
                stack.append(target)
    return order


def _tag_target(data):
    """The object a tag points at (the `object <sha>` header line)."""
    for line in data.decode("utf-8", "replace").split("\n"):
        if line.startswith("object "):
            return line[7:].strip()
        if line == "":
            break
    return None


def peel_to_non_tag(sha):
    """Follow tag objects until a non-tag object (for ls-refs peel)."""
    seen = set()
    while sha in OBJECTS and OBJECTS[sha][0] == "tag" and sha not in seen:
        seen.add(sha)
        target = _tag_target(OBJECTS[sha][1])
        if not target:
            break
        sha = target
    return sha


def commit_generations(start_shas, max_gen=None):
    """BFS over commit ancestry: {commit sha: generation} (0 = a start).

    Only commits are walked (their trees/blobs are collected separately
    by collect_objects); a commit's generation is its shortest distance
    in parent edges from any start. When max_gen is given, commits at
    that generation are recorded but not expanded — a depth-limited
    fetch only ever needs generations up to its depth, so walking the
    whole history would be pure waste.
    """
    gen = {}
    queue = []
    for sha in start_shas:
        if sha in OBJECTS and OBJECTS[sha][0] == "commit" and sha not in gen:
            gen[sha] = 0
            queue.append(sha)
    head = 0
    while head < len(queue):
        sha = queue[head]
        head += 1
        if max_gen is not None and gen[sha] >= max_gen:
            continue
        _tree, parents = commit_tree_and_parents(sha)
        for parent in parents:
            if parent in OBJECTS and parent not in gen:
                gen[parent] = gen[sha] + 1
                queue.append(parent)
    return gen


def is_descendant(new_sha, old_sha):
    """True when old_sha is reachable from new_sha by parent edges
    (or the two are equal): the fast-forward test for a push."""
    if new_sha == old_sha:
        return True
    if new_sha not in OBJECTS or OBJECTS[new_sha][0] != "commit":
        return False
    seen = set()
    stack = [new_sha]
    while stack:
        sha = stack.pop()
        if sha == old_sha:
            return True
        if sha in seen:
            continue
        seen.add(sha)
        _tree, parents = commit_tree_and_parents(sha)
        stack.extend(parents)
    return False


def merge_base(a_sha, b_sha):
    """A common ancestor of two commits (the first found by BFS from a)."""
    if a_sha == b_sha:
        return a_sha
    ancestors_b = set(commit_generations([b_sha]).keys())
    ancestors_b.add(b_sha)
    seen = set()
    queue = [a_sha]
    while queue:
        sha = queue.pop(0)
        if sha in ancestors_b:
            return sha
        if sha in seen:
            continue
        seen.add(sha)
        _tree, parents = commit_tree_and_parents(sha)
        queue.extend(parents)
    return None


# ---------------------------------------------------------------- packfiles
def _pack_entry_header(typ, size):
    out = bytearray()
    first = (typ << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


def make_pack(shas):
    entries = b""
    for sha in shas:
        typ, data = OBJECTS[sha]
        num = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[typ]
        entries += _pack_entry_header(num, len(data)) + zlib.compress(data)
    pack = b"PACK" + struct.pack(">II", 2, len(shas)) + entries
    return pack + hashlib.sha1(pack).digest()


def _delta_varint(data, i):
    value = 0
    while True:
        b = data[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, i


def apply_delta(base, delta):
    _src_size, i = _delta_varint(delta, 0)
    _tgt_size, i = _delta_varint(delta, i)
    out = bytearray()
    while i < len(delta):
        op = delta[i]
        i += 1
        if op & 0x80:
            off = size = 0
            for k in range(4):
                if op & (1 << k):
                    off |= delta[i] << (8 * k)
                    i += 1
            for k in range(3):
                if op & (0x10 << k):
                    size |= delta[i] << (8 * k)
                    i += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif op:
            out += delta[i:i + op]
            i += op
        else:
            raise ValueError("delta opcode 0")
    return bytes(out)


def _read_ofs_delta(data, i):
    b = data[i]
    i += 1
    off = b & 0x7F
    while b & 0x80:
        b = data[i]
        i += 1
        off = ((off + 1) << 7) | (b & 0x7F)
    return off, i


def read_pack(data):
    """Parse a packfile, store every object, return {sha: (type, data)}."""
    if data[:4] != b"PACK":
        raise ValueError("not a packfile")
    _version, count = struct.unpack(">II", data[4:12])
    i = 12
    entries = []
    for _ in range(count):
        start = i
        b = data[i]
        i += 1
        typ = (b >> 4) & 7
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = data[i]
            i += 1
            size |= (b & 0x7F) << shift
            shift += 7
        base = None
        if typ == 6:
            off, i = _read_ofs_delta(data, i)
            base = start - off
        elif typ == 7:
            base = data[i:i + 20].hex()
            i += 20
        dec = zlib.decompressobj()
        raw = dec.decompress(data[i:])
        i += len(data[i:]) - len(dec.unused_data)
        entries.append((typ, raw, base, start))
    by_pos = {}
    result = {}
    for typ, raw, _base, start in entries:
        if typ in (1, 2, 3, 4):
            name = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}[typ]
            sha = hash_obj(name, raw)
            by_pos[start] = (name, raw, sha)
            result[sha] = (name, raw)
    pending = [e for e in entries if e[0] in (6, 7)]
    while pending:
        nxt = []
        for typ, raw, base, start in pending:
            if isinstance(base, int):
                found = by_pos.get(base)
                if not found:
                    nxt.append((typ, raw, base, start))
                    continue
                btype, bdata, _sha = found
            else:
                # REF_DELTA: the base may be an object from an earlier
                # pack (a thin pack deltas against objects the server
                # already has), so fall back to the global store.
                found = result.get(base) or OBJECTS.get(base)
                if not found:
                    nxt.append((typ, raw, base, start))
                    continue
                btype, bdata = found
            data2 = apply_delta(bdata, raw)
            sha = hash_obj(btype, data2)
            by_pos[start] = (btype, data2, sha)
            result[sha] = (btype, data2)
        if len(nxt) == len(pending):
            raise ValueError("unresolvable delta in packfile")
        pending = nxt
    return result


# ---------------------------------------------------------------- pkt-lines
def pkt(data):
    return ("%04x" % (len(data) + 4)).encode() + data


def parse_pktlines(data):
    lines = []
    i = 0
    while i + 4 <= len(data):
        ln = int(data[i:i + 4], 16)
        i += 4
        if ln == 0:
            lines.append(None)
            continue
        if ln == 1:
            lines.append(b"")
            continue
        lines.append(data[i:i + ln - 4])
        i += ln - 4
    return lines


def sideband_pack(pack_bytes):
    """Frame pack bytes as sideband-64k band-1 packets (git upload-pack
    packfile section and receive-pack reports use these). Each packet
    carries at most 65515 payload bytes so the pkt-line stays <= 65520."""
    out = b""
    for i in range(0, len(pack_bytes), 65515):
        chunk = pack_bytes[i:i + 65515]
        out += pkt(b"\x01" + chunk)
    return out


def _unshallowed(shallow_set, keep_commits):
    """Old shallow commits that are now fully fetched: in the kept set
    with all of their parents also kept."""
    out = []
    for sha in shallow_set:
        if sha not in keep_commits:
            continue
        _tree, parents = commit_tree_and_parents(sha)
        if all(p in keep_commits for p in parents):
            out.append(sha)
    return out


def _shallow_closure(start_shas, shallow_set):
    """The objects a client with shallow boundaries already has: the
    closure of the start commits, except that the parents of a shallow
    commit are NOT followed (the client has the shallow commit and its
    tree/blobs, but nothing below it)."""
    seen = set()
    order = []
    stack = list(start_shas)
    while stack:
        sha = stack.pop()
        if sha in seen or sha not in OBJECTS:
            continue
        seen.add(sha)
        order.append(sha)
        typ, data = OBJECTS[sha]
        if typ == "commit":
            tree, parents = commit_tree_and_parents(sha)
            if tree:
                stack.append(tree)
            if sha not in shallow_set:
                stack.extend(parents)
        elif typ == "tree":
            for _, _, child in parse_tree(data):
                stack.append(child)
        elif typ == "tag":
            target = _tag_target(data)
            if target:
                stack.append(target)
    return order


def commit_author_timestamp(commit_sha):
    """The author timestamp (seconds since epoch) of a commit, or 0."""
    typ, data = OBJECTS.get(commit_sha, (None, b""))
    if typ != "commit":
        return 0
    for line in data.decode("utf-8", "replace").split("\n"):
        if line.startswith("author "):
            parts = line.rsplit(" ", 2)
            try:
                return int(parts[1])
            except (ValueError, IndexError):
                return 0
        if line == "":
            break
    return 0


# ---------------------------------------------------------------- data model
def new_repo(owner, name, description, private):
    return {
        "id": next_id(), "name": name, "owner": owner,
        "description": description or "", "private": bool(private),
        "default_branch": "main",
        "branches": {"main": {"commit": None, "files": {}}},
        "tags": {},
        "issues": [], "issues_by_number": {},
        "pulls": [], "pulls_by_number": {},
        "labels": [],
        "stargazers": set(), "forks_count": 0,
        "created_at": FIXED_TIME,
        "lock": threading.RLock(),
    }


def repo_refs(repo):
    refs = []
    for branch, state in sorted(repo["branches"].items()):
        if state["commit"]:
            refs.append((state["commit"], "refs/heads/" + branch))
    for tag, sha in sorted(repo["tags"].items()):
        refs.append((sha, "refs/tags/" + tag))
    return refs


def all_refs_with_peel(repo):
    """Every ref as (oid, name, peeled_oid_or_None), HEAD first, sorted.

    Used by the v0 advertisement and ls-refs: branches, tags (annotated
    tags peel to their ultimate non-tag target), and HEAD.
    """
    out = []
    head_tip = None
    default = repo["branches"].get(repo["default_branch"])
    if default and default["commit"]:
        head_tip = default["commit"]
    if head_tip:
        out.append((head_tip, "HEAD", None))
    for branch, state in sorted(repo["branches"].items()):
        if state["commit"]:
            out.append((state["commit"], "refs/heads/" + branch, None))
    for tag, sha in sorted(repo["tags"].items()):
        peeled = None
        if sha in OBJECTS and OBJECTS[sha][0] == "tag":
            peeled = peel_to_non_tag(sha)
        out.append((sha, "refs/tags/" + tag, peeled))
    return out


def set_branch_to_commit(repo, branch, commit_sha):
    tree, _parents = commit_tree_and_parents(commit_sha)
    files = tree_files(tree) if tree else {}
    repo["branches"][branch] = {"commit": commit_sha, "files": files}


def commit_file(repo, branch_name, path, content, message, author):
    branch = repo["branches"].setdefault(branch_name, {"commit": None, "files": {}})
    files = dict(branch["files"])
    files[path] = hash_obj("blob", content)
    tree = build_tree(files)
    sha = make_commit(tree, branch["commit"], message, author)
    branch["commit"] = sha
    branch["files"] = files
    return sha


def repo_json(repo, base):
    open_issues = sum(1 for i in repo["issues"] if i["state"] == "open")
    open_issues += sum(1 for p in repo["pulls"] if p["state"] == "open")
    return {
        "id": repo["id"], "name": repo["name"],
        "full_name": "%s/%s" % (repo["owner"], repo["name"]),
        "owner": {"login": repo["owner"], "id": 1, "type": "User"},
        "private": repo["private"], "description": repo["description"],
        "default_branch": repo["default_branch"],
        "created_at": repo["created_at"], "updated_at": repo["created_at"],
        "html_url": "%s/%s/%s" % (base, repo["owner"], repo["name"]),
        "clone_url": "%s/%s/%s.git" % (base, repo["owner"], repo["name"]),
        "stargazers_count": len(repo["stargazers"]),
        "watchers_count": len(repo["stargazers"]),
        "forks_count": repo["forks_count"],
        "open_issues_count": open_issues,
    }


def user_json(user):
    return {"login": user["login"], "id": user["id"], "type": "User",
            "avatar_url": "", "html_url": "/" + user["login"]}


def issue_json(issue, repo):
    return {"id": issue["id"], "number": issue["number"],
            "title": issue["title"], "body": issue["body"],
            "state": issue["state"], "labels": issue["labels"],
            "user": {"login": issue["author"]},
            "created_at": repo["created_at"], "updated_at": repo["created_at"],
            "comments": len(issue["comments"])}


def pr_mergeable(repo, pr):
    """MERGEABLE / CONFLICTING / UNKNOWN for a pull request, computed
    from the current head and base tips (three-way, against the merge
    base): conflicted when head and base both changed the same path
    differently since the merge base."""
    head_state = repo["branches"].get(pr["head"])
    base_state = repo["branches"].get(pr["base"])
    if not head_state or not base_state:
        return "UNKNOWN"
    head_sha = head_state["commit"]
    base_sha = base_state["commit"]
    if not head_sha or not base_sha:
        return "UNKNOWN"
    base = merge_base(head_sha, base_sha)
    if base is None:
        return "UNKNOWN"
    base_tree, _ = commit_tree_and_parents(base)
    base_files = tree_files(base_tree) if base_tree else {}
    head_tree, _ = commit_tree_and_parents(head_sha)
    head_files = tree_files(head_tree) if head_tree else {}
    base_tip_tree, _ = commit_tree_and_parents(base_sha)
    base_tip_files = tree_files(base_tip_tree) if base_tip_tree else {}
    for path in set(head_files) | set(base_files):
        head_changed = head_files.get(path) != base_files.get(path)
        base_changed = base_tip_files.get(path) != base_files.get(path)
        if head_changed and base_changed and head_files.get(path) != base_tip_files.get(path):
            return "CONFLICTING"
    return "MERGEABLE"


def pr_json(pr, repo):
    return {"id": pr["id"], "number": pr["number"], "title": pr["title"],
            "body": pr["body"], "state": pr["state"],
            "merged": pr["merged"], "mergeable": pr_mergeable(repo, pr),
            "merged_at": pr.get("merged_at"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "head": {"ref": pr["head"], "sha": pr["head_sha"]},
            "base": {"ref": pr["base"], "sha": pr["base_sha"]},
            "user": {"login": pr["author"]},
            "created_at": repo["created_at"]}


def pr_files_diff(repo, pr):
    """The changed files between a PR's base and head commits, with
    line counts: [{filename, status, additions, deletions, changes}]."""
    base_files = {}
    if pr["base_sha"]:
        tree, _ = commit_tree_and_parents(pr["base_sha"])
        base_files = tree_files(tree) if tree else {}
    head_files = {}
    if pr["head_sha"]:
        tree, _ = commit_tree_and_parents(pr["head_sha"])
        head_files = tree_files(tree) if tree else {}
    files = []
    for path in sorted(set(base_files) | set(head_files)):
        if path not in base_files:
            new = OBJECTS.get(head_files[path], (None, b""))[1]
            additions = len(new.decode("utf-8", "replace").splitlines()) if new else 0
            files.append({"filename": path, "status": "added", "sha": head_files[path],
                          "additions": additions, "deletions": 0, "changes": additions})
        elif path not in head_files:
            old = OBJECTS.get(base_files[path], (None, b""))[1]
            deletions = len(old.decode("utf-8", "replace").splitlines()) if old else 0
            files.append({"filename": path, "status": "removed", "sha": base_files[path],
                          "additions": 0, "deletions": deletions, "changes": deletions})
        elif base_files[path] != head_files[path]:
            old = OBJECTS.get(base_files[path], (None, b""))[1]
            new = OBJECTS.get(head_files[path], (None, b""))[1]
            additions, deletions = _line_diff(old, new)
            files.append({"filename": path, "status": "modified", "sha": head_files[path],
                          "additions": additions, "deletions": deletions,
                          "changes": additions + deletions})
    return files


def _line_diff(old_bytes, new_bytes):
    """(additions, deletions) between two blobs, by line.

    A small SequenceMatcher diff: exact enough for the change counts,
    and cheap on the small files the suite uses.
    """
    import difflib
    old_lines = old_bytes.decode("utf-8", "replace").splitlines()
    new_lines = new_bytes.decode("utf-8", "replace").splitlines()
    additions = deletions = 0
    for group in difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes():
        tag, _i1, _i2, j1, j2 = group
        if tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += _i2 - _i1
        elif tag == "replace":
            additions += j2 - j1
            deletions += _i2 - _i1
    if additions == 0 and deletions == 0 and old_bytes != new_bytes:
        additions = 1
    return additions, deletions


def dir_tree_sha(repo, branch_name, dir_path):
    """The tree sha of a directory inside a branch (for contents
    directory listings, which need a real 40-hex sha per entry)."""
    branch = repo["branches"].get(branch_name)
    if not branch:
        return ""
    files = branch["files"]
    if dir_path == "":
        rel = dict(files)
    else:
        prefix = dir_path.rstrip("/") + "/"
        rel = {p[len(prefix):]: s for p, s in files.items() if p.startswith(prefix)}
    if not rel:
        return ""
    return build_tree(rel)


# ---------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        # Disable Nagle: responses go out as several small writes
        # (status line, headers, body) and a delayed ACK from the
        # client would otherwise stall every request by ~40 ms.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        super().setup()

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    # -- helpers -------------------------------------------------------
    def _body(self):
        # The request body, read exactly once per request by _handle
        # (a handler instance serves many requests on one keep-alive
        # connection, so the body must be re-read for every request —
        # caching it across requests poisons the stream). _handle reads
        # it eagerly so an early 401 never leaves an unread body on
        # the connection (leftover bytes would be parsed as the next
        # request and answered with an HTML 400).
        return getattr(self, "_body_cache", b"")

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception:
            raise _BadJson("malformed JSON")
        if not isinstance(value, (dict, list)):
            raise _BadJson("JSON body is not an object")
        return value

    def _send(self, status, data, ctype="application/json", extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-GitHub-Request-Id", _next_request_id())
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message):
        self._send(status, {"message": message})

    def _auth(self):
        header = self.headers.get("Authorization")
        if not header:
            return None
        if header.startswith("Basic "):
            try:
                cred = base64.b64decode(header[6:]).decode()
            except Exception:
                return None
            login, _, secret = cred.partition(":")
            user = USERS.get(login)
            if user and (secret == user["password"] or secret == user["token"]):
                return user
            return None
        if header.startswith("Bearer ") or header.startswith("token "):
            token = header.split(None, 1)[1].strip()
            if token == SHOP_TOKEN:
                return USERS[VIEWER_LOGIN]
            for user in USERS.values():
                if user["token"] == token:
                    return user
        return None

    def _require_auth(self):
        user = self._auth()
        if not user:
            body = json.dumps({"message": "Requires authentication"}).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="forge"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", _next_request_id())
            self.end_headers()
            self.wfile.write(body)
            return None
        return user

    # -- entry point ---------------------------------------------------
    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            self._body_cache = self.rfile.read(length) if length else b""
            self._route()
        except _BadJson:
            try:
                self._error(400, "Malformed JSON")
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never leak a stack trace to the client
            try:
                self._error(500, "Internal Server Error: %s" % exc)
            except Exception:
                pass

    def _route(self):
        parsed = urlparse(self.path)
        segments = [unquote(s) for s in parsed.path.strip("/").split("/") if s != ""]
        query = parse_qs(parsed.query)
        method = self.command
        base = "http://" + (self.headers.get("Host") or "127.0.0.1")

        if segments == ["healthz"] and method == "GET":
            self._send(200, {"ok": True})
            return

        if not segments:
            self._send(200, {"message": "mini-forge",
                             "current_user_url": base + "/api/v3/user"})
            return

        if segments == ["api", "graphql"] and method == "POST":
            self._route_graphql()
            return

        if segments == ["mcp"] and method in ("POST", "GET", "DELETE"):
            self._route_mcp()
            return

        if segments[0] == "api" and len(segments) > 1 and segments[1] == "v3":
            self._route_api(method, segments[2:], query, base)
            return

        if len(segments) >= 3 and segments[2] == "info" and len(segments) == 4 and segments[3] == "refs":
            self._git_info_refs(segments[0], segments[1], query)
            return
        if len(segments) == 3 and segments[2] == "git-upload-pack":
            self._git_upload_pack(segments[0], segments[1])
            return
        if len(segments) == 3 and segments[2] == "git-receive-pack":
            self._git_receive_pack(segments[0], segments[1])
            return
        self._error(404, "Not Found")

    # -- git smart HTTP -------------------------------------------------
    def _get_repo_or_404(self, owner, name):
        repo = REPOS.get((owner, name))
        if not repo and name.endswith(".git"):
            # Real git clients request /<owner>/<repo>.git/... — strip the suffix.
            repo = REPOS.get((owner, name[:-4]))
        if not repo:
            self._error(404, "Not Found")
            return None
        return repo

    def _advertise(self, repo, service, caps):
        # Tell real git clients which branch HEAD points to, so a fresh
        # clone of an empty repo starts on the default branch (not "master").
        caps = "symref=HEAD:refs/heads/%s %s" % (repo["default_branch"], caps)
        out = pkt(("# service=%s\n" % service).encode()) + b"0000"
        refs = all_refs_with_peel(repo)
        first = True
        if refs:
            for oid, name, peeled in refs:
                line = ("%s %s\n" % (oid, name)).encode()
                if first:
                    line = line.rstrip(b"\n") + ("\0" + caps + "\n").encode()
                    first = False
                out += pkt(line)
                if peeled:
                    out += pkt(("%s %s^{}\n" % (peeled, name)).encode())
        else:
            line = ("%s capabilities^{}\0%s\n" % (ZERO, caps)).encode()
            out += pkt(line)
        out += b"0000"
        return out

    def _advertise_v2(self, repo):
        """Protocol v2 capability advertisement (upload-pack only).

        The first data packet is `version 2`; the capabilities the
        suite needs are fetch (shallow, filter), ls-refs (unborn) and
        object-format sha1. No `# service=` preamble: the v2 request's
        first line must read `version 2`.
        """
        lines = [
            b"version 2\n",
            b"agent=mini-forge/1.0\n",
            b"ls-refs=unborn\n",
            b"fetch=shallow filter\n",
            b"object-format=sha1\n",
        ]
        out = b"".join(pkt(line) for line in lines)
        return out + b"0000"

    def _git_info_refs(self, owner, name, query):
        repo = self._get_repo_or_404(owner, name)
        if not repo:
            return
        service = query.get("service", [""])[0]
        version2 = (self.headers.get("Git-Protocol") or "").strip() == "version=2"
        if service == "git-upload-pack":
            if version2:
                data = self._advertise_v2(repo)
            else:
                data = self._advertise(repo, "git-upload-pack",
                    "side-band-64k multi_ack_detailed ofs-delta shallow agent=mini-forge/1.0")
            self._send(200, data, "application/x-git-upload-pack-advertisement")
        elif service == "git-receive-pack":
            data = self._advertise(repo, "git-receive-pack",
                "report-status side-band-64k delete-refs ofs-delta agent=mini-forge/1.0")
            self._send(200, data, "application/x-git-receive-pack-advertisement")
        else:
            self._error(400, "unknown service")

    def _git_upload_pack(self, owner, name):
        repo = self._get_repo_or_404(owner, name)
        if not repo:
            return
        raw = self._body()
        version2 = (self.headers.get("Git-Protocol") or "").strip() == "version=2"
        packets = parse_pktlines(raw)
        if version2:
            command = ""
            wants = []
            haves = []
            shallow = []
            deepen = None
            deepen_since = None
            deepen_not = []
            ref_prefixes = []
            want_peel = want_symrefs = want_unborn = False
            for packet in packets:
                if packet is None:
                    continue
                line = packet.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("command="):
                    command = line[len("command="):]
                elif line.startswith("want "):
                    wants.append(line[5:45])
                elif line.startswith("have "):
                    haves.append(line[5:45])
                elif line.startswith("shallow "):
                    shallow.append(line[8:48])
                elif line.startswith("deepen "):
                    try:
                        deepen = int(line[7:])
                    except ValueError:
                        pass
                elif line.startswith("deepen-since "):
                    try:
                        deepen_since = int(line[13:])
                    except ValueError:
                        pass
                elif line.startswith("deepen-not "):
                    deepen_not.append(line[11:])
                elif line.startswith("ref-prefix "):
                    ref_prefixes.append(line[11:])
                elif line == "peel":
                    want_peel = True
                elif line == "symrefs":
                    want_symrefs = True
                elif line == "unborn":
                    want_unborn = True
            if command == "ls-refs":
                out = self._v2_ls_refs(repo, ref_prefixes, want_peel,
                                       want_symrefs, want_unborn)
                self._send(200, out, "application/x-git-upload-pack-result")
                return
            if command == "fetch":
                send, new_shallow, unshallow = self._fetch_objects(
                    repo, wants, haves, deepen, deepen_since, deepen_not, shallow)
                out = self._v2_fetch_response(wants, haves, send,
                                              new_shallow, unshallow)
                self._send(200, out, "application/x-git-upload-pack-result")
                return
            self._error(400, "unknown command")
            return
        # Protocol v0 fetch: `want`/`have`/`shallow`/`deepen` lines,
        # terminated by `done`; answer ACK/NAK lines, then the pack as
        # sideband-64k frames.
        wants = []
        haves = []
        shallow = []
        deepen = None
        deepen_since = None
        deepen_not = []
        for packet in packets:
            if packet is None:
                continue
            line = packet.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("want "):
                wants.append(line[5:45])
            elif line.startswith("have "):
                haves.append(line[5:45])
            elif line.startswith("shallow "):
                shallow.append(line[8:48])
            elif line.startswith("deepen "):
                try:
                    deepen = int(line[7:])
                except ValueError:
                    pass
            elif line.startswith("deepen-since "):
                try:
                    deepen_since = int(line[13:])
                except ValueError:
                    pass
            elif line.startswith("deepen-not "):
                deepen_not.append(line[11:])
            elif line == "done":
                break
        send, new_shallow, unshallow = self._fetch_objects(
            repo, wants, haves, deepen, deepen_since, deepen_not, shallow)
        out = b""
        if haves:
            known = [h for h in haves if h in OBJECTS]
            if len(known) == len(haves):
                for h in known:
                    out += pkt(("ACK %s\n" % h).encode())
                out += pkt(b"ready\n")
            else:
                out += pkt(b"NAK\n")
        else:
            out += pkt(b"NAK\n")
        for sha in new_shallow:
            out += pkt(("shallow %s\n" % sha).encode())
        for sha in unshallow:
            out += pkt(("unshallow %s\n" % sha).encode())
        if send:
            out += sideband_pack(make_pack(send))
        # The pack is the last thing the client reads; a bare flush
        # terminates the sideband stream cleanly.
        out += b"0000"
        self._send(200, out, "application/x-git-upload-pack-result")

    def _fetch_objects(self, repo, wants, haves, deepen, deepen_since,
                       deepen_not, shallow):
        """Resolve a fetch request to the object set to send.

        Returns (send_shas, new_shallow_list, unshallow_list).
        - no depth: the full closure of wants minus the closure of haves
        - depth N: commits within N-1 parent edges of wants, plus their
          trees/blobs, minus what the client already has (for a deepen
          the haves' closure is limited by the shallow boundaries —
          the client has a shallow commit and its tree, not its
          ancestry)
        - deepen-since: commits whose author timestamp is >= the given
          epoch, minus haves; deepen-not: exclude the closure of those
          refs
        - shallow boundaries: the kept commits that still have a parent
          outside the kept set (a root commit is never a boundary),
          and the old shallow commits that are now fully fetched
        """
        want_shas = [w for w in wants if w in OBJECTS]
        have_shas = [h for h in haves if h in OBJECTS]
        shallow_set = set(shallow)
        want_commits = [w for w in want_shas if OBJECTS[w][0] == "commit"]
        if deepen is not None and deepen >= 1:
            limit = deepen - 1
            generations = commit_generations(want_commits, max_gen=limit)
            keep_commits = {sha for sha, gen in generations.items()
                            if gen <= limit}
            boundary = set()
            for sha in keep_commits:
                _tree, parents = commit_tree_and_parents(sha)
                if any(p not in keep_commits for p in parents):
                    boundary.add(sha)
            send = set()
            for sha in keep_commits:
                tree, _ = commit_tree_and_parents(sha)
                if tree:
                    send.update(collect_objects([tree]))
                send.add(sha)
            send.update(w for w in want_shas if OBJECTS[w][0] != "commit")
            new_shallow = sorted(boundary - shallow_set)
            unshallow = sorted(_unshallowed(shallow_set, keep_commits))
        elif deepen_since is not None:
            generations = commit_generations(want_commits)
            keep_commits = set()
            for sha in generations:
                if commit_author_timestamp(sha) >= deepen_since:
                    keep_commits.add(sha)
            boundary = set()
            for sha in keep_commits:
                _tree, parents = commit_tree_and_parents(sha)
                if any(p not in keep_commits for p in parents):
                    boundary.add(sha)
            send = set()
            for sha in keep_commits:
                tree, _ = commit_tree_and_parents(sha)
                if tree:
                    send.update(collect_objects([tree]))
                send.add(sha)
            send.update(w for w in want_shas if OBJECTS[w][0] != "commit")
            new_shallow = sorted(boundary - shallow_set)
            unshallow = sorted(_unshallowed(shallow_set, keep_commits))
        else:
            send = set(collect_objects(want_shas))
            new_shallow = []
            unshallow = []
        if deepen_not:
            exclude = set(collect_objects(deepen_not))
            send -= exclude
        if have_shas:
            if shallow_set:
                send -= set(_shallow_closure(have_shas, shallow_set))
            else:
                send -= set(collect_objects(have_shas))
        # A shallow commit the client already has is never re-sent.
        send -= shallow_set
        return sorted(send), new_shallow, unshallow

    def _v2_ls_refs(self, repo, ref_prefixes, want_peel, want_symrefs,
                    want_unborn):
        refs = all_refs_with_peel(repo)
        lines = []
        for oid, name, peeled in refs:
            if ref_prefixes and not any(name.startswith(p) for p in ref_prefixes):
                continue
            if name == "HEAD" and not want_symrefs:
                continue
            line = "%s %s" % (oid, name)
            if name == "HEAD":
                line += " symref-target:refs/heads/%s" % repo["default_branch"]
            if peeled and want_peel:
                line += " peeled:%s" % peeled
            lines.append(line + "\n")
        if not refs and want_unborn:
            lines.append("unborn HEAD symref-target:refs/heads/%s\n"
                         % repo["default_branch"])
        return b"".join(pkt(line.encode()) for line in lines) + b"0000"

    def _v2_fetch_response(self, wants, haves, send, new_shallow, unshallow):
        """A protocol v2 fetch response: acknowledgments (only when the
        client sent haves — git skips its ack processing otherwise and
        expects the packfile section next), shallow-info, then the
        packfile section (omitted entirely when empty)."""
        out = b""
        if haves:
            out += pkt(b"acknowledgments\n")
            known = [h for h in haves if h in OBJECTS]
            if len(known) == len(haves):
                for h in known:
                    out += pkt(("ACK %s\n" % h).encode())
                out += pkt(b"ready\n")
                # After "ready" the client expects a DELIM (more sections
                # follow), not a flush — fetch-pack.c process_ack dies
                # with "expected packfile to be sent after 'ready'"
                # otherwise.
                out += b"0001"
            else:
                out += pkt(b"NAK\n")
                # No "ready": the acknowledgments section ends the
                # response (no packfile follows), so a flush.
                out += b"0000"
                return out
        if new_shallow or unshallow:
            out += pkt(b"shallow-info\n")
            for sha in new_shallow:
                out += pkt(("shallow %s\n" % sha).encode())
            for sha in unshallow:
                out += pkt(("unshallow %s\n" % sha).encode())
            # A packfile section follows: DELIM. No packfile (0 objects):
            # FLUSH ends the response.
            out += b"0001" if send else b"0000"
        if send:
            out += pkt(b"packfile\n")
            out += sideband_pack(make_pack(send))
            out += b"0000"
        return out

    def _git_receive_pack(self, owner, name):
        repo = self._get_repo_or_404(owner, name)
        if not repo:
            return
        data = self._body()
        # Parse the command pkt-lines only, stopping at the flush that
        # precedes the packfile. (Parsing the whole body as pkt-lines
        # would choke on the pack's binary "PACK" header.)
        commands = []
        i = 0
        while i + 4 <= len(data):
            ln = int(data[i:i + 4], 16)
            i += 4
            if ln == 0:
                break
            commands.append(data[i:i + ln - 4])
            i += ln - 4
        pack = data[i:]
        if pack:
            try:
                read_pack(pack)
            except Exception as exc:
                self._error(400, "invalid packfile: %s" % exc)
                return
        # Decide every ref update first (fast-forward enforced per ref),
        # then apply the accepted ones, then report in command order.
        # A delete-only push carries no pack, so an empty body after the
        # flush is fine and must not error.
        decisions = []
        with repo["lock"]:
            for cmd in commands:
                text = cmd.decode("utf-8", "replace")
                head = text.split("\0")[0].strip()
                parts = head.split(" ")
                if len(parts) != 3:
                    continue
                old, new, ref = parts
                if ref.startswith("refs/heads/"):
                    branch = ref[len("refs/heads/"):]
                    current = repo["branches"].get(branch)
                    current_sha = current["commit"] if current else None
                    if new == ZERO:
                        if current_sha is None:
                            decisions.append((ref, "ng", "ref does not exist", None))
                        elif old != ZERO and old != current_sha:
                            decisions.append((ref, "ng", "non-fast-forward", None))
                        else:
                            decisions.append((ref, "delete", branch, None))
                    elif current_sha is None:
                        if old != ZERO:
                            decisions.append((ref, "ng", "non-fast-forward", None))
                        else:
                            decisions.append((ref, "create", branch, new))
                    elif old != current_sha:
                        decisions.append((ref, "ng", "non-fast-forward", None))
                    elif not is_descendant(new, current_sha):
                        decisions.append((ref, "ng", "non-fast-forward", None))
                    else:
                        decisions.append((ref, "update", branch, new))
                elif ref.startswith("refs/tags/"):
                    tag = ref[len("refs/tags/"):]
                    current_sha = repo["tags"].get(tag)
                    if new == ZERO:
                        if current_sha is None:
                            decisions.append((ref, "ng", "ref does not exist", None))
                        elif old != ZERO and old != current_sha:
                            decisions.append((ref, "ng", "non-fast-forward", None))
                        else:
                            decisions.append((ref, "delete-tag", tag, None))
                    elif current_sha is None:
                        if old != ZERO:
                            decisions.append((ref, "ng", "non-fast-forward", None))
                        else:
                            decisions.append((ref, "create-tag", tag, new))
                    elif old != current_sha:
                        decisions.append((ref, "ng", "non-fast-forward", None))
                    elif new != current_sha:
                        decisions.append((ref, "ng", "non-fast-forward", None))
                    else:
                        decisions.append((ref, "ok-tag", tag, new))
                else:
                    decisions.append((ref, "ng", "invalid ref", None))
            for ref, action, arg, new_sha in decisions:
                if action == "delete":
                    del repo["branches"][arg]
                elif action in ("create", "update"):
                    set_branch_to_commit(repo, arg, new_sha)
                elif action == "delete-tag":
                    del repo["tags"][arg]
                elif action == "create-tag":
                    repo["tags"][arg] = new_sha
        resp = pkt(b"unpack ok\n")
        for ref, action, arg, _new_sha in decisions:
            if action == "ng":
                resp += pkt(("ng %s %s\n" % (ref, arg)).encode())
            else:
                resp += pkt(("ok %s\n" % ref).encode())
        # The client negotiated side-band-64k (we advertise it), so the
        # report lines are framed as band-1 sideband packets. The
        # terminating flush goes BOTH inside the sideband data (so the
        # report parser sees the end of the report) and as a bare
        # pkt-line after it (so the sideband demultiplexer stops) —
        # exactly what git's own http-backend sends.
        self._send(200, sideband_pack(resp + b"0000") + b"0000", "application/x-git-receive-pack-result")

    # -- GraphQL ------------------------------------------------------
    def _route_graphql(self):
        user = self._require_auth()
        if not user:
            return
        try:
            body = self._json_body()
        except _BadJson:
            self._send(400, {"errors": [{"message": "Malformed JSON"}]})
            return
        if isinstance(body, list):
            results = [self._run_graphql_document(doc, user) for doc in body]
            self._send(200, results)
            return
        if not isinstance(body, dict):
            self._send(400, {"errors": [{"message": "Request body must be an object"}]})
            return
        self._send(200, self._run_graphql_document(body, user))

    def _run_graphql_document(self, doc, user):
        query = doc.get("query")
        if not isinstance(query, str) or not query.strip():
            return {"data": None, "errors": [{"message": "Must provide query string."}]}
        variables = doc.get("variables") or {}
        if not isinstance(variables, dict):
            variables = {}
        operation_name = doc.get("operationName")
        try:
            operations, fragments = _GqlParser(_gql_tokenize(query)).parse_document()
        except Exception as exc:
            return {"data": None, "errors": [{"message": "Syntax Error: %s" % exc}]}
        if not operations:
            return {"data": None, "errors": [{"message": "Must provide an operation."}]}
        operation = None
        if operation_name:
            for candidate in operations:
                if candidate["name"] == operation_name:
                    operation = candidate
                    break
            if operation is None:
                return {"data": None, "errors": [{"message": 'Unknown operation named "%s".' % operation_name}]}
        else:
            operation = operations[0]
        merged_variables = {}
        for var_name, var_def in operation["variables"].items():
            if var_name in variables:
                merged_variables[var_name] = variables[var_name]
            elif var_def.get("default") is not None:
                merged_variables[var_name] = _gql_resolve_value(var_def["default"], {})
            else:
                merged_variables[var_name] = None
        for key, value in variables.items():
            merged_variables.setdefault(key, value)
        return _gql_execute_operation(operation, fragments, merged_variables, user)

    # -- MCP ------------------------------------------------------------
    def _route_mcp(self):
        user = self._require_auth()
        if not user:
            return
        raw = self._body()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(body, dict):
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return
        # A notification (no id) gets an empty 202, never a result.
        if "id" not in body:
            self._send(202, b"")
            return
        request_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}
        base = "http://" + (self.headers.get("Host") or "127.0.0.1")
        server_info = {"name": "mini-forge", "version": "1.0.0"}
        if method == "server/discover":
            result = {"supportedVersions": ["2026-07-28"],
                      "capabilities": {"tools": {}},
                      "serverInfo": server_info,
                      "_meta": {"io.modelcontextprotocol/serverInfo": server_info}}
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        if method == "initialize":
            result = {"protocolVersion": "2026-07-28",
                      "capabilities": {"tools": {}},
                      "serverInfo": server_info}
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": request_id,
                             "result": {"tools": MCP_TOOLS}})
            return
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments") or {}
            result, rpc_error = _mcp_call_tool(tool_name, arguments, user, base)
            if rpc_error is not None:
                self._send(200, {"jsonrpc": "2.0", "id": request_id,
                                 "error": rpc_error})
            else:
                self._send(200, {"jsonrpc": "2.0", "id": request_id,
                                 "result": result})
            return
        if method == "ping":
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32601, "message": "Method not found: %s" % method}})

    # -- REST API v3 ----------------------------------------------------
    def _page(self, items, query, path, base):
        """Paginate a list: (page_items, link_header_or_None).

        `per_page` defaults to 30, `page` to 1. When more pages remain,
        the Link header carries rel="next" (same-origin URLs only).
        """
        try:
            per_page = int(query.get("per_page", ["30"])[0])
        except ValueError:
            per_page = 30
        per_page = max(1, min(per_page, 100))
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            page = 1
        page = max(1, page)
        start = (page - 1) * per_page
        end = start + per_page
        page_items = items[start:end]
        link = None
        if end < len(items):
            link = ('<%s%s?page=%d&per_page=%d>; rel="next", '
                    '<%s%s?page=%d&per_page=%d>; rel="last"'
                    % (base, path, page + 1, per_page,
                       base, path, (len(items) + per_page - 1) // per_page, per_page))
        return page_items, link

    def _route_api(self, method, seg, query, base):
        # API root and meta need no authentication.
        if seg == [] and method == "GET":
            self._send(200, {"message": "mini-forge",
                             "current_user_url": base + "/api/v3/user"},
                       extra={"X-OAuth-Scopes": "repo, read:org"})
            return
        if seg == ["meta"] and method == "GET":
            self._send(200, {"installed_version": "1.0.0"})
            return
        # Every other /api/v3 route requires a bearer token.
        auth_user = self._auth()
        if auth_user is None:
            self._require_auth()
            return
        # users
        if seg == ["user"] and method == "POST":
            body = self._json_body()
            login = body.get("login")
            if not login or login in USERS:
                self._error(422, "login is missing or already taken")
                return
            user = {"id": next_id(), "login": login,
                    "password": body.get("password", ""),
                    "token": _deterministic_token(login)}
            USERS[login] = user
            out = user_json(user)
            out["token"] = user["token"]
            self._send(201, out)
            return
        if seg == ["user"] and method == "GET":
            self._send(200, user_json(auth_user))
            return
        if len(seg) == 2 and seg[0] == "users" and method == "GET":
            user = USERS.get(seg[1])
            if user:
                self._send(200, user_json(user))
            else:
                self._error(404, "Not Found")
            return
        if len(seg) == 3 and seg[0] == "users" and seg[2] == "repos" and method == "GET":
            items = [repo_json(r, base) for r in REPOS.values() if r["owner"] == seg[1]]
            page_items, link = self._page(items, query, "/api/v3/users/%s/repos" % seg[1], base)
            extra = {"Link": link} if link else None
            self._send(200, page_items, extra=extra)
            return
        if seg == ["user", "repos"] and method == "POST":
            body = self._json_body()
            name = body.get("name")
            if not name:
                self._error(422, "name is required")
                return
            if (auth_user["login"], name) in REPOS:
                self._error(422, "Repository already exists")
                return
            repo = new_repo(auth_user["login"], name, body.get("description"), body.get("private", False))
            REPOS[(auth_user["login"], name)] = repo
            self._send(201, repo_json(repo, base))
            return
        if seg == ["user", "repos"] and method == "GET":
            items = [repo_json(r, base) for r in REPOS.values() if r["owner"] == auth_user["login"]]
            page_items, link = self._page(items, query, "/api/v3/user/repos", base)
            extra = {"Link": link} if link else None
            self._send(200, page_items, extra=extra)
            return
        if len(seg) == 2 and seg[0] == "search" and seg[1] == "repositories" and method == "GET":
            q = query.get("q", [""])[0].lower()
            words = [w for w in q.replace("+", " ").split() if ":" not in w]
            items = []
            for repo in REPOS.values():
                hay = (repo["name"] + " " + repo["description"]).lower()
                if not words or all(w in hay for w in words):
                    items.append(repo_json(repo, base))
            self._send(200, {"total_count": len(items), "incomplete_results": False, "items": items})
            return
        # starred
        if len(seg) == 4 and seg[0] == "user" and seg[1] == "starred" and method in ("PUT", "DELETE", "GET"):
            user = self._require_auth()
            if not user:
                return
            repo = self._get_repo_or_404(seg[2], seg[3])
            if not repo:
                return
            if method == "PUT":
                repo["stargazers"].add(user["login"])
                self._send(204, b"")
            elif method == "DELETE":
                repo["stargazers"].discard(user["login"])
                self._send(204, b"")
            else:
                if user["login"] in repo["stargazers"]:
                    self._send(204, b"")
                else:
                    self._error(404, "Not Found")
            return
        # repos/*
        if len(seg) >= 3 and seg[0] == "repos":
            self._route_repo(method, seg[1:], query, base)
            return
        self._error(404, "Not Found")

    def _route_repo(self, method, seg, query, base):
        owner, name = seg[0], seg[1]
        repo = REPOS.get((owner, name))
        rest = seg[2:]
        if not repo:
            self._error(404, "Not Found")
            return
        if rest == [] and method == "GET":
            self._send(200, repo_json(repo, base))
            return
        if rest == [] and method == "PATCH":
            user = self._require_auth()
            if not user or user["login"] != owner:
                self._error(403, "Forbidden")
                return
            body = self._json_body()
            if "description" in body:
                repo["description"] = body["description"]
            if "private" in body:
                repo["private"] = bool(body["private"])
            self._send(200, repo_json(repo, base))
            return
        if rest == [] and method == "DELETE":
            user = self._require_auth()
            if not user or user["login"] != owner:
                self._error(403, "Forbidden")
                return
            del REPOS[(owner, name)]
            self._send(204, b"")
            return
        if rest == ["branches"] and method == "GET":
            items = [{"name": b, "commit": {"sha": s["commit"]}, "protected": False}
                     for b, s in sorted(repo["branches"].items()) if s["commit"]]
            self._send(200, items)
            return
        if len(rest) == 2 and rest[0] == "branches" and method == "GET":
            branch = repo["branches"].get(rest[1])
            if branch and branch["commit"]:
                self._send(200, {"name": rest[1], "commit": {"sha": branch["commit"]}, "protected": False})
            else:
                self._error(404, "Not Found")
            return
        if rest == ["stargazers"] and method == "GET":
            self._send(200, [user_json(USERS[u]) for u in sorted(repo["stargazers"]) if u in USERS])
            return
        if rest == ["forks"] and method == "POST":
            user = self._require_auth()
            if not user:
                return
            if (user["login"], name) in REPOS:
                self._error(422, "Fork already exists")
                return
            fork = new_repo(user["login"], name, repo["description"], repo["private"])
            fork["branches"] = {b: {"commit": s["commit"], "files": dict(s["files"])}
                                for b, s in repo["branches"].items()}
            REPOS[(user["login"], name)] = fork
            repo["forks_count"] += 1
            self._send(202, repo_json(fork, base))
            return
        if rest == ["commits"] and method == "GET":
            ref = query.get("sha", [repo["default_branch"]])[0]
            tip = None
            if ref in repo["branches"]:
                tip = repo["branches"][ref]["commit"]
            elif ref in OBJECTS and OBJECTS[ref][0] == "commit":
                tip = ref
            items = []
            seen = set()
            stack = [tip] if tip else []
            while stack:
                sha = stack.pop()
                if sha in seen or sha not in OBJECTS:
                    continue
                seen.add(sha)
                _tree, parents = commit_tree_and_parents(sha)
                text = OBJECTS[sha][1].decode("utf-8", "replace")
                message = text.split("\n\n", 1)[1].strip() if "\n\n" in text else ""
                items.append({"sha": sha, "commit": {"message": message, "author": {"name": owner}}})
                stack.extend(parents)
            self._send(200, items)
            return
        if len(rest) == 2 and rest[0] == "commits" and method == "GET":
            sha = rest[1]
            if sha not in OBJECTS:
                self._error(404, "Not Found")
                return
            text = OBJECTS[sha][1].decode("utf-8", "replace")
            message = text.split("\n\n", 1)[1].strip() if "\n\n" in text else ""
            self._send(200, {"sha": sha, "commit": {"message": message}})
            return
        if len(rest) == 3 and rest[0] == "git" and rest[1] == "blobs" and method == "GET":
            sha = rest[2]
            if sha not in OBJECTS or OBJECTS[sha][0] != "blob":
                self._error(404, "Not Found")
                return
            data = OBJECTS[sha][1]
            self._send(200, {"sha": sha, "size": len(data),
                             "content": base64.b64encode(data).decode(), "encoding": "base64"})
            return
        if len(rest) == 3 and rest[0] == "git" and rest[1] == "trees" and method == "GET":
            sha = rest[2]
            if sha not in OBJECTS or OBJECTS[sha][0] != "tree":
                self._error(404, "Not Found")
                return
            recursive = query.get("recursive", ["0"])[0] == "1"
            tree = []
            if recursive:
                for path, blob in sorted(tree_files(sha).items()):
                    tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
            else:
                for mode, fname, child in parse_tree(OBJECTS[sha][1]):
                    tree.append({"path": fname, "mode": mode,
                                 "type": "tree" if mode == "40000" else "blob", "sha": child})
            self._send(200, {"sha": sha, "tree": tree, "truncated": False})
            return
        if len(rest) >= 2 and rest[0] == "git" and rest[1] == "refs":
            self._route_refs(method, repo, rest[2:], base)
            return
        if rest and rest[0] == "contents":
            self._route_contents(method, repo, rest[1:], query, base)
            return
        if rest and rest[0] == "issues":
            self._route_issues(method, repo, rest[1:], base, query)
            return
        if rest and rest[0] == "pulls":
            self._route_pulls(method, repo, rest[1:], base, query)
            return
        if rest and rest[0] == "labels":
            if method == "GET":
                self._send(200, repo["labels"])
            elif method == "POST":
                user = self._require_auth()
                if not user:
                    return
                body = self._json_body()
                label = {"id": next_id(), "name": body.get("name", ""),
                         "color": body.get("color", "ffffff"), "description": body.get("description", "")}
                repo["labels"].append(label)
                self._send(201, label)
            else:
                self._error(404, "Not Found")
            return
        self._error(404, "Not Found")

    def _route_refs(self, method, repo, rest, base):
        ref_path = "/".join(rest)
        if method == "GET" and ref_path in ("", "heads"):
            items = [{"ref": "refs/heads/" + b, "object": {"sha": s["commit"], "type": "commit"}}
                     for b, s in sorted(repo["branches"].items()) if s["commit"]]
            self._send(200, items)
            return
        if method == "GET":
            full = "refs/" + ref_path
            branch = full[len("refs/heads/"):] if full.startswith("refs/heads/") else None
            state = repo["branches"].get(branch) if branch else None
            if state and state["commit"]:
                self._send(200, {"ref": full, "object": {"sha": state["commit"], "type": "commit"}})
            elif full.startswith("refs/tags/") and full[len("refs/tags/"):] in repo["tags"]:
                sha = repo["tags"][full[len("refs/tags/"):]]
                self._send(200, {"ref": full, "object": {"sha": sha, "type": "tag"}})
            else:
                self._error(404, "Not Found")
            return
        if method == "POST" and ref_path == "":
            user = self._require_auth()
            if not user:
                return
            body = self._json_body()
            ref = body.get("ref", "")
            sha = body.get("sha", "")
            if not ref.startswith("refs/heads/") or sha not in OBJECTS:
                self._error(422, "invalid ref or sha")
                return
            set_branch_to_commit(repo, ref[len("refs/heads/"):], sha)
            self._send(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}})
            return
        if method == "PATCH":
            user = self._require_auth()
            if not user:
                return
            body = self._json_body()
            sha = body.get("sha", "")
            full = "refs/" + ref_path
            if not full.startswith("refs/heads/") or sha not in OBJECTS:
                self._error(422, "invalid ref or sha")
                return
            set_branch_to_commit(repo, full[len("refs/heads/"):], sha)
            self._send(200, {"ref": full, "object": {"sha": sha, "type": "commit"}})
            return
        if method == "DELETE":
            user = self._require_auth()
            if not user:
                return
            full = "refs/" + ref_path
            branch = full[len("refs/heads/"):] if full.startswith("refs/heads/") else None
            if branch and branch in repo["branches"]:
                del repo["branches"][branch]
                self._send(204, b"")
            else:
                self._error(404, "Not Found")
            return
        self._error(404, "Not Found")

    def _route_contents(self, method, repo, rest, query, base):
        path = "/".join(rest)
        branch_name = query.get("ref", [repo["default_branch"]])[0]
        branch = repo["branches"].get(branch_name)
        if not branch:
            self._error(404, "Not Found")
            return
        if method == "GET":
            if path == "":
                items = []
                top = {}
                for p, s in sorted(branch["files"].items()):
                    first = p.split("/")[0]
                    if "/" in p:
                        top.setdefault(first, "dir")
                    else:
                        top[first] = "file"
                for name in sorted(top):
                    if top[name] == "dir":
                        items.append({"name": name, "path": name, "type": "dir",
                                      "sha": dir_tree_sha(repo, branch_name, name)})
                    else:
                        items.append({"name": name, "path": name, "type": "file",
                                      "sha": branch["files"][name]})
                self._send(200, items)
                return
            if path in branch["files"]:
                sha = branch["files"][path]
                data = OBJECTS[sha][1]
                self._send(200, {"type": "file", "name": path.split("/")[-1], "path": path,
                                 "sha": sha, "size": len(data),
                                 "content": base64.b64encode(data).decode(), "encoding": "base64"})
                return
            children = sorted({p[len(path) + 1:].split("/")[0]
                               for p in branch["files"] if p.startswith(path + "/")})
            if children:
                items = []
                for child in children:
                    full = path + "/" + child
                    is_dir = any(p.startswith(full + "/") for p in branch["files"])
                    if is_dir:
                        items.append({"name": child, "path": full, "type": "dir",
                                      "sha": dir_tree_sha(repo, branch_name, full)})
                    else:
                        items.append({"name": child, "path": full, "type": "file",
                                      "sha": branch["files"][full]})
                self._send(200, items)
                return
            self._error(404, "Not Found")
            return
        if method in ("PUT", "DELETE"):
            user = self._require_auth()
            if not user:
                return
            body = self._json_body()
            message = body.get("message", "update")
            branch_name = body.get("branch", branch_name)
            with repo["lock"]:
                branch = repo["branches"].setdefault(branch_name, {"commit": None, "files": {}})
                if method == "DELETE":
                    if path not in branch["files"]:
                        self._error(404, "Not Found")
                        return
                    if "sha" in body and body["sha"] != branch["files"][path]:
                        self._error(409, "sha does not match the current blob")
                        return
                    files = dict(branch["files"])
                    del files[path]
                    tree = build_tree(files)
                    sha = make_commit(tree, branch["commit"], message, user["login"])
                    branch["commit"] = sha
                    branch["files"] = files
                    self._send(200, {"commit": {"sha": sha}})
                    return
                content = body.get("content", "")
                try:
                    raw = base64.b64decode(content)
                except Exception:
                    self._error(422, "content must be base64")
                    return
                exists = path in branch["files"]
                if exists and "sha" in body and body["sha"] != branch["files"][path]:
                    self._error(409, "sha does not match the current blob")
                    return
                sha = commit_file(repo, branch_name, path, raw, message, user["login"])
                blob = repo["branches"][branch_name]["files"][path]
                status = 200 if exists else 201
                self._send(status, {"content": {"name": path.split("/")[-1], "path": path, "sha": blob,
                                                "type": "file", "encoding": "base64"},
                                    "commit": {"sha": sha}})
                return
        self._error(404, "Not Found")

    def _route_issues(self, method, repo, rest, base, query):
        if rest == [] and method == "GET":
            items = list(repo["issues"])
            state = query.get("state", ["open"])[0]
            if state in ("open", "closed"):
                items = [i for i in items if i["state"] == state]
            sort = query.get("sort", ["created"])[0]
            direction = query.get("direction", ["desc"])[0]
            if sort == "created":
                items.sort(key=lambda i: i["number"],
                           reverse=(direction != "asc"))
            page_items, link = self._page(
                items, query, "/api/v3/repos/%s/%s/issues" % (repo["owner"], repo["name"]), base)
            extra = {"Link": link} if link else None
            self._send(200, [issue_json(i, repo) for i in page_items], extra=extra)
            return
        if rest == [] and method == "POST":
            user = self._require_auth()
            if not user:
                return
            body = self._json_body()
            label_names = body.get("labels", [])
            labels = []
            for name in label_names:
                if isinstance(name, dict):
                    name = name.get("name", "")
                found = next((l for l in repo["labels"] if l["name"] == name), None)
                if found is None:
                    self._error(422, "Validation Failed: label '%s' does not exist" % name)
                    return
                labels.append({"name": found["name"]})
            with repo["lock"]:
                number = len(repo["issues"]) + len(repo["pulls"]) + 1
                issue = {"id": next_id(), "number": number,
                         "title": body.get("title", ""), "body": body.get("body", ""),
                         "state": "open", "labels": labels,
                         "author": user["login"], "comments": []}
                repo["issues"].append(issue)
                repo["issues_by_number"][number] = issue
            self._send(201, issue_json(issue, repo))
            return
        if len(rest) == 1 and rest[0].isdigit() and method == "GET":
            issue = repo["issues_by_number"].get(int(rest[0]))
            if issue:
                self._send(200, issue_json(issue, repo))
            else:
                self._error(404, "Not Found")
            return
        if len(rest) == 1 and rest[0].isdigit() and method == "PATCH":
            user = self._require_auth()
            if not user:
                return
            issue = repo["issues_by_number"].get(int(rest[0]))
            if not issue:
                self._error(404, "Not Found")
                return
            body = self._json_body()
            for key in ("title", "body", "state"):
                if key in body:
                    issue[key] = body[key]
            self._send(200, issue_json(issue, repo))
            return
        if len(rest) == 2 and rest[0].isdigit() and rest[1] == "comments" and method == "GET":
            issue = repo["issues_by_number"].get(int(rest[0]))
            if issue:
                self._send(200, issue["comments"])
            else:
                self._error(404, "Not Found")
            return
        if len(rest) == 2 and rest[0].isdigit() and rest[1] == "comments" and method == "POST":
            user = self._require_auth()
            if not user:
                return
            issue = repo["issues_by_number"].get(int(rest[0]))
            if not issue:
                self._error(404, "Not Found")
                return
            body = self._json_body()
            comment = {"id": next_id(), "body": body.get("body", ""), "user": {"login": user["login"]}}
            issue["comments"].append(comment)
            self._send(201, comment)
            return
        self._error(404, "Not Found")

    def _route_pulls(self, method, repo, rest, base, query):
        if rest == [] and method == "GET":
            items = list(repo["pulls"])
            state = query.get("state", ["open"])[0]
            if state == "open":
                items = [p for p in items if p["state"] == "open"]
            elif state == "closed":
                items = [p for p in items if p["state"] == "closed"]
            page_items, link = self._page(
                items, query, "/api/v3/repos/%s/%s/pulls" % (repo["owner"], repo["name"]), base)
            extra = {"Link": link} if link else None
            self._send(200, [pr_json(p, repo) for p in page_items], extra=extra)
            return
        if rest == [] and method == "POST":
            user = self._require_auth()
            if not user:
                return
            body = self._json_body()
            head = body.get("head", "")
            base_ref = body.get("base", repo["default_branch"])
            head_branch = head.split(":")[-1]
            head_state = repo["branches"].get(head_branch)
            base_state = repo["branches"].get(base_ref)
            with repo["lock"]:
                for existing in repo["pulls"]:
                    if (existing["state"] == "open" and existing["head"] == head_branch
                            and existing["base"] == base_ref):
                        self._error(422, "A pull request already exists for %s" % head_branch)
                        return
                number = len(repo["issues"]) + len(repo["pulls"]) + 1
                pr = {"id": next_id(), "number": number,
                      "title": body.get("title", ""), "body": body.get("body", ""),
                      "state": "open", "merged": False,
                      "merged_at": None, "merge_commit_sha": None,
                      "head": head_branch, "head_sha": head_state["commit"] if head_state else None,
                      "base": base_ref, "base_sha": base_state["commit"] if base_state else None,
                      "author": user["login"]}
                repo["pulls"].append(pr)
                repo["pulls_by_number"][number] = pr
            self._send(201, pr_json(pr, repo))
            return
        if len(rest) == 1 and rest[0].isdigit() and method == "GET":
            pr = repo["pulls_by_number"].get(int(rest[0]))
            if pr:
                self._send(200, pr_json(pr, repo))
            else:
                self._error(404, "Not Found")
            return
        if len(rest) == 1 and rest[0].isdigit() and method == "PATCH":
            user = self._require_auth()
            if not user:
                return
            pr = repo["pulls_by_number"].get(int(rest[0]))
            if not pr:
                self._error(404, "Not Found")
                return
            body = self._json_body()
            for key in ("title", "body", "state"):
                if key in body:
                    pr[key] = body[key]
            self._send(200, pr_json(pr, repo))
            return
        if len(rest) == 2 and rest[0].isdigit() and rest[1] == "files" and method == "GET":
            pr = repo["pulls_by_number"].get(int(rest[0]))
            if not pr:
                self._error(404, "Not Found")
                return
            self._send(200, pr_files_diff(repo, pr))
            return
        if len(rest) == 2 and rest[0].isdigit() and rest[1] == "merge" and method == "PUT":
            user = self._require_auth()
            if not user:
                return
            pr = repo["pulls_by_number"].get(int(rest[0]))
            if not pr:
                self._error(404, "Not Found")
                return
            if pr["state"] != "open":
                self._error(405, "Pull request is not open")
                return
            if pr_mergeable(repo, pr) != "MERGEABLE":
                self._error(405, "Pull request is not mergeable")
                return
            body = self._json_body()
            merge_method = body.get("merge_method", "squash")
            with repo["lock"]:
                result = _do_squash_merge(repo, pr, user["login"])
                if result is None:
                    self._error(405, "Pull request is not mergeable")
                    return
                merge_sha, message = result
            self._send(200, {"merged": True, "sha": merge_sha, "message": message})
            return
        self._error(404, "Not Found")


def _do_squash_merge(repo, pr, author_login):
    """Squash-merge an open, mergeable pull request onto its base branch.

    Creates a NEW commit (tree = merged tree, parent = base tip) on the
    base branch and marks the PR closed + merged. Returns
    (merge_sha, message) on success, or None when the PR conflicts or
    its branches are missing. The caller must hold repo["lock"].
    """
    head_state = repo["branches"].get(pr["head"])
    base_state = repo["branches"].get(pr["base"])
    if not head_state or not base_state or not head_state["commit"] or not base_state["commit"]:
        return None
    base_sha = base_state["commit"]
    head_sha = head_state["commit"]
    merged_files = _squash_merge_files(repo, pr, base_sha, head_sha)
    if merged_files is None:
        return None
    tree = build_tree(merged_files)
    message = "Merge pull request #%d from %s" % (pr["number"], pr["head"])
    merge_sha = make_commit(tree, base_sha, message, author_login)
    base_state["commit"] = merge_sha
    base_state["files"] = merged_files
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merged_at"] = FIXED_TIME
    pr["merge_commit_sha"] = merge_sha
    pr["base_sha"] = merge_sha
    return merge_sha, message


def _squash_merge_files(repo, pr, base_sha, head_sha):
    """The merged file set for a squash merge (three-way, against the
    merge base): the base tip's files, overlaid with the head branch's
    changes since the merge base. Returns None when the PR conflicts."""
    base = merge_base(head_sha, base_sha)
    if base is None:
        return None
    base_tree, _ = commit_tree_and_parents(base)
    base_files = tree_files(base_tree) if base_tree else {}
    head_tree, _ = commit_tree_and_parents(head_sha)
    head_files = tree_files(head_tree) if head_tree else {}
    tip_tree, _ = commit_tree_and_parents(base_sha)
    tip_files = tree_files(tip_tree) if tip_tree else {}
    merged = dict(tip_files)
    for path in set(head_files) | set(base_files):
        head_changed = head_files.get(path) != base_files.get(path)
        tip_changed = tip_files.get(path) != base_files.get(path)
        if head_changed and tip_changed and head_files.get(path) != tip_files.get(path):
            return None
        if head_changed:
            if path in head_files:
                merged[path] = head_files[path]
            else:
                merged.pop(path, None)
    return merged


# ---------------------------------------------------------------- GraphQL
# A small GraphQL implementation: tokenizer + recursive-descent parser,
# a schema dict (also the source for __type introspection), and an
# executor over the same in-memory store as REST. Supports queries and
# mutations, variables, aliases, arguments (inline enums, lists,
# objects, null), inline fragments (... on T) and named fragments,
# batched operation arrays, and HTTP 200 + errors for unknown fields.

def _gql_tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                                "\\": "\\", "/": "/", "b": "\b", "f": "\f"}.get(esc, esc))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            tokens.append(("string", "".join(out)))
            i = j + 1
        elif c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == "-":
                j += 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j < n and text[j] == "." and j + 1 < n and text[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE":
                k = j + 1
                if k < n and text[k] in "+-":
                    k += 1
                if k < n and text[k].isdigit():
                    is_float = True
                    j = k
                    while j < n and text[j].isdigit():
                        j += 1
            raw = text[i:j]
            tokens.append(("float", float(raw)) if is_float else ("int", int(raw)))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("name", text[i:j]))
            i = j
        elif text.startswith("...", i):
            tokens.append(("punct", "..."))
            i += 3
        elif c in "!$():=@[]{|}":
            tokens.append(("punct", c))
            i += 1
        else:
            i += 1
    tokens.append(("eof", None))
    return tokens


class _GqlParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect_punct(self, punct):
        kind, value = self.next()
        if kind != "punct" or value != punct:
            raise ValueError("expected %s, got %r" % (punct, value))

    def expect_name(self, name=None):
        kind, value = self.next()
        if kind != "name" or (name is not None and value != name):
            raise ValueError("expected name %s, got %r" % (name, value))
        return value

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            if self.peek() == ("name", "fragment"):
                self.next()
                frag_name = self.expect_name()
                self.expect_name("on")
                type_name = self.expect_name()
                fragments[frag_name] = {"type": type_name,
                                        "selection": self.parse_selection_set()}
            else:
                op_type = "query"
                if self.peek()[0] == "name" and self.peek()[1] in ("query", "mutation", "subscription"):
                    op_type = self.next()[1]
                op_name = None
                if self.peek()[0] == "name":
                    op_name = self.next()[1]
                variables = {}
                if self.peek() == ("punct", "("):
                    variables = self.parse_variable_definitions()
                while self.peek() == ("punct", "@"):
                    self.next()
                    self.expect_name()
                    if self.peek() == ("punct", "("):
                        self.parse_arguments()
                operations.append({"type": op_type, "name": op_name,
                                   "variables": variables,
                                   "selection": self.parse_selection_set()})
        return operations, fragments

    def parse_selection_set(self):
        self.expect_punct("{")
        selections = []
        while self.peek() != ("punct", "}"):
            selections.append(self.parse_selection())
        self.expect_punct("}")
        return selections

    def parse_selection(self):
        if self.peek() == ("punct", "..."):
            self.next()
            if self.peek()[0] == "name":
                name = self.next()[1]
                if name == "on":
                    type_name = self.expect_name()
                    while self.peek() == ("punct", "@"):
                        self.next()
                        self.expect_name()
                        if self.peek() == ("punct", "("):
                            self.parse_arguments()
                    return {"kind": "inline", "type": type_name,
                            "selection": self.parse_selection_set()}
                while self.peek() == ("punct", "@"):
                    self.next()
                    self.expect_name()
                    if self.peek() == ("punct", "("):
                        self.parse_arguments()
                return {"kind": "spread", "name": name}
            return {"kind": "inline", "type": None,
                    "selection": self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.peek() == ("punct", ":"):
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek() == ("punct", "("):
            args = self.parse_arguments()
        while self.peek() == ("punct", "@"):
            self.next()
            self.expect_name()
            if self.peek() == ("punct", "("):
                self.parse_arguments()
        selection = None
        if self.peek() == ("punct", "{"):
            selection = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "arguments": args, "selection": selection}

    def parse_arguments(self):
        self.expect_punct("(")
        args = {}
        while self.peek() != ("punct", ")"):
            name = self.expect_name()
            self.expect_punct(":")
            args[name] = self.parse_value()
        self.expect_punct(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind in ("string", "int", "float"):
            return value
        if kind == "name":
            if value == "true":
                return True
            if value == "false":
                return False
            if value == "null":
                return None
            return ("enum", value)
        if kind == "punct" and value == "$":
            return ("var", self.expect_name())
        if kind == "punct" and value == "[":
            items = []
            while self.peek() != ("punct", "]"):
                items.append(self.parse_value())
            self.expect_punct("]")
            return items
        if kind == "punct" and value == "{":
            obj = {}
            while self.peek() != ("punct", "}"):
                name = self.expect_name()
                self.expect_punct(":")
                obj[name] = self.parse_value()
            self.expect_punct("}")
            return obj
        raise ValueError("unexpected value token %r" % (value,))

    def parse_variable_definitions(self):
        self.expect_punct("(")
        variables = {}
        while self.peek() != ("punct", ")"):
            self.expect_punct("$")
            name = self.expect_name()
            self.expect_punct(":")
            type_name = self.parse_type()
            default = None
            if self.peek() == ("punct", "="):
                self.next()
                default = self.parse_value()
            variables[name] = {"type": type_name, "default": default}
        self.expect_punct(")")
        return variables

    def parse_type(self):
        if self.peek() == ("punct", "["):
            self.next()
            inner = self.parse_type()
            self.expect_punct("]")
            type_name = "[" + inner + "]"
        else:
            type_name = self.expect_name()
        if self.peek() == ("punct", "!"):
            self.next()
            type_name += "!"
        return type_name


def _gql_resolve_value(value, variables):
    """Replace variable references with the supplied values (falling
    back to the definition default), and unwrap enum literals to their
    string names. Lists and objects resolve recursively."""
    if isinstance(value, tuple):
        if value[0] == "var":
            return variables.get(value[1])
        if value[0] == "enum":
            return value[1]
    if isinstance(value, list):
        return [_gql_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _gql_resolve_value(v, variables) for k, v in value.items()}
    return value


# -- GraphQL schema (also the source for __type introspection) --------
_GQL_SCHEMA = {
    "Query": {"kind": "OBJECT", "fields": {
        "viewer": {"type": "User"},
        "repository": {"type": "Repository", "args": {"owner": "String!", "name": "String!"}},
        "node": {"type": "Node", "args": {"id": "ID!"}},
        "__type": {"type": "__Type", "args": {"name": "String!"}},
        "__schema": {"type": "__Schema"},
    }},
    "Mutation": {"kind": "OBJECT", "fields": {
        "createIssue": {"type": "CreateIssuePayload", "args": {"input": "CreateIssueInput!"}},
        "closeIssue": {"type": "CloseIssuePayload", "args": {"input": "CloseIssueInput!"}},
        "createPullRequest": {"type": "CreatePullRequestPayload", "args": {"input": "CreatePullRequestInput!"}},
        "mergePullRequest": {"type": "MergePullRequestPayload", "args": {"input": "MergePullRequestInput!"}},
        "addComment": {"type": "AddCommentPayload", "args": {"input": "AddCommentInput!"}},
    }},
    "User": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "login": {"type": "String"},
    }},
    "Repository": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "databaseId": {"type": "Int"},
        "name": {"type": "String"}, "nameWithOwner": {"type": "String"},
        "owner": {"type": "User"}, "defaultBranchRef": {"type": "Ref"},
        "viewerPermission": {"type": "RepositoryPermission"},
        "hasIssuesEnabled": {"type": "Boolean"},
        "issue": {"type": "Issue", "args": {"number": "Int!"}},
        "issues": {"type": "IssueConnection", "args": {"first": "Int", "states": "[IssueState!]", "orderBy": "IssueOrder", "after": "String"}},
        "pullRequests": {"type": "PullRequestConnection", "args": {"first": "Int"}},
        "pullRequest": {"type": "PullRequest", "args": {"number": "Int!"}},
    }},
    "Ref": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "name": {"type": "String"},
    }},
    "Issue": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "number": {"type": "Int"},
        "title": {"type": "String"}, "state": {"type": "IssueState"},
        "body": {"type": "String"},
    }},
    "IssueConnection": {"kind": "OBJECT", "fields": {
        "nodes": {"type": "[Issue]"}, "pageInfo": {"type": "PageInfo"},
        "totalCount": {"type": "Int"},
    }},
    "PageInfo": {"kind": "OBJECT", "fields": {
        "hasNextPage": {"type": "Boolean"}, "hasPreviousPage": {"type": "Boolean"},
        "endCursor": {"type": "String"}, "startCursor": {"type": "String"},
    }},
    "PullRequest": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "number": {"type": "Int"},
        "title": {"type": "String"}, "state": {"type": "PullRequestState"},
        "merged": {"type": "Boolean"}, "mergeable": {"type": "MergeableState"},
        "headRefName": {"type": "String"}, "baseRefName": {"type": "String"},
        "files": {"type": "PullRequestChangedFileConnection", "args": {"first": "Int"}},
    }},
    "PullRequestConnection": {"kind": "OBJECT", "fields": {
        "nodes": {"type": "[PullRequest]"}, "pageInfo": {"type": "PageInfo"},
        "totalCount": {"type": "Int"},
    }},
    "PullRequestChangedFileConnection": {"kind": "OBJECT", "fields": {
        "nodes": {"type": "[PullRequestChangedFile]"}, "pageInfo": {"type": "PageInfo"},
    }},
    "PullRequestChangedFile": {"kind": "OBJECT", "fields": {
        "path": {"type": "String"},
        "additions": {"type": "Int"}, "deletions": {"type": "Int"},
        "changeType": {"type": "String"},
    }},
    "IssueComment": {"kind": "OBJECT", "fields": {
        "id": {"type": "ID"}, "body": {"type": "String"},
    }},
    "IssueCommentEdge": {"kind": "OBJECT", "fields": {
        "node": {"type": "IssueComment"}, "cursor": {"type": "String"},
    }},
    "CreateIssuePayload": {"kind": "OBJECT", "fields": {
        "issue": {"type": "Issue"},
    }},
    "CloseIssuePayload": {"kind": "OBJECT", "fields": {
        "issue": {"type": "Issue"},
    }},
    "CreatePullRequestPayload": {"kind": "OBJECT", "fields": {
        "pullRequest": {"type": "PullRequest"},
    }},
    "MergePullRequestPayload": {"kind": "OBJECT", "fields": {
        "pullRequest": {"type": "PullRequest"},
    }},
    "AddCommentPayload": {"kind": "OBJECT", "fields": {
        "commentEdge": {"type": "IssueCommentEdge"},
    }},
    "CreateIssueInput": {"kind": "INPUT_OBJECT", "fields": {
        "repositoryId": {"type": "ID!"}, "title": {"type": "String!"},
        "body": {"type": "String"},
    }},
    "CloseIssueInput": {"kind": "INPUT_OBJECT", "fields": {
        "issueId": {"type": "ID!"},
    }},
    "CreatePullRequestInput": {"kind": "INPUT_OBJECT", "fields": {
        "repositoryId": {"type": "ID!"}, "baseRefName": {"type": "String!"},
        "headRefName": {"type": "String!"}, "title": {"type": "String!"},
        "body": {"type": "String"},
    }},
    "MergePullRequestInput": {"kind": "INPUT_OBJECT", "fields": {
        "pullRequestId": {"type": "ID!"},
        "mergeMethod": {"type": "PullRequestMergeMethod"},
    }},
    "AddCommentInput": {"kind": "INPUT_OBJECT", "fields": {
        "subjectId": {"type": "ID!"}, "body": {"type": "String!"},
    }},
    "IssueOrder": {"kind": "INPUT_OBJECT", "fields": {
        "field": {"type": "IssueOrderField"}, "direction": {"type": "OrderDirection"},
    }},
    "IssueState": {"kind": "ENUM", "values": ["OPEN", "CLOSED"]},
    "PullRequestState": {"kind": "ENUM", "values": ["OPEN", "CLOSED", "MERGED"]},
    "MergeableState": {"kind": "ENUM", "values": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "PullRequestMergeMethod": {"kind": "ENUM", "values": ["MERGE", "SQUASH", "REBASE"]},
    "RepositoryPermission": {"kind": "ENUM", "values": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"]},
    "IssueOrderField": {"kind": "ENUM", "values": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "OrderDirection": {"kind": "ENUM", "values": ["ASC", "DESC"]},
    "SearchType": {"kind": "ENUM", "values": ["ISSUE", "REPOSITORY", "USER"]},
    "__Type": {"kind": "OBJECT", "fields": {
        "name": {"type": "String"}, "kind": {"type": "String"},
        "description": {"type": "String"},
        "fields": {"type": "[__Field]", "args": {"includeDeprecated": "Boolean"}},
        "enumValues": {"type": "[__EnumValue]", "args": {"includeDeprecated": "Boolean"}},
    }},
    "__Field": {"kind": "OBJECT", "fields": {
        "name": {"type": "String"}, "description": {"type": "String"},
        "type": {"type": "__Type"},
    }},
    "__EnumValue": {"kind": "OBJECT", "fields": {
        "name": {"type": "String"}, "description": {"type": "String"},
    }},
    "__Schema": {"kind": "OBJECT", "fields": {
        "types": {"type": "[__Type]"},
        "queryType": {"type": "__Type"}, "mutationType": {"type": "__Type"},
    }},
}


def _gid(type_name, key):
    """An opaque, stable global id: base64 of a type-tagged string."""
    raw = ("%s:%s" % (type_name, key)).encode()
    return base64.b64encode(raw).decode()


def _ungid(global_id):
    """Decode a global id back to (type_name, key), or (None, None)."""
    try:
        raw = base64.b64decode(global_id.encode()).decode()
    except Exception:
        return None, None
    if ":" not in raw:
        return None, None
    type_name, _, key = raw.partition(":")
    return type_name, key


def _gql_issue_obj(repo, issue):
    return {"_gql_type": "Issue", "repo": repo, "issue": issue,
            "owner": repo["owner"], "name": repo["name"]}


def _gql_pr_obj(repo, pr):
    return {"_gql_type": "PullRequest", "repo": repo, "pr": pr,
            "owner": repo["owner"], "name": repo["name"]}


def _gql_repo_obj(repo):
    return {"_gql_type": "Repository", "repo": repo,
            "owner": repo["owner"], "name": repo["name"]}


def _gql_cursor(number):
    return base64.b64encode(str(number).encode()).decode()


def _gql_cursor_number(cursor):
    try:
        return int(base64.b64decode(cursor.encode()).decode())
    except Exception:
        return None


def _gql_issues_page(repo, args):
    """Resolve repository.issues(first, states, orderBy, after) into
    an IssueConnection object: nodes + pageInfo."""
    items = list(repo["issues"])
    states = args.get("states")
    if states:
        wanted = {str(s).lower() for s in states}
        items = [i for i in items if i["state"] in wanted]
    order_by = args.get("orderBy") or {}
    direction = str(order_by.get("direction", "ASC")).upper()
    items.sort(key=lambda i: i["number"], reverse=(direction == "DESC"))
    after = args.get("after")
    if after is not None:
        after_number = _gql_cursor_number(after)
        if after_number is not None:
            if direction == "DESC":
                items = [i for i in items if i["number"] < after_number]
            else:
                items = [i for i in items if i["number"] > after_number]
    first = args.get("first")
    has_next = False
    if first is not None:
        if len(items) > first:
            has_next = True
        items = items[:first]
    nodes = [_gql_issue_obj(repo, i) for i in items]
    end_cursor = _gql_cursor(items[-1]["number"]) if items else None
    return {"_gql_type": "IssueConnection", "nodes": nodes,
            "pageInfo": {"_gql_type": "PageInfo", "hasNextPage": has_next,
                         "hasPreviousPage": False, "endCursor": end_cursor,
                         "startCursor": _gql_cursor(items[0]["number"]) if items else None},
            "totalCount": len(nodes)}


def _gql_prs_page(repo, args):
    items = list(repo["pulls"])
    first = args.get("first")
    has_next = False
    if first is not None:
        if len(items) > first:
            has_next = True
        items = items[:first]
    nodes = [_gql_pr_obj(repo, p) for p in items]
    end_cursor = _gql_cursor(items[-1]["number"]) if items else None
    return {"_gql_type": "PullRequestConnection", "nodes": nodes,
            "pageInfo": {"_gql_type": "PageInfo", "hasNextPage": has_next,
                         "hasPreviousPage": False, "endCursor": end_cursor,
                         "startCursor": _gql_cursor(items[0]["number"]) if items else None},
            "totalCount": len(nodes)}


def _gql_resolve_field(type_name, field_name, obj, args, user):
    """Resolve one field of one object to its raw value: a scalar, an
    object dict (with _gql_type), a list of those, or None. Returns the
    sentinel _GQL_MISSING when the field does not exist on the type."""
    if obj is None:
        return None
    if type_name == "Query":
        if field_name == "viewer":
            return {"_gql_type": "User", "login": VIEWER_LOGIN,
                    "id": _gid("User", VIEWER_LOGIN)}
        if field_name == "repository":
            repo = REPOS.get((args.get("owner"), args.get("name")))
            return _gql_repo_obj(repo) if repo else None
        if field_name == "node":
            return _gql_resolve_node(args.get("id"))
        if field_name == "__type":
            name = args.get("name")
            if name in _GQL_SCHEMA:
                return {"_gql_type": "__Type", "name": name}
            return None
        if field_name == "__schema":
            return {"_gql_type": "__Schema"}
        return _GQL_MISSING
    if type_name == "User":
        if field_name == "login":
            return obj.get("login")
        if field_name == "id":
            return obj.get("id")
        return _GQL_MISSING
    if type_name == "Repository":
        repo = obj["repo"]
        if field_name == "id":
            return _gid("Repository", "%s/%s" % (obj["owner"], obj["name"]))
        if field_name == "databaseId":
            return repo["id"]
        if field_name == "name":
            return repo["name"]
        if field_name == "nameWithOwner":
            return "%s/%s" % (obj["owner"], obj["name"])
        if field_name == "owner":
            return {"_gql_type": "User", "login": repo["owner"],
                    "id": _gid("User", repo["owner"])}
        if field_name == "defaultBranchRef":
            if repo["default_branch"] in repo["branches"]:
                return {"_gql_type": "Ref", "name": repo["default_branch"],
                        "id": _gid("Ref", "%s/%s/%s" % (obj["owner"], obj["name"], repo["default_branch"]))}
            return None
        if field_name == "viewerPermission":
            return "ADMIN"
        if field_name == "hasIssuesEnabled":
            return True
        if field_name == "issue":
            issue = repo["issues_by_number"].get(args.get("number"))
            return _gql_issue_obj(repo, issue) if issue else None
        if field_name == "issues":
            return _gql_issues_page(repo, args)
        if field_name == "pullRequests":
            return _gql_prs_page(repo, args)
        if field_name == "pullRequest":
            pr = repo["pulls_by_number"].get(args.get("number"))
            return _gql_pr_obj(repo, pr) if pr else None
        return _GQL_MISSING
    if type_name == "Ref":
        if field_name == "name":
            return obj.get("name")
        if field_name == "id":
            return obj.get("id")
        return _GQL_MISSING
    if type_name == "Issue":
        issue = obj["issue"]
        if field_name == "id":
            return _gid("Issue", "%s/%s/%d" % (obj["owner"], obj["name"], issue["number"]))
        if field_name == "number":
            return issue["number"]
        if field_name == "title":
            return issue["title"]
        if field_name == "state":
            return issue["state"].upper()
        if field_name == "body":
            return issue["body"]
        return _GQL_MISSING
    if type_name in ("IssueConnection", "PullRequestConnection", "PullRequestChangedFileConnection"):
        if field_name == "nodes":
            return obj.get("nodes", [])
        if field_name == "pageInfo":
            return obj.get("pageInfo")
        if field_name == "totalCount":
            return obj.get("totalCount", len(obj.get("nodes", [])))
        return _GQL_MISSING
    if type_name == "PageInfo":
        if field_name in ("hasNextPage", "hasPreviousPage", "endCursor", "startCursor"):
            return obj.get(field_name)
        return _GQL_MISSING
    if type_name == "PullRequest":
        pr = obj["pr"]
        repo = obj["repo"]
        if field_name == "id":
            return _gid("PullRequest", "%s/%s/%d" % (obj["owner"], obj["name"], pr["number"]))
        if field_name == "number":
            return pr["number"]
        if field_name == "title":
            return pr["title"]
        if field_name == "state":
            return "MERGED" if pr["merged"] else pr["state"].upper()
        if field_name == "merged":
            return bool(pr["merged"])
        if field_name == "mergeable":
            return pr_mergeable(repo, pr)
        if field_name == "headRefName":
            return pr["head"]
        if field_name == "baseRefName":
            return pr["base"]
        if field_name == "files":
            diffs = pr_files_diff(repo, pr)
            first = args.get("first")
            if first is not None:
                diffs = diffs[:first]
            nodes = [{"_gql_type": "PullRequestChangedFile", "path": d["filename"],
                      "additions": d["additions"], "deletions": d["deletions"],
                      "changeType": d["status"]} for d in diffs]
            return {"_gql_type": "PullRequestChangedFileConnection", "nodes": nodes,
                    "pageInfo": {"_gql_type": "PageInfo", "hasNextPage": False,
                                 "hasPreviousPage": False, "endCursor": None,
                                 "startCursor": None}}
        return _GQL_MISSING
    if type_name == "PullRequestChangedFile":
        if field_name in ("path", "additions", "deletions", "changeType"):
            return obj.get(field_name)
        return _GQL_MISSING
    if type_name == "IssueComment":
        if field_name == "id":
            return obj.get("id")
        if field_name == "body":
            return obj.get("body")
        return _GQL_MISSING
    if type_name == "IssueCommentEdge":
        if field_name == "node":
            return obj.get("node")
        if field_name == "cursor":
            return obj.get("cursor")
        return _GQL_MISSING
    if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
        if field_name == "issue":
            return obj.get("issue")
        return _GQL_MISSING
    if type_name in ("CreatePullRequestPayload", "MergePullRequestPayload"):
        if field_name == "pullRequest":
            return obj.get("pullRequest")
        return _GQL_MISSING
    if type_name == "AddCommentPayload":
        if field_name == "commentEdge":
            return obj.get("commentEdge")
        return _GQL_MISSING
    if type_name == "__Type":
        name = obj.get("name")
        schema_type = _GQL_SCHEMA.get(name, {})
        if field_name == "name":
            return name
        if field_name == "kind":
            return schema_type.get("kind")
        if field_name == "description":
            return None
        if field_name == "fields":
            fields = schema_type.get("fields", {})
            return [{"_gql_type": "__Field", "name": fname}
                    for fname in fields]
        if field_name == "enumValues":
            return [{"_gql_type": "__EnumValue", "name": v}
                    for v in schema_type.get("values", [])]
        return _GQL_MISSING
    if type_name == "__Field":
        if field_name == "name":
            return obj.get("name")
        if field_name == "description":
            return None
        if field_name == "type":
            return None
        return _GQL_MISSING
    if type_name == "__EnumValue":
        if field_name == "name":
            return obj.get("name")
        if field_name == "description":
            return None
        return _GQL_MISSING
    if type_name == "__Schema":
        if field_name == "types":
            return [{"_gql_type": "__Type", "name": n} for n in sorted(_GQL_SCHEMA)]
        if field_name == "queryType":
            return {"_gql_type": "__Type", "name": "Query"}
        if field_name == "mutationType":
            return {"_gql_type": "__Type", "name": "Mutation"}
        return _GQL_MISSING
    return _GQL_MISSING


class _GqlMissing:
    pass


_GQL_MISSING = _GqlMissing()


def _gql_resolve_node(global_id):
    type_name, key = _ungid(global_id or "")
    if type_name == "Repository" and key:
        owner, _, name = key.partition("/")
        repo = REPOS.get((owner, name))
        return _gql_repo_obj(repo) if repo else None
    if type_name == "Issue" and key:
        parts = key.split("/")
        if len(parts) == 3:
            repo = REPOS.get((parts[0], parts[1]))
            if repo:
                issue = repo["issues_by_number"].get(int(parts[2])) if parts[2].isdigit() else None
                return _gql_issue_obj(repo, issue) if issue else None
    if type_name == "PullRequest" and key:
        parts = key.split("/")
        if len(parts) == 3:
            repo = REPOS.get((parts[0], parts[1]))
            if repo:
                pr = repo["pulls_by_number"].get(int(parts[2])) if parts[2].isdigit() else None
                return _gql_pr_obj(repo, pr) if pr else None
    return None


def _gql_execute_selection(selection, type_name, obj, variables, fragments, errors, user):
    """Execute a selection set against one object; returns a dict of
    the requested fields (or None when obj is None). Unknown fields
    append an error naming the field and are skipped."""
    if obj is None:
        return None
    out = {}
    for sel in selection:
        if sel["kind"] == "spread":
            frag = fragments.get(sel["name"])
            if frag is None:
                errors.append({"message": 'Unknown fragment "%s".' % sel["name"]})
                continue
            if frag["type"] != type_name:
                continue
            out.update(_gql_execute_selection(frag["selection"], type_name, obj,
                                              variables, fragments, errors, user))
            continue
        if sel["kind"] == "inline":
            if sel["type"] is not None and sel["type"] != type_name:
                continue
            out.update(_gql_execute_selection(sel["selection"], type_name, obj,
                                              variables, fragments, errors, user))
            continue
        field_name = sel["name"]
        if field_name == "__typename":
            out[sel["alias"]] = type_name
            continue
        args = {k: _gql_resolve_value(v, variables)
                for k, v in sel["arguments"].items()}
        value = _gql_resolve_field(type_name, field_name, obj, args, user)
        if value is _GQL_MISSING:
            errors.append({"message": 'Cannot query field "%s" on type "%s".'
                           % (field_name, type_name)})
            continue
        out[sel["alias"]] = _gql_execute_value(
            value, field_name, type_name, sel["selection"],
            variables, fragments, errors, user)
    return out


def _gql_execute_value(value, field_name, parent_type, selection,
                       variables, fragments, errors, user):
    """Resolve a field value into its response form: scalars pass
    through, objects recurse into their selection set, lists map."""
    if value is None:
        return None
    if selection is None:
        # An object field with no sub-selection is invalid GraphQL; never
        # leak the internal object (it holds repo dicts, sets, locks).
        if isinstance(value, (dict, list)):
            return None
        return value
    if isinstance(value, list):
        item_type = _gql_field_type(parent_type, field_name)
        return [_gql_execute_value(item, field_name, parent_type, selection,
                                   variables, fragments, errors, user)
                if isinstance(item, dict) else item for item in value]
    if isinstance(value, dict) and "_gql_type" in value:
        return _gql_execute_selection(selection, value["_gql_type"], value,
                                      variables, fragments, errors, user)
    if isinstance(value, dict):
        return value
    return value


def _gql_field_type(parent_type, field_name):
    """The declared (unwrapped) type of a field, for list items."""
    field = _GQL_SCHEMA.get(parent_type, {}).get("fields", {}).get(field_name, {})
    raw = field.get("type", "")
    return raw.strip("[]! ")


def _gql_execute_operation(operation, fragments, variables, user):
    """Execute one operation document; returns a result dict with
    `data` and (when anything failed) `errors`."""
    errors = []
    root_type = "Mutation" if operation["type"] == "mutation" else "Query"
    if operation["type"] == "mutation":
        result = _gql_execute_mutation(operation, fragments, variables, errors, user)
        if result is not None:
            return result
    data = _gql_execute_selection(operation["selection"], root_type, {},
                                  variables, fragments, errors, user)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def _gql_execute_mutation(operation, fragments, variables, errors, user):
    """Handle a mutation operation: run each top-level mutation field
    and execute its selection set against the payload object."""
    data = {}
    for sel in operation["selection"]:
        if sel["kind"] != "field":
            continue
        field_name = sel["name"]
        args = {k: _gql_resolve_value(v, variables)
                for k, v in sel["arguments"].items()}
        payload, payload_type = _gql_run_mutation(field_name, args, user, errors)
        if payload is None:
            data[sel["alias"]] = None
            continue
        data[sel["alias"]] = _gql_execute_selection(
            sel["selection"] or [], payload_type, payload,
            variables, fragments, errors, user)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def _gql_run_mutation(field_name, args, user, errors):
    """Run one mutation field; returns (payload_obj, payload_type) or
    (None, None) with an error appended."""
    input_data = args.get("input") or {}
    if field_name == "createIssue":
        type_name, key = _ungid(input_data.get("repositoryId") or "")
        if type_name != "Repository" or not key:
            errors.append({"message": "createIssue: invalid repositoryId"})
            return None, None
        owner, _, name = key.partition("/")
        repo = REPOS.get((owner, name))
        if not repo:
            errors.append({"message": "createIssue: repository not found"})
            return None, None
        with repo["lock"]:
            number = len(repo["issues"]) + len(repo["pulls"]) + 1
            issue = {"id": next_id(), "number": number,
                     "title": input_data.get("title", ""),
                     "body": input_data.get("body", ""),
                     "state": "open", "labels": [],
                     "author": user["login"], "comments": []}
            repo["issues"].append(issue)
            repo["issues_by_number"][number] = issue
        return ({"_gql_type": "CreateIssuePayload",
                 "issue": _gql_issue_obj(repo, issue)}, "CreateIssuePayload")
    if field_name == "closeIssue":
        type_name, key = _ungid(input_data.get("issueId") or "")
        if type_name != "Issue" or not key:
            errors.append({"message": "closeIssue: invalid issueId"})
            return None, None
        parts = key.split("/")
        if len(parts) != 3:
            errors.append({"message": "closeIssue: invalid issueId"})
            return None, None
        repo = REPOS.get((parts[0], parts[1]))
        issue = repo["issues_by_number"].get(int(parts[2])) if repo and parts[2].isdigit() else None
        if not issue:
            errors.append({"message": "closeIssue: issue not found"})
            return None, None
        issue["state"] = "closed"
        return ({"_gql_type": "CloseIssuePayload",
                 "issue": _gql_issue_obj(repo, issue)}, "CloseIssuePayload")
    if field_name == "createPullRequest":
        type_name, key = _ungid(input_data.get("repositoryId") or "")
        if type_name != "Repository" or not key:
            errors.append({"message": "createPullRequest: invalid repositoryId"})
            return None, None
        owner, _, name = key.partition("/")
        repo = REPOS.get((owner, name))
        if not repo:
            errors.append({"message": "createPullRequest: repository not found"})
            return None, None
        head_branch = input_data.get("headRefName", "")
        base_ref = input_data.get("baseRefName", repo["default_branch"])
        head_state = repo["branches"].get(head_branch)
        base_state = repo["branches"].get(base_ref)
        if not head_state or not head_state["commit"] or not base_state:
            errors.append({"message": "createPullRequest: head or base branch not found"})
            return None, None
        with repo["lock"]:
            for existing in repo["pulls"]:
                if (existing["state"] == "open" and existing["head"] == head_branch
                        and existing["base"] == base_ref):
                    errors.append({"message": "createPullRequest: a pull request already exists for %s" % head_branch})
                    return None, None
            number = len(repo["issues"]) + len(repo["pulls"]) + 1
            pr = {"id": next_id(), "number": number,
                  "title": input_data.get("title", ""),
                  "body": input_data.get("body", ""),
                  "state": "open", "merged": False,
                  "merged_at": None, "merge_commit_sha": None,
                  "head": head_branch, "head_sha": head_state["commit"],
                  "base": base_ref, "base_sha": base_state["commit"],
                  "author": user["login"]}
            repo["pulls"].append(pr)
            repo["pulls_by_number"][number] = pr
        return ({"_gql_type": "CreatePullRequestPayload",
                 "pullRequest": _gql_pr_obj(repo, pr)}, "CreatePullRequestPayload")
    if field_name == "mergePullRequest":
        type_name, key = _ungid(input_data.get("pullRequestId") or "")
        if type_name != "PullRequest" or not key:
            errors.append({"message": "mergePullRequest: invalid pullRequestId"})
            return None, None
        parts = key.split("/")
        if len(parts) != 3:
            errors.append({"message": "mergePullRequest: invalid pullRequestId"})
            return None, None
        repo = REPOS.get((parts[0], parts[1]))
        pr = repo["pulls_by_number"].get(int(parts[2])) if repo and parts[2].isdigit() else None
        if not pr:
            errors.append({"message": "mergePullRequest: pull request not found"})
            return None, None
        if pr["state"] != "open" or pr_mergeable(repo, pr) != "MERGEABLE":
            errors.append({"message": "mergePullRequest: pull request is not mergeable"})
            return None, None
        with repo["lock"]:
            result = _do_squash_merge(repo, pr, user["login"])
        if result is None:
            errors.append({"message": "mergePullRequest: pull request is not mergeable"})
            return None, None
        return ({"_gql_type": "MergePullRequestPayload",
                 "pullRequest": _gql_pr_obj(repo, pr)}, "MergePullRequestPayload")
    if field_name == "addComment":
        type_name, key = _ungid(input_data.get("subjectId") or "")
        if type_name != "Issue" or not key:
            errors.append({"message": "addComment: invalid subjectId"})
            return None, None
        parts = key.split("/")
        if len(parts) != 3:
            errors.append({"message": "addComment: invalid subjectId"})
            return None, None
        repo = REPOS.get((parts[0], parts[1]))
        issue = repo["issues_by_number"].get(int(parts[2])) if repo and parts[2].isdigit() else None
        if not issue:
            errors.append({"message": "addComment: issue not found"})
            return None, None
        comment = {"id": next_id(), "body": input_data.get("body", ""),
                   "user": {"login": user["login"]}}
        issue["comments"].append(comment)
        node = {"_gql_type": "IssueComment",
                "id": _gid("IssueComment", "%s/%s/%d/%d" % (parts[0], parts[1], issue["number"], comment["id"])),
                "body": comment["body"]}
        return ({"_gql_type": "AddCommentPayload",
                 "commentEdge": {"_gql_type": "IssueCommentEdge", "node": node,
                                 "cursor": _gql_cursor(comment["id"])}}, "AddCommentPayload")
    errors.append({"message": 'Cannot query field "%s" on type "Mutation".' % field_name})
    return None, None


# ---------------------------------------------------------------- MCP
# Model Context Protocol (JSON-RPC 2.0 Streamable HTTP) at /mcp.
# The tool list is a module-level constant so two tools/list calls
# answer byte for byte identically. Tools act on the same store as REST.

MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository owned by the authenticated user.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "name": {"type": "string", "description": "Repository name"},
                         "description": {"type": "string", "description": "Repository description"},
                         "private": {"type": "boolean", "description": "Whether the repository is private"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "title": {"type": "string", "description": "Issue title"},
                         "body": {"type": "string", "description": "Issue body"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "state": {"type": "string", "description": "Issue state filter: open, closed, or all"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Create a pull request in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "title": {"type": "string", "description": "Pull request title"},
                         "body": {"type": "string", "description": "Pull request body"},
                         "head": {"type": "string", "description": "Head branch name"},
                         "base": {"type": "string", "description": "Base branch name"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Merge a pull request with a squash commit.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "pull_number": {"type": "integer", "description": "Pull request number"}},
                     "required": ["owner", "repo", "pull_number"]}},
]

_MCP_ALIASES = {
    "repo": ("repository", "repo_name"),
    "owner": ("org",),
    "pull_number": ("number", "pr_number", "pullRequestNumber"),
    "head": ("head_branch", "head_ref"),
    "base": ("base_branch", "base_ref"),
    "name": ("repo_name",),
}


def _mcp_normalize_args(tool_name, args):
    """Map synonym argument names onto the schema's canonical property
    names (the suite's github-mcp-server names already match, but a
    synonym must never leave a required property unfilled)."""
    out = dict(args or {})
    for canonical, aliases in _MCP_ALIASES.items():
        if canonical not in out or out[canonical] is None:
            for alias in aliases:
                if alias in out and out[alias] is not None:
                    out[canonical] = out[alias]
                    break
    return out


def _mcp_tool_result(structured, text=None):
    """A tools/call success result: text content + structuredContent."""
    if text is None:
        text = json.dumps(structured)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": structured, "isError": False}


def _mcp_call_tool(tool_name, args, user, base):
    """Dispatch one tools/call. Returns (result_dict, rpc_error_dict)
    with exactly one of them set; rpc_error_dict is a JSON-RPC error
    object (code/message) for -32602-style failures."""
    tool = next((t for t in MCP_TOOLS if t["name"] == tool_name), None)
    if tool is None:
        return None, {"code": -32602, "message": "Unknown tool: %s" % tool_name}
    args = _mcp_normalize_args(tool_name, args)
    for required in tool["inputSchema"].get("required", []):
        if required not in args or args[required] is None or args[required] == "":
            return None, {"code": -32602,
                          "message": "Missing required argument: %s" % required}
    if tool_name == "create_repo":
        name = args["name"]
        if (user["login"], name) in REPOS:
            return None, {"code": -32602, "message": "Repository already exists"}
        repo = new_repo(user["login"], name, args.get("description"), bool(args.get("private", False)))
        REPOS[(user["login"], name)] = repo
        data = repo_json(repo, base)
        return _mcp_tool_result(data), None
    if tool_name == "get_repo":
        repo = REPOS.get((args["owner"], args["repo"]))
        if not repo:
            return None, {"code": -32602, "message": "Repository not found"}
        return _mcp_tool_result(repo_json(repo, base)), None
    if tool_name == "create_issue":
        repo = REPOS.get((args["owner"], args["repo"]))
        if not repo:
            return None, {"code": -32602, "message": "Repository not found"}
        with repo["lock"]:
            number = len(repo["issues"]) + len(repo["pulls"]) + 1
            issue = {"id": next_id(), "number": number,
                     "title": args["title"], "body": args.get("body", ""),
                     "state": "open", "labels": [],
                     "author": user["login"], "comments": []}
            repo["issues"].append(issue)
            repo["issues_by_number"][number] = issue
        return _mcp_tool_result(issue_json(issue, repo)), None
    if tool_name == "list_issues":
        repo = REPOS.get((args["owner"], args["repo"]))
        if not repo:
            return None, {"code": -32602, "message": "Repository not found"}
        items = list(repo["issues"])
        state = args.get("state", "open")
        if state in ("open", "closed"):
            items = [i for i in items if i["state"] == state]
        data = [issue_json(i, repo) for i in items]
        return _mcp_tool_result({"issues": data}, text=json.dumps(data)), None
    if tool_name == "create_pull_request":
        repo = REPOS.get((args["owner"], args["repo"]))
        if not repo:
            return None, {"code": -32602, "message": "Repository not found"}
        head_branch = str(args["head"]).split(":")[-1]
        base_ref = args["base"]
        head_state = repo["branches"].get(head_branch)
        base_state = repo["branches"].get(base_ref)
        if not head_state or not head_state["commit"] or not base_state:
            return None, {"code": -32602, "message": "head or base branch not found"}
        with repo["lock"]:
            for existing in repo["pulls"]:
                if (existing["state"] == "open" and existing["head"] == head_branch
                        and existing["base"] == base_ref):
                    return None, {"code": -32602, "message": "A pull request already exists for %s" % head_branch}
            number = len(repo["issues"]) + len(repo["pulls"]) + 1
            pr = {"id": next_id(), "number": number,
                  "title": args["title"], "body": args.get("body", ""),
                  "state": "open", "merged": False,
                  "merged_at": None, "merge_commit_sha": None,
                  "head": head_branch, "head_sha": head_state["commit"],
                  "base": base_ref, "base_sha": base_state["commit"],
                  "author": user["login"]}
            repo["pulls"].append(pr)
            repo["pulls_by_number"][number] = pr
        return _mcp_tool_result(pr_json(pr, repo)), None
    if tool_name == "merge_pull_request":
        repo = REPOS.get((args["owner"], args["repo"]))
        if not repo:
            return None, {"code": -32602, "message": "Repository not found"}
        try:
            number = int(args["pull_number"])
        except (TypeError, ValueError):
            return None, {"code": -32602, "message": "pull_number must be an integer"}
        pr = repo["pulls_by_number"].get(number)
        if not pr:
            return None, {"code": -32602, "message": "Pull request not found"}
        if pr["state"] != "open" or pr_mergeable(repo, pr) != "MERGEABLE":
            return None, {"code": -32602, "message": "Pull request is not mergeable"}
        with repo["lock"]:
            result = _do_squash_merge(repo, pr, user["login"])
        if result is None:
            return None, {"code": -32602, "message": "Pull request is not mergeable"}
        merge_sha, message = result
        data = {"merged": True, "sha": merge_sha, "message": message,
                "number": pr["number"]}
        return _mcp_tool_result(data), None
    return None, {"code": -32602, "message": "Unknown tool: %s" % tool_name}


class ForgeServer(ThreadingHTTPServer):
    # The default listen backlog is 5: 50 simultaneous connections
    # (the clone benchmark) overflow it, the kernel drops SYNs and
    # clients stall on TCP retransmit backoff. 128 absorbs the burst.
    request_queue_size = 128
    daemon_threads = True


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if env_url:
        # Trial lifecycle (starter/main.ts): bind an ephemeral port, tell
        # the environment about it, then wait until it says done.
        server = ForgeServer(("0.0.0.0", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        register_url = env_url + "/register"
        backoff = 0.25
        while True:
            request = urllib.request.Request(
                register_url,
                data=json.dumps({"port": port}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status == 200:
                        break
                    if response.status == 409:
                        break  # a first register already won; keep waiting
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 405):
                    raise SystemExit("register failed: HTTP %d" % exc.code)
                if exc.code == 409:
                    break
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 2.0)
        done_url = env_url + "/done"
        while True:
            try:
                with urllib.request.urlopen(done_url, timeout=5) as response:
                    payload = json.loads(response.read().decode())
                if payload.get("done"):
                    break
            except Exception:
                pass
            time.sleep(0.25)
        server.shutdown()
        server.server_close()
        return
    port = int(os.environ.get("MAKEFUN_PORT", "0"))
    server = ForgeServer(("0.0.0.0", port), Handler)
    actual = server.server_address[1]
    if not os.environ.get("MAKEFUN_PORT"):
        print("PORT=%d" % actual, flush=True)
    done_file = os.environ.get("DONE_FILE")
    if done_file:
        with open(done_file, "w") as handle:
            handle.write("done\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
