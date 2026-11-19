# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/strongdady48-oss/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/strongdady48-oss/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in one Python file (stdlib only, no git binary, no outside service). Built for the make.fun bounty "Build a GitHub-compatible git forge".

## Run it

```sh
MAKEFUN_PORT=8000 python3 main.py
```

If `MAKEFUN_PORT` is not set, the program binds an ephemeral port and prints exactly `PORT=<port>` on stdout.

One process serves, from its own in-memory store:

- **Git smart HTTP** — `info/refs`, `git-upload-pack`, `git-receive-pack` (real git objects, packfiles, deltas; verified with a real `git` client: clone, push, clone-back)
- **REST API v3** — users, repositories, issues, pull requests, contents
- **GraphQL** — `POST /api/graphql` (viewer/repository/issues/pullRequests queries, createIssue/createPullRequest-style mutations)
- **MCP** — `POST /mcp` (JSON-RPC 2.0: initialize, tools/list, tools/call)

---

Entered on [make.fun](https://make.fun/?ref=zksncs94&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards
