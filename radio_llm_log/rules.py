"""Deterministic clean-up and flagging. No AI here, on purpose:
safety flags must not depend on a model being up, or being right.

- drop/flag Whisper's well-known hallucinations on silence and noise
- expand Morse (CW) shorthand: Q-codes, prosigns, common abbreviations
- flag emergency and severe-weather wording
- spot repeats (NOAA loops its broadcast; FT8 stations call CQ every 15 s)
"""
import re
from difflib import SequenceMatcher

# Whisper produces these on silence, static or music. Compared after normalising.
HALLUCINATIONS = {
    "you", "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "thank you so much for watching", "please subscribe",
    "subscribe", "bye", "okay", "oh", "so", "the end", "subtitles by the amara.org community",
    "transcription by castingwords", "i'm sorry", "music", "applause",
}
_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")   # [BLANK_AUDIO] (static) *music*


def normalise(text):
    return re.sub(r"[^a-z0-9.' ]+", " ", (text or "").lower()).strip(" .")


def is_noise(text):
    """True when a transcript has no real speech in it."""
    stripped = _BRACKETED.sub(" ", text or "")
    n = re.sub(r"\s+", " ", normalise(stripped)).strip()
    return not n or n in HALLUCINATIONS or len(n) < 2


def is_looping(text, min_repeats=5):
    """Whisper sometimes repeats one phrase over and over on noise."""
    words = normalise(text).split()
    if len(words) < min_repeats * 2:
        return False
    for size in (1, 2, 3):
        grams = [" ".join(words[i:i + size]) for i in range(0, len(words) - size + 1, size)]
        if grams:
            top = max(set(grams), key=grams.count)
            if grams.count(top) >= min_repeats and grams.count(top) * size >= 0.6 * len(words):
                return True
    return False


# --- Morse / CW shorthand ---------------------------------------------------
CW_TERMS = {
    "CQ": "calling any station", "DE": "this is", "K": "over", "KN": "over, named station only",
    "AR": "end of message", "SK": "end of contact", "BK": "break", "R": "received",
    "QTH": "location", "QSL": "confirmed", "QRZ": "who is calling?", "QRM": "interference",
    "QRN": "static", "QSB": "fading", "QRP": "low power", "QRO": "high power", "QRT": "stopping",
    "QSY": "changing frequency", "QRL": "is this frequency in use?", "QRS": "send slower",
    "QRQ": "send faster", "QSO": "contact", "QRV": "ready", "QRX": "wait",
    "RST": "signal report", "5NN": "signal report 599", "599": "signal report: perfect",
    "UR": "your", "FB": "fine", "OM": "old man (friend)", "YL": "young lady (operator)",
    "XYL": "wife", "73": "best regards", "88": "love and kisses", "TNX": "thanks", "TU": "thank you",
    "PSE": "please", "HR": "here", "WX": "weather", "ES": "and", "GM": "good morning",
    "GA": "good afternoon", "GE": "good evening", "ANT": "antenna", "RIG": "radio", "PWR": "power",
    "AGN": "again", "HW": "how (do you copy)?", "CPY": "copy", "SRI": "sorry", "BTU": "back to you",
    "NR": "number", "CUL": "see you later", "GL": "good luck", "OP": "operator", "TEST": "contest call",
    "POTA": "Parks on the Air", "SOTA": "Summits on the Air", "SOS": "DISTRESS",
}


def expand_cw(text):
    """'CQ CQ DE N0CALL K' -> 'CQ (calling any station) CQ ... DE (this is) N0CALL K (over)'.
    Unknown words (callsigns, names) are left as they are."""
    out = []
    for word in (text or "").split():
        meaning = CW_TERMS.get(word.upper().strip(".,?"))
        out.append(f"{word} ({meaning})" if meaning else word)
    return " ".join(out)


# --- Flags -------------------------------------------------------------------
FLAG_PATTERNS = {
    "emergency": re.compile(
        r"\b(sos|mayday|pan[- ]?pan|emergency traffic|priority traffic|distress)\b", re.I),
    "severe_weather": re.compile(
        r"\b(tornado (warning|watch|emergency)|severe thunderstorm warning|flash flood (warning|emergency)|"
        r"hurricane (warning|watch)|tropical storm (warning|watch)|storm surge (warning|watch)|"
        r"extreme wind warning|tsunami|civil emergency|evacuat\w*|shelter in place|take cover)\b", re.I),
}


def flags_for(text):
    return sorted(name for name, rx in FLAG_PATTERNS.items() if rx.search(text or ""))


def similar(a, b):
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()
