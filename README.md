# MJPEG Question Detection & Multimodal LLM Application

A lightweight, containerized Python 3.12+ application that consumes a continuous MJPEG HTTP stream, detects new questions in authorized educational content using perceptual hashing, SSIM, local OCR, and RapidFuzz deduplication, submits question images to an OpenAI-compatible multimodal LLM endpoint, and exposes answers via a bearer-authenticated FastAPI HTTP API.

## Architecture

- **MJPEG Ingestion (`app/source/`)**: Persistent async `httpx` streaming client with multipart boundary detection, incremental JPEG extraction preserving original bytes, malformed-frame handling, bounded memory buffers, configurable auth headers, and exponential-backoff reconnection.
- **Question Detection (`app/detection/`)**: Modular pipeline combining 64-bit DCT perceptual hashing (`pHash`), Structural Similarity Index (`SSIM`), frame stability verification, local RapidOCR or Tesseract, text normalization, heuristic question classification, and RapidFuzz sliding-window deduplication. Detection state is isolated from LLM request state.
- **Multimodal LLM Client (`app/llm/`)**: Async `httpx` client posting base64-encoded JPEG data URLs directly to the configured Chat Completions endpoint with bounded retries and strict credential redaction.
- **Output HTTP API (`app/api/`)**: FastAPI server on port `8000` with public `/health` and bearer-authenticated `/status`, `/latest`, `/latest/text`, `/history`, `/questions/{id}`, and `/metrics`.

## Configuration

1. Copy `.env.example` to `.env` and fill in your values (do not commit `.env`):
   - `MJPEG_URL`: e.g. `http://127.0.0.1:8080/stream` (native) or `http://host.docker.internal:8080/stream` (Docker)
   - `LLM_ENDPOINT`: `https://llm.example.com/v1/chat/completions`
   - `LLM_MODEL`: `gemma-4-E4B-it-GGUF:UD-Q4_K_XL`
   - `LLM_API_KEY`: your LiteLLM API key
   - `SERVER_TOKEN`: a separate bearer token for this application's HTTP API
2. Adjust `configuration.yaml` or `system_prompt.txt` as needed without modifying source code.

RapidOCR uses up to four CPU threads by default. Adjust `detection.ocr_threads` for the machine running the service. Keep the detection crop fitted to the question and answer choices so toolbars and timers outside it can change without triggering OCR. The default stability check requires two visually similar frames before OCR. Each accepted question passes text deduplication before entering the LLM queue.

## Running Natively

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
python -m app.main
```

## Running with Docker Compose

```bash
docker compose up --build
```

For Linux host networking, uncomment `network_mode: host` in `docker-compose.yaml` (and remove `ports` / `extra_hosts`), then set `MJPEG_URL=http://127.0.0.1:8080/stream`.

## API Usage

```bash
curl http://127.0.0.1:8000/health

curl -H "Authorization: Bearer ${SERVER_TOKEN}" http://127.0.0.1:8000/latest/text
curl -H "Authorization: Bearer ${SERVER_TOKEN}" http://127.0.0.1:8000/latest
curl -H "Authorization: Bearer ${SERVER_TOKEN}" http://127.0.0.1:8000/status
curl -H "Authorization: Bearer ${SERVER_TOKEN}" http://127.0.0.1:8000/history
curl -H "Authorization: Bearer ${SERVER_TOKEN}" http://127.0.0.1:8000/metrics
```

Use `ocr_p50_ms` and `ocr_p95_ms` to measure local OCR. `frame_to_upload_p50_ms` and `frame_to_upload_p95_ms` measure from receipt of the accepted frame to the first completed client upload for requests that returned an answer. The wait for the first stable frame pair and any upstream capture delay precede this measurement.

## Testing

Run unit and mocked integration tests:

```bash
pytest -m "not integration"
```

Run the opt-in live LiteLLM integration test (requires `LIVE_INTEGRATION=1` and `LLM_API_KEY`):

```bash
LIVE_INTEGRATION=1 LLM_API_KEY=your_key pytest -m integration
```
