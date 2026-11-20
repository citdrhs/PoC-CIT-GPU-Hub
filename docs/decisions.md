# Implementation decisions

| Decision | Reason and consequence |
| --- | --- |
| Start with a single-host SQLite control store | Transactions, unique active reservations and durable launch records support a repeatable local workflow. A school-wide multi-host deployment needs a controller transport and storage architecture before a second node can run submissions. |
| Use separate native API, scheduler and worker processes | Students use HTTP clients. Docker authority and host-level control stay in the worker. The worker can reconcile labeled containers and enforce a local runtime during API outages. |
| Serve the web console as ES modules and CSS | The repository runs without an npm install or CDN. The dashboard and standard-library CLI share the versioned REST interface. FastAPI exposes the machine-readable schema at `/openapi.json`; integration tests and browser smoke checks cover client calls. |
| Two fixed envelopes, one active principal | Each running attempt or builder consumes a fixed slot. Recent usage influences queue order within a hardware pool. The limits on the remaining attempt stay fixed after another finishes. |
| Distinguish deterministic demonstration from Linux containers | The demo exercises authentication, revisions, slots, deadlines, cancellation and outputs using a timer worker. The Docker worker runs the actual submitted source with kernel resource settings. Both may label GPU capacity as simulated. |
| Use operator-issued tokens for the first private deployment | Tokens expire and support read-only scopes and revocation. OIDC federation with the school is a later deployment decision. Development identities require an explicit local server flag. |
| Support an offline, hash-locked native wheelhouse | Builders run within a reserved envelope and mount the operator’s curated wheelhouse read-only. Cross-architecture environments need their own base image and matching wheels. |
| Gate physical GPUs independently | The Dell needs the supported fixed two-instance MIG layout, matching evidence, native image and verified device readback. Spark stays blocked pending an enforceable aggregate compute and unified-memory boundary. |
| Separate evidence for each milestone | The development test suite, native Linux cgroup tests, hardware isolation qualification and school pilot answer different questions. The [qualification procedure](qualification.md) records those evidence requirements. |
