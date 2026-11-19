import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from io import BytesIO

import youtube_replay


class ReplayHelpersTests(unittest.TestCase):
    def test_extract_json_assignment_reads_object(self):
        html = 'prefix var ytInitialData = {"contents":{"ok":true}}; suffix'
        self.assertEqual(
            youtube_replay._extract_json_assignment(html, "ytInitialData"),
            {"contents": {"ok": True}},
        )

    def test_extract_json_assignment_rejects_missing_value(self):
        with self.assertRaisesRegex(youtube_replay.ReplayError, "không cung cấp dữ liệu"):
            youtube_replay._extract_json_assignment("var other = {};", "ytInitialData")

    def test_extract_json_assignment_supports_window_bracket_form(self):
        html = 'window["ytInitialData"] = {"video": 1};'
        self.assertEqual(
            youtube_replay._extract_json_assignment(html, "ytInitialData"),
            {"video": 1},
        )

    def test_walk_visits_nested_dicts(self):
        value = {"a": [{"b": 1}, {"c": {"d": 2}}]}
        nodes = list(youtube_replay._walk(value))
        self.assertEqual(len(nodes), 4)
        self.assertTrue(any(node.get("d") == 2 for node in nodes))

    def test_download_replay_rejects_invalid_video_id_before_network(self):
        with patch.object(youtube_replay, "_request") as request:
            with self.assertRaisesRegex(youtube_replay.ReplayError, "Video ID không hợp lệ"):
                youtube_replay.download_replay("not-a-video-id")
            request.assert_not_called()

    def test_request_reports_http_429_without_retrying(self):
        error = HTTPError("https://youtube.test", 429, "rate limited", {}, BytesIO(b""))
        with patch.object(youtube_replay, "urlopen", side_effect=error) as opener:
            with self.assertRaisesRegex(youtube_replay.ReplayError, "HTTP 429"):
                youtube_replay._request("https://youtube.test")
            opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
