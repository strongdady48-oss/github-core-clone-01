# github-core-clone-01

A GitHub-compatible git forge in one Python file (stdlib only, no git binary, no outside service). Built for the make.fun bounty "Build a GitHub-compatible git forge" (make.fun/bounty/github-core-clone-01).

One process serves, from its own in-memory store:

- **Git smart HTTP** — `info/refs`, `git-upload-pack`, `git-receive-pack` (real git objects, packfiles, deltas; verified with a real `git` client: clone, push, clone-back)
- **REST API v3** — users, repositories, issues, pull requests, contents
- **GraphQL** — `POST /graphql` (viewer/repository/issues/pullRequests queries, createIssue/createPullRequest-style mutations, in the shape `gh` and github-mcp-server send)
- **MCP** — `POST /mcp` (JSON-RPC 2.0: initialize, tools/list, tools/call)

## Run

```
MAKEFUN_PORT=8000 python3 main.py
```

If `MAKEFUN_PORT` is not set, the program binds an ephemeral port and prints exactly `PORT=<port>` on stdout.
