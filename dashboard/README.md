# Hermes feature-check dashboard

A single-container dashboard for feature-check runs. The worker publishes
**data** over an authenticated API; the app stores it in SQLite and renders the
views itself, so changing the design never requires republishing a run.

```
worker ──► POST /api/runs                 status, events, graph, metrics, documents
       └─► POST /api/runs/<id>/media      one screenshot per request, changed files only
              │
              ▼
        /data/dashboard.sqlite3           runs · events · documents · artifacts
        /data/media/<run_id>/             screenshots, GIFs, generated thumbnails
              │
              ▼
        GET /                             all runs, live
        GET /run/<run_id>                 one run
```

Everything durable lives under the mounted `/data`. Nothing persistent is
written anywhere else, so reinstalling, updating or deleting the container loses
nothing — and the app **refuses to start** if `/data` is not writable, rather
than quietly filling the container's ephemeral layer.

## Layout

| Path | Purpose |
|---|---|
| `app/` | The application (FastAPI, Jinja2, SQLite, Pillow, markdown-it). |
| `clients/publish_snapshot.py` | Publishes a snapshot directory. Drop-in for `GitDeployment`. |
| `clients/status_http.py` | Status / heartbeat pings during a run. |
| `tests/` | The test suite. |
| `Dockerfile`, `compose.yaml` | Build and deploy. |

## Getting the image onto the NAS

A Custom App installs from a compose file, which names an image — it cannot
build one for you. Pick one of these. The image installs no compiler and pulls
five pinned wheels, so a build takes seconds either way.

### Option A — build on the NAS (no registry, no login)

TrueNAS SCALE's Apps are Docker-based, so the daemon is already there. Copy this
directory to a dataset and build in place:

```sh
docker build -t hermes-dashboard:latest /mnt/tank/src/dashboard
```

Then in the compose file use the local tag and stop Docker looking upstream:

```yaml
image: hermes-dashboard:latest
pull_policy: never
```

Without `pull_policy: never` the install tries to pull `hermes-dashboard:latest`
from Docker Hub and fails. Rebuild and redeploy the app to update.

### Option B — push to a registry

```sh
cd dashboard
docker build -t ghcr.io/<you>/hermes-dashboard:latest .
docker push ghcr.io/<you>/hermes-dashboard:latest
```

Pushing needs a GitHub personal access token (classic) with `write:packages`.

**GHCR makes a newly pushed package private by default**, even if the source
repository is public, so pulling then needs credentials:

- **Make the package public** (GitHub → Packages → the package → settings →
  change visibility) and the NAS pulls anonymously with no login. Nothing secret
  is baked into the image — `.dockerignore` excludes the tests, clients, compose
  file and data directory, and the API key arrives at runtime as an environment
  variable — but the application source does become publicly downloadable.
- **Keep it private** and run `docker login ghcr.io` on the NAS with a classic
  PAT carrying `read:packages`. A compose file cannot carry registry
  credentials, so this has to happen on the host; it is stored in
  `/root/.docker/config.json`, which a major TrueNAS upgrade can clear (the
  symptom is a pull failure on the next deploy). If your TrueNAS release has a
  registry-credentials screen under Apps, prefer that — it survives upgrades.

## Deploy on TrueNAS

1. Create a dataset for the data, e.g. `/mnt/tank/apps/hermes-dashboard/data`.
2. Give it to the container user: `chown -R 10001:10001 /mnt/tank/apps/hermes-dashboard/data`
   (or set a matching `user:` in the compose file).
3. **Apps → Discover Apps → Custom App → Install via YAML**, paste `compose.yaml`,
   and change the image, the `HFCD_API_KEY` and the volume path.
4. Open `http://<nas>:8080/`. `GET /api/healthz` is the health endpoint and is
   already wired as the image's `HEALTHCHECK`.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `HFCD_API_KEY` | *(unset)* | Required for publishing. Sent by the worker as `X-API-Key`. **While unset the dashboard is read-only and every ingest route answers 503** — it never accepts anonymous writes. |
| `HFCD_DATA_DIR` | `/data` | Database + media root. Must be a mounted volume. |
| `HFCD_SITE_NAME` | `Hermes` | Name shown in the header. |
| `HFCD_PORT` / `HFCD_HOST` | `8080` / `0.0.0.0` | Listen address. |
| `HFCD_ROOT_PATH` | *(empty)* | Set only when served under a reverse-proxy subpath, e.g. `/dashboard`. |
| `HFCD_ISSUE_URL_TEMPLATE` | *(unset)* | Fallback pattern for linking an issue, e.g. `https://github.com/owner/{project}/issues/{number}`. Only used when a run states a bare issue number instead of a URL. Must contain `{number}`; `{project}` is optional. Malformed values are rejected at startup. |
| `HFCD_ISSUE_URL_TEMPLATES` | *(unset)* | JSON object of project → template, for projects on different hosts or owners. Wins over `HFCD_ISSUE_URL_TEMPLATE` for the projects it names. |
| `TZ` | UTC | Timezone for displayed times. Storage is always UTC. |

### Runs from several projects

Nothing needs configuring. A run whose request document carries a real issue
URL is linked from that URL directly, and the repository name in it becomes the
run's project — so `poke-defense-godot` and `piwotworki` runs each link to their
own tracker, and the dashboard grows a project column and project filter chips
the moment a second project appears.

The templates below matter only for runs that state a bare `#116` with no URL.
Pick whichever fits:

```yaml
# One owner, many repositories - {project} is filled per run:
HFCD_ISSUE_URL_TEMPLATE: "https://github.com/daniell0gda/{project}/issues/{number}"
```

```yaml
# Projects on different hosts or owners:
HFCD_ISSUE_URL_TEMPLATES: >
  {"poke-defense-godot": "https://github.com/daniell0gda/poke-defense-godot/issues/{number}",
   "piwotworki":         "https://gitea.lan/dan/piwotworki/issues/{number}"}
```

Both may be set: the map wins for the projects it names, and the template covers
the rest. If a `{project}` template is configured but the run's project is
unknown, no link is produced — an unlinked `#116` beats a link to the wrong
repository.

### Access model

Reads are open to anyone who can reach the port; only writes need the key.
Restrict exposure at the TrueNAS port / reverse proxy / firewall level, not in
the app. Pages are served `noindex, nofollow`.

## Wiring the worker

```python
from publish_snapshot import HttpDeployment

deployment = HttpDeployment("http://truenas.lan:8080", api_key="...")
deployment.publish(snapshot_dir)   # same call signature as GitDeployment
```

Or from the shell:

```sh
export HFCD_API_KEY=...
python3 clients/publish_snapshot.py --base http://truenas.lan:8080 \
    --source .gen/feature-check-dashboard
```

Between publishes, keep a run marked live without re-uploading anything:

```sh
python3 clients/status_http.py --base http://truenas.lan:8080 \
    --run-id my-run --status running --phase code
python3 clients/status_http.py --base http://truenas.lan:8080 --run-id my-run --heartbeat
```

`--dry-run` prints the run id, event count, every document with its size and
every screenshot with its sha1, without contacting a server.

## Did the issue actually get closed?

A run's verdict says the gate passed; it does not say the issue was resolved.
Report the forge's own answer and the dashboard shows it as a column in the run
list, a tag on the run page, an *Issues closed* tile and a Done / Not done
filter:

```sh
curl -X POST -H "X-API-Key: $HFCD_API_KEY" -H 'Content-Type: application/json' \
    -d '{"gh_status": "closed"}' \
    http://truenas.lan:8080/api/runs/corrosive-soak-90-r1/issue
```

Anything the forge calls it is accepted as `gh_status`; `closed` and `merged`
are the two that mean done. Nothing polls GitHub from inside the container —
whatever already knows the issue state (the worker after it closes the issue, or
a cron job over `gh issue view`) posts it. Until something does, the column and
the filter are not rendered at all, so the view stays exactly as it was.

## Repeat runs for one job

The run id is the only key the dashboard has. It comes from the snapshot's
`status.json` (or the `runs/<id>` directory name), and everything — timeline,
documents, screenshots — hangs off it. So a second attempt at the same job has
two possible shapes, and they behave very differently.

**A new run id (the normal case).** The published history uses a counter suffix:
`corrosive-soak-90-r1`, `…-r2`, `req-89-exposed-plating-r8`. Each is an
independent run with its own verdict and timeline; all they share is the issue
they name and the feature name. The dashboard deliberately does not group them —
there is no *supersedes*, no *latest attempt* marker, and no list of sibling runs
on a run page. Find the set from the outside instead:

```sh
# every attempt at one feature, newest first — the r1/r2 pair above shares
# feature "corrosive-soak-perk", so an exact match collects both
curl 'http://truenas.lan:8080/api/runs?feature=corrosive-soak-perk'
```

or type the shared part of the id into the list's search box (`q` matches run id,
feature and project).

Note that **revisions** on a run is a different thing entirely: extra passes
through the coding stage *inside* one run, not a second run.

**Reusing the run id (republishing an attempt).** `POST /api/runs` is an upsert,
so this is the right thing to do while a run is still going, or to correct a
snapshot. Events and documents are replaced wholesale and the derived columns are
recomputed — but **screenshots are only ever added**. Artifacts are keyed on
`(run_id, path)`, `sync_media` only uploads what the manifest is missing, and
nothing prunes what the new snapshot no longer contains, so images from the
earlier attempt stay in the gallery. If the second attempt is genuinely a
different piece of work, delete first rather than publishing over it:

```sh
curl -X DELETE -H "X-API-Key: $HFCD_API_KEY" \
    http://truenas.lan:8080/api/runs/corrosive-soak-90-r1
```

## API

Writes need `X-API-Key`; reads are open (CORS `*`).

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/runs` | Upsert a run. Idempotent — events and documents are replaced wholesale, in one transaction. |
| POST | `/api/runs/<id>/media?path=screenshots/x.png` | Store one image. Raw bytes as the body, not base64. |
| GET | `/api/runs/<id>/media` | `path → sha1` manifest, so a publisher uploads only what changed. |
| POST | `/api/runs/<id>/status` | Patch `status`, `phase`, `active_node`, `last_node`, `error`; bumps the heartbeat. |
| POST | `/api/runs/<id>/issue` | Set `gh_status` — how the run's issue stands on the forge. `done` is derived, not accepted. Does **not** bump the heartbeat. |
| POST | `/api/runs/<id>/heartbeat` | Heartbeat only. |
| DELETE | `/api/runs/<id>` | Remove a run, its timeline, documents and media files. |
| GET | `/api/runs?status=&feature=&project=&done=&q=&page=&per_page=` | Run list, newest first. `status` also takes the derived `abandoned`, and `other` for everything outside running/completed/failed. `done` takes `yes` / `no`. |
| GET | `/api/runs/<id>` | One run with timeline, stages, document index and media index. |
| GET | `/api/summary` | Aggregate counters. |
| GET | `/api/healthz` | Liveness probe. |
| GET | `/api/docs` | Generated OpenAPI docs. |

## What the app derives

The worker does not report these; they are computed once at publish time.

- **duration** from `started_at`/`ended_at`; a live run shows elapsed time instead.
- **worker time** as the sum of event durations — real compute, not wall clock.
- **revisions** as the number of extra passes through the coding stage.
- **verdict** parsed from `**Classification:**` in the team-leader report, falling
  back to `classification:` in the check report. The real vocabulary is
  `pass`, `fixable`, `blocked`, `unknown`, `design_failure` — each coloured
  distinctly, not just pass/fail.
- **token totals** summed from `metrics.json`'s `invocations` list, which the old
  dashboard ignored (it reported "Unavailable"). Cost stays blank while every
  invocation reports `cost_status: unknown`, because the `0.0` in that case is a
  placeholder, not a real $0.00.
- **spend today / this week / this month**, alongside the all-time total, from
  the cost of the runs that began in each window — the run's own start, falling
  back to when it was published. Derived on read, so the windows roll over on
  their own. Days, weeks and months are the container's local ones (the week
  starting Monday), matching the timezone every absolute time is displayed in.
  A window holding no priced run reads as a dash, not $0.00. `/api/summary`
  carries the three figures under `spend`.
- **the originating issue and its project**, resolved best-evidence-first:
  1. `issue_url` / `issue_number` / `project` stated outright in the publish
     payload;
  2. a real issue URL inside the run's own documents — the request document
     carries one on **147 of the 182** published runs. This is authoritative and
     needs **no configuration for any number of projects**, because the URL names
     its own repository. GitHub, Gitea and GitLab (`/-/issues/`) shapes are all
     recognised, including inside markdown links and angle brackets;
  3. the number in the request heading (`# Request: #116 …`), turned into a link
     from a configured template. Without one the number is still shown,
     unlinked, rather than pointing somewhere invented.

  Two things are deliberately **never** inferred. The issue number is not taken
  from the run id — ids also embed timestamps and revision counters
  (`heart-hud-beat-139-1787422828`), so that would risk linking to the wrong
  issue. And the project is not taken from workspace paths or the `Project:`
  line: those name the local runner workspace, and measurably disagree with the
  repository on 12 published runs (they say `godot-td` where the issue lives in
  `poke-defense-godot`).

  Measured over the published history, tiers 2 and 3 cover 154/182 runs and
  never disagree with each other.
- **done**, from the `gh_status` last reported for the run's issue: `closed` and
  `merged` count as done, everything else — `open`, `reopened`, a project column
  name — does not. The flag is derived rather than accepted, so it can never
  contradict the state it summarises. A run nobody has reported a state for is
  not done, but it is not *open* either: the issue column shows a dash and the
  run page grows no tag, so a run predating the integration says nothing rather
  than claiming its issue is still open.
- **liveness** — a run still marked `running` shows as *possibly stale* after
  2 minutes without a heartbeat and *abandoned* after 10. Derived on read, so
  there is no cron job to keep alive. Abandoned is a filter of its own
  (`?status=abandoned`) and is excluded from *Running now* and from the running
  count, so a stuck run is never reported as live work.
- **stage pipeline** — `graph.json` names stages as gerunds (`implementing`)
  while events use worker names (`code`); both fold onto one canonical stage so
  the graph's shape can be filled in with real event data.

## Storage notes

- Timestamps are stored UTC and displayed in the container's timezone.
- Screenshots are served as ordinary static files with normal caching; only
  metadata is in the database.
- Uploads are decoded with Pillow before being written and must match their
  extension — an extension alone is not evidence, and the media directory is
  public.
- Animated GIFs are never resized (it would kill the animation); they are served
  whole and flagged `GIF` in the gallery. Detection is Pillow's `is_animated`.
- Markdown is stored raw and rendered by the app with raw HTML escaped, so a
  report cannot inject markup. Image references resolve only against that run's
  own artifacts; an unknown reference renders as its alt text.
- SQLite runs in WAL mode, so a publish never blocks a reader.

## Development

```sh
cd dashboard
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest

HFCD_API_KEY=dev HFCD_DATA_DIR=./data python -m app
```
