"""巡检流水线：遍历 启用渠道 × App 清单，调用采集器，写入结果。"""
import json
import re
import threading
import calendar
from datetime import datetime, timezone

import core.db as db
from collectors import get_collector
from config import load_config
from core.download_policy import decide_download
from core.version import version_key

_check_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_published_at(value) -> str:
    """将各市场的时间戳、ISO 时间和本地日期统一为 YYYY-MM-DD。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{10,13}", raw):
        stamp = int(raw)
        if len(raw) == 13:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""
    match = re.search(r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", raw)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)),
                            int(match.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    # 部分市场只提供相对时间（如 OPPO 的“1 个月前”），按当天推算近似日期。
    relative = re.search(r"(\d+)\s*(年前|个月前|月前|周前|天前|小时前)", raw)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        today = datetime.now()
        if unit == "年前":
            approx = today.replace(year=today.year - amount)
        elif unit in ("个月前", "月前"):
            month = today.month - amount
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            day = min(today.day, calendar.monthrange(year, month)[1])
            approx = today.replace(year=year, month=month, day=day)
        elif unit == "周前":
            from datetime import timedelta
            approx = today - timedelta(weeks=amount)
        elif unit == "天前":
            from datetime import timedelta
            approx = today - timedelta(days=amount)
        else:  # 小时前：按当天近似
            approx = today
        return approx.strftime("%Y-%m-%d")
    return ""


def _binding_app_id(market_id: str, app_id: int) -> str:
    row = db.query(
        "SELECT market_app_id FROM bindings WHERE market_id=? AND app_id=?",
        (market_id, app_id), one=True,
    )
    return row["market_app_id"] if row else ""


def run_check(trigger: str = "manual", app_ids=None, market_ids=None):
    """执行一轮巡检。app_ids/market_ids 为 None 表示全部启用项。
    返回 check_runs.id；巡检在后台线程执行，页面轮询进度。"""
    if not _check_lock.acquire(blocking=False):
        raise RuntimeError("已有巡检任务在执行中")
    run_id = db.execute(
        "INSERT INTO check_runs (trigger, started_at, status) VALUES (?,?, 'running')",
        (trigger, _now()),
    )

    def _worker():
        summary = {"total": 0, "completed": 0, "ok": 0, "offline": 0,
                   "not_published": 0, "limited": 0, "need_review": 0, "items": []}
        try:
            cfg = load_config()
            http_cfg = cfg.get("http", {})
            if app_ids:
                apps = db.query(
                    "SELECT * FROM apps WHERE id IN ({})".format(",".join("?" * len(app_ids))), app_ids,
                )
            else:
                # 定时/全量巡检跳过尚未确认具体身份的名称占位项。
                apps = db.query(
                    """SELECT * FROM apps
                       WHERE (platform='android' AND (COALESCE(package_name,'')<>''
                              OR COALESCE(search_keywords,'')<>''))
                          OR (platform='ios' AND (COALESCE(ios_bundle_id,'')<>''
                              OR COALESCE(ios_app_id,'')<>''))"""
                )
            markets = db.query("SELECT * FROM markets WHERE enabled=1 ORDER BY sort_order")
            if market_ids:
                markets = [m for m in markets if m["id"] in market_ids]

            summary["total"] = sum(
                1 for m in markets for app in apps if m["platform"] == app["platform"]
            )
            for m in markets:
                collector = get_collector(m["adapter"])
                if collector is None:
                    continue
                for app in apps:
                    if m["platform"] != app["platform"]:
                        continue
                    res = None
                    try:
                        market_app_id = _binding_app_id(m["id"], app["id"]) or m["app_id"]
                        if app["platform"] == "ios" and not market_app_id:
                            market_app_id = app["ios_app_id"] or ""
                        package_name = (app["package_name"] or app["search_keywords"]
                                        if app["platform"] == "android"
                                        else app["ios_bundle_id"])
                        collect_kwargs = {
                            "package_name": package_name,
                            "market_app_id": market_app_id,
                            "app_name": app["app_name"],
                            "company_name": app["company_name"],
                            "timeout": http_cfg.get("timeout", 15),
                        }
                        if m["id"] == "baidu" and app["platform"] == "android":
                            # 百度在本地巡检中以已授权的 Android 客户端为主；
                            # 只有客户端不存在或无法取得详情时才回落网页。
                            # 这样网页节点抖动不会覆盖模拟器现场证据。
                            from collectors.device_markets import get_device_fallback_collector
                            from executors.device import DeviceExecutor
                            device_report = DeviceExecutor().compatibility_report(["baidu"])
                            baidu_market = next(
                                (item for item in device_report.get("markets", [])
                                 if item.get("market_id") == "baidu"), {}
                            )
                            device_res = None
                            if device_report.get("ready") and baidu_market.get("installed"):
                                device_collector = get_device_fallback_collector("baidu")
                                device_res = device_collector.collect(**collect_kwargs) if device_collector else None
                            if device_res and device_res.status == "ok" and device_res.version_name:
                                device_res.detail = (
                                    "已使用已安装并完成初始化的百度手机助手客户端"
                                    f"按包名复核：版本 {device_res.version_name}。"
                                    "该结果来自 Android 测试设备现场。"
                                )
                                device_res.extra = dict(device_res.extra or {})
                                device_res.extra["evidence_scope"] = "baidu_android_client_primary"
                                res = device_res
                            else:
                                web_res = collector.collect(**collect_kwargs)
                                if device_res and device_res.detail:
                                    web_res.detail = (
                                        "百度客户端优先复核未完成，已使用官方网页备用："
                                        + device_res.detail + "；" + (web_res.detail or "")
                                    )
                                res = web_res
                        else:
                            res = collector.collect(**collect_kwargs)
                    except Exception as e:  # 采集器自身异常兜底
                        from collectors import CollectResult, ST_ERROR
                        res = CollectResult(status=ST_ERROR, detail=f"异常: {e!r}")

                    status = res.status
                    previous = db.query(
                        "SELECT version_name FROM results WHERE market_id=? AND app_id=?",
                        (m["id"], app["id"]), one=True,
                    )
                    previous_version = previous["version_name"] if previous else ""
                    binding = db.query(
                        "SELECT authority FROM bindings WHERE market_id=? AND app_id=?",
                        (m["id"], app["id"]), one=True,
                    )
                    authority = binding["authority"] if binding else "unknown"
                    if m["platform"] == "android":
                        action, reason = decide_download(
                            status, res.version_name, previous_version, authority,
                            app["download_policy"] or "on_change",
                        )
                    else:
                        action, reason = "not_needed", "非 Android 渠道不执行 APK 下载校验"
                    screenshot = str((res.extra or {}).get("screenshot") or "")
                    screenshot_url = ("/screenshots/" + screenshot.replace("\\", "/").split("/")[-1]
                                      if screenshot else "")
                    developer_name = str((res.extra or {}).get("developer") or "").strip()
                    operator_name = str((res.extra or {}).get("operator") or "").strip()
                    source_url = str((res.extra or {}).get("source_url") or "").strip()
                    download_url = str((res.extra or {}).get("download_url") or "").strip()
                    try:
                        declared_size = int((res.extra or {}).get("declared_size") or 0)
                    except (TypeError, ValueError):
                        declared_size = 0
                    observed_package = str(
                        (res.extra or {}).get("observed_package") or ""
                    ).strip()
                    if status == "package_mismatch":
                        risk_level = "critical"
                        risk_reason = "发现同名应用但包名与监测对象不一致，疑似仿冒或错误条目"
                    elif authority == "third_party" and status == "ok":
                        risk_level = "high"
                        risk_reason = "非授权渠道发现该应用，疑似未授权发布，需核验安装包签名"
                    elif authority == "third_party":
                        # 非授权渠道未检测到该应用版本，不构成发布风险
                        risk_level, risk_reason = "none", ""
                    else:
                        risk_level, risk_reason = "none", ""
                    published_at = normalize_published_at(
                        (res.extra or {}).get("published_at")
                        or (res.extra or {}).get("updated_at")
                        or (res.extra or {}).get("release_date")
                    )
                    if res.status == "ok":
                        summary["ok"] += 1
                    elif res.status == "offline":
                        summary["offline"] += 1
                    elif res.status == "not_published":
                        summary["not_published"] += 1
                    elif res.status == "web_limited":
                        summary["limited"] += 1
                    else:
                        summary["need_review"] += 1
                    summary["items"].append({
                        "market": m["id"], "app": app["id"], "status": status,
                        "version": res.version_name, "detail": res.detail,
                        "developer": developer_name, "operator": operator_name,
                        "published_at": published_at,
                        "source_url": source_url, "download_url": bool(download_url),
                        "declared_size": declared_size, "observed_package": observed_package,
                        "risk_level": risk_level, "risk_reason": risk_reason,
                        "download_action": action, "screenshot_url": screenshot_url,
                    })
                    db.execute(
                        """INSERT INTO results (market_id, app_id, version_name, version_code,
                           developer_name, operator_name, status, detail, screenshot_url, checked_at,
                           previous_version, version_changed, download_action, download_reason,
                           published_at, source_url, download_url, declared_size,
                           observed_package, risk_level, risk_reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(market_id, app_id) DO UPDATE SET
                           version_name=excluded.version_name, version_code=excluded.version_code,
                           developer_name=excluded.developer_name,
                           operator_name=excluded.operator_name,
                           status=excluded.status, detail=excluded.detail,
                           screenshot_url=excluded.screenshot_url, checked_at=excluded.checked_at,
                           previous_version=excluded.previous_version,
                           version_changed=excluded.version_changed,
                           download_action=excluded.download_action,
                           download_reason=excluded.download_reason,
                           published_at=excluded.published_at,
                           source_url=excluded.source_url, download_url=excluded.download_url,
                           declared_size=excluded.declared_size,
                           observed_package=excluded.observed_package,
                           risk_level=excluded.risk_level, risk_reason=excluded.risk_reason""",
                        (m["id"], app["id"], res.version_name, res.version_code,
                         developer_name, operator_name, status,
                         res.detail[:500], screenshot_url, _now(), previous_version,
                         int(bool(status == "ok" and previous_version and
                                  previous_version != res.version_name)),
                         action, reason, published_at, source_url, download_url,
                         declared_size, observed_package, risk_level, risk_reason),
                    )
                    db.execute(
                        """INSERT INTO observations
                           (run_id, market_id, app_id, version_name, version_code,
                            developer_name, operator_name, status, detail, published_at, checked_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, m["id"], app["id"], res.version_name, res.version_code,
                         developer_name, operator_name, status, res.detail[:500],
                         published_at, _now()),
                    )
                    summary["completed"] += 1
                    db.execute("UPDATE check_runs SET summary_json=? WHERE id=?",
                               (json.dumps(summary, ensure_ascii=False), run_id))

            # 只比较同一操作系统内的版本。iOS 与 Android 的版本编号
            # 本来就可能不同，不能互相判定为落后。
            for app in apps:
                platform_rows = db.query(
                    """SELECT m.platform, r.version_name FROM results r
                       JOIN markets m ON m.id=r.market_id
                       WHERE r.app_id=? AND r.status='ok' AND m.enabled=1 AND r.version_name<>''""",
                    (app["id"],),
                )
                grouped = {}
                for row in platform_rows:
                    grouped.setdefault(row["platform"], set()).add(row["version_name"])
                for platform, versions in grouped.items():
                    if len(versions) <= 1:
                        continue
                    latest_version = max(versions, key=version_key)
                    db.execute(
                        """UPDATE results SET
                           download_action=CASE WHEN download_action='required' THEN 'required' ELSE 'recommended' END,
                           download_reason='该渠道版本低于同平台最新版本，建议获取安装包自动校验',
                           risk_level=CASE WHEN risk_level IN ('critical','high') THEN risk_level ELSE 'warning' END,
                           risk_reason=CASE WHEN risk_level IN ('critical','high') THEN risk_reason
                               ELSE '该渠道版本低于同平台其他渠道，可能是发布延迟或异常旧版本' END
                           WHERE app_id=? AND status='ok' AND version_name<>? AND market_id IN
                           (SELECT id FROM markets WHERE platform=?)""",
                        (app["id"], latest_version, platform),
                    )
            final_status = "done" if summary["need_review"] == 0 else "partial"
            db.execute(
                "UPDATE check_runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
                (_now(), final_status, json.dumps(summary, ensure_ascii=False), run_id),
            )
        except Exception as exc:
            summary["error"] = f"{type(exc).__name__}: {exc}"
            db.execute(
                "UPDATE check_runs SET finished_at=?, status='failed', summary_json=? WHERE id=?",
                (_now(), json.dumps(summary, ensure_ascii=False), run_id),
            )
        finally:
            _check_lock.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return run_id


def run_b_check(market_id: str, app_id: int, do_download: bool = False):
    """B 路径单渠道检测：市场 App 读取版本并截图。

    ``do_download`` 只为兼容旧调用保留；DeviceExecutor 会安全拒绝设备端盲搜下载。
    安装包获取统一由 core.artifacts 处理。
    """
    if not _check_lock.acquire(blocking=False):
        raise RuntimeError("已有巡检任务在执行中")
    run_id = db.execute(
        "INSERT INTO check_runs (trigger, started_at, status) VALUES ('b_path', ?, 'running')",
        (_now(),),
    )

    def _worker():
        try:
            from executors.device import DeviceExecutor
            market = db.query("SELECT * FROM markets WHERE id=?", (market_id,), one=True)
            app = db.query("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
            b_row = db.query("SELECT * FROM bindings WHERE market_id=? AND app_id=?",
                             (market_id, app_id), one=True)
            b = dict(b_row) if b_row else {}
            summary = {"total": 1, "completed": 0, "items": []}
            if not market or not app:
                summary["items"].append({"status": "error", "detail": "渠道或 App 不存在"})
            else:
                ex = DeviceExecutor()
                ok, msg = ex.check_ready()
                if not ok:
                    summary["items"].append({"market": market_id, "app": app_id,
                                             "status": "device_unavailable", "detail": msg})
                else:
                    pkg = app["package_name"] or ""
                    if not pkg:
                        raise ValueError("该 App 尚未确认 Android 包名，不能执行真机下载")
                    shot_dir = "data/screenshots"
                    if do_download:
                        res = ex.download_and_verify(
                            market_id=market_id, package_name=pkg,
                            baseline_sha256=b.get("baseline_sha256", ""),
                            baseline_sig=b.get("baseline_sig", ""),
                            screenshot_dir=shot_dir, app_name=app["app_name"])
                    else:
                        res = ex.inspect_market_detail(
                            market_id=market_id, package_name=pkg,
                            screenshot_dir=shot_dir, app_name=app["app_name"])
                        if (market_id == "honor" and
                                res.get("status") == "region_unavailable"):
                            # Keep the single-channel retry useful on a generic
                            # emulator: use the same transparent official
                            # AppGallery fallback as the main巡检 path.
                            try:
                                from collectors.huawei import HuaweiCollector
                                fallback = HuaweiCollector().collect(
                                    package_name=pkg, app_name=app["app_name"])
                            except Exception:
                                fallback = None
                            if fallback and fallback.status == "ok" and fallback.version_name:
                                extra = dict(fallback.extra or {})
                                extra.update({
                                    # This is the Honor client's unavailable
                                    # page, not proof of the Huawei fallback.
                                    # Keep it out of the primary evidence link.
                                    "screenshot": "",
                                    "device_screenshot": res.get("screenshot", ""),
                                    "evidence_scope": "huawei_appgallery_fallback",
                                    "device_detail": res.get("detail", ""),
                                })
                                res = {
                                    "ok": True,
                                    "status": "fallback_ok",
                                    "version": fallback.version_name,
                                    "version_code": fallback.version_code,
                                    "detail": ("荣耀客户端因当前模拟器地区不可用；已取得华为/荣耀官方 "
                                                "AppGallery 网页替代证据，需在荣耀认证环境复核"),
                                    **extra,
                                }
                    status = res.get("status") or (
                        "ok" if res.get("ok", res.get("version_name") or res.get("sha256"))
                        else "need_review"
                    )
                    verify = res.get("verify_result", "n/a")
                    if res.get("detail"):
                        detail = res["detail"]
                    elif res.get("version"):
                        detail = f"版本 {res['version']}"
                    else:
                        detail = "已完成"
                    # 手机复核失败不能覆盖已有的可信网页版本；只有确实取得
                    # 安装包或详情版本后才合并新的校验证据。
                    if status in ("ok", "fallback_ok"):
                        db.execute(
                            """INSERT INTO results (market_id, app_id, version_name,
                               developer_name, operator_name, status, apk_sha256, sig_fingerprint,
                               verify_result, screenshot_url, detail, published_at, source_url, checked_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(market_id, app_id) DO UPDATE SET
                               version_name=COALESCE(NULLIF(excluded.version_name,''),results.version_name),
                               developer_name=COALESCE(NULLIF(excluded.developer_name,''),results.developer_name),
                               operator_name=COALESCE(NULLIF(excluded.operator_name,''),results.operator_name),
                               apk_sha256=COALESCE(NULLIF(excluded.apk_sha256,''),results.apk_sha256),
                               sig_fingerprint=COALESCE(NULLIF(excluded.sig_fingerprint,''),results.sig_fingerprint),
                               verify_result=excluded.verify_result,
                               status=excluded.status,
                               screenshot_url=CASE WHEN excluded.status='fallback_ok' THEN ''
                                                   ELSE COALESCE(NULLIF(excluded.screenshot_url,''),results.screenshot_url) END,
                               detail=excluded.detail,
                               published_at=COALESCE(NULLIF(excluded.published_at,''),results.published_at),
                               source_url=COALESCE(NULLIF(excluded.source_url,''),results.source_url),
                               checked_at=excluded.checked_at""",
                            (market_id, app_id,
                             res.get("version_name") or res.get("version", ""),
                             res.get("developer", ""), res.get("operator", ""), status,
                             res.get("sha256", ""), res.get("sig", ""), verify,
                             ("/screenshots/" + str(res.get("screenshot", "")).split("/")[-1])
                             if res.get("screenshot") else "",
                             str(detail)[:500], normalize_published_at(res.get("published_at")),
                             str(res.get("source_url") or ""), _now()),
                        )
                    else:
                        # A failed device recheck must still be visible in the
                        # report (especially region_unavailable).  Preserve a
                        # previously trusted web version, but update the latest
                        # device status, detail and screenshot instead of leaving
                        # a stale generic need_review state behind.
                        existing = db.query(
                            "SELECT id FROM results WHERE market_id=? AND app_id=?",
                            (market_id, app_id), one=True,
                        )
                        shot_url = ("/screenshots/" + str(res.get("screenshot", "")).split("/")[-1]
                                    if res.get("screenshot") else "")
                        if existing:
                            db.execute(
                                """UPDATE results SET status=?, detail=?,
                                   screenshot_url=COALESCE(NULLIF(?,''), screenshot_url),
                                   checked_at=? WHERE market_id=? AND app_id=?""",
                                (status, str(detail)[:500], shot_url, _now(), market_id, app_id),
                            )
                        else:
                            db.execute(
                                """INSERT INTO results (market_id, app_id, status,
                                   verify_result, screenshot_url, detail, checked_at)
                                   VALUES (?,?,?,?,?,?,?)""",
                                (market_id, app_id, status, verify, shot_url,
                                 str(detail)[:500], _now()),
                            )
                    summary["items"].append({
                        "market": market_id, "app": app_id, "status": status,
                        "version": res.get("version_name") or res.get("version", ""),
                        "verify": verify, "detail": str(detail)[:120],
                    })
            summary["completed"] = 1
            final_status = ("done" if summary["items"] and
                            all(x.get("status") == "ok" for x in summary["items"])
                            else "partial")
            db.execute(
                "UPDATE check_runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
                (_now(), final_status, json.dumps(summary, ensure_ascii=False), run_id),
            )
        except Exception as exc:
            db.execute(
                "UPDATE check_runs SET finished_at=?, status='failed', summary_json=? WHERE id=?",
                (_now(), json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), run_id),
            )
        finally:
            _check_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return run_id
