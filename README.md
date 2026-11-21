# A&M FutureTech AI Assistant - Backend

FastAPI backend for the independently maintained `chatbot-be` repository.
The frontend is a separate project and calls this API over HTTP.

## Requirements

- Python 3.10+
- pip

## Installation

From the backend repository root, create and activate a virtual environment,
install requirements, and copy the example configuration:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` is ignored by Git. Keep real credentials there or, preferably, supply
them through the deployment platform's secret manager. Never put backend API
keys in frontend code or frontend environment variables.

## AI modes and provider setup

Mock mode is enabled by default:

```dotenv
USE_MOCK_AI=true
```

In this mode, the server returns a clearly labeled demo response and does not
instantiate a provider client or make external AI requests.

To explicitly enable live Groq responses, configure these variables in the
backend environment:

```dotenv
USE_MOCK_AI=false
AI_PROVIDER=groq
GROQ_API_KEY=your-server-side-secret
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_BASE_URL=https://api.groq.com/openai/v1
```

Do not use the example placeholder values as credentials. The model name is
required when live mode is enabled. Missing configuration returns HTTP `503`;
the application does not silently substitute a demo response. The Groq-compatible client
is created only for configured live requests. Request retries are disabled to
avoid unintentionally repeating billable calls.

`GROQ_BASE_URL` optionally overrides the SDK's provider endpoint (for example,
when using a compatible gateway). Use HTTPS in production and do not embed
credentials in its URL. `GROQ_TIMEOUT_SECONDS` configures the provider
request timeout; valid values are greater than zero and at most 120 seconds.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `APP_NAME` | `A&M FutureTech AI Assistant API` | FastAPI application name |
| `USE_MOCK_AI` | `true` | Use demo responses; set `false` to request live AI |
| `DATABASE_PATH` | `data/chatbot.db` | SQLite file used for chat exchange persistence |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated, exact browser origins; no wildcard |
| `AI_PROVIDER` | `groq` | Provider adapter to use; Groq is currently supported |
| `GROQ_API_KEY` | unset | Backend-only Groq credential, required in live mode |
| `GROQ_MODEL` | unset | Groq model name, required in live mode |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq OpenAI-compatible API base URL |
| `GROQ_TIMEOUT_SECONDS` | `30` | Provider timeout, greater than 0 and at most 120 |
| `CHAT_CONTEXT_LIMIT` | `20` | Maximum recent messages sent as provider context |
| `UPLOAD_ROOT` | `data/uploads` | Private server-side document storage directory |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum upload size per file |
| `DOCUMENT_CHUNK_CHARS` | `3500` | Document chunk size for retrieval |
| `DOCUMENT_RETRIEVAL_CHUNKS` | `4` | Maximum document chunks sent with a question |
| `MEMORY_RETRIEVAL_LIMIT` | `6` | Maximum saved memories sent with a question |
| `SESSION_SECRET` | unset | Secret used to sign browser session cookies |
| `ENVIRONMENT` | `development` | Use `production` to require HTTPS cookies and reject insecure configuration |
| `SESSION_EXPIRE_HOURS` | `168` | Server-side authentication session lifetime |
| `RESET_TOKEN_MINUTES` | `30` | Password reset token lifetime |
| `INITIAL_ADMIN_EMAIL` | unset | Optional initial administrator email; configure only for setup |
| `INITIAL_ADMIN_PASSWORD` | unset | Optional initial administrator password; do not commit |

| `DATABASE_URL` | `sqlite:///data/chatbot.db` | Database location/configuration; preserve the SQLite implementation unless migration tooling is added |
| `COOKIE_SAMESITE` | `lax` | Allowed values: lax or strict |
| `COOKIE_SECURE` | `false` | Must be true in production |
| `MAX_REQUEST_BYTES` | `12582912` | General request body limit |
| `LOGIN_RATE_LIMIT` | `5` | Login attempts per 15-minute database-backed window |
| `REGISTRATION_RATE_LIMIT` | `3` | Registrations per 15-minute database-backed window |
| `RESET_RATE_LIMIT` | `3` | Forgot/reset attempts per 15-minute database-backed window |
| `SMTP_HOST` | unset | SMTP server host |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USER` | unset | SMTP authentication user |
| `SMTP_PASSWORD` | unset | SMTP authentication secret |
| `SMTP_FROM` | unset | Verified sender address |
| `SMTP_USE_TLS` | `true` | Use STARTTLS for SMTP |

| `OCR_ENABLED` | `true` | Enable local Tesseract OCR |
| `TESSERACT_CMD` | unset | Optional absolute Windows Tesseract executable path |
| `POPPLER_PATH` | unset | Optional Poppler `bin` directory for scanned PDF rendering |
| `OCR_LANGUAGE` | `eng` | Tesseract language code |
| `OCR_TIMEOUT_SECONDS` | `30` | OCR/process timeout |
| `OCR_MAX_IMAGE_PIXELS` | `25000000` | Maximum image dimensions in pixels |
| `OCR_MAX_PDF_PAGES` | `30` | Maximum scanned PDF pages processed |

## PostgreSQL migration

PostgreSQL migration support uses `psycopg` and does not delete the source SQLite
database or move uploaded files.

Run a read-only inventory and dry run:

```powershell
python scripts/migrate_sqlite_to_postgres.py --sqlite data/chatbot.db --dry-run
```

Apply the PostgreSQL schema and migrate rows into an existing empty/staging
database:

```powershell
python scripts/check_postgres_connection.py --postgres-url "postgresql://USER:PASSWORD@HOST:5432/DATABASE"
python scripts/migrate_sqlite_to_postgres.py --sqlite data/chatbot.db --postgres-url "postgresql://USER:PASSWORD@HOST:5432/DATABASE"
```

The migration uses transactions, preserves primary keys and timestamps, excludes
expired authentication sessions and expired verification/reset tokens, and
validates destination counts. Use `--replace-existing` only for a deliberate
refresh of matching primary keys.

PostgreSQL runtime repository support is not complete in this codebase: the
application repository implementation remains SQLite-backed. Do not switch
`DATABASE_URL` to PostgreSQL and assume the API will use it without completing
the repository adapter. The migration utility is ready for staged cutover.

`CORS_ORIGINS` must contain the frontend's exact origin, including scheme and
port. The checked-in `.env.example` includes a replaceable production-domain
placeholder. Replace it with the deployed frontend origin; do not use `*`.

## Run locally

With the virtual environment activated:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Or run it without activating the environment:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET http://127.0.0.1:8000/` - liveness and AI mode
- `POST http://127.0.0.1:8000/chat` - demo or live chat
- `POST http://127.0.0.1:8000/chat/stream` - Server-Sent Events live/demo chat stream
- `GET http://127.0.0.1:8000/conversations` - sorted conversation history
- `POST http://127.0.0.1:8000/conversations` - create a conversation
- `GET http://127.0.0.1:8000/conversations/{id}` - open conversation
- `PATCH http://127.0.0.1:8000/conversations/{id}` - rename conversation
- `DELETE http://127.0.0.1:8000/conversations/{id}` - delete conversation
- `DELETE http://127.0.0.1:8000/conversations/{id}/messages` - clear messages
- `GET http://127.0.0.1:8000/docs` - interactive OpenAPI documentation

## API

### `POST /chat`

Request JSON:

```json
{
  "message": "Hello"
}
```

`message` is trimmed and must contain 1-4,000 characters. Unknown request
properties are rejected.

Successful demo and live responses retain the same frontend-compatible shape:

```json
{
  "request_id": 1,
  "response": "A response generated by the configured mode.",
  "mode": "demo"
}
```

`mode` is `"demo"` or `"live"`. The live response is generated by the configured
provider; it is never replaced with a fake response after a provider failure.

Common error statuses:

- `422` - malformed JSON, invalid message, or unknown request property
- `429` - provider rate limit
- `502` / `503` - provider failure or missing provider configuration
- `503` - SQLite storage unavailable
- `504` - provider request timeout

Errors contain safe, user-facing messages and do not include provider response
bodies, API keys, or internal exception text.

The streaming endpoint uses `text/event-stream`. It emits `token` events, then a
single `done` event containing the saved exchange, or an `error` event. Provider
and database failures do not save incomplete responses.

### `GET /`

Example:

```json
{
  "status": "ok",
  "service": "A&M FutureTech AI Assistant API",
  "ai_mode": "demo"
}
```

## Connect `chatbot-fe`

Keep the frontend and backend in separate repositories. In the frontend's
environment, set:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

For local development, allow `http://localhost:5173` in backend
`CORS_ORIGINS`. In production, set `CORS_ORIGINS` to the exact deployed
frontend origin and configure the frontend API URL to the public HTTPS backend
origin. The frontend submits `{"message":"..."}` to `/chat` and reads the
`response` and `mode` fields.

## Conversation data

The existing SQLite behavior is retained: chat messages and generated responses
are saved in `DATABASE_PATH`. There is no conversation-history API or
conversation context in this backend. Protect the database file, define an
appropriate retention policy for your deployment, and avoid sending sensitive
content unless your data policy allows it.

## Production deployment

1. Install dependencies from `requirements.txt` in the deployment environment.
2. Set `USE_MOCK_AI=true` unless live AI has been explicitly configured and
   approved.
3. For live requests, inject `GROQ_API_KEY`, `GROQ_MODEL`, and
   `AI_PROVIDER=groq` as backend environment variables or managed secrets.
4. Configure exact local/production `CORS_ORIGINS`; never allow all origins.
5. Set `DATABASE_PATH` to durable storage with suitable file permissions and a
   backup/retention policy.
6. Serve behind a TLS-terminating reverse proxy or managed HTTPS ingress.
   Bind Uvicorn to the deployment interface (commonly `0.0.0.0`) only inside
   the protected deployment network; do not expose an unprotected development
   server directly to the internet.
7. Monitor sanitized application status and provider errors without logging
   credentials or conversation contents.

## Authentication and migrations

Authentication uses normalized email addresses, bcrypt password hashes, signed
HttpOnly browser cookies, and server-side revocable sessions. State-changing
authenticated requests require the `X-CSRF-Token` returned by login, registration,
or `GET /auth/me`.

Production deployments must set:

```dotenv
ENVIRONMENT=production
SESSION_SECRET=<at-least-32-random-characters>
```

`COOKIE_SECURE` is not currently a separate setting; production mode enables the
Secure cookie attribute for authentication cookies.

Create an initial administrator explicitly through environment variables:

```dotenv
INITIAL_ADMIN_EMAIL=admin@example.com
INITIAL_ADMIN_PASSWORD=<strong-secret>
```

The application creates or promotes that account during startup. Do not commit
real credentials. Remove or rotate these variables after setup.

Legacy anonymous conversations, documents, and memories remain preserved. When
an existing browser session registers or logs in, its legacy records are safely
claimed by that new authenticated account. Records with no browser-session
lineage remain legacy records and are not assigned to an arbitrary user.

Password reset tokens are random, hashed, short-lived, and single-use. No SMTP
delivery service is configured, so `/auth/forgot-password` intentionally does
not claim that email delivery occurred.

## Tests

```powershell
python -m pytest
```

Tests use fake providers and never make billable provider requests.

## Backup and restore

Stop or quiesce the application before copying the SQLite database. Preserve
the configured `DATABASE_PATH` and private `UPLOAD_ROOT` together. Verify the
database backup and a representative upload restore in a staging environment.

Example backup procedure:

```powershell
Copy-Item data/chatbot.db "backups/chatbot-$(Get-Date -Format yyyyMMdd-HHmmss).db"
Copy-Item -Recurse data/uploads "backups/uploads-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

For PostgreSQL, use `pg_dump` and `pg_restore` after configuring credentials
through environment variables. Do not put credentials in shell history when possible.

```powershell
$env:PGPASSWORD="<managed-password>"
pg_dump --format=custom --dbname="postgresql://USER@HOST:5432/DATABASE" --file="backups/chatbot-postgres.dump"
pg_restore --clean --if-exists --dbname="postgresql://USER@HOST:5432/DATABASE" "backups/chatbot-postgres.dump"
Remove-Item Env:PGPASSWORD
```

To restore, stop the application, replace the database and upload directory,
then start the application and verify `/` and authenticated record access.
Run migrations by starting the application; repository initialization performs
backward-compatible `CREATE TABLE` and `ALTER TABLE` migrations. Take a backup
before every production migration and test it in staging.

## OCR setup on Windows

Local OCR uses Tesseract OCR and, for scanned PDFs, Poppler.

1. Install Tesseract OCR, typically at
   `C:\Program Files\Tesseract-OCR\tesseract.exe`.
2. Install Poppler for Windows and record the directory containing `pdftoppm.exe`.
3. Set backend environment variables:

```dotenv
OCR_ENABLED=true
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
POPPLER_PATH=C:\Tools\poppler\Library\bin
OCR_LANGUAGE=eng
OCR_TIMEOUT_SECONDS=30
OCR_MAX_IMAGE_PIXELS=25000000
OCR_MAX_PDF_PAGES=30
```

Tesseract and Poppler paths must remain server-side. Uploads do not expose these
paths to the frontend. If either executable is unavailable, the API rejects OCR
processing with a safe configuration error rather than pretending OCR succeeded.

Groq image understanding is not enabled. OCR output is text-only and is sent to
Groq only as document context after the user asks a question. The Groq provider
does not receive raw image or PDF binary data.
