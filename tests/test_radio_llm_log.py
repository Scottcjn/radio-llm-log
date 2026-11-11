import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from radio_llm_log import cli, llm, rules, store
from radio_llm_log.normalize import normalize

SAMPLES = Path(__file__).resolve().parent.parent / "examples" / "sample-spots.jsonl"


class Rules(unittest.TestCase):
    def test_whisper_hallucinations_are_noise(self):
        for t in [" [BLANK_AUDIO]", "Thank you for watching!", " you", "(static)", "*music*", ""]:
            self.assertTrue(rules.is_noise(t), t)
        self.assertFalse(rules.is_noise("Net control, this is N0CALL checking in"))

    def test_looping(self):
        self.assertTrue(rules.is_looping("the the the the the the the the the the the the"))
        self.assertFalse(rules.is_looping("Hazardous weather outlook for southwest Louisiana and southeast Texas"))

    def test_cw_expansion_keeps_unknown_words(self):
        out = rules.expand_cw("CQ CQ DE N0CALL K")
        self.assertIn("CQ (calling any station)", out)
        self.assertIn("DE (this is) N0CALL K (over)", out)

    def test_flags(self):
        self.assertEqual(rules.flags_for("SOS SOS DE N0CALL"), ["emergency"])
        self.assertEqual(rules.flags_for("issued a Tornado Warning for Greene County"), ["severe_weather"])
        self.assertEqual(rules.flags_for("CQ contest"), [])
        self.assertEqual(rules.flags_for("a tornado watch and mayday"), ["emergency", "severe_weather"])


class Normalize(unittest.TestCase):
    def test_modes(self):
        spots = [json.loads(line) for line in SAMPLES.read_text().splitlines() if line.strip()]
        kinds = [normalize(s, 1.0)["kind"] for s in spots]
        self.assertEqual(kinds.count("voice"), 5)
        self.assertEqual(kinds.count("morse"), 2)
        self.assertEqual(kinds.count("digital"), 2)
        aprs = normalize(spots[-1], 1.0)
        self.assertEqual(aprs["callsign"], "N0CALL-9")
        self.assertIn("monitoring 146.52", aprs["text"])
        self.assertAlmostEqual(normalize(spots[0], 1.0)["ts"], 1762800000.0)

    def test_empty_and_garbage(self):
        self.assertIsNone(normalize({"mode": "SPEECH", "text": ""}, 1.0))
        self.assertIsNone(normalize("not a dict", 1.0))
        self.assertEqual(normalize({"mode": "WEIRD", "text": "x"}, 5.0)["kind"], "other")


class StoreAndReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.conn = store.connect(self.db)
        for line in SAMPLES.read_text().splitlines():
            cli.ingest_one(self.conn, line, received=1762800000.0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def rows(self):
        return {r["id"]: r for r in self.conn.execute("SELECT * FROM entries ORDER BY id")}

    def test_noise_repeats_and_flags(self):
        r = self.rows()
        self.assertEqual(len(r), 10)
        self.assertEqual(r[2]["repeat_of"], 1, "NOAA loop repeat 10 min later")
        self.assertEqual([r[i]["noise"] for i in (3, 4, 5)], [1, 1, 1])
        self.assertEqual(json.loads(r[1]["flags"]), ["severe_weather"])
        self.assertEqual(json.loads(r[7]["flags"]), ["emergency"])
        self.assertIn("(this is)", r[6]["expanded"])
        self.assertEqual(r[9]["repeat_of"], 8, "FT8 CQ 15 s later")

    def test_pending_ai_puts_flags_first_and_skips_noise(self):
        with mock.patch("time.time", return_value=1762800300.0):
            ids = [x["id"] for x in store.pending_ai(self.conn, ["voice", "morse"], limit=10)]
        self.assertEqual(set(ids), {1, 6, 7})
        self.assertIn(ids[0], (1, 7))

    def test_export_markdown(self):
        out = []
        with mock.patch.dict(os.environ, {"RLL_DB": self.db}), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            cli.main(["export", "--date", "2025-11-10"])
        text = "\n".join(out)
        self.assertIn("ALERT (rule): severe_weather", text)
        self.assertNotIn("BLANK_AUDIO", text)
        self.assertEqual(text.count("tornado warning"), 1, "repeat suppressed in export")


class FakeModel(BaseHTTPRequestHandler):
    replies = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeModel.seen = body
        content = FakeModel.replies.pop(0)
        out = json.dumps({"model": "fake-1", "choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class Llm(unittest.TestCase):
    def test_parse_reply_tolerates_fences_and_think(self):
        s, f, c = llm.parse_reply('<think>hmm {"x":1}</think>Sure!\n```json\n{"summary": "A CQ call.", '
                                  '"callsigns": ["n0call"], "flags": ["nothing_notable", "made_up"]}\n```')
        self.assertEqual((s, f, c), ("A CQ call.", ["nothing_notable"], ["N0CALL"]))
        with self.assertRaises(ValueError):
            llm.parse_reply("no json here")

    def test_annotate_against_fake_openai_server(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            FakeModel.replies = ['{"summary": "Station calling any station on 40 m.", "callsigns": ["N0CALL"], "flags": []}']
            entry = {"mode": "CW", "kind": "morse", "freq": 7012300, "callsign": "N0CALL",
                     "text": "CQ CQ DE N0CALL K", "expanded": rules.expand_cw("CQ CQ DE N0CALL K")}
            s, f, c, m = llm.annotate(entry, f"http://127.0.0.1:{srv.server_port}/v1", "any", api_key="k")
            self.assertEqual(m, "fake-1")
            self.assertIn("calling any station", s)
            self.assertIn("CQ CQ DE N0CALL K", FakeModel.seen["messages"][1]["content"])
            self.assertEqual(FakeModel.seen["messages"][0]["role"], "system")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_ai_loop_retries_when_server_down(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "t.db")
        conn = store.connect(db)
        cli.ingest_one(conn, json.dumps({"mode": "CW", "msg": "CQ DE N0CALL", "callsign": "N0CALL",
                                         "freq": 7010000, "timestamp": time.time() * 1000}))
        stop = threading.Event()
        c = dict(cli.cfg(), db=db, llm_url="http://127.0.0.1:9/v1", ai_interval=0.05)
        t = threading.Thread(target=cli.ai_loop, args=(c, stop))
        with mock.patch.object(cli, "log"):
            t.start()
            time.sleep(0.3)
            stop.set()
            t.join(2)
        row = conn.execute("SELECT ai_ts, ai_error FROM entries").fetchone()
        self.assertIsNone(row["ai_ts"], "unreachable model must leave the entry pending, not failed")
        conn.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
