"""Optional AI layer: any OpenAI-compatible /v1/chat/completions endpoint
(llama.cpp server, Ollama, vLLM, LM Studio, a hosted API...).

The model only describes what the decoder already produced. It is told the text
is machine-decoded and may be wrong, and its output is stored apart from the
rule-based flags, so a reader can always tell the two apart.
"""
import json
import re
import urllib.error
import urllib.request

AI_FLAGS = {"emergency", "severe_weather", "net_or_traffic", "unintelligible", "nothing_notable"}

SYSTEM = """You annotate a radio listening log. Each item is text produced by an automatic
decoder (speech-to-text for voice, a Morse decoder for CW). Decoders make mistakes.

Return ONLY a JSON object, no other text:
{"summary": "<one plain-English sentence saying what was heard>",
 "callsigns": ["<amateur callsigns that clearly appear in the text>"],
 "flags": [<zero or more of: "emergency", "severe_weather", "net_or_traffic", "unintelligible", "nothing_notable">]}

Rules:
- Describe only what is in the text. Never add facts, places, names or numbers that are not there.
- If the text is garbled or makes no sense, say so and use the "unintelligible" flag.
- For Morse, expand ham shorthand into plain words (CQ = calling any station, 73 = best regards, QTH = location).
- "emergency" only for a real distress or emergency call; "severe_weather" only for an actual warning or watch."""


def _endpoint(base):
    base = base.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _item(e):
    lines = [f"mode: {e['mode']} ({e['kind']})", f"frequency: {e['freq'] / 1e6:.4f} MHz"]
    if e["callsign"]:
        lines.append(f"decoder-reported callsign: {e['callsign']}")
    lines.append(f"decoded text: {e['text']}")
    if e["expanded"]:
        lines.append(f"shorthand expanded by rule: {e['expanded']}")
    return "\n".join(lines)


def parse_reply(content):
    """Pull the first JSON object out of a model reply; tolerate code fences and chatter."""
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    start = content.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    summary = str(obj.get("summary") or "").strip()[:500]
                    flags = [f for f in obj.get("flags") or [] if f in AI_FLAGS]
                    calls = [str(c).upper() for c in obj.get("callsigns") or [] if str(c).strip()][:10]
                    return summary, flags, calls
        start = content.find("{", start + 1)
    raise ValueError("no JSON object in model reply")


def annotate(entry, url, model, api_key=None, timeout=120):
    """-> (summary, flags, callsigns, model_that_answered)."""
    body = {"model": model, "temperature": 0.2, "max_tokens": 300,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _item(entry)}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(_endpoint(url), data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.loads(r.read())
    content = reply["choices"][0]["message"]["content"]
    summary, flags, calls = parse_reply(content)
    return summary, flags, calls, reply.get("model") or model
