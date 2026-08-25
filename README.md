# awrepl

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awrepl/)**  ·  [Source](https://github.com/Aitherium/awrepl)  ·  `pip install awrepl`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awrepl** is one of its 36 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Give an agent a live session it can keep poking at, so the next question is asked of the object instead of of its own memory.

<!-- aither-header:end -->

**A REPL an agent can actually use — state that survives between turns.**

```bash
pip install awrepl
```

```python
from awrepl import ReplSession

session = ReplSession("agent-1")
result = session.execute("items = [1, 2, 3]")
result = session.execute("print(len(items))")
print(result.stdout)  # "3"
session.close()
```

```bash
awrepl run "print('hello')"
awrepl serve                    # Interactive session
awrepl --session myagent run "x = 42"
awrepl --self-test              # Verify the contract
```

---

## The problem it exists for

An agent given one-shot shell commands rebuilds its whole world on every call, so it guesses instead of looks. Every variable it wanted is gone the moment the command exits. This REPL keeps state.

**You ask the live object, not the agent's memory.**

```python
# Turn 1
session.execute("data = load_file('config.json')")

# Turn 2 (later, different agent instance)
session.execute("print(data['server'])")  # data is STILL there
```

---

## ⚠️ This is NOT a sandbox

**Critical: awrepl executes arbitrary code with the privileges of the process that started it.**

It does not protect against:
- **Filesystem access** — code can read/write/delete files
- **Network access** — code can make HTTP requests, connect to services
- **Subprocess execution** — code can launch processes and scripts
- **Resource exhaustion** — timeouts can be circumvented (see below)
- **Memory/disk consumption** — unbounded allocations are possible

**Real isolation requires a container (Docker/Podman) or VM.** awrepl is a session manager, not a sandbox. Shipping something that _looks_ like a sandbox and is not would be actively dangerous — so this is stated plainly.

Use awrepl only:
- In trusted environments (your own machine, internal tools)
- When running code you wrote or thoroughly reviewed
- Behind proper auth and network boundaries if exposed as a service
- With resource limits imposed at the OS/container level if needed

If you need to run untrusted code, use a container or virtual machine. Do not rely on awrepl alone.

---

## API

### ReplSession

A persistent Python interpreter backed by a subprocess worker.

```python
from awrepl import ReplSession, ExecResult

session = ReplSession(
    session_id="my-session",
    timeout_ms=30000,           # per-call timeout
    max_output_bytes=65536,     # output cap before truncation
)

# Execute code
result: ExecResult = session.execute("x = [1, 2, 3]")
```

**ExecResult** fields:
- `stdout` (str): Captured stdout
- `stderr` (str): Captured stderr  
- `value` (str|None): `repr()` of the last expression, if any
- `exception` (str|None): Error message if execution failed
- `traceback` (str): Full traceback on exception
- `duration_ms` (float): Wall-clock time for execution
- `truncated` (bool): Output was truncated due to size limit
- `truncated_bytes` (int): How many bytes were dropped

**Methods:**

```python
# Execute code in the persistent namespace
result = session.execute("code", timeout_ms=30000)

# Get all bound variables (type and short repr)
variables: dict[str, str] = session.variables()
# {"x": "list: [1, 2, 3]", "name": "str: 'Alice'"}

# Inspect a single variable (type, repr, docstring, etc.)
info: dict = session.inspect("x")
# {"type": "list", "repr": "[1, 2, 3]", "dir": [...], "len": 3}

# Clear all user-defined variables (keeps builtins)
session.reset()

# Close the session and terminate the worker
session.close()

# Use as a context manager
with ReplSession("temp") as s:
    s.execute("x = 42")
```

### SessionPool

Manage multiple REPL sessions indexed by ID.

```python
from awrepl import SessionPool

pool = SessionPool(timeout_ms=30000, max_output_bytes=65536)

# Create a session (auto-generate ID)
sid1 = pool.create_session()

# Create a session with a custom ID
sid2 = pool.create_session("agent-2")

# Get a session
session = pool.get_session(sid2)
session.execute("x = 42")

# List all session IDs
sessions = pool.list_sessions()

# Delete a session
pool.delete_session(sid1)

# Close all sessions
pool.close_all()
```

Each session has its own namespace — one agent's variables don't affect another.

---

## CLI

```bash
# Execute code once (ephemeral session)
awrepl run "print(1 + 2)"

# Use a persistent session across multiple calls
awrepl --session myagent run "x = 42"
awrepl --session myagent run "print(x)"  # Prints 42

# Output as JSON
awrepl run "42" --json
# {"stdout": "", "stderr": "", "value": "42", "exception": null, ...}

# Interactive REPL (basic, stdin-based)
awrepl serve

# Session-based interactive REPL
awrepl --session agent serve

# Verify the REPL contract (no network required)
awrepl --self-test
```

Options:
- `--session ID` — Use a specific session (creates if needed)
- `--json` — Output result as JSON
- `--traceback` — Show full traceback on exception

---

## How it works

awrepl runs a Python subprocess (`python -i`-style worker) and communicates via JSON over pipes. The worker:
- Maintains a single persistent namespace
- Executes code and captures stdout/stderr
- Handles timeouts and output truncation
- Survives syntax errors and exceptions

Multiple `ReplSession` instances can run in parallel, each with its own worker subprocess, enabling concurrent agents to maintain separate state.

**Why subprocess, not in-process `exec()`?**
- In-process execution lets agent code crash or corrupt the host process
- Subprocess isolation means a fatal error in agent code doesn't kill the agent
- Each session gets its own Python interpreter with its own memory space
- Timeouts are more reliable (can interrupt the subprocess)

**Platform support:** Linux, macOS, Windows (tested on all three). Uses only Python standard library — no external dependencies.

---

## `--self-test`

Every install can prove the contract, with no service and no network:

```console
$ awrepl --self-test
  PASS  Variables persist across calls
  PASS  Syntax error doesn't kill session
  PASS  Exception doesn't kill session
  PASS  Output truncation works
  PASS  variables() lists bound names
  PASS  Pool sessions are isolated

SELF-TEST: awrepl ok (6/6)
```

The test asserts:
1. Variables defined in one call are readable in another (the whole point)
2. A syntax error doesn't terminate the session
3. An exception is reported, but the session survives
4. Output longer than `max_output_bytes` is truncated with a flag
5. `variables()` lists bound names correctly
6. Sessions in a pool don't see each other's variables

---

## The bug this package exists to prevent

An agent with access to a live object can **look at it** instead of guessing about it from memory.

Without awrepl:
```python
# Agent's turn 1: I ran this, but I don't remember the result
code = "data = load_json('config.json')"
result = subprocess.run(["python", "-c", code])
# Stdout gone, no access to `data`

# Agent's turn 2: Guess based on memory (but agent memory is compressed/forgotten)
# "What was in that file again? Let me re-run the whole thing..."
```

With awrepl:
```python
# Agent's turn 1: Run code, data persists
session.execute("data = load_json('config.json')")

# Agent's turn 2: Look at it
vars = session.variables()  # {"data": "dict: {...}"}
info = session.inspect("data")  # {"type": "dict", "len": 5, "keys": [...]}
```

Cuts through the memory layer. What you ask is what you get.

---

## Limitations and design choices

**Timeouts:** The `timeout_ms` parameter exists, but relies on the subprocess signal handler. Some heavy operations (deep recursion, infinite loops in C extensions) may not be interruptible. Use OS-level resource limits (cgroups, ulimit) for hard guarantees.

**Namespace pollution:** The namespace persists, so a large object assigned to `x` stays in memory until `reset()` or the session closes. Plan for that.

**No remote execution:** This is a local subprocess REPL. For distributed execution, wrap it in an HTTP service or use it alongside a message queue.

---

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| **awrepl** _(you are here)_ | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | — |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| **awrepl** _(you are here)_ | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | — |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awrepl"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
