import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from collectors.baidu import BaiduCollector
from collectors.appstore import AppStoreCollector
from collectors import CollectResult, SearchCandidate
from collectors.device_markets import OppoCollector
from collectors.google_play import GooglePlayCollector
from collectors.harmony import HarmonyCollector
from collectors.huawei import HuaweiCollector
from collectors.qihu360 import Qihu360Collector
from collectors.yyb import YybCollector
from collectors.samsung import SamsungCollector
from collectors.meizu import MeizuCollector
from core.discovery import (_app_search_terms, _company_search_terms, _relevance,
                            search_apps)
from core.checker import normalize_published_at
from core.download_policy import decide_download
from core.env_check import check_usb_phone, find_adb, parse_adb_devices
from core import apk_verify
from core.artifacts import (_device_extract_artifact, _download_with_resume,
                            _find_cached_artifact, _fresh_collect)
from executors.markets.yyb import _ocr_version, _pinyin_query
from executors.markets.generic import (GenericStoreDriver, OppoDeviceDriver,
                                       find_company_name, parse_entities,
                                       parse_published_at, parse_version)
from executors.device import DeviceExecutor


def next_page(records):
    payload = {"props": {"pageProps": {"dynamicCardResponse": {"data": records}}}}
    return '<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">' + \
        json.dumps(payload, ensure_ascii=False) + '</script>'


class FakeResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeStatusResponse(FakeResponse):
    def __init__(self, text, status_code=200):
        super().__init__(text)
        self.status_code = status_code


class FakeJsonResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class SamsungCollectorTests(unittest.TestCase):
    @patch("collectors.samsung.httpx.get")
    def test_collect_matches_package_and_version(self, get):
        get.return_value = FakeJsonResponse({"DetailMain": {
            "appId": "com.example.finance", "contentName": "示例金融",
            "sellerName": "示例金融", "contentBinaryVersion": "8.2.40",
            "modifyDate": "2026.08.05.",
        }})
        result = SamsungCollector().collect("com.example.finance")
        self.assertEqual("ok", result.status)
        self.assertEqual("8.2.40", result.version_name)

    @patch("collectors.samsung.httpx.get")
    def test_collect_rejects_mismatched_package(self, get):
        get.return_value = FakeJsonResponse({"DetailMain": {
            "appId": "com.example.fake", "contentBinaryVersion": "8.2.40",
        }})
        result = SamsungCollector().collect("com.example.finance")
        self.assertEqual("package_mismatch", result.status)
        self.assertEqual("com.example.fake", result.extra["observed_package"])

    @patch("collectors.samsung.httpx.get")
    def test_error_payload_is_offline_not_mismatch(self, get):
        get.return_value = FakeJsonResponse({
            "errCode": "9901",
            "errMsg": "this content is suspended or terminated",
        })
        result = SamsungCollector().collect("com.example.app")
        self.assertEqual("offline", result.status)
        self.assertIn("未收录或已下架", result.detail)


class HuaweiCollectorTests(unittest.TestCase):
    @patch.object(HuaweiCollector, "_search_items")
    def test_collect_uses_search_and_exact_package(self, search):
        search.return_value = [{"name": "示例金融", "package": "com.example.finance",
                                "version": "8.2.40", "versionCode": 1381,
                                "developer": "示例科技", "releaseDate": "2026-08-06"}]
        result = HuaweiCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("ok", result.status)
        self.assertEqual("1381", result.version_code)

    def test_expired_interface_code_refreshes_and_retries_once(self):
        collector = HuaweiCollector()
        expired_client = MagicMock()
        expired_client.get.return_value = FakeStatusResponse("forbidden", 403)
        fresh_client = MagicMock()
        fresh_client.get.return_value = FakeStatusResponse("{}", 200)
        collector._client = expired_client
        collector._code = "expired"

        def refresh():
            collector._client = fresh_client
            return "fresh"

        with patch.object(collector, "_get_code", side_effect=refresh):
            response = collector._request({"method": "test"})
        self.assertEqual(200, response.status_code)
        expired_client.close.assert_called_once()
        self.assertEqual("fresh", collector._code)
        self.assertEqual(1, fresh_client.get.call_count)


class BaiduCollectorTests(unittest.TestCase):
    def test_initial_state_parser_reads_visible_page_record(self):
        from collectors.baidu import _initial_state_payload
        payload = {"data": {"appDocNew": {"cardList": [
            {"cardType": "detailBusinessCardApp", "versionNum": "8.2.0"},
            {"cardType": "detailBusinessDownloadUad", "appDoc": {
                "sname": "示例金融", "package": "com.example.finance",
                "docid": 2002, "packageid": 3003,
                "versioncode": 1341,
            }},
        ]}}}
        state = {"appDoc": {"resList": {"d_1001": payload}}}
        html = "<script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script>"
        self.assertEqual(payload, _initial_state_payload(html, "1001"))

    @patch("collectors.baidu.httpx.get")
    def test_detail_prefers_visible_page_and_keeps_entry_docid(self, get):
        visible_payload = {"data": {"appDocNew": {"cardList": [
            {"cardType": "detailBusinessCardApp", "versionNum": "8.2.0",
             "appUpdateDate": "2026年6月6日"},
            {"cardType": "detailBusinessDownloadUad", "appDoc": {
                "sname": "示例金融", "package": "com.example.finance",
                "docid": 2002, "packageid": 3003,
                "versioncode": 1341,
            }},
        ]}}}
        state = {"appDoc": {"resList": {"d_1001": visible_payload}}}
        get.return_value = FakeResponse(
            "<script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script>"
        )
        item = BaiduCollector()._detail(
            {"docid": 1001, "packageid": 3003}, 15
        )
        self.assertEqual("8.2.0", item["version_name"])
        self.assertEqual("visible_page", item["data_source"])
        self.assertEqual("1001", item["market_app_id"])
        self.assertEqual("2002", item["download_docid"])
        self.assertEqual(1, get.call_count)

    @patch("collectors.baidu.httpx.get")
    def test_detail_uses_api_only_when_visible_page_cannot_be_parsed(self, get):
        api_payload = {"data": {"appDocNew": {"cardList": [
            {"cardType": "detailBusinessCardApp", "versionNum": "8.1.0"},
            {"cardType": "detailBusinessDownloadUad", "appDoc": {
                "sname": "示例金融", "package": "com.example.finance",
                "docid": 99, "packageid": 2, "versioncode": 10,
            }},
        ]}}}
        get.side_effect = [FakeResponse("<html>页面结构已变化</html>"),
                           FakeJsonResponse(api_payload)]
        item = BaiduCollector()._detail({"docid": 1, "packageid": 2}, 15)
        self.assertEqual("8.1.0", item["version_name"])
        self.assertEqual("api_fallback", item["data_source"])
        self.assertIn("详情网页解析失败", item["data_warning"])
        self.assertEqual("1", item["market_app_id"])

    @patch.object(BaiduCollector, "_page_detail")
    def test_detail_selects_highest_visible_version_across_nodes(self, page_detail):
        base = {"app_name": "示例金融", "package_name": "com.example.finance",
                "package_id": "3003", "developer": "示例",
                "updated_at": "", "icon_url": "", "download_url": "",
                "declared_size": 0, "data_source": "visible_page"}
        page_detail.side_effect = [
            {**base, "market_app_id": "old", "version_name": "8.0.90",
             "version_code": "1231"},
            {**base, "market_app_id": "new", "version_name": "8.2.0",
             "version_code": "1341"},
            {**base, "market_app_id": "old", "version_name": "8.0.90",
             "version_code": "1231"},
        ]
        item = BaiduCollector()._detail(
            {"docid": 1001, "packageid": 3003}, 15, attempts=3
        )
        self.assertEqual("8.2.0", item["version_name"])
        self.assertEqual("1341", item["version_code"])
        self.assertEqual("new", item["download_docid"])
        self.assertEqual(["8.2.0", "8.0.90"], item["version_candidates"])
        self.assertIn("节点返回版本不一致", item["data_warning"])

    def test_detail_fields_accept_business_card_variant(self):
        from collectors.baidu import _detail_fields
        payload = {"data": {"appDocNew": {"cardList": [
            {"cardType": "detailBusinessHeader"},
            {"cardType": "detailBusinessCardApp", "versionNum": "8.2.0"},
            {"cardType": "detailBusinessCardDev", "devname": "示例科技"},
            {"cardType": "detailBusinessDownloadUad", "appDoc": {
                "sname": "示例金融", "package": "com.example.finance",
                "docid": 5015042382, "packageid": 1222923,
                "version": "8.2.0", "versioncode": 1341,
                "downloadUrl": "https://gdown.baidu.com/example.apk",
            }},
        ]}}}
        fields = _detail_fields(payload)
        self.assertEqual("com.example.finance", fields["package_name"])
        self.assertEqual("8.2.0", fields["version_name"])
        self.assertEqual("https://gdown.baidu.com/example.apk", fields["download_url"])

    def test_detail_fields_fall_back_to_app_doc_body(self):
        from collectors.baidu import _detail_fields
        payload = {"data": {"appDocNew": {
            "cardList": [
                {"cardType": "detailBusinessCardApp", "versionNum": "8.2.0"},
                {"cardType": "detailBusinessDownloadUad"},
            ],
            "appDoc": {
                "sname": "示例金融", "package": "com.example.finance",
                "docid": 5015042382, "packageid": 1222923,
                "versionname": "8.2.0", "versioncode": 1341,
                "downloadUrl": "https://gdown.baidu.com/example.apk",
            },
        }}}
        fields = _detail_fields(payload)
        self.assertEqual("com.example.finance", fields["package_name"])
        self.assertEqual("8.2.0", fields["version_name"])
        self.assertEqual("https://gdown.baidu.com/example.apk", fields["download_url"])

    @patch.object(BaiduCollector, "_resolved")
    def test_collect_matches_resolved_package(self, resolved):
        resolved.return_value = [{"app_name": "示例金融", "package_name": "com.example.finance",
                                  "market_app_id": "1", "package_id": "2",
                                  "version_name": "8.1.90", "version_code": "1332",
                                  "developer": "示例", "updated_at": "2026年4月21日",
                                  "icon_url": ""}]
        result = BaiduCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("ok", result.status)
        self.assertEqual("8.1.90", result.version_name)
        resolved.assert_called_once_with("示例金融", 15)

    @patch.object(BaiduCollector, "_resolved", return_value=[])
    def test_candidate_search_uses_single_detail_sample(self, resolved):
        self.assertEqual([], BaiduCollector().search("示例金融"))
        resolved.assert_called_once_with("示例金融", 15, exact_attempts=1)


class AppStoreCollectorTests(unittest.TestCase):
    @patch("collectors.appstore.httpx.get")
    def test_collect_matches_ios_name_and_keeps_two_company_fields(self, get):
        get.return_value = FakeJsonResponse({"results": [{
            "trackName": "示例金融-黄金基金理财借贷保险一站式平台",
            "version": "8.2.40", "trackId": 895682747,
            "bundleId": "com.example.finance.ios",
            "artistName": "Example Financial Technology Holdings Co., Ltd.",
            "sellerName": "Example Financial Technology Holdings Co., Ltd",
            "currentVersionReleaseDate": "2026-08-10T08:02:16Z",
        }]})
        result = AppStoreCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("ok", result.status)
        self.assertEqual("8.2.40", result.version_name)
        self.assertTrue(result.extra["developer"])
        self.assertTrue(result.extra["operator"])

    @patch("collectors.appstore.httpx.get")
    def test_search_returns_ios_candidate_without_android_package(self, get):
        get.return_value = FakeJsonResponse({"results": [{
            "trackId": 12345, "trackName": "示例云设备",
            "bundleId": "com.example.cloud.ios", "artistName": "Example Cloud",
            "sellerName": "Example Cloud Co., Ltd.", "version": "4.13.3",
        }]})
        hits = AppStoreCollector().search("示例云")
        self.assertEqual(1, len(hits))
        self.assertEqual("ios", hits[0].platform)
        self.assertEqual("", hits[0].package_name)
        self.assertEqual("com.example.cloud.ios", hits[0].bundle_id)
        self.assertEqual("12345", hits[0].market_app_id)


class Qihu360CollectorTests(unittest.TestCase):
    @patch("collectors.qihu360.httpx.get")
    def test_published_at_reads_mobile_detail_page(self, get):
        from collectors.qihu360 import Qihu360Collector
        get.return_value = FakeResponse(
            "<html><div>更新时间：2026-08-06 14:32:12</div></html>"
        )
        collector = Qihu360Collector()
        self.assertEqual("2026-08-06", collector._published_at("com.example.finance", 1, 15))

    @patch("collectors.qihu360.httpx.get")
    def test_published_at_missing_stays_empty(self, get):
        from collectors.qihu360 import Qihu360Collector
        get.return_value = FakeResponse("<html><div>无更新时间</div></html>")
        collector = Qihu360Collector()
        self.assertEqual("", collector._published_at("com.example.finance", 1, 15))

    @patch.object(Qihu360Collector, "_published_at", return_value="2026-08-06")
    @patch.object(Qihu360Collector, "_rows")
    def test_collect_matches_exact_android_package(self, rows, published_at):
        rows.return_value = [{"apkid": "com.example.finance", "id": "1898619",
                              "name": "示例金融", "version_name": "8.2.40",
                              "version_code": "1381",
                              "soft_corp_name": "北京示例电子商务有限公司"}]
        result = Qihu360Collector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("ok", result.status)
        self.assertEqual("1381", result.version_code)
        self.assertEqual("2026-08-06", result.extra["published_at"])


class GooglePlayCollectorTests(unittest.TestCase):
    @patch("collectors.google_play.httpx.get")
    def test_404_is_normal_not_published_state(self, get):
        get.return_value = FakeStatusResponse("", 404)
        result = GooglePlayCollector().collect("com.example.finance")
        self.assertEqual("not_published", result.status)

    @patch("collectors.google_play.httpx.get")
    def test_collect_parses_public_detail_page(self, get):
        html = '''<h1><span itemprop="name">示例</span></h1>
        <a href="/store/apps/developer?id=example"><span>示例</span></a>
        <div class="Bne0R ">开发者信息</div>
        <div class="HhKIQc"><div>北京示例电子商务有限公司</div></div>
        <script>AF_initDataCallback({data:[[["x"]],[[["15.9.50"]],[[[35]]]]]});</script>'''
        get.return_value = FakeStatusResponse(html)
        result = GooglePlayCollector().collect("com.example.mall")
        self.assertEqual("ok", result.status)
        self.assertEqual("15.9.50", result.version_name)
        self.assertIn("北京示例", result.extra["operator"])


class HarmonyCollectorTests(unittest.TestCase):
    @patch.object(HuaweiCollector, "_search_items")
    def test_android_result_is_not_misreported_as_harmony(self, search):
        search.return_value = [{"name": "示例金融", "package": "com.example.finance",
                                "version": "8.2.40", "ctype": 0}]
        result = HarmonyCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("web_limited", result.status)
        self.assertEqual("", result.version_name)


class MeizuCollectorTests(unittest.TestCase):
    payload = {"value": {"list": [{
        "id": 1896143, "name": "&#x793a;&#x4f8b;&#x91d1;&#x878d;",
        "package_name": "com.example.finance", "publisher": "&#x793a;&#x4f8b;&#x79d1;&#x6280;",
        "version_name": "8.2.40", "version_code": 1381, "sale_time": "2026-08-06",
    }]}}

    @patch("collectors.meizu.httpx.get")
    def test_collect_exact_package_and_unescapes_text(self, get):
        get.return_value = FakeJsonResponse(self.payload)
        result = MeizuCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("ok", result.status)
        self.assertEqual("8.2.40", result.version_name)
        self.assertIn("示例科技", result.detail)

    @patch("collectors.meizu.httpx.get")
    def test_search_returns_candidate(self, get):
        get.return_value = FakeJsonResponse(self.payload)
        hits = MeizuCollector().search("示例科技")
        self.assertEqual("com.example.finance", hits[0].package_name)
        self.assertEqual("示例金融", hits[0].app_name)


class YybCollectorTests(unittest.TestCase):
    def setUp(self):
        self.market_record = {
            "pkg_name": "com.tencent.android.qqdownloader", "app_id": "5848",
            "name": "应用宝", "version_name": "9.2.5", "developer": "腾讯",
        }
        self.target_record = {
            "pkg_name": "com.example.finance", "app_id": "10914638", "name": "示例金融",
            "version_name": "8.2.40", "developer": "示例科技控股股份有限公司",
            "operator": "北京示例贸易有限公司",
            "update_time": "1785932520",
        }

    @patch("collectors.yyb.httpx.get")
    def test_404_is_offline_not_error(self, get):
        get.return_value = FakeStatusResponse("", 404)
        result = YybCollector().collect("com.example.notfound")
        self.assertEqual("offline", result.status)
        self.assertIn("未收录", result.detail)

    @patch("collectors.yyb.httpx.get")
    def test_search_404_returns_empty(self, get):
        get.return_value = FakeStatusResponse("", 404)
        hits = YybCollector().search("不存在的应用")
        self.assertEqual([], hits)

    @patch("collectors.yyb.httpx.get")
    def test_collect_matches_target_package_not_market_client(self, get):
        get.return_value = FakeResponse(next_page([self.market_record, self.target_record]))
        result = YybCollector().collect("com.example.finance")
        self.assertEqual("ok", result.status)
        self.assertEqual("8.2.40", result.version_name)
        self.assertNotEqual("9.2.5", result.version_name)
        self.assertEqual("北京示例贸易有限公司", result.extra["operator"])

    @patch("collectors.yyb.httpx.get")
    def test_search_returns_confirmable_candidate(self, get):
        get.return_value = FakeResponse(next_page([self.target_record]))
        hits = YybCollector().search("示例科技")
        self.assertEqual(1, len(hits))
        self.assertEqual("com.example.finance", hits[0].package_name)
        self.assertEqual("示例科技控股股份有限公司", hits[0].developer)

    @patch("collectors.yyb.httpx.get")
    def test_search_excludes_pc_client_container_record(self, get):
        pc_record = {
            "pkg_name": "com.tencent.pcgame.examplefinance",
            "app_id": "200510611", "name": "示例金融",
            "version_name": "1.0.24", "developer": "examplecorp", "operator": "examplecorp",
        }
        get.return_value = FakeResponse(next_page([pc_record, self.target_record]))
        hits = YybCollector().search("示例金融")
        self.assertEqual(["com.example.finance"], [x.package_name for x in hits])


class DownloadPolicyTests(unittest.TestCase):
    def test_new_version_is_recommended(self):
        action, reason = decide_download("ok", "2.0.0", "1.9.0")
        self.assertEqual("recommended", action)
        self.assertIn("1.9.0", reason)

    def test_third_party_is_required(self):
        action, _ = decide_download("ok", "2.0.0", "2.0.0", authority="third_party")
        self.assertEqual("required", action)

    def test_stable_official_version_skips_download(self):
        action, _ = decide_download("ok", "2.0.0", "2.0.0", authority="official")
        self.assertEqual("not_needed", action)


class PublishedTimeTests(unittest.TestCase):
    def test_normalizes_epoch_iso_and_chinese_dates(self):
        self.assertEqual("2026-08-05", normalize_published_at("1785932520"))
        self.assertEqual("2026-08-10", normalize_published_at("2026-08-10T08:02:16Z"))
        self.assertEqual("2026-04-21", normalize_published_at("2026年4月21日"))

    def test_invalid_or_missing_dates_stay_empty(self):
        self.assertEqual("", normalize_published_at("市场未提供"))
        self.assertEqual("", normalize_published_at(""))

    def test_relative_time_converts_to_approximate_date(self):
        import calendar
        from datetime import datetime
        today = datetime.now()
        month = today.month - 1
        year = today.year
        if month <= 0:
            month += 12
            year -= 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        expected = today.replace(year=year, month=month, day=day).strftime("%Y-%m-%d")
        self.assertEqual(expected, normalize_published_at("1个月前"))
        self.assertEqual(today.strftime("%Y-%m-%d"), normalize_published_at("3小时前"))


class DevicePublishDateParsingTests(unittest.TestCase):
    def test_parses_labelled_absolute_date(self):
        self.assertEqual(
            "2026-07-13",
            parse_published_at(["版本：7.20.70", "更新时间：2026/07/13", "开发者：示例"]),
        )
        self.assertEqual(
            "2026-07-13",
            parse_published_at(["最近更新", "2026-07-13", "版本 7.20.70"]),
        )

    def test_parses_relative_date_text(self):
        self.assertEqual("1个月前", parse_published_at(["版本 7.20.70", "1 个月前", "示例科技"]))
        self.assertEqual("3天前", parse_published_at(["3天前"]))

    def test_missing_date_stays_empty(self):
        self.assertEqual("", parse_published_at(["示例应用", "217 MB", "打开"]))


class CompanyNameParsingTests(unittest.TestCase):
    def test_prefers_full_suffix_company_name(self):
        texts = ["版本 7.20.70  |  1 个月前", "示例金融信息服务有限公司",
                 "隐私", "权限", "举报"]
        self.assertEqual("示例金融信息服务有限公司", find_company_name(texts))

    def test_skips_record_and_operator_rows(self):
        texts = [
            "京ICP备00000000号-1A（主办单位：示例电子商务有限公司）",
            "示例科技控股股份有限公司",
        ]
        self.assertEqual("示例科技控股股份有限公司", find_company_name(texts))

    def test_falls_back_to_truncated_suffix(self):
        self.assertEqual(
            "示例科技有限公司…",
            find_company_name(["示例科技有限公司…", "隐私", "权限"]),
        )

    def test_empty_when_no_company(self):
        self.assertEqual("", find_company_name(["示例应用", "217 MB", "打开"]))


class DiscoveryRankingTests(unittest.TestCase):
    def test_app_search_expands_ascii_case_for_case_sensitive_markets(self):
        self.assertEqual(["示例ai", "示例AI"], _app_search_terms("示例ai"))

    def test_app_search_uses_case_variant_to_recall_exact_app(self):
        collector = MagicMock(
            supports_search=True, supports_package_lookup=False,
            display_name="测试市场",
        )
        collector.search.side_effect = [
            [],
            [SearchCandidate(app_name="示例AI", package_name="com.example.office",
                             developer="示例电子商务有限公司")],
        ]
        with patch("core.discovery.all_collectors",
                   return_value={"test": collector}):
            result = search_apps("示例ai", search_type="app")
        self.assertEqual(["com.example.office"],
                         [item["package_name"] for item in result["candidates"]])
        self.assertEqual(2, collector.search.call_count)
        self.assertEqual("名称精确匹配", result["candidates"][0]["match_reason"])

    def test_app_search_keeps_conservative_fuzzy_candidate_for_confirmation(self):
        self.assertGreater(
            _relevance({"app_name": "示例协同办工"}, "示例协同办公", "app"), 0
        )
        self.assertEqual(
            0, _relevance({"app_name": "完全不同应用"}, "示例协同办公", "app")
        )

    def test_discovery_enriches_xiaomi_by_confirmed_package(self):
        searchable = MagicMock(
            supports_search=True, supports_package_lookup=False,
            display_name="可搜索市场",
        )
        searchable.search.return_value = [
            SearchCandidate(app_name="示例AI", package_name="com.example.office",
                            developer="示例电子商务有限公司")
        ]
        xiaomi = MagicMock(
            supports_search=False, supports_package_lookup=True,
            display_name="小米应用商店",
        )
        xiaomi.collect.return_value = CollectResult(
            version_name="7.20.80", status="ok",
            extra={"developer": "示例电子商务有限公司",
                   "source_url": "https://app.mi.com/details?id=com.example.office"},
        )
        with patch("core.discovery.all_collectors", return_value={
                "searchable": searchable, "xiaomi": xiaomi}):
            result = search_apps("示例AI", search_type="app")
        candidate = result["candidates"][0]
        self.assertEqual(
            {"searchable", "xiaomi"},
            {match["market_id"] for match in candidate["matches"]},
        )
        self.assertEqual("7.20.80", candidate["matches"][1]["version_name"])
        self.assertEqual(1, result["package_lookup"]["matched"])
        xiaomi.collect.assert_called_once_with(
            "com.example.office", app_name="示例AI", timeout=15,
        )

    def test_company_name_expands_to_conservative_brand_term(self):
        self.assertEqual(
            ["北京示例电子商务有限公司", "示例"],
            _company_search_terms("北京示例电子商务有限公司"),
        )

    def test_company_match_ranks_above_weak_market_hit(self):
        company_hit = {"app_name": "示例金融", "developer": "示例科技控股股份有限公司"}
        weak_hit = {"app_name": "惠聚", "developer": "哈尔滨圆弧科技有限公司"}
        self.assertGreater(_relevance(company_hit, "示例科技", "company"),
                           _relevance(weak_hit, "示例科技", "company"))

    def test_app_search_filters_same_company_but_unrelated_names(self):
        collector = MagicMock(supports_search=True, display_name="测试市场")
        collector.search.return_value = [
            SearchCandidate(app_name="示例云设备", package_name="com.example.cloud.router",
                            developer="北京示例公司"),
            SearchCandidate(app_name="示例金融", package_name="com.example.finance.alt",
                            developer="北京示例公司"),
            SearchCandidate(app_name="完全无关", package_name="com.other.app",
                            developer="其他公司"),
        ]
        with patch("core.discovery.all_collectors", return_value={"test": collector}):
            result = search_apps("示例云", search_type="app")
        self.assertEqual(["示例云设备"], [x["app_name"] for x in result["candidates"]])
        self.assertEqual(2, result["filtered_out"])

    def test_company_search_uses_developer_evidence(self):
        collector = MagicMock(supports_search=True, display_name="测试市场")
        collector.search.return_value = [
            SearchCandidate(app_name="企业服务", package_name="com.example.service",
                            developer="示例科技控股股份有限公司"),
            SearchCandidate(app_name="无关应用", package_name="com.other.app",
                            developer="其他公司"),
        ]
        with patch("core.discovery.all_collectors", return_value={"test": collector}):
            result = search_apps("示例科技", search_type="company")
        self.assertEqual(["企业服务"], [x["app_name"] for x in result["candidates"]])

    def test_company_search_uses_full_name_and_brand_then_marks_evidence(self):
        collector = MagicMock(supports_search=True, display_name="测试市场")
        collector.search.side_effect = [
            [],
            [SearchCandidate(app_name="示例云设备", package_name="com.example.cloud.router",
                             developer="北京示例电子商务有限公司"),
             SearchCandidate(app_name="示例收银", package_name="com.other.cashier",
                             developer="其他公司")],
        ]
        with patch("core.discovery.all_collectors", return_value={"test": collector}):
            result = search_apps("北京示例电子商务有限公司",
                                 search_type="company")
        self.assertEqual(["北京示例电子商务有限公司", "示例"],
                         result["search_terms"])
        self.assertEqual(2, collector.search.call_count)
        self.assertEqual("公司主体匹配", result["candidates"][0]["match_reason"])
        self.assertEqual("品牌名匹配，主体待确认", result["candidates"][1]["match_reason"])

    def test_discovery_keeps_candidates_beyond_first_page(self):
        collector = MagicMock()
        collector.supports_search = True
        collector.display_name = "测试市场"
        collector.search.return_value = [
            SearchCandidate(app_name=f"公司应用{i}", package_name=f"com.example.app{i}")
            for i in range(27)
        ]
        with patch("core.discovery.all_collectors", return_value={"test": collector}):
            result = search_apps("公司")
        self.assertEqual(27, result["total"])
        self.assertEqual(27, len(result["candidates"]))
        self.assertEqual({"searched": 1, "successful": 1, "failed": 0},
                         result["source_summary"])

    def test_discovery_distinguishes_source_failure_from_no_matches(self):
        failed = MagicMock(supports_search=True, display_name="失败市场")
        failed.search.side_effect = TimeoutError("timeout")
        empty = MagicMock(supports_search=True, display_name="空结果市场")
        empty.search.return_value = []
        with patch("core.discovery.all_collectors",
                   return_value={"failed": failed, "empty": empty}):
            result = search_apps("公司")
        self.assertEqual(0, result["total"])
        self.assertEqual({"searched": 2, "successful": 1, "failed": 1},
                         result["source_summary"])
        self.assertFalse(result["sources"][0]["ok"])


class DeviceDetectionTests(unittest.TestCase):
    @patch("core.env_check.shutil.which", return_value="/usr/local/bin/adb")
    def test_project_platform_tools_take_priority(self, _which):
        with tempfile.TemporaryDirectory() as tmp:
            adb = Path(tmp) / "adb"
            adb.write_text("portable", encoding="utf-8")
            adb.chmod(0o755)
            with patch("core.env_check.PORTABLE_ADB", adb):
                found, source = find_adb()
        self.assertEqual(str(adb), found)
        self.assertEqual("项目内置 Platform-Tools", source)

    def test_emulator_is_not_classified_as_usb_phone(self):
        output = """List of devices attached
emulator-5554 device product:sdk_phone_arm64 model:Android_SDK device:generic_arm64
ABC123 device product:venus model:Phone device:venus
"""
        devices = parse_adb_devices(output)
        self.assertTrue(devices[0]["is_emulator"])
        self.assertFalse(devices[1]["is_emulator"])

    def test_adb_startup_log_is_not_misclassified_as_devices(self):
        output = """* daemon not running; starting now at tcp:5037
ADB server didn't ACK
Full server startup log: /tmp/adb.log
08-12 13:52:30 I adb : Android Debug Bridge version 1.0.41
adb: failed to check server version: cannot connect to daemon
"""
        self.assertEqual([], parse_adb_devices(output))

    @patch("core.env_check._run")
    @patch("core.env_check.find_adb", return_value=("/usr/local/bin/adb", "test"))
    def test_emulator_is_accepted_as_android_test_device(self, _find, run):
        run.return_value = (0, "List of devices attached\n"
                            "emulator-5554 device product:sdk_phone_arm64 "
                            "model:Android_SDK device:generic_arm64\n")
        status, message, actions = check_usb_phone()
        self.assertEqual("ok", status)
        self.assertIn("模拟器 emulator-5554", message)
        self.assertEqual([], actions)


class YybDeviceDriverTests(unittest.TestCase):
    def test_ascii_query_is_preserved(self):
        self.assertEqual("ExampleFinance", _pinyin_query("Example Finance"))

    def test_ocr_version_parser_reads_detail_label(self):
        with patch("executors.markets.yyb.shutil.which", return_value="/usr/bin/tesseract"), \
             patch("executors.markets.yyb.subprocess.run") as run:
            run.return_value.stdout = "运营商: 示例  版本: 8.2.40 | 2026-08-05更新"
            run.return_value.stderr = ""
            version, _ = _ocr_version("detail.png")
        self.assertEqual("8.2.40", version)


class GenericDeviceDriverTests(unittest.TestCase):
    def test_version_parser_requires_version_context(self):
        self.assertEqual("8.2.40", parse_version(["应用信息", "版本号：8.2.40", "更新日期 2026.08.06"]))
        self.assertEqual("", parse_version(["更新日期", "2026.08.06", "下载 3.2.1 万次"]))

    def test_developer_and_operator_are_kept_separate(self):
        entities = parse_entities([
            "开发者", ": 示例金融信息服务有限公司",
            "版本：8.2.40", "主办者：示例运营信息技术有限公司",
        ])
        self.assertEqual("示例金融信息服务有限公司", entities["developer"])
        self.assertEqual("示例运营信息技术有限公司", entities["operator"])

    def test_entity_parser_discards_an_adjacent_version_row(self):
        entities = parse_entities([
            "开发者", ": 示例科技控股股份有限公司 版本 8.2.40",
            "主办单位", ": 示例运营信息技术有限公司",
        ])
        self.assertEqual("示例科技控股股份有限公司", entities["developer"])
        self.assertEqual("示例运营信息技术有限公司", entities["operator"])

    @patch("executors.markets.generic.time.sleep", return_value=None)
    def test_oppo_uses_foreground_browser_bridge(self, _sleep):
        device = MagicMock()
        device.reverse.return_value = True
        device.shell.side_effect = lambda command, **_: (
            "package:/data/app/firefox.apk" if command == "pm path org.mozilla.firefox"
            else "Status: ok"
        )
        bridge_nodes = [{
            "text": "打开 OPPO 软件商店", "desc": "", "enabled": True,
            "clickable": True, "package": "org.mozilla.firefox", "cx": 500, "cy": 800,
        }]
        detail_nodes = [{
            "text": "示例金融", "desc": "", "enabled": True,
            "clickable": False, "package": "com.heytap.market", "cx": 200, "cy": 200,
        }]
        device.nodes.side_effect = [bridge_nodes, detail_nodes]
        driver = OppoDeviceDriver(device)
        ok, nodes = driver._open_package_detail("com.example.finance")
        self.assertTrue(ok)
        self.assertEqual(detail_nodes, nodes)
        device.reverse.assert_called_once_with(5001)
        device.tap.assert_called_once_with(500, 800)
        self.assertTrue(any("/device/oppo-bridge?package_name=com.example.finance" in
                            call.args[0] for call in device.shell.call_args_list))

    def test_detail_reader_rejects_another_market_page(self):
        driver = OppoDeviceDriver(MagicMock())
        nodes = [{"package": "com.hihonor.appmarket", "text": "示例金融", "desc": ""}]
        self.assertIsNone(driver._read_opened_detail(
            "com.example.finance", "示例金融", "data/screenshots", nodes,
        ))

    @patch("executors.markets.generic.time.sleep", return_value=None)
    def test_oppo_reads_about_app_after_stable_detail_toggle(self, _sleep):
        device = MagicMock()
        initial = [
            {"package": "com.heytap.market", "text": "示例金融", "desc": "",
             "rid": "", "clickable": False},
            {"package": "com.heytap.market", "text": "", "desc": "",
             "rid": "com.heytap.market:id/show_more_area_ll", "clickable": True,
             "cx": 500, "cy": 600},
        ]
        expanded = [{"package": "com.heytap.market", "text": "关于应用", "desc": ""}]
        about = [
            {"package": "com.heytap.market", "text": "版本 8.2.40  |  6 天前", "desc": ""},
            {"package": "com.heytap.market", "text": "示例科技控股股份有限公司", "desc": ""},
        ]
        device.nodes.side_effect = [expanded, about]
        device.screenshot.return_value = False
        result = OppoDeviceDriver(device)._read_opened_detail(
            "com.example.finance", "示例金融", "data/screenshots", initial,
        )
        self.assertTrue(result["ok"])
        self.assertEqual("8.2.40", result["version"])
        self.assertEqual("示例科技控股股份有限公司", result["developer"])
        device.tap.assert_called_once_with(500, 600)

    def test_privacy_footer_alone_is_not_treated_as_consent_gate(self):
        driver = GenericStoreDriver(object())
        blocked, _ = driver._blocked([{"text": "隐私政策", "desc": ""}])
        self.assertFalse(blocked)

    def test_explicit_consent_page_is_left_for_user(self):
        driver = GenericStoreDriver(object())
        blocked, reason = driver._blocked([{"text": "请阅读隐私政策", "desc": ""},
                                           {"text": "同意并继续", "desc": ""}])
        self.assertTrue(blocked)
        self.assertIn("手工处理", reason)

    def test_recommendation_name_does_not_validate_wrong_detail_page(self):
        device = MagicMock()
        driver = GenericStoreDriver(device)
        wrong_page = [
            {"text": value, "desc": ""} for value in
            ["快手", "短视频榜第2名", "版本：14.7.10.49551", "开发者：快手科技",
             "详情", "评论", "推荐", "大家还安装了", "示例金融"]
        ]
        self.assertIsNone(driver._read_opened_detail(
            "com.example.finance", "示例金融", "data/screenshots", wrong_page,
        ))


class DeviceMarketCollectorTests(unittest.TestCase):
    @patch("executors.device.DeviceExecutor")
    def test_missing_market_client_has_specific_status(self, executor_cls):
        executor = MagicMock()
        executor.check_ready.return_value = (True, "设备 emulator-5554")
        executor.inspect_market_detail.return_value = {
            "ok": False, "status": "market_app_missing", "detail": "设备未安装市场App"
        }
        executor_cls.return_value = executor
        result = OppoCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("market_app_missing", result.status)

    @patch("executors.device.DeviceExecutor")
    def test_unavailable_device_has_specific_status(self, executor_cls):
        executor_cls.return_value.check_ready.return_value = (False, "未检测到可用设备")
        result = OppoCollector().collect("com.example.finance", app_name="示例金融")
        self.assertEqual("device_unavailable", result.status)


class DeviceDownloadVerificationTests(unittest.TestCase):
    def test_compatibility_report_reads_model_and_package_aliases(self):
        executor = DeviceExecutor.__new__(DeviceExecutor)
        executor.dev = MagicMock(serial="USB-EXAMPLE")
        executor.dev.ready.return_value = (True, "设备 USB-EXAMPLE")
        responses = {
            "pm list packages": "package:com.oppo.market\npackage:com.bbk.appstore\n",
            "getprop ro.product.manufacturer": "ExampleVendor\n",
            "getprop ro.product.model": "Model One\n",
            "getprop ro.build.version.release": "15\n",
            "getprop ro.build.version.sdk": "35\n",
        }
        executor.dev.shell.side_effect = lambda command: responses[command]
        report = executor.compatibility_report(["oppo", "vivo", "honor"])
        self.assertTrue(report["ready"])
        self.assertEqual("Model One", report["device"]["model"])
        states = {item["market_id"]: item for item in report["markets"]}
        self.assertEqual("com.oppo.market", states["oppo"]["package"])
        self.assertTrue(states["vivo"]["installed"])
        self.assertFalse(states["honor"]["installed"])

    def test_unsafe_name_search_download_is_always_disabled(self):
        executor = DeviceExecutor.__new__(DeviceExecutor)
        executor.dev = MagicMock()
        executor.dev.shell.return_value = "package:/data/app/com.example.finance/base.apk"
        with patch.object(executor, "_ensure_market_app",
                          return_value=(True, "com.tencent.android.qqdownloader")):
            result = executor.download_and_verify(
                "yyb", "com.example.finance", app_name="示例金融"
            )
        self.assertEqual("unsafe_device_download_disabled", result["status"])
        self.assertIn("已禁用 Android 设备端按名称自动搜索", result["detail"])
        executor.dev.start_app.assert_not_called()


class ApkVerificationTests(unittest.TestCase):
    @patch("core.apk_verify.parse_apk")
    def test_expected_package_mismatch_is_a_hard_failure(self, parse):
        parse.return_value = {
            "package": "com.example.fake", "version_name": "1.0",
            "version_code": "1", "sig_sha256": "aa",
        }
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "sample.apk"
            apk.write_bytes(b"sample")
            result = apk_verify.verify(apk, expected_package="com.example.real")
        self.assertEqual("diff", result["verify_result"])
        self.assertIn("包名不一致", result["detail"])

    @patch("core.apk_verify.parse_apk")
    def test_signature_only_baseline_is_compared(self, parse):
        parse.return_value = {
            "package": "com.example.real", "version_name": "1.0",
            "version_code": "1", "sig_sha256": "bb",
        }
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "sample.apk"
            apk.write_bytes(b"sample")
            result = apk_verify.verify(apk, baseline_sig="aa")
        self.assertEqual("diff", result["verify_result"])
        self.assertIn("签名", result["detail"])


class DeviceExtractMessageTests(unittest.TestCase):
    @patch("executors.device.DeviceExecutor")
    def test_emulator_can_be_used_as_device_artifact_source(self, executor_cls):
        dev = MagicMock()
        dev.serial = "emulator-5554"
        dev.shell.return_value = ""
        executor_cls.return_value.dev = dev
        executor_cls.return_value.check_ready.return_value = (True, "设备 emulator-5554")
        result = _device_extract_artifact(
            {"package_name": "com.example.finance", "id": 1},
            {"id": "xiaomi"}, "/tmp",
        )
        self.assertEqual("app_not_installed", result["status"])
        self.assertIn("模拟器", result["detail"])

    @patch("executors.device.DeviceExecutor")
    def test_app_not_installed_returns_clear_message(self, executor_cls):
        dev = MagicMock()
        dev.serial = "27111FDH2002WJ"
        dev.shell.return_value = ""
        executor_cls.return_value.dev = dev
        executor_cls.return_value.check_ready.return_value = (True, "设备 27111FDH2002WJ")
        result = _device_extract_artifact(
            {"package_name": "com.example.finance", "id": 1},
            {"id": "xiaomi"}, "/tmp",
        )
        self.assertEqual("app_not_installed", result["status"])
        self.assertIn("未安装", result["detail"])


class DeviceInteractionTests(unittest.TestCase):
    def test_locked_phone_is_not_treated_as_query_failure(self):
        dev = MagicMock()
        dev.ready.return_value = (True, "设备 phone")
        dev.shell.side_effect = ["mWakefulness=Awake", "deviceLocked=1", ""]
        from executors.adb_device import AdbDevice
        ok, detail = AdbDevice.interaction_ready(dev)
        self.assertFalse(ok)
        self.assertIn("手机已锁定", detail)

    def test_sleeping_phone_is_woken_before_interaction(self):
        dev = MagicMock()
        dev.ready.return_value = (True, "设备 phone")
        dev.shell.side_effect = [
            "mWakefulness=Asleep", "", "mWakefulness=Awake", "deviceLocked=0", "",
        ]
        from executors.adb_device import AdbDevice
        with patch("executors.adb_device.time.sleep"):
            ok, detail = AdbDevice.interaction_ready(dev)
        self.assertTrue(ok)
        self.assertIn("已亮屏并解锁", detail)
        self.assertEqual("input keyevent 224", dev.shell.call_args_list[1].args[0])


class ArtifactDownloadTests(unittest.TestCase):
    def test_single_stream_resumes_existing_partial_file(self):
        response = MagicMock()
        response.status_code = 206
        response.url = "https://dd.qq.com/sample.apk"
        response.headers = {"content-range": "bytes 3-5/6", "content-length": "3"}
        response.iter_bytes.return_value = [b"def"]
        response.raise_for_status.return_value = None
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.apk.part"
            path.write_bytes(b"abc")
            with patch("core.artifacts._download_probe",
                       return_value=("https://dd.qq.com/sample.apk", 6, False)), \
                    patch("core.artifacts.httpx.stream", return_value=context):
                size, expected, mode = _download_with_resume(
                    "yyb", "https://dd.qq.com/sample.apk", path, 30
                )
            self.assertEqual(b"abcdef", path.read_bytes())
            self.assertEqual((6, 6, "resume"), (size, expected, mode))

    def test_parallel_parts_are_merged_in_order(self):
        def fake_range(market_id, url, path, start, end, timeout, on_bytes):
            data = bytes([65 + start]) * (end - start + 1)
            path.write_bytes(data)
            on_bytes(len(data))
            return len(data)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.apk.part"
            with patch("core.artifacts._SEGMENT_THRESHOLD", 1), \
                    patch("core.artifacts._download_probe",
                          return_value=("https://dd.qq.com/sample.apk", 8, True)), \
                    patch("core.artifacts._stream_range", side_effect=fake_range):
                size, expected, mode = _download_with_resume(
                    "yyb", "https://dd.qq.com/sample.apk", path, 30
                )
            self.assertEqual(8, path.stat().st_size)
            self.assertEqual((8, 8, "parallel"), (size, expected, mode))


class ArtifactCacheTests(unittest.TestCase):
    @patch("core.artifacts.get_collector")
    def test_fresh_collect_returns_collector_result(self, get_collector):
        collector = MagicMock()
        collector.collect.return_value = "COLLECT_RESULT"
        get_collector.return_value = collector
        with patch("core.artifacts.db.query", return_value=None):
            result = _fresh_collect(
                {"package_name": "com.example.finance", "app_name": "示例金融",
                 "company_name": "示例科技", "id": 1},
                {"adapter": "yyb", "id": "yyb"}, 15,
            )
        self.assertEqual("COLLECT_RESULT", result)
        collector.collect.assert_called_once()

    def test_same_market_package_and_version_reuses_verified_file(self):
        import core.db as db
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                app_id = db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("示例应用", "com.example.app"),
                )
                apk = Path(tmp) / "cached.apk"
                apk.write_bytes(b"cached-apk")
                digest = apk_verify.sha256(apk)
                artifact_id = db.execute(
                    """INSERT INTO artifacts
                       (app_id,market_id,file_name,local_path,file_size,sha256,
                        package_name,version_name,risk_level,conclusion)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, "yyb", apk.name, str(apk), apk.stat().st_size, digest,
                     "com.example.app", "1.2.3", "info", "已记录"),
                )
                app_row = db.query("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
                cached = _find_cached_artifact(
                    app_row, "yyb", CollectResult(version_name="1.2.3", status="ok")
                )
                self.assertEqual(artifact_id, cached["id"])
                db.execute(
                    """INSERT INTO results (app_id,market_id,status,version_name)
                       VALUES (?,?, 'ok',?)""",
                    (app_id, "yyb", "1.2.3"),
                )
                cached_from_last_result = _find_cached_artifact(
                    app_row, "yyb", CollectResult(status="offline")
                )
                self.assertEqual(artifact_id, cached_from_last_result["id"])
                apk.write_bytes(b"tampered")
                self.assertIsNone(_find_cached_artifact(
                    app_row, "yyb", CollectResult(version_name="1.2.3", status="ok")
                ))
        finally:
            db.DB_PATH = old_path


class DatabaseMigrationTests(unittest.TestCase):
    def test_fresh_db_enables_only_package_ready_markets(self):
        import core.db as db
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                enabled = {row["id"] for row in db.query("SELECT id FROM markets WHERE enabled=1")}
                self.assertEqual(
                    {"huawei", "xiaomi", "yyb", "samsung", "baidu", "meizu",
                     "appstore", "qihu360"}, enabled,
                )
                platforms = {row["id"]: row["platform"] for row in db.query("SELECT * FROM markets")}
                names = {row["id"]: row["name"] for row in db.query("SELECT * FROM markets")}
                self.assertEqual("ios", platforms["appstore"])
                self.assertEqual("OPPO 软件商店", names["oppo"])
                self.assertEqual("vivo 应用商店", names["vivo"])
                self.assertEqual("三星 Galaxy Store", names["samsung"])
                self.assertEqual("360 手机助手", names["qihu360"])
                self.assertNotIn("harmony", platforms)
                result_cols = {row[1] for row in db.query("PRAGMA table_info(results)")}
                self.assertIn("download_action", result_cols)
                self.assertIn("developer_name", result_cols)
                self.assertIn("operator_name", result_cols)
                self.assertIn("published_at", result_cols)
                self.assertIn("risk_level", result_cols)
                self.assertIn("observed_package", result_cols)
                observation_cols = {row[1] for row in db.query("PRAGMA table_info(observations)")}
                self.assertIn("published_at", observation_cols)
                self.assertNotIn("google_play", platforms)
                self.assertTrue(db.query("SELECT name FROM sqlite_master WHERE name='observations'", one=True))
                self.assertTrue(db.query("SELECT name FROM sqlite_master WHERE name='artifacts'", one=True))
        finally:
            db.DB_PATH = old_path

    def test_delete_app_removes_current_and_historical_rows(self):
        import core.db as db
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                app_id = db.execute("INSERT INTO apps (app_name) VALUES (?)", ("待删除应用",))
                db.execute("INSERT INTO bindings (app_id,market_id) VALUES (?,?)",
                           (app_id, "huawei"))
                db.execute("INSERT INTO results (app_id,market_id) VALUES (?,?)",
                           (app_id, "huawei"))
                db.execute("INSERT INTO observations (app_id,market_id) VALUES (?,?)",
                           (app_id, "huawei"))
                db.delete_app(app_id)
                self.assertIsNone(db.query("SELECT id FROM apps WHERE id=?", (app_id,), one=True))
                self.assertEqual(0, db.query("SELECT COUNT(*) n FROM bindings WHERE app_id=?",
                                             (app_id,), one=True)["n"])
                self.assertEqual(0, db.query("SELECT COUNT(*) n FROM results WHERE app_id=?",
                                             (app_id,), one=True)["n"])
                self.assertEqual(0, db.query("SELECT COUNT(*) n FROM observations WHERE app_id=?",
                                             (app_id,), one=True)["n"])
        finally:
            db.DB_PATH = old_path

    def test_v8_removes_legacy_yyb_pc_container_and_related_rows(self):
        import core.db as db
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                db.execute("DELETE FROM schema_meta WHERE key='remove_yyb_pcgame_v8'")
                app_id = db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("示例金融", "com.tencent.pcgame.examplefinance"),
                )
                db.execute("INSERT INTO bindings (app_id,market_id) VALUES (?,?)", (app_id, "yyb"))
                db.execute("INSERT INTO results (app_id,market_id) VALUES (?,?)", (app_id, "yyb"))
                db.execute("INSERT INTO observations (app_id,market_id) VALUES (?,?)", (app_id, "yyb"))
                db.init_db()
                self.assertIsNone(db.query("SELECT id FROM apps WHERE id=?", (app_id,), one=True))
                self.assertEqual(0, db.query("SELECT COUNT(*) n FROM results WHERE app_id=?",
                                             (app_id,), one=True)["n"])
        finally:
            db.DB_PATH = old_path


class AppApiTests(unittest.TestCase):
    def test_scheduled_monitoring_is_disabled_and_usb_tool_has_narrow_scope(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                client = app.test_client()
                page = client.get("/config").get_data(as_text=True)
                self.assertIn("Android 设备渠道复核（可选）", page)
                self.assertIn("不会自动下载或安装目标 App", page)
                self.assertNotIn("每周自动巡检", page)
                self.assertNotIn("sched-enabled", page)

                disabled = client.post("/api/settings", json={"schedule_enabled": True})
                self.assertEqual(410, disabled.status_code)
                self.assertFalse(disabled.get_json()["ok"])
                self.assertEqual(
                    {"scheduled_monitoring": False},
                    client.get("/api/config").get_json(),
                )
        finally:
            db.DB_PATH = old_path

    @patch("executors.device.DeviceExecutor.compatibility_report")
    @patch("core.env_check.run_all")
    def test_usb_check_reports_phone_and_installed_market_clients(self, run_all, compatibility):
        from app import app
        run_all.return_value = [
            {"step": 1, "name": "Python 依赖", "status": "ok", "message": "ok", "actions": []},
            {"step": 2, "name": "ADB 工具", "status": "ok", "message": "ok", "actions": []},
            {"step": 3, "name": "Android 测试设备", "status": "ok", "message": "ok", "actions": []},
        ]
        compatibility.return_value = {
            "ready": True,
            "message": "设备已连接",
            "device": {"brand": "Example", "model": "Phone", "android": "15"},
            "markets": [
                {"market_id": "oppo", "market_name": "OPPO 软件商店", "installed": True},
                {"market_id": "vivo", "market_name": "vivo 应用商店", "installed": True},
                {"market_id": "honor", "market_name": "荣耀应用市场", "installed": True},
            ],
        }
        result = app.test_client().get("/api/env/check").get_json()
        self.assertTrue(result["phone_ready"])
        self.assertEqual("设备应用市场客户端", result["steps"][-1]["name"])
        self.assertIn("已安装市场客户端：OPPO 软件商店、vivo 应用商店、荣耀应用市场",
                      result["steps"][-1]["message"])
        self.assertNotIn("oppo, vivo, honor", result["steps"][-1]["message"])

    def test_patrol_actions_have_one_clear_meaning_per_page(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("示例应用", "com.example.app"),
                )
                client = app.test_client()
                config_page = client.get('/config').get_data(as_text=True)
                self.assertEqual(1, config_page.count('onclick="runCheck(this)"'))
                self.assertNotIn('保存并开始巡检', config_page)
                self.assertNotIn('开始新一轮巡检', config_page)
                self.assertIn('不会清空监测清单或历史报表', config_page)
                index_page = client.get('/').get_data(as_text=True)
                self.assertIn('>开始巡检</button>', index_page)
                results_page = client.get('/results').get_data(as_text=True)
                self.assertIn('>按当前配置重新巡检</button>', results_page)
                self.assertIn('不会清空监测清单', results_page)
        finally:
            db.DB_PATH = old_path

    def test_channel_retry_is_available_without_false_artifact_button(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                app_id = db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("示例应用", "com.example.android"),
                )
                db.execute(
                    """INSERT INTO results
                       (app_id,market_id,status,version_name,download_action)
                       VALUES (?,?, 'ok','1.0.0','optional')""",
                    (app_id, "huawei"),
                )
                page = app.test_client().get('/results').get_data(as_text=True)
                self.assertIn("onclick='retryMarket(\"huawei\",", page)
                self.assertIn(f'id="artifact-status-huawei-{app_id}"', page)
                self.assertNotIn("onclick='acquireArtifact(\"huawei\",", page)
                self.assertIn("该渠道当前没有官方网页 APK 直链", page)
                self.assertNotIn("用手机下载并校验", page)
                self.assertNotIn("setTimeout(()=>location.href='/results',1800)", page)
        finally:
            db.DB_PATH = old_path

    def test_direct_download_button_declares_method(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                app_id = db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("示例应用", "com.example.android"),
                )
                db.execute(
                    """INSERT INTO results
                       (app_id,market_id,status,version_name,download_url)
                       VALUES (?,?, 'ok','1.0.0',?)""",
                    (app_id, "yyb", "https://dd.qq.com/sample.apk"),
                )
                page = app.test_client().get('/results').get_data(as_text=True)
                self.assertIn("下载并校验 APK", page)
                self.assertIn("acquireArtifact(\"yyb\",", page)
                self.assertIn('"direct",this)', page)
                self.assertIn("4 路分段下载和断点续传", page)
        finally:
            db.DB_PATH = old_path

    def test_old_phone_download_api_is_rejected(self):
        from app import app
        response = app.test_client().post('/api/check/b-run', json={
            'market_id': 'yyb', 'app_id': 1, 'do_download': True,
        })
        self.assertEqual(410, response.status_code)
        self.assertIn('已禁用', response.get_json()['msg'])

    def test_package_mismatch_is_rendered_as_critical_risk(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                app_id = db.execute(
                    "INSERT INTO apps (app_name,package_name) VALUES (?,?)",
                    ("目标应用", "com.example.real"),
                )
                db.execute(
                    """INSERT INTO results
                       (app_id,market_id,status,observed_package,risk_level,risk_reason)
                       VALUES (?,?, 'package_mismatch',?,'critical',?)""",
                    (app_id, "yyb", "com.example.fake", "发现同名错包"),
                )
                page = app.test_client().get('/results').get_data(as_text=True)
                self.assertIn("严重：疑似仿冒/错包", page)
                self.assertIn("市场包名：com.example.fake", page)
        finally:
            db.DB_PATH = old_path

    def test_rejects_yyb_pc_container_as_android_candidate(self):
        from app import app
        response = app.test_client().post('/api/apps/from-discovery', json={
            'app_name': '示例金融', 'platform': 'android',
            'package_name': 'com.tencent.pcgame.examplefinance',
            'developer': 'examplecorp', 'query': '示例金融', 'matches': [],
        })
        self.assertEqual(400, response.status_code)
        self.assertIn('不是 Android App', response.get_json()['msg'])

    def test_market_sections_follow_monitored_platforms(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                client = app.test_client()
                client.post('/api/apps', json={
                    'app_name': '仅安卓应用', 'package_name': 'com.example.android',
                })
                android_page = client.get('/config').get_data(as_text=True)
                self.assertRegex(
                    android_page,
                    r'id="ios-market-section"[^>]*display:none',
                )
                client.post('/api/apps/from-discovery', json={
                    'app_name': '仅iOS应用', 'platform': 'ios',
                    'bundle_id': 'com.example.ios', 'ios_app_id': '12345',
                    'developer': '示例公司', 'query': '示例', 'matches': [],
                })
                ios_page = client.get('/config').get_data(as_text=True)
                ios_tag = ios_page.split('id="ios-market-section"', 1)[1].split('>', 1)[0]
                self.assertNotIn('display:none', ios_tag)
                self.assertIn('Apple App Store', ios_page)
                self.assertNotIn('Google Play', ios_page)
        finally:
            db.DB_PATH = old_path

    def test_confirmed_candidate_completes_same_name_placeholder(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                client = app.test_client()
                self.assertTrue(client.post('/api/apps', json={
                    'app_name': '示例云',
                }).get_json()['ok'])
                response = client.post('/api/apps/from-discovery', json={
                    'app_name': '示例云',
                    'package_name': 'com.example.cloud',
                    'developer': '示例公司',
                    'query': '示例云',
                    'matches': [],
                }).get_json()
                self.assertTrue(response['resolved_pending'])
                rows = db.query('SELECT * FROM apps')
                self.assertEqual(1, len(rows))
                self.assertEqual('com.example.cloud', rows[0]['package_name'])
                self.assertEqual('confirmed', rows[0]['discovery_status'])
        finally:
            db.DB_PATH = old_path

    def test_ios_only_candidate_can_be_added_and_bound(self):
        import core.db as db
        from app import app
        old_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "monitor.db"
                db.init_db()
                response = app.test_client().post('/api/apps/from-discovery', json={
                    'app_name': '仅iOS应用', 'platform': 'ios',
                    'bundle_id': 'com.example.iosonly', 'ios_app_id': '12345',
                    'developer': '示例公司', 'query': '仅iOS应用',
                    'matches': [{'market_id': 'appstore', 'market_app_id': '12345'}],
                }).get_json()
                self.assertTrue(response['ok'])
                row = db.query('SELECT * FROM apps WHERE id=?', (response['id'],), one=True)
                self.assertEqual('ios', row['platform'])
                self.assertEqual('', row['package_name'])
                self.assertEqual('com.example.iosonly', row['ios_bundle_id'])
                binding = db.query('SELECT * FROM bindings WHERE app_id=? AND market_id=?',
                                   (response['id'], 'appstore'), one=True)
                self.assertEqual('12345', binding['market_app_id'])
        finally:
            db.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()
