# Citation-aware RAG

Upload documents, run the worker, and ask questions with citations. PostgreSQL + pgvector is the recommended backend. It keeps document versions, chunks, search indexes, jobs, and traces in one database; uploaded files remain on the local filesystem.

## 1. What you need

| Required | Optional |
| --- | --- |
| Python 3.11+, `pip`, and two terminals | An LLM API key for generated answers |
| Docker with Compose v2 for PostgreSQL | Docling for DOCX or scanned PDFs |
| A gateway API key in `RAG_API_KEYS` | Sentence-transformers for stronger semantic search |

**An LLM is not required.** Without one, answers are cited extracts. Docker is not required for the [SQLite fallback](#6-sqlite-fallback-and-migration), but SQLite does not support document versions. Run all commands from the repository root. `.env.example` is a reference; the app does not load it automatically.

Create the virtual environment and install the PostgreSQL dependencies:

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[postgres,test]"
```

```bash
# macOS/Linux Bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[postgres,test]'
```

On Windows, try `py -3 -m venv .venv` if `python` is not found.

## 2. Connect an LLM API (optional)

Do this **before starting the API** if you want model-generated answers. `RAG_API_KEYS` is for callers of *your RAG service*. `OPENAI_API_KEY` is for the *outbound LLM provider*. The worker does not need the LLM key.

This app uses the OpenAI Python client's **Chat Completions** format. OpenAI and compatible providers work without code changes. A compatible endpoint accepts `model`, `messages`, and `temperature=0` and returns text at `choices[0].message.content`.

Obtain an API key and model ID from your provider. In the **API terminal**, install the LLM extra and enter the key without echoing it:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[llm]"
$secret = Read-Host 'LLM provider API key' -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
```

```bash
.venv/bin/python -m pip install -e '.[llm]'
read -r -s -p 'LLM provider API key: ' OPENAI_API_KEY; printf '\n'
export OPENAI_API_KEY
```

Then set the provider model and endpoint **in that same terminal**:

- **OpenAI:** set `$env:RAG_LLM_MODEL = 'gpt-4o-mini'` in PowerShell or `export RAG_LLM_MODEL='gpt-4o-mini'` in Bash (or choose a model available to you). Leave `OPENAI_BASE_URL` unset; if you previously set it, run `Remove-Item Env:OPENAI_BASE_URL -ErrorAction SilentlyContinue` or `unset OPENAI_BASE_URL`.
- **Another Chat Completions-compatible provider:** set `RAG_LLM_MODEL` to its model ID and `OPENAI_BASE_URL` to its base URL, usually ending in `/v1`. Do not include `/chat/completions`. For example:

```powershell
$env:RAG_LLM_MODEL = 'provider-model-id'
$env:OPENAI_BASE_URL = 'https://api.provider.example/v1'
```

```bash
export RAG_LLM_MODEL='provider-model-id'
export OPENAI_BASE_URL='https://api.provider.example/v1'
```

Replace both placeholders. The variable is still named `OPENAI_API_KEY` because the installed client reads it; use **your chosen provider's** key. For a trusted local server that needs no auth, the current code still needs a nonempty local placeholder key to enable LLM calls. Never use a dummy key for an external service.

Keep these variables in the API terminal for step 3. Never commit real keys. External calls may incur charges and send questions plus retrieved evidence to the provider. [Different API formats](#5-other-llm-apis-and-features) need an adapter.

## 3. Start PostgreSQL, API, and worker

Start Docker and the included pgvector database:

```text
docker compose up -d --wait
```

**Terminal 1 - API.** Use the same terminal as step 2 if you configured an LLM:

```powershell
$env:RAG_DATABASE_URL = 'postgresql://rag:local-rag-dev@127.0.0.1:55432/rag'
$env:RAG_API_KEYS = '{"local-dev-key":{"tenant":"demo","scopes":["default"]}}'
$env:RAG_DATA_DIR = (Join-Path (Get-Location) 'data')
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

```bash
export RAG_DATABASE_URL='postgresql://rag:local-rag-dev@127.0.0.1:55432/rag'
export RAG_API_KEYS='{"local-dev-key":{"tenant":"demo","scopes":["default"]}}'
export RAG_DATA_DIR="$PWD/data"
.venv/bin/python -m uvicorn app.main:app --reload
```

**Terminal 2 - worker.** Open a new terminal in the repository root:

```powershell
$env:RAG_DATABASE_URL = 'postgresql://rag:local-rag-dev@127.0.0.1:55432/rag'
$env:RAG_DATA_DIR = (Join-Path (Get-Location) 'data')
.\.venv\Scripts\python.exe -m app.worker
```

```bash
export RAG_DATABASE_URL='postgresql://rag:local-rag-dev@127.0.0.1:55432/rag'
export RAG_DATA_DIR="$PWD/data"
.venv/bin/python -m app.worker
```

Keep both processes running. They must use the **same** database URL, data directory, and embedding settings. Open `http://127.0.0.1:8000/health` to check the API and `http://127.0.0.1:8000/docs` for all routes. Health does not check the worker or LLM.

## 4. Upload, query, and update a document

Use a **third** PowerShell terminal from the repository root. Create two local sample documents for this walkthrough: the first says **30 days** and the replacement says **45 days**. These files are created under the ignored `data/` directory; no bundled examples are needed.

```powershell
$base = 'http://127.0.0.1:8000'
$headers = @{ 'X-API-Key' = 'local-dev-key' }
New-Item -ItemType Directory -Force -Path data | Out-Null
Set-Content -Path data/policy.md -Encoding utf8 -Value 'Customers may request a refund within 30 days of purchase.'
Set-Content -Path data/policy_v2.md -Encoding utf8 -Value 'Customers may request a refund within 45 days of purchase.'
$upload = curl.exe -sS -X POST "$base/v1/documents" -H 'X-API-Key: local-dev-key' -F 'scope=default' -F 'file=@data/policy.md' | ConvertFrom-Json
$documentId = $upload.id

# Uploads are queued. Wait for the worker to publish the version.
do {
    Start-Sleep -Seconds 1
    $document = Invoke-RestMethod "$base/v1/documents/$documentId" -Headers $headers
} while ($document.status -in @('queued', 'processing'))
$document | Select-Object status, active_version_id, error

$body = '{"scope":"default","question":"What is the refund period?"}'
$answer = Invoke-RestMethod "$base/v1/query/sync" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$answer | ConvertTo-Json -Depth 6
```

Now upload **different content** as a new version:

```powershell
curl.exe -sS -X POST "$base/v1/documents/$documentId/versions" -H 'X-API-Key: local-dev-key' -F 'scope=default' -F 'file=@data/policy_v2.md'
do {
    Start-Sleep -Seconds 1
    $document = Invoke-RestMethod "$base/v1/documents/$documentId" -Headers $headers
} while ($document.status -in @('queued', 'processing'))
$document | Select-Object status, active_version_id, latest_version_id, error
(Invoke-RestMethod "$base/v1/documents/$documentId/versions" -Headers $headers).versions |
    Select-Object version_number, status, active, version_id
$answer = Invoke-RestMethod "$base/v1/query/sync" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$answer | ConvertTo-Json -Depth 6
```

The citation `version_id` should match `active_version_id`. PostgreSQL publishes chunks and the active pointer in one transaction. A failed replacement leaves the old version searchable; `POST /v1/documents/{id}/retry` retries the failed latest version. Re-uploading identical bytes is deduplicated.

On macOS/Linux, create the same local documents:

```bash
mkdir -p data
printf '%s\n' 'Customers may request a refund within 30 days of purchase.' > data/policy.md
printf '%s\n' 'Customers may request a refund within 45 days of purchase.' > data/policy_v2.md
```

Use `curl` with the same routes. Upload `data/policy.md`, copy its returned `id`, poll `GET /v1/documents/{id}` until `ready`, then call `POST /v1/query/sync` with JSON `{"scope":"default","question":"What is the refund period?"}`. Upload `data/policy_v2.md` to `POST /v1/documents/{id}/versions` and query again. The exact request schemas are at `/docs`.

### Did the LLM run?

`generation_mode: llm` means a model answer passed the grounding check. `generation_mode: extractive` is the cited-excerpt fallback. `status: abstained` means retrieval found too little evidence to call the LLM. Inspect the query trace if you expected an LLM answer:

```powershell
$answer | Select-Object status, generation_mode, trace_id
Invoke-RestMethod "$base/v1/observability/traces/$($answer.trace_id)" -Headers $headers
```

Look for `llm_generation` errors or a rejected `grounding_check`. Setting a key alone does not test connectivity.

## 5. Other LLM APIs and features

**Different LLM API format:** There is no universal URL setting. If your provider does not support Chat Completions, adapt `app/retrieval.py` at `_rewrite`, `_hypothetical`, and `_generate`, preferably through one shared provider client. Map the existing prompts to its request schema, extract plain text from its response, update the `OPENAI_API_KEY` checks if using another key variable, and keep the evidence-only prompt, `[n]` citations, grounding check, and extractive fallback. Test success, provider errors, and weak-evidence abstention with mocked responses before a live key.

**Embeddings:** `app/embedding.py` supports a 256-dimensional hash baseline and local sentence-transformers. An external embedding API needs a new embedder used by **both** API and worker. Install `.[semantic]` and set `RAG_EMBEDDER=sentence-transformers` with `RAG_EMBEDDING_DIMENSIONS=384` for local semantic retrieval. Changing models/dimensions requires a fresh PostgreSQL index and reindexing.

**Documents:** Upload `.txt`, `.md`, `.pdf`, or `.docx` (default limit 20 MiB). Install `.[documents]` for Docling and DOCX/scanned-PDF processing. Without Docling, text PDFs use `pdfplumber`; scanned PDFs need OCR.

**Retrieval and security:** PostgreSQL combines full-text `tsvector`/`ts_rank_cd` and pgvector search with RRF, reranking, a relevance gate, and parent expansion. Every `/v1` call needs `X-API-Key`; `RAG_API_KEYS` maps keys to tenants and permitted scopes. Queries and citations stay within that tenant/scope.

**Streaming and observability:** `POST /v1/query/sync` returns JSON. `POST /v1/query` sends SSE `started`, `result`, and `done` events; it buffers the final answer rather than streaming tokens. `GET /v1/observability/stages?scope=default&hours=24` shows stage latency and failures. Trace IDs connect upload, worker, and query stages.

## 6. SQLite fallback and migration

For a small trial without Docker, omit `RAG_DATABASE_URL`, install `.[test]` rather than `.[postgres,test]`, and start the API/worker with the same `RAG_DATA_DIR`. Upload and query work; document-version routes return `501`.

To move existing SQLite data to PostgreSQL, stop the SQLite API/worker, keep `data/metadata.sqlite3` **and its original object files** readable, start PostgreSQL, set `RAG_DATABASE_URL`, then run:

```powershell
.\.venv\Scripts\python.exe -m app.migrate_sqlite --sqlite-db data/metadata.sqlite3 --dry-run
.\.venv\Scripts\python.exe -m app.migrate_sqlite --sqlite-db data/metadata.sqlite3
```

Run the PostgreSQL worker afterward and verify documents reach `ready` before switching the API. Migration verifies SHA-256 and preserves document IDs; the worker must still be able to read the old object paths.

## 7. Tests and troubleshooting

With Docker running, create a **disposable** test database once, then run tests from a fresh terminal with `RAG_DATABASE_URL` unset:

```powershell
docker compose exec postgres createdb -U rag rag_test
$env:RAG_TEST_DATABASE_URL = 'postgresql://rag:local-rag-dev@127.0.0.1:55432/rag_test'
.\.venv\Scripts\python.exe -m pytest -q
```

Skip `createdb` if `rag_test` already exists. Without `RAG_TEST_DATABASE_URL`, PostgreSQL tests skip and SQLite tests run. To try PDF ingestion, upload your own text PDF using the same upload command in step 4, replacing `data/policy.md` with its path.

If something fails:

- **Queued forever:** start the worker; check that API and worker share the database URL and data directory.
- **401/403:** check `X-API-Key` and its permitted scope in `RAG_API_KEYS`.
- **Extractive instead of LLM:** check the provider key, model, compatible base URL, and `.[llm]` install; inspect the query trace.
- **PostgreSQL connection error:** start Docker, check `docker compose ps` and port `55432`.
- **DOCX/scanned PDF fails:** install `.[documents]`.
- **Embedding signature error:** restore the original model/dimensions or reindex in a new database.

The included database password and gateway key are **local development values**. For production, add shared object storage, secret management, connection pooling, backups, and trace/version retention. Retrieval and grounding checks are heuristics, not proof of factual correctness.
