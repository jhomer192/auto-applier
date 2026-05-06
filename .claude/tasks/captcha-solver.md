# CAPTCHA Solver — Spec

## Goal

Reduce manual Telegram-fallback CAPTCHA solving for the auto-applier. Layered approach so we never spend more engineering time than the friction is costing. Build only the layer that telemetry justifies.

## Reality check (read first)

LinkedIn rarely shows traditional image CAPTCHAs anymore. What you'll actually hit:

- **Behavioral risk scores** (reCAPTCHA v3, Cloudflare Turnstile) — invisible; no puzzle to solve. The "fix" is browser fingerprint + persistent session, not a solver.
- **Arkose / FunCaptcha** — LinkedIn's primary checkpoint challenge. Designed specifically against ML; rotates challenge types weekly. Effectively unsolvable without paid solver farm.
- **LinkedIn checkpoint challenge** (`/checkpoint/challenge`) — phone/email verify, not a CAPTCHA. Needs Telegram fallback, period.
- **reCAPTCHA v2 image grid** — the only one that's even theoretically solvable in-house. Probably <10% of what we'll see.

So a homegrown solver attacks at most one slice of the problem. Plan accordingly.

## Layers (build order)

### Layer 0 — Avoidance (most leverage; build first)

Not really a "solver" but the cheapest minute spent. If we're seeing CAPTCHAs at all, layer 0 is leaking.

- `playwright-stealth` plugin on every browser context
- Persistent `storageState` (`data/linkedin_auth.json`) — never burn the session
- Human-like timing: jitter mouse moves, 800–2400ms between field fills, scroll before click
- Don't open >1 LinkedIn tab; respect a 30-min poll floor on `SEARCH_POLL_INTERVAL`
- Rotate User-Agent only on session creation, never mid-session
- Bonus: residential proxy (Oxylabs/Bright Data) if the Hetzner IP starts getting flagged

### Layer 1 — Detection + telemetry (foundation; week 1)

`bot/captcha.py`:

```python
class CaptchaKind(Enum):
    NONE = "none"
    RECAPTCHA_V2 = "recaptcha_v2"          # image grid
    RECAPTCHA_V3 = "recaptcha_v3"          # invisible score
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    ARKOSE = "arkose"                       # LinkedIn's main one
    LINKEDIN_CHECKPOINT = "li_checkpoint"   # not a captcha — verify flow

async def detect(page) -> CaptchaKind: ...
```

Detection is by iframe URL match + DOM probe:

| Kind | Signal |
|------|--------|
| reCAPTCHA v2 | `iframe[src*="recaptcha/api2"]` |
| reCAPTCHA v3 | `grecaptcha.execute` failure / score < 0.5 |
| hCaptcha | `iframe[src*="hcaptcha.com"]` |
| Turnstile | `iframe[src*="challenges.cloudflare.com"]` |
| Arkose | `iframe[src*="arkoselabs"]` or `iframe[src*="funcaptcha"]` |
| LI checkpoint | URL matches `/checkpoint/challenge` |

Every hit gets logged to `data/captcha_log.jsonl` — kind, URL, timestamp, screenshot path. Ship this alone for a week, look at the log, then decide what (if anything) layer 2/3 needs to be.

**Decision point:** if frequency is <2/week, stop. Manual Telegram fallback is fine; you'll spend more time building than solving.

### Layer 2 — Audio reCAPTCHA v2 solver (homegrown, weekend-sized)

The only "buildable in-house" angle. reCAPTCHA v2 ships an audio accessibility option — plays digits, you type them.

Pipeline:

1. Click the headphone icon on the v2 widget
2. Grab the audio MP3 URL from the panel anchor
3. Download with `aiohttp`
4. Transcribe with `faster-whisper` (small model, ~250MB, runs CPU on the VPS, ~95% on clean digit audio). `openai-whisper` is the fallback; SpeechRecognition+Google Web Speech is the lightweight option but rate-limited.
5. Strip to digits, fill the textbox, click Verify
6. On failure, click "new challenge" up to 3x, then bail to layer 4

Realistic accuracy: **50–70%**. Google deliberately injects noise (overlapping voices, frequency distortion) to break ML. Modern Whisper is good but not great at this.

Dependencies (~270MB additional):
```
faster-whisper==1.0.0
ctranslate2==4.0.0
```

**Skip this layer entirely** if telemetry shows reCAPTCHA v2 is <30% of hits. It's almost all going to be Arkose on LinkedIn.

### Layer 3 — CapSolver (paid, drop-in for the rest)

For everything we can't solve ourselves: reCAPTCHA v3, hCaptcha, Turnstile, Arkose. ~$0.003–$0.02 per solve depending on type.

```python
async def solve_via_capsolver(kind, sitekey, page_url) -> str: ...
```

- API key in `.env` as `CAPSOLVER_API_KEY=` (optional; if blank, fall through to layer 4)
- Token injection: write to `g-recaptcha-response` (or equivalent hidden field) and dispatch the form's verification callback
- Budget guardrail: track spend in `data/captcha_log.jsonl`, alert at $5/month

**Skip this layer** if Layer 1 telemetry shows <5 hits/week — manual Telegram is cheaper than the integration time.

### Layer 4 — Telegram manual fallback (always present)

Last resort, but also the simplest. Already aligns with the bot's existing Y/N pattern.

- Playwright takes a full-page screenshot
- Bot sends to Jack: `"CAPTCHA hit on {site}. Reply with the answer text, or 'skip' to abandon this job."`
- Application state is parked for up to 5 min waiting for reply
- On reply: type into the active CAPTCHA field via Playwright, click verify, resume

For Arkose's "rotate the picture" or "match the animal" puzzles, screenshot + Telegram is fine — Jack just types a coordinate or answer.

## File layout

```
bot/
  captcha.py              # detection, dispatcher, audio solver (layers 1–2)
  captcha_capsolver.py    # paid solver client (layer 3, added later)
  captcha_telegram.py     # manual fallback (layer 4)
data/
  captcha_log.jsonl       # telemetry — every hit, every layer's outcome
```

Hooks: any Playwright navigation in `bot/auto_apply.py`, `bot/scraper.py`, `bot/linkedin_audit.py` runs `await captcha.detect(page)` before reading the result.

## Phasing (calendar)

| Phase | What | When | Decision after |
|-------|------|------|----------------|
| 0 | Stealth + persistent session audit | Day 1 | Are we actually getting hit? |
| 1 | Detection + telemetry + Telegram fallback | Day 2 | What kinds dominate the log? |
| 2 | Audio v2 solver | Weekend, only if v2 ≥30% of hits | Keep or rip out |
| 3 | CapSolver integration | Only if total hits ≥5/week | Watch budget |

## Risks

- **LinkedIn ToS.** Automated CAPTCHA solving violates LinkedIn's terms. Account-level risk regardless of method. Throttle, don't burst — `SEARCH_POLL_INTERVAL` ≥1800 stays.
- **Audio path triggers v3.** Solving the audio challenge fast looks robotic to v3 fingerprinting. Add 4–8s of natural delay between play and answer.
- **Session > solver.** If `linkedin_auth.json` goes stale, every layer degrades. Session refresh is a higher priority than any solver work.
- **Telemetry without action.** If we ship Layer 1 and never look at the log, this whole spec was wasted. Set a calendar reminder for one week post-deploy to review `captcha_log.jsonl`.

## Open questions

- Do we want a `/captcha stats` Telegram command surfacing the log? (cheap to add with Layer 1)
- Worth exposing `--no-captcha-solver` flag to fall straight to manual? (yes, for debugging)
