# Notification sound — design

_2026-07-19._

## Problem

When an agent transitions into `waiting_input` (or `error`), the dashboard fires
a browser `Notification` and pulses the card orange. But a browser notification
is easy to miss — it may be off-screen, in Do Not Disturb, or the tab isn't
focused. The user wants an **audible** alert so they know an agent needs them
without watching the screen.

## Decisions

1. **Synthesize the sound, don't ship an asset.** A `<audio>` file or data-URI
   blob would work, but the Web Audio API can generate a short chime with an
   oscillator — zero bytes, no CDN, no external dependency (honors the
   "everything local" rule). Two quick sine tones ≈ 0.3s.

2. **Reuse the existing mute control.** The bell already toggles `muted` and
   gates `notify()`. The sound lives *inside* `notify()`, so the same bell
   silences popup + sound together — one control, no new UI. (Label stays
   "Notifications on / Muted".)

3. **Unlock audio on a user gesture.** Browser autoplay policy blocks
   `AudioContext` until a gesture. Create/`resume()` the context on the bell
   click (already a gesture, and where notification permission is requested) and
   on the first document `pointerdown` as a fallback. Once resumed it stays
   running, so later chimes fire even with no recent gesture.

4. **Once per transition.** Play only on the same `prev !== status` edge that
   already triggers `notify()` — not on every WebSocket state push. No looping /
   nagging (annoying); a single chime per new waiting/error event.

5. **Distinct tone per kind.** `waiting` = a gentle rising two-note chime;
   `error` = a lower falling two-note tone, so the user can tell them apart by
   ear. Kept quiet (low gain, short envelope) so it alerts without startling.

## Fail-safe

Wrapped in try/catch and feature-detected (`window.AudioContext ||
webkitAudioContext`). If Web Audio is unavailable or blocked, the popup
notification still fires exactly as before — the sound is purely additive and
can never break the dashboard.

## Implementation (frontend only, `static/index.html`)

- `audioCtx` (lazy) + `unlockAudio()` — create/resume on bell click and first
  pointerdown.
- `playChime(kind)` — two scheduled oscillator+gain notes; no-op if `muted`, no
  context, or context not running.
- Call `playChime(s.status)` in `notify(s)` (after the permission/mute guards
  that already exist there, but the chime should fire even if Notification
  permission is denied — so gate the chime on `muted` only, not on notification
  permission).

## Verification

Live against the running server (frontend served fresh from disk): simulate a
`waiting_input` transition in the page and confirm a chime plays and no console
errors. No pytest change (pure frontend, no server surface).
