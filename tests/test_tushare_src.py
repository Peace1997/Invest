from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ashare.sources import tushare_src


class TushareSourceTest(unittest.TestCase):
    def tearDown(self):
        tushare_src.get_pro.cache_clear()

    def test_client_uses_sdk_token_initialization_and_custom_endpoint(self):
        client = SimpleNamespace(_DataApi__http_url=None)
        with (
            patch.object(tushare_src, "_read_token", return_value="secret-token"),
            patch.object(tushare_src.ts, "set_token") as set_token,
            patch.object(tushare_src.ts, "pro_api", return_value=client) as pro_api,
        ):
            result = tushare_src.get_pro()

        self.assertIs(result, client)
        set_token.assert_called_once_with("secret-token")
        pro_api.assert_called_once_with()
        self.assertEqual(client._DataApi__http_url, "https://ts.gyzcloud.top/api")


if __name__ == "__main__":
    unittest.main()
