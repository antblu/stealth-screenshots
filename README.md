# Stealth Screenshots

A C++20 Windows application that captures a selected window or display every 10 seconds using Windows Graphics Capture (C++/WinRT) and sends each in-memory screenshot to a configurable HTTPS LLM endpoint with a custom system prompt.

## Features

- **No tray icon & no foreground window while running:** Configure settings and start capture from a compact control window, which hides once the session starts. Control active sessions via global hotkeys (`Ctrl+Alt+O` Show/Hide, `Ctrl+Alt+P` Pause/Resume, `Ctrl+Alt+S` Stop).
- **Windows Graphics Capture:** Uses `GraphicsCapturePicker` and keeps Windows' capture indicator enabled. Automatically stops if the target window closes or Windows locks (`WM_WTSSESSION_CHANGE`).
- **In-memory encoding:** Captures D3D11 frames and encodes them to PNG/JPEG in memory via WIC (never writes screenshots to disk).
- **Secure credential storage:** Stores the API credential in Windows Credential Manager (`StealthScreenshots/ApiCredential`).
- **Bounded uploads & backoff:** Uses a monotonic 10-second clock, allows at most 1 upload in flight (skipping missed ticks), backs off on network errors and `429` rate limits, and logs only timestamps, HTTP status codes, and error categories.

## Documentation

- [API Contract](docs/API_CONTRACT.md)
- [Windows Setup, Verification, and Packaging](docs/SETUP_AND_VERIFICATION.md)
