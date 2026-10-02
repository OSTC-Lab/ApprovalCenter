# ApprovalCenter

ApprovalCenter is a small, self-hosted approval service.
Clients create approvals through HTTP, reviewers make decisions through Discord buttons, and clients poll the results.

## Features

- Single-step approval with multiple eligible reviewers. The first valid decision is final.
- Immutable approval content with configurable deadlines and automatic timeout handling.
- HTTP Basic authentication and isolation between registered clients.
- Administrator clients for cross-client inspection and custom data updates.
- Opaque per-approval byte storage with optional optimistic version checks.
- SQLite persistence and recovery of unfinished Discord message synchronization.
- Configurable retention, console logging, and a service health endpoint.

## Architecture

FastAPI, the Discord bot, and periodic maintenance run in one process.
They share the approval service and SQLite database.

```mermaid
flowchart LR
    client["HTTP client"]
    reviewers["Reviewers"]
    platform["Discord"]
    database[("SQLite")]

    subgraph center["ApprovalCenter (single process)"]
        api["HTTP API"]
        service["Approval service"]
        bot["Discord bot"]
        maintenance["Periodic maintenance"]
    end

    client <-->|Create, poll, replace data| api
    api <-->|Operations and results| service
    service <-->|Persist and query| database
    reviewers -->|Approve or reject| platform
    platform <-->|Cards and interactions| bot
    bot -->|Submit decision| service
    maintenance -->|Timeouts, cleanup, sync state| service
    maintenance -->|Publish or update cards| bot
```

### Clients and reviewers

A client is an HTTP integration identified by `client_id` and authenticated with `client_secret`.
Both are configured in TOML. Each approval belongs to its creating client.
Changing a secret or display name preserves ownership.

Reviewers are Discord users matched against configured user IDs or role IDs. Either match grants approval permission.
Client administrator permissions affect HTTP data access only.
Discord administrator permissions do not grant approval permission automatically.

### Approvals

Each approval has a non-reused, auto-incrementing integer `approval_id`.
Approval content contains a title, description, and ordered display fields.
The service stores and renders this content without interpreting its business meaning.

All API timestamps are integer Unix timestamps in seconds.

| Status      | Meaning                                           |
|-------------|---------------------------------------------------|
| `pending`   | Awaiting a decision before the deadline           |
| `approved`  | Approved by an eligible reviewer                  |
| `rejected`  | Rejected by an eligible reviewer                  |
| `timed_out` | The deadline was reached without a valid decision |

The final three states cannot change. `expires_at` bounds the decision window.
An approval in the approved state retains that state after the deadline.
Clients define the validity and consumption rules for approved operations.

### Custom data and persistence

Each approval has a byte payload owned by its client. HTTP transports it as standard Base64.
The service does not interpret the payload or display it in Discord.

The payload starts at version `0`.
Every successful replacement increments its version and updates the approval's `updated_at`.
Payload updates do not modify approval content, trigger card updates, or extend retention.
Payloads remain writable in final states.

SQLite stores approvals, payloads, and Discord message associations in three tables.
Creating an approval commits its records before returning an ID.
Background maintenance publishes or updates its Discord card.
A persistent `needs_message_sync` flag preserves unfinished synchronization across failures and restarts.

Queries and decisions enforce the deadline independently of the maintenance interval.
Records are retained until `expires_at + retention_seconds`.
Expired records are excluded from API access and removed during maintenance.
Discord history is retained; deleted cards are not recreated.

Clients are responsible for executing approved operations and matching their execution targets to the approved content.

## Deployment

### Requirements

- Python 3.13 or later and [uv](https://docs.astral.sh/uv/) for source deployment.
- Docker for container deployment; Docker Compose v2 for the supplied template.
- A Discord bot with access to the configured server and approval channel.

The bot needs View Channel, Send Messages, Embed Links, and Read Message History permissions.
It uses the Guilds intent; Message Content and Server Members privileged intents are not required.
Cards and interaction messages use Chinese.

### Source deployment

Run these commands from the repository root:

```bash
uv sync --locked --no-dev --python 3.13
cp config.example.toml config.toml
```

Edit `config.toml` to supply the bot token, Discord IDs, reviewer rules, and client secrets. Start the service:

```bash
.venv/bin/python src/main.py --config config.toml
```

The default configuration path is `config.toml`.
Relative database paths resolve against the configuration file's directory. Configuration changes require a restart.

### Docker deployment

Image: `ghcr.io/ostc-lab/approvalcenter:master` (`linux/amd64`).

Mount `config.toml` at `/config/config.toml` and persist `/data`.
Set `service.host` to `0.0.0.0` and `service.database` to `/data/approval_center.sqlite3`.

A minimal [Docker Compose template](docker/docker-compose.yml) is available to copy and adapt for deployment.

### Operation and development

Logs are written to the console.
They include approval IDs, client IDs, decisions, timeouts, and synchronization errors;
credentials and custom payloads are excluded.

Invalid configuration or database initialization errors prevent startup.
A Discord connection failure leaves HTTP available and health degraded.
Unfinished message synchronization resumes after a temporary connection failure.
A stopped bot task is logged and requires a service restart.

The service runs as a single instance.
Interactive API documentation is available at `/docs`, with the OpenAPI schema at `/openapi.json`.

Development dependencies and checks:

```bash
uv sync --locked --python 3.13
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m mypy src
```

Automated tests use temporary SQLite databases and Discord substitutes.
Live Discord deployment requires validation of card publishing, reviewer permissions, decisions, timeouts,
and interactions after restart.

## Configuration

Configuration is a single TOML file. Unknown fields are rejected. Discord IDs are quoted decimal strings.
At least one client and one reviewer user or role must be configured.

The complete configuration template is available in [config.example.toml](config.example.toml).

### Service

| Field      | Type    | Default                     | Description                                                  |
|------------|---------|-----------------------------|--------------------------------------------------------------|
| `host`     | string  | `"127.0.0.1"`               | HTTP listening address; use `"0.0.0.0"` inside the container |
| `port`     | integer | `8731`                      | HTTP port, from 1 to 65535                                   |
| `database` | string  | `"approval_center.sqlite3"` | SQLite file path                                             |

Relative database paths resolve against the configuration directory.
Container deployments use `/data/approval_center.sqlite3`. The database parent directory is created when needed.

### Discord

| Field               | Type         | Default  | Description                        |
|---------------------|--------------|----------|------------------------------------|
| `token`             | string       | Required | Non-empty bot token                |
| `proxy_url`         | string       | Omitted  | Optional `http://` proxy URL       |
| `guild_id`          | string       | Required | Discord server ID                  |
| `channel_id`        | string       | Required | Approval text channel or thread ID |
| `reviewer_ids`      | string array | `[]`     | Eligible reviewer user IDs         |
| `reviewer_role_ids` | string array | `[]`     | Eligible reviewer role IDs         |

At least one reviewer list must be non-empty. Current membership and roles are checked when a user makes a decision.
Each approval stores its destination server and channel; changing configuration does not migrate existing cards.

Omit `proxy_url` to connect directly. To use an HTTP proxy, set it under `[discord]`, for example
`proxy_url = "http://127.0.0.1:7890"`. Authentication is supported with
`http://username:password@host:port`; percent-encode reserved characters in credentials.
The URL must have a host and an optional port, with no path other than `/`, query, or fragment.
Empty values and proxy schemes other than `http://` are rejected.

The proxy must support CONNECT for Discord HTTPS and Gateway WebSocket connections.
When configured, all bot REST requests, Gateway connections, interaction acknowledgements and follow-up replies,
and library CDN downloads use this proxy. Proxy failures never fall back to a direct connection.
System proxy environment variables are not used. Proxy URLs are stored as secrets; restart the service after changing them.

### Clients

Each `[[clients]]` entry registers an integration.

| Field           | Type    | Default  | Description                                             |
|-----------------|---------|----------|---------------------------------------------------------|
| `client_id`     | string  | Required | Unique, stable ownership identifier                     |
| `client_secret` | string  | Required | Non-empty HTTP Basic password                           |
| `display_name`  | string  | Required | Non-blank card author name, up to 256 UTF-16 code units |
| `enabled`       | boolean | `true`   | Whether this client may authenticate                    |
| `is_admin`      | boolean | `false`  | Allow access to any client's approval and payload       |

`client_id` accepts 1–128 characters from `A-Z`, `a-z`, `0-9`, `_`, `.`, and `-`.

Disabling a client does not remove its approvals.
The example administrator client is disabled until explicitly enabled and assigned a secret.

### Policy

| Field                  | Type    | Default    | Description                                              |
|------------------------|---------|------------|----------------------------------------------------------|
| `maintenance_interval` | number  | `5`        | Positive interval between maintenance cycles, in seconds |
| `max_approval_seconds` | integer | `604800`   | Maximum time between creation and deadline; 7 days       |
| `retention_seconds`    | integer | `15552000` | Time retained after the deadline; 180 days               |
| `max_data_bytes`       | integer | `1048576`  | Maximum decoded custom payload size; 1 MiB               |

All policy values must be positive. Payload updates do not reset retention.
Changes to retention apply to existing records.

### Logging

| Field   | Type   | Default  | Description                                     |
|---------|--------|----------|-------------------------------------------------|
| `level` | string | `"INFO"` | Standard Python logging level; case-insensitive |

Supported standard names include `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, and `NOTSET`.

## API

### HTTP format and authentication

Business endpoints use HTTP Basic. The username is `client_id` and the password is `client_secret`.
Set the `Authorization` header to `Basic ` followed by the standard Base64 encoding of `client_id:client_secret`.

```http
Authorization: Basic <base64(client_id:client_secret)>
Content-Type: application/json
```

POST and PUT bodies use JSON with `Content-Type: application/json`. GET endpoints have no JSON body.
The health endpoint does not require authentication.

| Operation                   | Regular client    | Administrator client   |
|-----------------------------|-------------------|------------------------|
| Create an approval          | Owned by itself   | Owned by itself        |
| List with `all=false`       | Its own approvals | Its own approvals      |
| List with `all=true`        | HTTP 403          | All clients' approvals |
| Read an approval or payload | Its own approvals | Any client's approvals |
| Replace a payload           | Its own approvals | Any client's approvals |

An inaccessible approval and a nonexistent approval both return HTTP 404.
Clients cannot choose an owner during creation.

### Endpoint summary

| Method | Path                                  | Success |
|--------|---------------------------------------|---------|
| POST   | `/api/v1/approval`                    | 201     |
| GET    | `/api/v1/approval`                    | 200     |
| GET    | `/api/v1/approval/{approval_id}`      | 200     |
| GET    | `/api/v1/approval-data/{approval_id}` | 200     |
| PUT    | `/api/v1/approval-data/{approval_id}` | 200     |
| GET    | `/heathz`                             | 200     |

### Create an approval

`POST /api/v1/approval`

| Body field   | Type    | Required | Description                                      |
|--------------|---------|----------|--------------------------------------------------|
| `content`    | object  | Yes      | Immutable display content                        |
| `expires_at` | integer | Yes      | Future Unix deadline in seconds                  |
| `data`       | string  | No       | Standard Base64 payload; defaults to empty bytes |

The deadline must be strictly later than service time and within `max_approval_seconds`.

Display content:

| Field             | Type         | Default  | Limit                                   |
|-------------------|--------------|----------|-----------------------------------------|
| `title`           | string       | Required | Non-blank, up to 256 UTF-16 code units  |
| `description`     | string       | `""`     | Up to 4096 UTF-16 code units            |
| `fields`          | object array | `[]`     | Up to 25 ordered fields                 |
| `fields[].name`   | string       | Required | Non-blank, up to 256 UTF-16 code units  |
| `fields[].value`  | string       | Required | Non-blank, up to 1024 UTF-16 code units |
| `fields[].inline` | boolean      | `false`  | Allow inline layout                     |

The combined length of title, description, and field names and values must not exceed 5500 UTF-16 code units.
The remaining 500 units of the Discord embed capacity are reserved for service metadata.
Unknown JSON body and content fields are rejected.

JSON body:

```json
{
  "content": {
    "title": "Restore backup",
    "description": "Recover from an accidental change",
    "fields": [
      {"name": "Player", "value": "Steve", "inline": true},
      {"name": "Server", "value": "survival", "inline": true},
      {"name": "Backup", "value": "#123", "inline": false}
    ]
  },
  "expires_at": 1790866200,
  "data": ""
}
```

`expires_at` must be a future timestamp when the API is called.

Response structure:

```json
{
  "approval_id": 1,
  "status": "pending",
  "created_at": 1790865600,
  "expires_at": 1790866200,
  "updated_at": 1790865600
}
```

Success confirms database persistence. Discord publication is asynchronous.

### Read an approval

`GET /api/v1/approval/{approval_id}`

| Response field | Type           | Description                                                           |
|----------------|----------------|-----------------------------------------------------------------------|
| `approval_id`  | integer        | Unique approval ID                                                    |
| `client_id`    | string         | Creating client's ID                                                  |
| `content`      | object         | Original display content                                              |
| `status`       | string         | `pending`, `approved`, `rejected`, or `timed_out`                     |
| `created_at`   | integer        | Creation timestamp                                                    |
| `expires_at`   | integer        | Decision deadline                                                     |
| `updated_at`   | integer        | Last status or payload change                                         |
| `decision`     | object or null | Null while pending; otherwise contains `reviewer_id` and `decided_at` |
| `data`         | string         | Current Base64 payload                                                |
| `data_version` | integer        | Current payload version                                               |

When status is `approved` or `rejected`,
`decision.reviewer_id` is the reviewer's decimal Discord ID and `decision.decided_at` is the decision timestamp.
For timeout, the reviewer is null and the decision timestamp equals `expires_at`.

Clients can poll this endpoint once per second. A status change is independent of a payload update.
Multiple updates can share the same second-level `updated_at`; use the payload version for optimistic concurrency.

### List approvals

`GET /api/v1/approval`

| Query parameter  | Type    | Default | Description                              |
|------------------|---------|---------|------------------------------------------|
| `status`         | string  | Unset   | Filter by one approval status            |
| `created_from`   | integer | Unset   | Inclusive creation timestamp lower bound |
| `created_before` | integer | Unset   | Exclusive creation timestamp upper bound |
| `updated_from`   | integer | Unset   | Inclusive update timestamp lower bound   |
| `updated_before` | integer | Unset   | Exclusive update timestamp upper bound   |
| `limit`          | integer | `100`   | Page size, from 1 to 1000                |
| `offset`         | integer | `0`     | Non-negative number of records to skip   |
| `all`            | boolean | `false` | Administrator-only full-client scope     |

When both bounds of a time range are supplied, the lower bound must be less than the upper bound.
Results are ordered by `created_at DESC, approval_id DESC`.

The response contains `items`, `limit`, and `offset`. Each item has the complete approval structure described above.
Increase the offset to read subsequent pages; a page shorter than the limit ends the current listing.
Offset pagination reflects current data rather than a fixed snapshot.

Administrator clients use `all=true` to list all owners. Without that parameter they receive only their own approvals.

### Read custom data

`GET /api/v1/approval-data/{approval_id}`

```json
{
  "data": "eyJkb25lIjp0cnVlfQ==",
  "version": 1,
  "updated_at": 1790865601
}
```

`updated_at` is the approval timestamp and can also change because of an approval decision or timeout.

### Replace custom data

`PUT /api/v1/approval-data/{approval_id}`

| Body field         | Type            | Required | Description                                              |
|--------------------|-----------------|----------|----------------------------------------------------------|
| `data`             | string          | Yes      | Complete replacement payload, encoded as standard Base64 |
| `expected_version` | integer or null | No       | Optional current payload version                         |

JSON body:

```json
{
  "data": "eyJkb25lIjp0cnVlfQ==",
  "expected_version": 0
}
```

A successful response contains the new version and approval timestamp:

```json
{
  "version": 1,
  "updated_at": 1790865601
}
```

A mismatched version returns HTTP 409 and preserves the existing payload.
Read the current version before submitting another version-checked update.
Omitting `expected_version` or setting it to null accepts direct replacement.
An empty string clears the payload to empty bytes.

Payload writes and execution of an external operation do not form a cross-system transaction.
Clients define their own execution and recovery behavior.

### Health

`GET /heathz`

```json
{
  "status": "ok",
  "service": true,
  "database": true,
  "discord": true,
  "maintenance": true
}
```

HTTP 200 indicates healthy operation.
HTTP 503 indicates degraded operation,
with `status` set to `"degraded"` and component flags identifying the failing checks.
HTTP business endpoints can remain available during Discord degradation.

### Errors

Errors use a stable code and a readable message:

```json
{
  "code": "data_version_conflict",
  "message": "Custom data version does not match"
}
```

| HTTP status | Code                    | Meaning                                                          |
|-------------|-------------------------|------------------------------------------------------------------|
| 401         | `authentication_failed` | Missing, invalid, or disabled client credentials                 |
| 403         | `forbidden`             | A regular client requested `all=true`                            |
| 404         | `approval_not_found`    | Missing, inaccessible, or retention-expired approval             |
| 409         | `data_version_conflict` | Payload version mismatch                                         |
| 422         | `invalid_request`       | Invalid body, query, path, deadline, display content, or payload |
| 500         | `internal_error`        | Unexpected service failure                                       |

Framework errors such as unknown paths or unsupported methods use `http_error` with their corresponding HTTP status.
Authentication failures include a `WWW-Authenticate: Basic` header.
