# Agent Session Loop (ASL)

ASL executes a prepared plan one session at a time. Its Autopilot supervisor starts or attaches to `asl run`, watches the active worker, and uses a per-plan mailbox to exchange live guidance, notices, questions, and answers without restarting the session.

```mermaid
flowchart TD
    U[User] <--> S[Autopilot supervisor]
    S -->|starts or attaches| R[asl run]
    R -->|launches next session| W[Worker agent]

    subgraph State[Per-plan ASL state]
        M[(mailbox.jsonl<br/>append-only message log)]
        L[mailbox-lease.json<br/>supervisor heartbeat]
    end

    S -->|renews| L
    S -->|guidance, reply, or resolution| M
    M -->|guidance| W
    W -->|notice or question| M
    M -->|notice or question| S
    M -->|reply or resolution| W
    W -->|consumption receipt| M

    W -->|session result| R
    R -->|next session or final status| S
    W -. no fresh lease, timeout, or mailbox error .-> P[Ordinary paused-run flow]
    P --> S
```

The mailbox is a durable, append-only history; the first reply or resolution closes a question, and the worker records when it consumes that closure. The separate lease proves that a supervisor is still watching; if supervision disappears or mailbox waiting fails, ASL falls back to its normal pause-and-resume path. When `asl outbox wait` actually surfaces an open question, it also persists that run's body-free question projection and makes a best-effort `AgentSessionLoopMailboxQuestion` handoff to AgentMux. Notification failure never changes the outbox result, and closed history does not alert.

> **Maintenance note:** Any work on ASL should include checking this README and updating it when behavior or architecture changes.
