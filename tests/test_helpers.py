import unittest

import agent


class HelperBehaviorTests(unittest.TestCase):
    def test_normalize_host_adds_http_scheme(self):
        self.assertEqual(agent.normalize_host("localhost"), "http://localhost")
        self.assertEqual(agent.normalize_host("http://example.com"), "http://example.com")

    def test_is_qbt_auth_error_matches_expected_exceptions(self):
        self.assertTrue(agent.is_qbt_auth_error(agent.qbittorrentapi.Forbidden403Error("forbidden")))
        self.assertTrue(agent.is_qbt_auth_error(agent.qbittorrentapi.Unauthorized401Error("unauthorized")))
        self.assertTrue(agent.is_qbt_auth_error(agent.qbittorrentapi.LoginFailed("login")))
        self.assertFalse(agent.is_qbt_auth_error(ValueError("boom")))


if __name__ == "__main__":
    unittest.main()
