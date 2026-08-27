"""应用市场版本巡检系统 · Flask 主应用"""
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from flask import (Flask, render_template, request, jsonify, send_file, redirect,
                   url_for, flash, Response)
import core.db as db
from config import load_config, BASE_DIR
from core.checker import run_check, run_b_check
from core.version import version_key

app = Flask(__name__)
app.secret_key = os.environ.get("AMM_SECRET") or secrets.token_hex(32)
db.init_db()

# 兼容 json_each 等高级查询需求：本应用用简单参数化即可

HIDDEN_MARKETS = ("coolapk", "harmony", "google_play")
HIDDEN_MARKET_PARAMS = ",".join("?" for _ in HIDDEN_MARKETS)


def _visible_markets(enabled_only=False):
    where = f"WHERE id NOT IN ({HIDDEN_MARKET_PARAMS})"
    if enabled_only:
        where += " AND enabled=1"
    return db.query(f"SELECT * FROM markets {where} ORDER BY sort_order", HIDDEN_MARKETS)


def _artifact_capabilities(apps, markets, result_map):
    """Describe only artifact actions that are currently executable."""
    from core.artifacts import _allowed_url

    installed_packages = set()
    phone_detail = "未连接可用的实体手机"
    needs_phone_probe = any(
        a.get("platform") == "android" and m.get("platform") == "android"
        and not result_map.get((m["id"], a["id"]), {}).get("download_url")
        for a in apps for m in markets
    )
    if needs_phone_probe:
        try:
            from executors.adb_device import AdbDevice
            dev = AdbDevice()
            ready, phone_detail = dev.ready()
            if ready and dev.serial and not str(dev.serial).startswith("emulator-"):
                installed_packages = {
                    line.removeprefix("package:").strip()
                    for line in dev.shell("pm list packages").splitlines()
                    if line.startswith("package:")
                }
                phone_detail = f"实体手机 {dev.serial} 已连接"
            elif ready:
                phone_detail = "当前仅连接模拟器，不能作为实体市场安装包来源"
        except Exception as exc:
            phone_detail = f"暂时无法读取手机状态：{type(exc).__name__}"

    capabilities = {}
    for app_item in apps:
        for market in markets:
            key = (market["id"], app_item["id"])
            if app_item.get("platform") != "android" or market.get("platform") != "android":
                continue
            result = result_map.get(key)
            download_url = str((result or {}).get("download_url") or "").strip()
            if download_url and _allowed_url(market["id"], download_url):
                capabilities[key] = {
                    "method": "direct", "label": "下载并校验 APK",
                    "detail": "该渠道提供官方网页直链；支持 4 路分段下载和断点续传。",
                }
            elif download_url:
                capabilities[key] = {
                    "method": "none",
                    "detail": "下载地址不在该渠道官方域名白名单中，已禁止操作。",
                }
            elif result and result.get("status") == "package_mismatch":
                capabilities[key] = {
                    "method": "none", "detail": "发现同名错包，已禁止获取安装包。",
                }
            elif app_item.get("package_name") in installed_packages:
                capabilities[key] = {
                    "method": "device", "label": "从手机提取并校验 APK",
                    "detail": (f"{phone_detail}，且已安装目标应用；只提取现有安装包，"
                               "不会自动搜索、下载或安装。"),
                }
            else:
                capabilities[key] = {
                    "method": "none",
                    "detail": ("该渠道当前没有官方网页 APK 直链。"
                               f"{phone_detail}；需先在实体手机安装目标应用后才能提取校验。"),
                }
    return capabilities


def _report_snapshot():
    """Build business-facing version/risk summaries from raw collector rows."""
    markets = [dict(m) for m in _visible_markets(enabled_only=True)]
    apps = [dict(a) for a in db.query("SELECT * FROM apps ORDER BY id")]
    rows = [dict(r) for r in db.query(
        f"SELECT * FROM results WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS})", HIDDEN_MARKETS
    )]
    bindings = {
        (b["app_id"], b["market_id"]): dict(b)
        for b in db.query(
            f"SELECT * FROM bindings WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS})",
            HIDDEN_MARKETS,
        )
    }
    result_map = {(r["market_id"], r["app_id"]): r for r in rows}
    artifact_map = {}
    for artifact in db.query("SELECT * FROM artifacts ORDER BY id DESC"):
        value = dict(artifact)
        artifact_map.setdefault((value["market_id"], value["app_id"]), value)
    artifact_capabilities = _artifact_capabilities(apps, markets, result_map)
    app_reports = {}
    inconsistent = suspicious = warnings = changed = 0
    platform_names = {"android": "Android", "ios": "iOS"}
    enabled_market_ids = {m["id"] for m in markets}
    market_platforms = {m["id"]: m.get("platform", "android") for m in markets}
    app_platforms = {a["id"]: a.get("platform", "android") for a in apps}
    not_found_statuses = {"not_published", "offline"}
    for item in apps:
        app_platform = item.get("platform") or "android"
        app_markets = [m for m in markets if m.get("platform", "android") == app_platform]
        app_market_ids = {m["id"] for m in app_markets}
        app_rows = [
            r for r in rows
            if r["app_id"] == item["id"] and r["market_id"] in app_market_ids
        ]
        online = [r for r in app_rows if r["status"] == "ok" and r["version_name"]]
        by_platform = {}
        for row in online:
            market = next((m for m in markets if m["id"] == row["market_id"]), None)
            platform = (market or {}).get("platform", "android")
            by_platform.setdefault(platform, set()).add(row["version_name"])
        versions_by_platform = {
            platform: sorted(values, key=version_key, reverse=True)
            for platform, values in by_platform.items()
        }
        latest_by_platform = {k: v[0] for k, v in versions_by_platform.items() if v}
        inconsistent_platforms = [k for k, v in versions_by_platform.items() if len(v) > 1]
        is_inconsistent = bool(inconsistent_platforms)
        risk_rows = []
        warning_rows = []
        for row in app_rows:
            authority = bindings.get((item["id"], row["market_id"]), {}).get(
                "authority", "unknown"
            )
            stored_level = row.get("risk_level") or "none"
            stored_reason = row.get("risk_reason") or ""
            level, reason = stored_level, stored_reason
            if row.get("status") == "package_mismatch":
                level = "critical"
                observed = row.get("observed_package") or "未知包名"
                reason = (f"同名应用包名不一致：监测目标 {item.get('package_name') or '未设置'}，"
                          f"市场结果 {observed}。疑似仿冒或错误条目")
            elif stored_level not in ("critical", "high") and row.get("verify_result") == "diff":
                level = "critical"
                reason = stored_reason or "安装包包名、哈希或签名与已有证据不一致"
            elif authority == "third_party" and row.get("status") == "ok":
                level = "high"
                reason = "非授权渠道发现该应用，疑似未授权发布，需核验主体和签名"
            elif authority == "third_party":
                # 非授权渠道未检测到该应用版本，不构成风险（同时覆盖历史误标）
                level, reason = "none", ""
            latest = latest_by_platform.get(app_platform)
            if (level in ("none", "info") and row.get("status") == "ok" and latest
                    and row.get("version_name") and row["version_name"] != latest):
                level = "warning"
                reason = (f"该渠道版本 {row['version_name']} 低于同平台最新版本 {latest}；"
                          "可能是发布延迟或异常旧版本，不直接等于仿冒")
            row["computed_risk_level"] = level
            row["computed_risk_reason"] = reason
            if level in ("critical", "high"):
                risk_rows.append(row)
            elif level == "warning":
                warning_rows.append(row)
        suspicious_rows = risk_rows
        has_change = any(r.get("version_changed") for r in app_rows)
        if is_inconsistent:
            inconsistent += 1
        suspicious += len(suspicious_rows)
        warnings += len(warning_rows)
        if has_change:
            changed += 1
        checked_count = len(app_rows)
        not_found_count = sum(1 for r in app_rows if r["status"] in not_found_statuses)
        issue_count = sum(
            1 for r in app_rows
            if r["status"] not in not_found_statuses and r["status"] != "ok"
        )
        pending_count = max(0, len(app_markets) - checked_count)
        if online:
            coverage_state = "found"
            coverage_text = f"已在 {len(online)} 个渠道发现版本"
        elif app_platform == "android" and not (
                item.get("package_name") or item.get("search_keywords")):
            coverage_state = "pending"
            coverage_text = "应用身份尚未确认：请先从候选中选择或补充包名"
        elif app_platform == "ios" and not (
                item.get("ios_bundle_id") or item.get("ios_app_id")):
            coverage_state = "pending"
            coverage_text = "iOS 应用身份尚未确认：请先选择 App Store 候选"
        elif not app_markets:
            coverage_state = "pending"
            coverage_text = f"尚未启用 {platform_names.get(app_platform, app_platform)} 监测渠道"
        elif checked_count == 0:
            coverage_state = "pending"
            coverage_text = "尚未开始巡检"
        elif pending_count:
            coverage_state = "pending"
            coverage_text = f"巡检未完成：已检查 {checked_count}/{len(app_markets)} 个渠道"
        elif not_found_count == len(app_markets):
            coverage_state = "not_found"
            coverage_text = f"已检查 {len(app_markets)} 个渠道，均未发现该应用"
        else:
            coverage_state = "issue"
            parts = []
            if not_found_count:
                parts.append(f"{not_found_count} 个未发现")
            if issue_count:
                parts.append(f"{issue_count} 个查询异常或待确认")
            coverage_text = "未采集到版本：" + "，".join(parts)
        app_reports[item["id"]] = {
            "latest_version": latest_by_platform.get("android") or next(iter(latest_by_platform.values()), ""),
            "latest_by_platform": latest_by_platform,
            "platform_versions": [
                {"key": key, "name": platform_names.get(key, key), "version": value}
                for key, value in latest_by_platform.items()
            ],
            "versions": versions_by_platform,
            "online_count": len(online),
            "inconsistent": is_inconsistent,
            "inconsistent_platforms": inconsistent_platforms,
            "suspicious_count": len(suspicious_rows),
            "warning_count": len(warning_rows),
            "has_change": has_change,
            "checked_count": checked_count,
            "not_found_count": not_found_count,
            "issue_count": issue_count,
            "pending_count": pending_count,
            "coverage_state": coverage_state,
            "coverage_text": coverage_text,
            "platform": app_platform,
            "platform_name": platform_names.get(app_platform, app_platform),
            "market_count": len(app_markets),
        }
    collected = sum(
        1 for r in rows if r["status"] == "ok" and r["market_id"] in enabled_market_ids
        and market_platforms.get(r["market_id"]) == app_platforms.get(r["app_id"])
    )
    expected = sum(
        1 for item in apps for market in markets
        if market.get("platform", "android") == item.get("platform", "android")
    )
    return {
        "markets": markets,
        "apps": apps,
        "rows": rows,
        "result_map": result_map,
        "artifact_map": artifact_map,
        "artifact_capabilities": artifact_capabilities,
        "bindings": bindings,
        "auth_map": {k: v.get("authority", "unknown") for k, v in bindings.items()},
        "app_reports": app_reports,
        "stats": {
            "apps": len(apps), "markets": len(markets), "collected": collected,
            "expected": expected, "inconsistent": inconsistent,
            "suspicious": suspicious, "warnings": warnings, "changed": changed,
        },
    }


# ---------- 页面 ----------
@app.route("/")
def index():
    last_run = db.query("SELECT * FROM check_runs ORDER BY id DESC LIMIT 1", one=True)
    snapshot = _report_snapshot()
    return render_template("index.html", last_run=last_run, snapshot=snapshot,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@app.route("/wizard")
def wizard_page():
    return redirect(url_for("apps_page"))


@app.route("/results")
def results():
    snapshot = _report_snapshot()
    cfg = load_config()
    return render_template("results.html", cfg=cfg, **snapshot)


@app.get("/device/oppo-bridge")
def oppo_device_bridge():
    """Local USB bridge used to launch OPPO's official deep link from a browser."""
    package_name = (request.args.get("package_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
        return Response("无效的 Android 包名", status=400, mimetype="text/plain")
    # Android intent URI pins the handler to OPPO.  A plain market:// link can
    # open another installed store and contaminate the channel result.
    market_uri = (f"intent://details?id={package_name}#Intent;scheme=market;"
                  "package=com.heytap.market;end")
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OPPO 应用详情跳转</title>
<style>body{{font:16px system-ui;margin:48px 24px;color:#172033}}
a{{display:block;padding:16px;text-align:center;background:#1769e0;color:white;
text-decoration:none;border-radius:12px;font-weight:700}}</style></head>
<body><p>目标包名：{package_name}</p>
<a href="{market_uri}">打开 OPPO 软件商店</a></body></html>"""
    response = Response(page, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/config")
@app.route("/apps")
def apps_page():
    apps = [dict(a) for a in db.query("SELECT * FROM apps ORDER BY id")]
    markets = [dict(m) for m in _visible_markets()]
    bindings = [dict(b) for b in db.query(
        f"SELECT * FROM bindings WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS})", HIDDEN_MARKETS
    )]
    return render_template("apps.html", apps=apps, markets=markets,
                           bindings=bindings,
                           app_platforms={a.get("platform") or "android" for a in apps})


@app.route("/settings")
def settings_page():
    return redirect(url_for("apps_page"))


# ---------- App 管理 API ----------
@app.post("/api/apps")
def api_add_app():
    data = request.get_json(silent=True) or {}
    name = (data.get("app_name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "应用名称不能为空"}), 400
    package_name = (data.get("package_name") or "").strip()
    if package_name and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package_name):
        return jsonify({"ok": False, "msg": "Android 包名格式不正确"}), 400
    if package_name.lower().startswith("com.tencent.pcgame."):
        return jsonify({"ok": False, "msg": "这是应用宝电脑版容器条目，不是 Android App 包名"}), 400
    if package_name:
        exists = db.query("SELECT id FROM apps WHERE package_name=?", (package_name,), one=True)
        if exists:
            return jsonify({"ok": False, "msg": "该包名已在监测清单中"}), 409
    app_id = db.execute(
        """INSERT INTO apps
           (app_name, package_name, search_keywords, company_name, note, discovery_status)
           VALUES (?,?,?,?,?,?)""",
        (name, package_name,
         (data.get("search_keywords") or "").strip(),
         (data.get("company_name") or "").strip(),
         (data.get("note") or "").strip(),
         "manual" if package_name else "unresolved"),
    )
    return jsonify({"ok": True, "id": app_id})


@app.post("/api/discovery/search")
def api_discovery_search():
    from core.discovery import search_apps
    data = request.get_json(silent=True) or {}
    try:
        result = search_apps((data.get("query") or "").strip(),
                             timeout=load_config().get("http", {}).get("timeout", 15),
                             search_type=(data.get("search_type") or "app").strip())
    except ValueError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.post("/api/apps/from-discovery")
def api_add_discovered_app():
    """Persist a user-confirmed candidate and its per-market identifiers."""
    data = request.get_json(silent=True) or {}
    name = (data.get("app_name") or "").strip()
    platform = (data.get("platform") or "android").strip()
    package_name = (data.get("package_name") or "").strip()
    bundle_id = (data.get("bundle_id") or "").strip()
    ios_app_id = (data.get("ios_app_id") or "").strip()
    developer = (data.get("developer") or "").strip()
    query = (data.get("query") or "").strip()
    if not name or platform not in ("android", "ios"):
        return jsonify({"ok": False, "msg": "候选应用缺少有效名称或平台"}), 400
    if platform == "android" and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package_name):
        return jsonify({"ok": False, "msg": "Android 候选缺少有效包名"}), 400
    if platform == "android" and package_name.lower().startswith("com.tencent.pcgame."):
        return jsonify({"ok": False, "msg": "已拦截应用宝电脑版条目；它不是 Android App"}), 400
    if platform == "ios" and not (re.fullmatch(
            r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", bundle_id) or ios_app_id.isdigit()):
        return jsonify({"ok": False, "msg": "iOS 候选缺少有效 Bundle ID 或 App Store ID"}), 400

    if platform == "android":
        existing = db.query("SELECT id FROM apps WHERE platform='android' AND package_name=?",
                            (package_name,), one=True)
    else:
        existing = db.query(
            """SELECT id FROM apps WHERE platform='ios' AND
               ((ios_bundle_id<>'' AND ios_bundle_id=?) OR (ios_app_id<>'' AND ios_app_id=?))""",
            (bundle_id, ios_app_id), one=True,
        )
    resolved_pending = False
    if not existing:
        # 用户可能先手工录入了名称占位项。确认搜索候选时直接补全该项，
        # 避免清单中出现一个“待确认”和一个已确认的同名 App。
        existing = db.query(
            """SELECT id FROM apps
               WHERE app_name=? AND COALESCE(package_name,'')=''
                 AND COALESCE(ios_bundle_id,'')=''
               ORDER BY id LIMIT 1""",
            (name,), one=True,
        )
        resolved_pending = bool(existing)
    if existing:
        app_id = existing["id"]
        db.execute(
            """UPDATE apps SET app_name=?, platform=?, package_name=?, ios_bundle_id=?,
               ios_app_id=?, company_name=?, discovery_query=?,
               discovery_status='confirmed' WHERE id=?""",
            (name, platform, package_name, bundle_id, ios_app_id,
             developer, query, app_id),
        )
    else:
        app_id = db.execute(
            """INSERT INTO apps
               (app_name, platform, package_name, ios_bundle_id, ios_app_id,
                search_keywords, company_name, discovery_query, discovery_status)
               VALUES (?,?,?,?,?,?,?,?,'confirmed')""",
            (name, platform, package_name, bundle_id, ios_app_id,
             query, developer, query),
        )
    for match in data.get("matches") or []:
        market_id = str(match.get("market_id") or "").strip()
        market_app_id = str(match.get("market_app_id") or "").strip()
        if not db.query("SELECT id FROM markets WHERE id=?", (market_id,), one=True):
            continue
        db.execute(
            """INSERT INTO bindings (app_id, market_id, market_app_id)
               VALUES (?,?,?) ON CONFLICT(app_id,market_id) DO UPDATE SET
               market_app_id=excluded.market_app_id""",
            (app_id, market_id, market_app_id),
        )
    if platform == "ios" and ios_app_id:
        db.execute(
            """INSERT INTO bindings (app_id, market_id, market_app_id)
               VALUES (?,?,?) ON CONFLICT(app_id,market_id) DO UPDATE SET
               market_app_id=excluded.market_app_id""",
            (app_id, "appstore", ios_app_id),
        )
    return jsonify({"ok": True, "id": app_id, "existing": bool(existing),
                    "resolved_pending": resolved_pending})


@app.post("/api/apps/<int:app_id>/delete")
def api_delete_app(app_id):
    app_row = db.query("SELECT app_name FROM apps WHERE id=?", (app_id,), one=True)
    if not app_row:
        return jsonify({"ok": False, "msg": "监测对象不存在或已被移除"}), 404
    db.delete_app(app_id)
    return jsonify({"ok": True, "deleted": app_id})


@app.get("/api/apps/template")
def api_app_template():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["app_name", "package_name", "company_name", "search_keywords", "note"])
    w.writerow(["示例App", "com.example.app", "示例科技有限公司", "示例科技", "官方App"])
    data = "\ufeff" + buf.getvalue()  # BOM 兼容 Excel
    return send_file(io.BytesIO(data.encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name="app_template.csv")


@app.post("/api/apps/import")
def api_app_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "未上传文件"}), 400
    content = f.read().decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    count = 0
    for row in reader:
        name = (row.get("app_name") or "").strip()
        if not name:
            continue
        db.execute(
            """INSERT INTO apps
               (app_name, package_name, company_name, search_keywords, note, discovery_status)
               VALUES (?,?,?,?,?,?)""",
            (name, (row.get("package_name") or "").strip(),
             (row.get("company_name") or "").strip(),
             (row.get("search_keywords") or "").strip(),
             (row.get("note") or "").strip(),
             "manual" if (row.get("package_name") or "").strip() else "unresolved"),
        )
        count += 1
    return jsonify({"ok": True, "imported": count})


# ---------- 绑定管理 ----------
@app.post("/api/bindings")
def api_save_binding():
    data = request.get_json(silent=True) or {}
    app_id = data.get("app_id")
    market_id = data.get("market_id")
    market_app_id = (data.get("market_app_id") or "").strip()
    authority = (data.get("authority") or "unknown").strip()
    baseline_sha256 = (data.get("baseline_sha256") or "").strip()
    baseline_sig = (data.get("baseline_sig") or "").strip()
    if not app_id or not market_id:
        return jsonify({"ok": False, "msg": "参数缺失"}), 400
    if market_app_id or authority != "unknown" or baseline_sha256 or baseline_sig:
        db.execute(
            "INSERT INTO bindings (app_id, market_id, market_app_id, authority, baseline_sha256, baseline_sig) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(app_id, market_id) DO UPDATE SET "
            "market_app_id=excluded.market_app_id, authority=excluded.authority, "
            "baseline_sha256=excluded.baseline_sha256, baseline_sig=excluded.baseline_sig",
            (app_id, market_id, market_app_id, authority, baseline_sha256, baseline_sig),
        )
    else:
        db.execute("DELETE FROM bindings WHERE app_id=? AND market_id=?", (app_id, market_id))
    return jsonify({"ok": True})


@app.post("/api/bindings/authority")
def api_save_authority():
    """批量保存 App×渠道 权威性标记（巡检结果页使用）"""
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    for it in items:
        app_id, market_id = it.get("app_id"), it.get("market_id")
        authority = (it.get("authority") or "unknown").strip()
        if not app_id or not market_id or authority not in ("official", "third_party", "unknown"):
            continue
        db.execute(
            "INSERT INTO bindings (app_id, market_id, authority) VALUES (?,?,?) "
            "ON CONFLICT(app_id, market_id) DO UPDATE SET authority=excluded.authority",
            (app_id, market_id, authority),
        )
    return jsonify({"ok": True, "saved": len(items)})


# ---------- 环境检测（网页引导） ----------
@app.get("/api/env/check")
def api_env_check():
    from core.env_check import run_all
    results = run_all(load_config())
    by_name = {r["name"]: r for r in results}
    web_ready = by_name.get("Python 依赖", {}).get("status") == "ok"
    phone_required = ("Python 依赖", "ADB 工具", "USB 安卓测试机")
    phone_ready = all(by_name.get(name, {}).get("status") == "ok" for name in phone_required)
    profile = {}
    market_clients = []

    # A connected phone is only useful when at least one phone-side market
    # client is installed. Report the actual clients instead of mixing this
    # optional tool with desktop APK parsing dependencies.
    if phone_ready:
        try:
            from executors.device import DeviceExecutor
            from config import DEVICE_READY_MARKETS
            executor = DeviceExecutor()
            report = executor.compatibility_report()
            ready = report["ready"]
            installed = [item for item in report["markets"] if item["installed"]]
            missing = [item for item in report["markets"]
                       if item["market_id"] in DEVICE_READY_MARKETS and not item["installed"]]
            profile = report.get("device") or {}
            market_clients = report.get("markets") or []
            model = " ".join(filter(None, (profile.get("brand"), profile.get("model"))))
            message = (f"{model or 'Android 手机'} · Android {profile.get('android') or '未知'}；"
                       f"已安装市场客户端：{'、'.join(x['market_name'] for x in installed) or '无'}")
            has_market_client = bool(installed)
            results.append({
                "step": len(results) + 1,
                "name": "手机应用市场客户端",
                "status": "ok" if ready and has_market_client else "warn",
                "message": message,
                "actions": (["缺少：" + "、".join(x["market_name"] for x in missing)] if missing else [])
                           + ([] if has_market_client else [
                               "需要复核哪个手机渠道，就先在手机上安装该渠道的官方应用市场客户端"
                           ]),
            })
        except Exception as exc:
            results.append({
                "step": len(results) + 1,
                "name": "手机应用市场客户端",
                "status": "warn",
                "message": f"暂时无法读取已安装市场客户端：{exc}",
                "actions": ["重新连接手机后再次检测"],
            })
    return jsonify({"ok": True, "ready": web_ready, "web_ready": web_ready,
                    "phone_ready": phone_ready,
                    # Backward-compatible alias for old local pages.
                    "device_ready": phone_ready,
                    "device_profile": profile,
                    "market_clients": market_clients,
                    "steps": results})


# ---------- 报表导出 ----------
@app.get("/api/report/markdown")
def api_report_markdown():
    """生成 Markdown 巡检报表：各渠道版本信息 + 权威性标记"""
    snapshot = _report_snapshot()
    markets, apps = snapshot["markets"], snapshot["apps"]
    result_map, bindings = snapshot["result_map"], snapshot["bindings"]
    artifact_map = snapshot["artifact_map"]
    lines = ["# 应用市场发布巡检报表", "",
             f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for a in apps:
        app_platform = a["platform"] or "android"
        platform_label = {"android": "Android", "ios": "iOS"}.get(app_platform, app_platform)
        identifier = (a["ios_bundle_id"] or a["ios_app_id"]
                      if app_platform == "ios" else a["package_name"])
        lines.append(f"## {a['app_name']}（{platform_label}）")
        if identifier:
            lines.append(f"- 平台标识：`{identifier}`")
        lines.append("")
        lines.append("| 渠道 | 系统 | 版本 | 版本发布时间 | 开发者/发布者 | 运营者/主办者 | 监测状态 | 风险结论 | 发布标记 | 监测时间 | APK 大小 | SHA-256 | 证据 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for m in markets:
            if m["platform"] != app_platform:
                continue
            r = result_map.get((m["id"], a["id"]))
            artifact = artifact_map.get((m["id"], a["id"]))
            b = dict(bindings.get((a["id"], m["id"]), {}))
            auth = b.get("authority", "unknown")
            auth_txt = {"official": "已授权发布", "third_party": "非授权发布",
                        "unknown": "未确定"}.get(auth, "未确定")
            platform_txt = {"android": "Android", "ios": "iOS"}.get(
                m["platform"], m["platform"]
            )
            if r:
                ver = r["version_name"] or "-"
                st = {"ok": "已发现", "offline": "未发现",
                      "not_published": "未发现",
                      "web_limited": "查询受限",
                      "device_unavailable": "未完成：手机未连接",
                      "market_app_missing": "未完成：市场未安装",
                      "login_required": "未完成：需登录或授权", "need_review": "待确认",
                      "package_mismatch": "发现同名错包应用",
                      "need_adapt": "未完成：采集能力待接入", "error": "查询失败"}.get(r["status"], r["status"])
                developer = r["developer_name"] or "市场未提供"
                operator = r["operator_name"] or "市场未提供"
                published_at = r["published_at"] or "市场未提供"
                risk = {"critical": "严重风险", "high": "高风险",
                        "warning": "提醒", "info": "信息",
                        "none": "未发现直接异常"}.get(
                            r.get("computed_risk_level") or "none", "证据不足"
                        )
                risk_reason = (r.get("computed_risk_reason") or "").replace("|", "/")
                risk_text = f"{risk}：{risk_reason}" if risk_reason else risk
                size_text = (f"{artifact['file_size'] / 1048576:.2f} MB" if artifact else "-")
                sha_text = artifact["sha256"] if artifact else (r.get("apk_sha256") or "-")
                lines.append(
                    f"| {m['name']} | {platform_txt} | {ver} | {published_at} | {developer} | {operator} | "
                    f"{st} | {risk_text} | {auth_txt} | {r['checked_at'] or '-'} | {size_text} | "
                    f"`{sha_text}` | {r['detail'][:40]} |"
                )
            else:
                lines.append(f"| {m['name']} | {platform_txt} | - | - | - | - | 尚未巡检 | 证据不足 | {auth_txt} | - | - | - | |")
        lines.append("")
    text = "\n".join(lines)
    return send_file(io.BytesIO(text.encode("utf-8")), mimetype="text/markdown",
                     as_attachment=True, download_name=f"巡检报表_{datetime.now().strftime('%Y%m%d')}.md")


# ---------- 渠道 / 设置 ----------
@app.post("/api/markets")
def api_update_markets():
    data = request.get_json(silent=True) or {}
    items = data.get("markets") or []
    for it in items:
        mid = it.get("id")
        if not mid or mid in HIDDEN_MARKETS:
            continue
        sets, args = ["enabled=?"], [1 if it.get("enabled") else 0]
        if it.get("trust_level"):
            sets.append("trust_level=?")
            args.append(it["trust_level"])
        if it.get("name"):
            sets.append("name=?")
            args.append(it["name"])
        args.append(mid)
        db.execute(f"UPDATE markets SET {', '.join(sets)} WHERE id=?", args)
    return jsonify({"ok": True, "updated": len(items)})


@app.post("/api/settings")
def api_save_settings():
    return jsonify({
        "ok": False,
        "msg": "定时监测暂未启用；请在监测配置页手动开始巡检",
    }), 410


@app.get("/api/config")
def api_get_config():
    return jsonify({"scheduled_monitoring": False})


# ---------- 巡检 ----------
@app.post("/api/check/run")
def api_check_run():
    data = request.get_json(silent=True) or {}
    try:
        run_id = run_check(trigger="manual",
                           app_ids=data.get("app_ids"), market_ids=data.get("market_ids"))
    except RuntimeError as e:
        return jsonify({"ok": False, "msg": str(e)}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.get("/api/check/status/<int:run_id>")
def api_check_status(run_id):
    run = db.query("SELECT * FROM check_runs WHERE id=?", (run_id,), one=True)
    if not run:
        return jsonify({"ok": False, "msg": "任务不存在"}), 404
    summary = json.loads(run["summary_json"] or "{}")
    return jsonify({"ok": True, "run": dict(run), "summary": summary})


# ---------- B 路径（市场 App 自动化） ----------
@app.post("/api/check/b-run")
def api_check_b_run():
    data = request.get_json(silent=True) or {}
    market_id = data.get("market_id")
    app_id = data.get("app_id")
    do_download = bool(data.get("do_download"))
    if not market_id or not app_id:
        return jsonify({"ok": False, "msg": "缺少应用市场或监测对象参数"}), 400
    if do_download:
        return jsonify({
            "ok": False,
            "msg": ("已禁用手机端按名称自动下载，避免误下同名应用。"
                    "请使用报表中的‘获取并校验 APK’。"),
        }), 410
    try:
        run_id = run_b_check(market_id, app_id, do_download=do_download)
    except RuntimeError as e:
        return jsonify({"ok": False, "msg": str(e)}), 409
    return jsonify({"ok": True, "run_id": run_id})


# ---------- 官方网页安装包获取与校验 ----------
@app.post("/api/artifacts/start")
def api_artifact_start():
    from core.artifacts import start_artifact_download
    data = request.get_json(silent=True) or {}
    market_id = str(data.get("market_id") or "").strip()
    try:
        app_id = int(data.get("app_id"))
    except (TypeError, ValueError):
        app_id = 0
    if not market_id or not app_id:
        return jsonify({"ok": False, "msg": "缺少应用市场或监测对象参数"}), 400
    try:
        run_id = start_artifact_download(
            market_id, app_id, method=str(data.get("method") or "auto")
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.get("/api/artifacts/<int:artifact_id>/file")
def api_artifact_file(artifact_id):
    artifact = db.query("SELECT * FROM artifacts WHERE id=?", (artifact_id,), one=True)
    if not artifact:
        return jsonify({"ok": False, "msg": "安装包记录不存在"}), 404
    if artifact["risk_level"] in ("critical", "high"):
        return jsonify({"ok": False, "msg": "该安装包未通过安全校验，禁止下载"}), 403
    root = (BASE_DIR / "data" / "artifacts").resolve()
    local_path = Path(artifact["local_path"]).resolve()
    if root not in local_path.parents or not local_path.is_file():
        return jsonify({"ok": False, "msg": "安装包文件不存在"}), 404
    return send_file(local_path, as_attachment=True, download_name=artifact["file_name"])


# ---------- 截图留证 ----------
@app.get("/screenshots/<path:filename>")
def api_screenshot(filename):
    from flask import send_from_directory
    return send_from_directory(str(BASE_DIR / "data" / "screenshots"), filename)


@app.get("/api/results")
def api_results():
    markets = db.query(
        f"SELECT id, name, trust_level, platform FROM markets WHERE id NOT IN ({HIDDEN_MARKET_PARAMS}) ORDER BY sort_order",
        HIDDEN_MARKETS,
    )
    apps = db.query("""SELECT id, app_name, platform, package_name, ios_bundle_id,
                     ios_app_id, company_name, discovery_status FROM apps""")
    rows = db.query(
        f"SELECT * FROM results WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS})", HIDDEN_MARKETS
    )
    bindings = [dict(b) for b in db.query(
        f"SELECT app_id, market_id, authority FROM bindings WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS})",
        HIDDEN_MARKETS,
    )]
    return jsonify({
        "markets": [dict(m) for m in markets],
        "apps": [dict(a) for a in apps],
        "results": [dict(r) for r in rows],
        "bindings": bindings,
        "observations": [dict(x) for x in db.query(
            f"SELECT * FROM observations WHERE market_id NOT IN ({HIDDEN_MARKET_PARAMS}) "
            "ORDER BY id DESC LIMIT 200",
            HIDDEN_MARKETS,
        )],
    })


if __name__ == "__main__":
    port = int(os.environ.get("AMM_PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False)
