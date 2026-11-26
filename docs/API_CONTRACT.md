# Custom LLM API Contract

All API-specific serialization, authentication header formatting, size limits, and HTTP status classification are isolated behind `stealth::core::IApiClient` and `stealth::core::ApiContractConfig` in [src/core/api_contract.hpp](../src/core/api_contract.hpp).

## Default Request Format

- **Method:** `POST`
- **Transport:** HTTPS required (`WINHTTP_FLAG_SECURE`). `http://localhost` / `http://127.0.0.1` is permitted only when `allow_http_localhost` is explicitly enabled for local mock testing.
- **Headers:**
  - `Content-Type: application/json; charset=utf-8`
  - `Authorization: Bearer <api_credential>` (header name and scheme configurable via `ApiContractConfig`)
- **JSON Body:**

```json
{
  "model": "default",
  "messages": [
    {
      "role": "system",
      "content": "<configured system prompt>"
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Screenshot captured at 2026-11-25T12:00:00Z"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,<base64-encoded-image>"
          }
        }
      ]
    }
  ]
}
```

## Image Format & Size Limits

- **Default format:** `image/png` (falls back to `image/jpeg` if encoded PNG exceeds `max_image_bytes`).
- **Max encoded image size:** `4 MiB` (`4 * 1024 * 1024` bytes) by default.
- **Max dimension:** `1920 px` on the longest edge (downscaled in-memory via WIC if larger).
- **Storage:** Screenshots are encoded in memory (`IStream` / `HGLOBAL`) and never written to disk.

## Response & Error Handling

- **2xx:** Delivery succeeded; response body is drained and discarded in v1.
- **401 / 403:** Treated as `ErrorCategory::Auth`; stops the capture session immediately.
- **429:** Treated as `ErrorCategory::RateLimit`; backs off exponentially (or honors numeric `Retry-After` header, capped at `max_backoff`) and resumes with a fresh screenshot.
- **408 / 5xx / Network error:** Treated as transient failure; backs off exponentially and resumes with a fresh screenshot.
- **Request timeout:** `15000 ms` default.
