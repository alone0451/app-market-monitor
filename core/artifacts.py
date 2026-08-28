"""从市场官方网页下载安装包，并在安装前完成身份校验。"""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

import core.db as db
from collectors import get_collector
from config import BASE_DIR, load_config
from core import apk_verify


_artifact_lock = threading.Lock()
_ALLOWED_HOST_SUFFIXES = {
    "yyb": ("dd.qq.com",),
    "baidu": ("gdown.baidu.com",),
    "qihu360": ("qihucdn.com",),
}
_MAX_BYTES = 500 * 1024 * 1024
_SEGMENT_THRESHOLD = 16 * 1024 * 1024
_SEGMENT_WORKERS = 4


class _RangeUnsupported(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _allowed_url(market_id: str, value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _ALLOWED_HOST_SUFFIXES.get(market_id, ())
    )


def _download_probe(client: httpx.Client, market_id: str, url: str) -> tuple[str, int, bool]:
    """Read size/range support without trusting redirects outside the allowlist."""
    try:
        response = client.head(url)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return url, 0, False
    final_url = str(response.url)
    if not _allowed_url(market_id, final_url):
        raise ValueError("下载重定向离开官方域名，已终止")
    try:
        total = int(response.headers.get("content-length") or 0)
    except ValueError:
        total = 0
    if total > _MAX_BYTES:
        raise ValueError("安装包超过 500MB 安全上限")
    ranges = "bytes" in str(response.headers.get("accept-ranges") or "").lower()
    return final_url, total, ranges


def _stream_range(market_id: str, url: str, path: Path, start: int, end: int,
                  timeout: int, on_bytes) -> int:
    wanted = end - start + 1
    existing = path.stat().st_size if path.is_file() else 0
    if existing > wanted:
        path.unlink()
        existing = 0
    if existing == wanted:
        return existing
    request_start = start + existing
    headers = {"User-Agent": "Mozilla/5.0", "Range": f"bytes={request_start}-{end}"}
    with httpx.stream("GET", url, headers=headers, timeout=timeout,
                      follow_redirects=True) as response:
        if response.status_code != 206:
            raise _RangeUnsupported(f"服务器未返回分段响应（HTTP {response.status_code}）")
        response.raise_for_status()
        if not _allowed_url(market_id, str(response.url)):
            raise ValueError("下载重定向离开官方域名，已终止")
        content_range = str(response.headers.get("content-range") or "")
        if not content_range.lower().startswith(f"bytes {request_start}-"):
            raise _RangeUnsupported("服务器返回的分段范围与请求不一致")
        with open(path, "ab") as output:
            for chunk in response.iter_bytes(1 << 20):
                if not chunk:
                    continue
                existing += len(chunk)
                if existing > wanted:
                    raise ValueError("服务器返回的分段数据超过预期大小")
                output.write(chunk)
                on_bytes(len(chunk))
    if existing != wanted:
        raise httpx.ReadError(f"分段下载不完整：{existing}/{wanted} 字节")
    return existing


def _download_with_resume(market_id: str, url: str, temp_path: Path, timeout: int,
                          on_progress=None) -> tuple[int, int, str]:
    """Download with safe redirect checks, four-way ranges and resumable parts."""
    timeout = max(30, int(timeout or 30))
    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=timeout,
    )
    try:
        final_url, total, supports_ranges = _download_probe(client, market_id, url)
    finally:
        client.close()

    def report(done: int, expected: int, mode: str):
        if on_progress:
            on_progress(done, expected, mode)

    if supports_ranges and total >= _SEGMENT_THRESHOLD:
        workers = min(_SEGMENT_WORKERS, max(1, total // _SEGMENT_THRESHOLD + 1))
        segment_size = (total + workers - 1) // workers
        specs = []
        for index in range(workers):
            start = index * segment_size
            end = min(total - 1, start + segment_size - 1)
            if start <= end:
                specs.append((temp_path.with_name(temp_path.name + f".seg{index}"), start, end))
        progress_lock = threading.Lock()
        downloaded = sum(
            min(path.stat().st_size, end - start + 1) if path.is_file() else 0
            for path, start, end in specs
        )
        report(downloaded, total, "parallel")

        def add_progress(count: int):
            nonlocal downloaded
            with progress_lock:
                downloaded += count
                report(downloaded, total, "parallel")

        try:
            with ThreadPoolExecutor(max_workers=len(specs),
                                    thread_name_prefix="apk-segment") as pool:
                futures = [
                    pool.submit(_stream_range, market_id, final_url, path, start, end,
                                timeout, add_progress)
                    for path, start, end in specs
                ]
                for future in futures:
                    future.result()
        except _RangeUnsupported:
            for path, _, _ in specs:
                path.unlink(missing_ok=True)
        else:
            with open(temp_path, "wb") as output:
                for path, _, _ in specs:
                    with open(path, "rb") as source:
                        while True:
                            chunk = source.read(1 << 20)
                            if not chunk:
                                break
                            output.write(chunk)
                    path.unlink(missing_ok=True)
            if temp_path.stat().st_size != total:
                raise httpx.ReadError("分段合并后的安装包大小不完整")
            report(total, total, "parallel")
            return total, total, "parallel"

    existing = temp_path.stat().st_size if temp_path.is_file() else 0
    if total and existing > total:
        temp_path.unlink()
        existing = 0
    headers = {"User-Agent": "Mozilla/5.0"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    with httpx.stream("GET", final_url, headers=headers, timeout=timeout,
                      follow_redirects=True) as response:
        if existing and response.status_code == 416 and total and existing == total:
            report(existing, total, "resume")
            return existing, total, "resume"
        response.raise_for_status()
        if not _allowed_url(market_id, str(response.url)):
            raise ValueError("下载重定向离开官方域名，已终止")
        append = bool(existing and response.status_code == 206)
        if existing and not append:
            existing = 0
        content_range = str(response.headers.get("content-range") or "")
        range_total = 0
        if "/" in content_range:
            try:
                range_total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                range_total = 0
        try:
            content_length = int(response.headers.get("content-length") or 0)
        except ValueError:
            content_length = 0
        expected = total or range_total or (existing + content_length)
        if expected > _MAX_BYTES:
            raise ValueError("安装包超过 500MB 安全上限")
        size = existing
        report(size, expected, "resume" if append else "single")
        with open(temp_path, "ab" if append else "wb") as output:
            for chunk in response.iter_bytes(1 << 20):
                if not chunk:
                    continue
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise ValueError("安装包超过 500MB 安全上限")
                output.write(chunk)
                report(size, expected, "resume" if append else "single")
    if expected and size != expected:
        raise httpx.ReadError(f"安装包下载不完整：{size}/{expected} 字节")
    return size, expected, "resume" if append else "single"


def _fresh_collect(app, market, timeout: int):
    collector = get_collector(market["adapter"])
    if not collector:
        return None
    binding = db.query(
        "SELECT market_app_id FROM bindings WHERE app_id=? AND market_id=?",
        (app["id"], market["id"]), one=True,
    )
    return collector.collect(
        package_name=app["package_name"],
        market_app_id=(binding["market_app_id"] if binding else ""),
        app_name=app["app_name"], company_name=app["company_name"], timeout=timeout,
    )


def _find_cached_artifact(app, market_id: str, collected):
    """复用同渠道、同包名、同版本的已校验制品。

    读取前重新计算 SHA-256，避免本地文件被替换后仍当作缓存使用。
    """
    expected_version = (
        collected.version_name
        if collected and collected.status == "ok" and collected.version_name else ""
    )
    if not expected_version:
        last_result = db.query(
            """SELECT version_name FROM results
               WHERE app_id=? AND market_id=? AND status='ok' AND version_name<>''""",
            (app["id"], market_id), one=True,
        )
        expected_version = last_result["version_name"] if last_result else ""
    if not expected_version:
        return None
    artifact = db.query(
        """SELECT * FROM artifacts
           WHERE app_id=? AND market_id=? AND package_name=? AND version_name=?
             AND risk_level NOT IN ('critical','high') AND sha256<>''
           ORDER BY id DESC LIMIT 1""",
        (app["id"], market_id, app["package_name"], expected_version), one=True,
    )
    if not artifact:
        return None
    path = Path(artifact["local_path"])
    if not path.is_file() or path.stat().st_size != artifact["file_size"]:
        return None
    if apk_verify.sha256(path).lower() != artifact["sha256"].lower():
        return None
    return artifact


def _device_extract_artifact(app, market, save_dir) -> dict:
    """无网页直链时，从已连接 Android 测试设备提取 APK 并完成身份校验。

    只提取、不自动安装；包名、版本、签名与监测目标不一致时按风险处理，
    与网页直链下载共用同一套 apk_verify 校验口径。
    """
    try:
        from executors.device import DeviceExecutor
        executor = DeviceExecutor()
        ok, message = executor.check_ready()
        serial = executor.dev.serial
        if not ok or not serial:
            return {"status": "device_unavailable",
                    "detail": message or "未检测到可用设备"}
        device_kind = "模拟器" if str(serial).startswith("emulator-") else "实体手机"
        path_out = executor.dev.shell(f"pm path {app['package_name']}")
        if "package:" not in path_out:
            return {
                "status": "app_not_installed",
                "detail": (f"{device_kind}已连接（{serial}），但未安装目标应用 {app['package_name']}，"
                           "无法从该设备提取；请先通过官方渠道安装后再试。"),
            }
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        pulled = executor.dev.extract_apk(app["package_name"], str(save_dir))
        if not pulled or not Path(pulled).is_file():
            return {"status": "extract_failed",
                    "detail": "从 Android 测试设备提取 APK 失败，请检查 ADB 连接后重试"}
        file_name = (f"app{app['id']}_{market['id']}_"
                     f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.apk")
        local_path = save_dir / file_name
        Path(pulled).replace(local_path)
        size = Path(local_path).stat().st_size
        baseline = db.query(
            """SELECT sig_fingerprint FROM artifacts
               WHERE app_id=? AND package_name=? AND sig_fingerprint<>''
                 AND risk_level NOT IN ('critical','high')
               ORDER BY id DESC LIMIT 1""",
            (app["id"], app["package_name"]), one=True,
        )
        verified = apk_verify.verify(
            local_path,
            baseline_sig=(baseline["sig_fingerprint"] if baseline else ""),
            expected_package=app["package_name"],
        )
        actual_package = verified.get("package") or ""
        actual_version = verified.get("version_name") or ""
        market_version = db.query(
            """SELECT version_name FROM results
               WHERE app_id=? AND market_id=? AND status='ok' AND version_name<>''
               ORDER BY id DESC LIMIT 1""",
            (app["id"], market["id"]), one=True,
        )
        market_version = market_version["version_name"] if market_version else ""
        risk_level = "info"
        verify_result = verified["verify_result"]
        if not actual_package:
            risk_level = "high"
            conclusion = "提取文件不是可解析的 APK，禁止安装"
        elif actual_package != app["package_name"]:
            risk_level = "critical"
            verify_result = "diff"
            conclusion = (f"包名不一致：期望 {app['package_name']}，实际 {actual_package}。"
                          "疑似错误提取或仿冒包，禁止安装")
        elif not verified.get("sig"):
            risk_level = "high"
            verify_result = "need_review"
            conclusion = "未能读取 APK 签名证书，无法证明发布者身份，禁止安装"
        elif baseline and verified["verify_result"] == "diff":
            risk_level = "critical"
            conclusion = "签名与此前同包名可信制品不一致，疑似非官方签名，禁止安装"
        else:
            conclusion = (f"已从{device_kind}提取已安装应用：包名与监测目标一致，SHA-256 和签名已记录。"
                          + ("签名与已有制品一致" if baseline else "暂无签名基线，未发现直接冒用证据"))
            if market_version and actual_version and market_version != actual_version:
                risk_level = "warning"
                conclusion += (f" 手机安装版本 {actual_version} 与市场页面版本 {market_version} "
                               "不同，请核实是否为内部版本或异常包。")
        artifact_id = db.execute(
            """INSERT INTO artifacts
               (app_id,market_id,platform,file_name,local_path,source_url,file_size,
                sha256,package_name,version_name,version_code,sig_fingerprint,
                verify_result,risk_level,conclusion)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (app["id"], market["id"], "android", file_name, str(local_path), "",
             size, verified["sha256"], actual_package, actual_version,
             verified.get("version_code", ""), verified.get("sig", ""),
             verify_result, risk_level, conclusion),
        )
        db.execute(
            """UPDATE results SET apk_sha256=?,sig_fingerprint=?,verify_result=?,
               risk_level=?,risk_reason=?,source_url=?,download_url=?,declared_size=?
               WHERE app_id=? AND market_id=?""",
            (verified["sha256"], verified.get("sig", ""), verify_result,
             risk_level, conclusion, "", "", 0, app["id"], market["id"]),
        )
        return {
            "status": "ok" if risk_level in ("none", "info") else "risk",
            "artifact_id": artifact_id, "risk_level": risk_level,
            "detail": conclusion, "file_size": size,
            "sha256": verified["sha256"], "package_name": actual_package,
            "version_name": actual_version,
            "download_file_url": (f"/api/artifacts/{artifact_id}/file"
                                  if risk_level not in ("critical", "high") else ""),
        }
    except Exception as exc:
        return {"status": "error",
                "detail": f"从 Android 测试设备提取 APK 失败：{type(exc).__name__}: {exc}"}


def start_artifact_download(market_id: str, app_id: int, method: str = "auto") -> int:
    method = str(method or "auto").strip().lower()
    if method not in ("auto", "direct", "device"):
        raise RuntimeError("未知的安装包获取方式")
    if not _artifact_lock.acquire(blocking=False):
        raise RuntimeError("已有安装包下载或校验任务在执行中")
    run_id = db.execute(
        "INSERT INTO check_runs (trigger,started_at,status) VALUES ('artifact',?,'running')",
        (_now(),),
    )

    def worker():
        summary = {"total": 1, "completed": 0, "market_id": market_id,
                   "app_id": app_id,
                   "save_dir": str(BASE_DIR / "data" / "artifacts"), "items": []}
        db.execute("UPDATE check_runs SET summary_json=? WHERE id=?",
                   (json.dumps(summary, ensure_ascii=False), run_id))
        try:
            app = db.query("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
            market = db.query("SELECT * FROM markets WHERE id=?", (market_id,), one=True)
            if not app or not market:
                raise ValueError("监测对象或应用市场不存在")
            if app["platform"] != "android" or market["platform"] != "android":
                summary["items"].append({
                    "status": "ios_official_link_only",
                    "detail": "Apple App Store 不公开 IPA 安装包；只能使用 Apple 官方安装页面。",
                })
            else:
                timeout = load_config().get("http", {}).get("timeout", 15)
                collected = _fresh_collect(app, market, timeout)
                extra = (collected.extra if collected else {}) or {}
                download_url = str(extra.get("download_url") or "").strip()
                source_url = str(extra.get("source_url") or "").strip()
                declared_size = int(extra.get("declared_size") or 0)
                stored = db.query(
                    "SELECT download_url,source_url,declared_size FROM results "
                    "WHERE app_id=? AND market_id=?", (app_id, market_id), one=True,
                )
                if method == "direct" and not download_url and stored:
                    download_url = str(stored["download_url"] or "").strip()
                    source_url = source_url or str(stored["source_url"] or "").strip()
                    declared_size = declared_size or int(stored["declared_size"] or 0)
                cached = _find_cached_artifact(app, market_id, collected)
                if collected and collected.status == "package_mismatch":
                    observed = str(extra.get("observed_package") or "未知")
                    summary["items"].append({
                        "status": "risk", "risk_level": "critical",
                        "detail": (f"市场返回了同名应用，但包名为 {observed}，"
                                   f"监测目标为 {app['package_name']}。疑似仿冒或错误条目，已禁止下载。"),
                    })
                elif cached:
                    summary["items"].append({
                        "status": "ok", "artifact_id": cached["id"], "cached": True,
                        "risk_level": cached["risk_level"],
                        "detail": (f"已复用本地已校验安装包：{cached['version_name']} · "
                                   f"{cached['file_size'] / 1048576:.2f} MB，无需重复下载。"
                                   f"{cached['conclusion']}"),
                        "file_size": cached["file_size"], "sha256": cached["sha256"],
                        "package_name": cached["package_name"],
                        "version_name": cached["version_name"],
                        "download_file_url": f"/api/artifacts/{cached['id']}/file",
                    })
                elif method == "device":
                    summary["items"].append(
                        _device_extract_artifact(app, market, BASE_DIR / "data" / "artifacts")
                    )
                elif not download_url and method == "direct":
                    summary["items"].append({
                        "status": "direct_unavailable",
                        "detail": "该渠道当前没有可验证的官方网页 APK 直链，请重新巡检后再试。",
                    })
                elif not download_url:
                    summary["items"].append(
                        _device_extract_artifact(app, market, BASE_DIR / "data" / "artifacts")
                    )
                elif not _allowed_url(market_id, download_url):
                    summary["items"].append({
                        "status": "unsafe_download_url",
                        "detail": "市场返回的下载地址不在官方白名单中，系统已拒绝下载。",
                    })
                else:
                    artifact_dir = BASE_DIR / "data" / "artifacts"
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    file_name = f"app{app_id}_{market_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.apk"
                    local_path = artifact_dir / file_name
                    resume_key = hashlib.sha256(download_url.encode("utf-8")).hexdigest()[:12]
                    temp_path = artifact_dir / f"app{app_id}_{market_id}_{resume_key}.apk.part"
                    progress_lock = threading.Lock()
                    progress_saved = {"bytes": -1}

                    def save_progress(done: int, expected: int, download_mode: str):
                        with progress_lock:
                            # Limit SQLite writes while still keeping the UI visibly moving.
                            if (progress_saved["bytes"] >= 0 and done < expected
                                    and done - progress_saved["bytes"] < (2 << 20)):
                                return
                            progress_saved["bytes"] = done
                            summary["downloaded_bytes"] = done
                            summary["expected_bytes"] = declared_size or expected
                            summary["download_mode"] = download_mode
                            db.execute(
                                "UPDATE check_runs SET summary_json=? WHERE id=?",
                                (json.dumps(summary, ensure_ascii=False), run_id),
                            )

                    size, content_length, download_mode = _download_with_resume(
                        market_id, download_url, temp_path, timeout, save_progress,
                    )
                    summary["download_mode"] = download_mode
                    temp_path.replace(local_path)
                    baseline = db.query(
                        """SELECT sig_fingerprint FROM artifacts
                           WHERE app_id=? AND package_name=? AND sig_fingerprint<>''
                             AND risk_level NOT IN ('critical','high')
                           ORDER BY id DESC LIMIT 1""",
                        (app_id, app["package_name"]), one=True,
                    )
                    verified = apk_verify.verify(
                        local_path,
                        baseline_sig=(baseline["sig_fingerprint"] if baseline else ""),
                        expected_package=app["package_name"],
                        expected_version=(collected.version_name if collected else ""),
                    )
                    actual_package = verified.get("package") or ""
                    actual_version = verified.get("version_name") or ""
                    expected_version = (collected.version_name if collected else "") or ""
                    risk_level = "info"
                    verify_result = verified["verify_result"]
                    if not actual_package:
                        risk_level = "high"
                        conclusion = "下载文件不是可解析的 APK，禁止安装"
                    elif actual_package != app["package_name"]:
                        risk_level = "critical"
                        verify_result = "diff"
                        conclusion = (f"包名不一致：期望 {app['package_name']}，实际 {actual_package}。"
                                      "疑似错误下载或仿冒包，禁止安装")
                    elif expected_version and actual_version and actual_version != expected_version:
                        risk_level = "high"
                        verify_result = "diff"
                        conclusion = (f"包名一致，但版本与市场页面不一致：页面 {expected_version}，"
                                      f"安装包 {actual_version}。暂不建议安装")
                    elif not verified.get("sig"):
                        risk_level = "high"
                        verify_result = "need_review"
                        conclusion = "未能读取 APK 签名证书，无法证明发布者身份，禁止安装"
                    elif baseline and verified["verify_result"] == "diff":
                        risk_level = "critical"
                        conclusion = "签名与此前同包名可信制品不一致，疑似非官方签名，禁止安装"
                    else:
                        size_note = ""
                        if declared_size and abs(size - declared_size) > 1024:
                            risk_level = "warning"
                            size_note = f"；实际大小与市场声明相差 {abs(size-declared_size)} 字节"
                        conclusion = (f"包名、版本均与监测目标一致，SHA-256 和签名已记录{size_note}。"
                                      + ("签名与已有制品一致" if baseline else "暂无签名基线，未发现直接冒用证据"))
                    artifact_id = db.execute(
                        """INSERT INTO artifacts
                           (app_id,market_id,platform,file_name,local_path,source_url,file_size,
                            sha256,package_name,version_name,version_code,sig_fingerprint,
                            verify_result,risk_level,conclusion)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (app_id, market_id, "android", file_name, str(local_path), source_url,
                         size, verified["sha256"], actual_package, actual_version,
                         verified.get("version_code", ""), verified.get("sig", ""),
                         verify_result, risk_level, conclusion),
                    )
                    db.execute(
                        """UPDATE results SET apk_sha256=?,sig_fingerprint=?,verify_result=?,
                           risk_level=?,risk_reason=?,source_url=?,download_url=?,declared_size=?
                           WHERE app_id=? AND market_id=?""",
                        (verified["sha256"], verified.get("sig", ""), verify_result,
                         risk_level, conclusion, source_url, download_url, declared_size,
                         app_id, market_id),
                    )
                    summary["items"].append({
                        "status": "ok" if risk_level in ("none", "info") else "risk",
                        "artifact_id": artifact_id, "risk_level": risk_level,
                        "detail": conclusion, "file_size": size,
                        "sha256": verified["sha256"], "package_name": actual_package,
                        "version_name": actual_version,
                        "download_file_url": (f"/api/artifacts/{artifact_id}/file"
                                              if risk_level not in ("critical", "high") else ""),
                    })
            summary["completed"] = 1
            item = summary["items"][0] if summary["items"] else {}
            final = "done" if item.get("status") == "ok" else "partial"
            db.execute(
                "UPDATE check_runs SET finished_at=?,status=?,summary_json=? WHERE id=?",
                (_now(), final, json.dumps(summary, ensure_ascii=False), run_id),
            )
        except Exception as exc:
            candidate = locals().get("local_path")
            if candidate and Path(candidate).is_file():
                Path(candidate).unlink(missing_ok=True)
            partial = locals().get("temp_path")
            resumable = bool(partial and (
                Path(partial).is_file()
                or list(Path(partial).parent.glob(Path(partial).name + ".seg*"))
            ))
            if isinstance(exc, httpx.TimeoutException):
                detail = "官方下载源响应超时，未生成可用 APK"
            else:
                detail = f"{type(exc).__name__}: {exc}"
            if resumable:
                detail += "；已保留安全临时分片，下次点击同一渠道将断点续传"
            summary["completed"] = 1
            summary["items"] = [{"status": "error", "detail": detail}]
            db.execute(
                "UPDATE check_runs SET finished_at=?,status='failed',summary_json=? WHERE id=?",
                (_now(), json.dumps(summary, ensure_ascii=False), run_id),
            )
        finally:
            _artifact_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return run_id
