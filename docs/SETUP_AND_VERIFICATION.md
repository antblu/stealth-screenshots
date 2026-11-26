# Windows Build, Verification, and Packaging Guide

## Prerequisites (Windows 10/11 x64)

- Visual Studio 2022 (with **Desktop development with C++** workload, MSVC v143+, Windows 10/11 SDK `10.0.19041.0` or newer)
- CMake 3.22+ and Ninja (included with Visual Studio Developer Command Prompt)
- Python 3.10+ (for the local mock endpoint server)

## Build & Run Unit Tests

Open **x64 Native Tools Command Prompt for VS 2022**:

```cmd
cmake --preset x64-release
cmake --build --preset x64-release
ctest --preset x64-release
```

## Verification Checklist

1. **Mock endpoint (single & recurring 10s cadence):**
   - Start `python tools\mock_server.py --mode ok --token test-secret`
   - Run `build\x64-release\stealth_screenshots.exe`
   - Click **Settings...**, enter `http://127.0.0.1:8080/v1/chat/completions`, model `default`, credential `test-secret`, system prompt, and check **Allow http://localhost for local mock testing**.
   - Click **Start...**, pick a window or display. Confirm the control window hides, Windows' yellow capture indicator remains visible, and `mock_server.py` logs `valid_json=True system=True image=True status=200` every ~10 seconds.
2. **Hotkeys (no tray icon):**
   - `Ctrl+Alt+P`: Pause / Resume capture.
   - `Ctrl+Alt+O`: Show / Hide the control window.
   - `Ctrl+Alt+S`: Stop capture.
3. **Slow responses (bounded uploads & tick skipping):**
   - Run `python tools\mock_server.py --mode slow --slow-seconds 12`
   - Verify only 1 upload is in flight at a time and `Skipped` increments when an upload exceeds 10 seconds.
4. **Rate limits & backoff:**
   - Run `python tools\mock_server.py --mode 429` (or `--mode mixed`)
   - Verify exponential / `Retry-After` backoff and resumption with a fresh screenshot.
5. **Invalid credentials:**
   - Run `python tools\mock_server.py --mode 401`
   - Verify session stops immediately with `Auth` error.
6. **Window closure & screen lock:**
   - Close the captured target window -> session stops automatically.
   - Lock Windows (`Win+L`) during an active session -> session stops automatically.
7. **Real HTTPS LLM API:**
   - Uncheck `Allow http://localhost`, enter your production `https://...` endpoint and real API credential, and verify end-to-end delivery.

## Packaging (x64)

```cmd
cpack --config build\x64-release\CPackConfig.cmake -B dist
```

This produces `dist\stealth-screenshots-win64-0.1.0-win64.zip` containing `stealth_screenshots.exe`, `README.md`, `LICENSE`, `API_CONTRACT.md`, and `SETUP_AND_VERIFICATION.md`.
