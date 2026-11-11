# radio-llm-log

A listening log for an [OpenWebRX+](https://github.com/luarvique/openwebrx) receiver, with an optional AI layer.

OpenWebRX+ already decodes a lot: speech (through a [whisper.cpp](https://github.com/ggml-org/whisper.cpp) server), Morse/CW (its CW skimmer), FT8 and other WSJT modes, APRS. With MQTT reporting turned on, it announces each decode as a small JSON message. **radio-llm-log** subscribes to those messages and:

1. **Keeps everything** in one SQLite log: time, frequency, mode, callsign, and the exact decoded text.
2. **Applies fixed rules (no AI):**
   - drops Whisper's well-known hallucinations on silence ("Thank you for watching", `[BLANK_AUDIO]`, one word looping);
   - expands Morse shorthand (`CQ DE N0CALL K` becomes "CQ (calling any station) DE (this is) N0CALL K (over)");
   - flags emergency wording (SOS, MAYDAY, PAN-PAN) and severe-weather wording (tornado warning, flash flood warning, evacuate...);
   - marks repeats (NOAA Weather Radio loops its broadcast; FT8 stations call CQ every 15 seconds).
3. **Optionally asks a language model** (any OpenAI-compatible endpoint: llama.cpp server, Ollama, vLLM, LM Studio, or a hosted API) for a one-sentence plain-English summary of each voice and Morse entry.

### Rules and AI are kept apart on purpose
- Alerts come from the rules, so they still work if the model is down, slow or wrong.
- AI summaries are stored in separate columns, always shown as "AI summary (model, may be wrong)", and the model is told to describe only what is in the decoded text.
- The original decoded text is never changed.

```
2025-11-10 18:42:30 UTC      7.0150 MHz  CW      N0CALL
    heard (machine-decoded): SOS SOS DE N0CALL QTH OHIO
    shorthand (rule): SOS (DISTRESS) SOS (DISTRESS) DE (this is) N0CALL QTH (location) OHIO
    ALERT (rule): emergency
    AI summary (qwen3-8b, may be wrong): A distress call from N0CALL indicating their location is Ohio
```
(The entry above is real output: the bundled sample message run through Qwen3-8B on a local llama.cpp server. The name in brackets is whatever model name your server reports.)

## Requirements
- Python 3.9+, standard library only, plus `paho-mqtt` for the `run` command (Debian/Ubuntu: `apt install python3-paho-mqtt`).
- An MQTT broker the receiver can publish to (Debian/Ubuntu: `apt install mosquitto`; version 2 listens on localhost only by default).
- OpenWebRX+ 1.2.x. For speech you also need a whisper.cpp `whisper-server` (see below).

## Set up OpenWebRX+
In the OpenWebRX+ settings (or `settings.json`):

| Setting | Value |
|---|---|
| `mqtt_enabled` | `true` |
| `mqtt_host` | `localhost` |
| `speech_url` | `http://127.0.0.1:8178/inference` (your whisper-server) |
| `services_enabled` | `true` (decode in the background when nobody is using the receiver) |
| `services_decoders` | e.g. `["ft8", "packet", "cwskimmer", "speech"]` |

Background decoders listen on the band plan's "dial frequencies". The stock band plans already include CW skimmer and FT8 dials on HF, and APRS on 2 m. To transcribe NOAA Weather Radio (US), add a band to your region's band plan file. See [`examples/noaa-band.json`](examples/noaa-band.json).

A single SDR can only cover one band at a time. Use the OpenWebRX+ scheduler to switch profiles by time of day, or add a second SDR.

### whisper-server
```
git clone --branch v1.9.5 https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build && cmake --build build -j --target whisper-server
./models/download-ggml-model.sh small.en
./build/bin/whisper-server -m models/ggml-small.en.bin --host 127.0.0.1 --port 8178 -t 4
```
`small.en` runs comfortably on a modern CPU. Use `base.en` on weaker machines.

## Run
```
export RLL_DB=/var/lib/radio-llm-log/radio-log.db
export RLL_LLM_URL=http://127.0.0.1:8080/v1     # optional: leave unset for rules only
export RLL_LLM_MODEL=qwen3-8b
python3 -m radio_llm_log run
```
A systemd unit is in [`examples/radio-llm-log.service`](examples/radio-llm-log.service).

Look at the log:
```
python3 -m radio_llm_log tail -n 30 [--local] [--all]     # --all also shows noise and repeats
python3 -m radio_llm_log export --date 2026-01-15         # Markdown for one UTC day
python3 -m radio_llm_log export --format jsonl            # for other tools
python3 -m radio_llm_log replay examples/sample-spots.jsonl   # try it without a radio
```

| Variable | Default | Meaning |
|---|---|---|
| `RLL_DB` | `radio-log.db` | SQLite file |
| `RLL_MQTT_HOST` / `RLL_MQTT_PORT` | `localhost` / `1883` | broker |
| `RLL_MQTT_TOPIC` | `openwebrx` | OpenWebRX+'s `mqtt_topic` |
| `RLL_LLM_URL` | unset (AI off) | OpenAI-compatible base URL, e.g. `http://host:8080/v1` |
| `RLL_LLM_MODEL` | `default` | model name sent to that endpoint |
| `RLL_LLM_KEY` | unset | bearer token, if the endpoint needs one |
| `RLL_AI_KINDS` | `voice,morse` | which entries get AI summaries |
| `RLL_AI_INTERVAL` | `20` | seconds between AI passes |

## Database
One table, `entries`. Rule columns: `noise`, `repeat_of`, `flags`, `expanded`. AI columns: `ai_summary`, `ai_flags`, `ai_model`, `ai_ts`, `ai_error`. `raw` keeps the original JSON message. Other programs can read the file directly; it uses WAL mode.

## Status
Version 0.1.0.
- **Tested:** unit tests with recorded-format sample messages, plus summaries from a real local model (Qwen3-8B via llama.cpp).
- **Not yet tested:** a long unattended run on live off-air signals.

Message field names follow OpenWebRX+ 1.2.130.

## Legal note
Receiving and logging amateur, NOAA and other unencrypted public broadcasts is legal in the US and most countries. Do not use this to intercept or publish private communications such as cellular calls or encrypted services. Local laws vary. This tool only listens; it cannot transmit.

## License
MIT. OpenWebRX+, whisper.cpp and Whisper models have their own licenses. Keep a copy with any model you download.
