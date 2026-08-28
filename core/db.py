"""应用市场版本巡检系统 · SQLite 数据层"""
import sqlite3
import threading
import re
from datetime import datetime
from config import (DB_PATH, DEFAULT_MARKETS, MARKET_PLATFORMS,
                    PACKAGE_READY_MARKETS)

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    platform TEXT DEFAULT 'android',
    package_name TEXT DEFAULT '',
    ios_bundle_id TEXT DEFAULT '',
    ios_app_id TEXT DEFAULT '',
    search_keywords TEXT DEFAULT '',
    company_name TEXT DEFAULT '',
    discovery_query TEXT DEFAULT '',
    discovery_status TEXT DEFAULT 'manual',
    download_policy TEXT DEFAULT 'on_change',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    trust_level TEXT DEFAULT 'official',
    app_id TEXT DEFAULT '',          -- 该市场分配给 App 的 ID（如华为 C100xxx），绑定后回填
    baseline_sha256 TEXT DEFAULT '',
    baseline_sig TEXT DEFAULT '',
    platform TEXT DEFAULT 'android', -- android / ios / harmony
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bindings (
    app_id INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    market_app_id TEXT DEFAULT '',   -- 绑定后的市场ID/包名
    authority TEXT DEFAULT 'unknown', -- 渠道权威性标记（App×渠道）：official / third_party / unknown
    baseline_sha256 TEXT DEFAULT '',  -- 官方包 SHA-256 基线（B 路径比对用）
    baseline_sig TEXT DEFAULT '',     -- 官方包签名证书指纹基线
    PRIMARY KEY (app_id, market_id)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    app_id INTEGER NOT NULL,
    version_name TEXT DEFAULT '',
    version_code TEXT DEFAULT '',
    developer_name TEXT DEFAULT '', -- 市场展示的开发者/发布者
    operator_name TEXT DEFAULT '',  -- 市场展示的运营者/主办者/主办单位
    status TEXT DEFAULT 'unknown',   -- online / offline / need_review
    apk_sha256 TEXT DEFAULT '',
    sig_fingerprint TEXT DEFAULT '',
    verify_result TEXT DEFAULT 'n/a', -- ok / diff / need_review / n/a
    screenshot_url TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    version_changed INTEGER DEFAULT 0,
    download_action TEXT DEFAULT 'not_needed', -- not_needed / optional / recommended / required
    download_reason TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    download_url TEXT DEFAULT '',
    declared_size INTEGER DEFAULT 0,
    observed_package TEXT DEFAULT '',
    risk_level TEXT DEFAULT 'none', -- none / info / warning / high / critical
    risk_reason TEXT DEFAULT '',
    published_at TEXT DEFAULT '',      -- 市场提供的当前版本发布时间（标准格式）
    checked_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (market_id, app_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    market_id TEXT NOT NULL,
    app_id INTEGER NOT NULL,
    version_name TEXT DEFAULT '',
    version_code TEXT DEFAULT '',
    developer_name TEXT DEFAULT '',
    operator_name TEXT DEFAULT '',
    status TEXT DEFAULT 'unknown',
    detail TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    checked_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT DEFAULT 'manual',   -- manual / b_path（旧数据可能为 schedule）
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'running',   -- running / done / partial / failed
    summary_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    platform TEXT DEFAULT 'android',
    file_name TEXT DEFAULT '',
    local_path TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    package_name TEXT DEFAULT '',
    version_name TEXT DEFAULT '',
    version_code TEXT DEFAULT '',
    sig_fingerprint TEXT DEFAULT '',
    verify_result TEXT DEFAULT 'need_review',
    risk_level TEXT DEFAULT 'info',
    conclusion TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS market_initializations (
    device_serial TEXT NOT NULL,
    market_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    version_name TEXT DEFAULT '',
    version_code TEXT DEFAULT '',
    consented_at TEXT DEFAULT (datetime('now','localtime')),
    screenshot_before TEXT DEFAULT '',
    screenshot_after TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    PRIMARY KEY (device_serial, market_id, package_name, version_name, version_code)
);
"""


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        # Small additive migrations keep yesterday's database usable.
        binding_cols = [r[1] for r in conn.execute("PRAGMA table_info(bindings)")]
        if "authority" not in binding_cols:
            conn.execute("ALTER TABLE bindings ADD COLUMN authority TEXT DEFAULT 'unknown'")
        if "baseline_sha256" not in binding_cols:
            conn.execute("ALTER TABLE bindings ADD COLUMN baseline_sha256 TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bindings ADD COLUMN baseline_sig TEXT DEFAULT ''")
        app_cols = [r[1] for r in conn.execute("PRAGMA table_info(apps)")]
        for name, ddl in [
            ("platform", "TEXT DEFAULT 'android'"),
            ("ios_bundle_id", "TEXT DEFAULT ''"),
            ("ios_app_id", "TEXT DEFAULT ''"),
            ("company_name", "TEXT DEFAULT ''"),
            ("discovery_query", "TEXT DEFAULT ''"),
            ("discovery_status", "TEXT DEFAULT 'manual'"),
            ("download_policy", "TEXT DEFAULT 'on_change'"),
        ]:
            if name not in app_cols:
                conn.execute(f"ALTER TABLE apps ADD COLUMN {name} {ddl}")
        result_cols = [r[1] for r in conn.execute("PRAGMA table_info(results)")]
        for name, ddl in [
            ("previous_version", "TEXT DEFAULT ''"),
            ("version_changed", "INTEGER DEFAULT 0"),
            ("download_action", "TEXT DEFAULT 'not_needed'"),
            ("download_reason", "TEXT DEFAULT ''"),
            ("developer_name", "TEXT DEFAULT ''"),
            ("operator_name", "TEXT DEFAULT ''"),
            ("published_at", "TEXT DEFAULT ''"),
            ("source_url", "TEXT DEFAULT ''"),
            ("download_url", "TEXT DEFAULT ''"),
            ("declared_size", "INTEGER DEFAULT 0"),
            ("observed_package", "TEXT DEFAULT ''"),
            ("risk_level", "TEXT DEFAULT 'none'"),
            ("risk_reason", "TEXT DEFAULT ''"),
        ]:
            if name not in result_cols:
                conn.execute(f"ALTER TABLE results ADD COLUMN {name} {ddl}")
        observation_cols = [r[1] for r in conn.execute("PRAGMA table_info(observations)")]
        for name in ("developer_name", "operator_name", "published_at"):
            if name not in observation_cols:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT DEFAULT ''")

        market_cols = [r[1] for r in conn.execute("PRAGMA table_info(markets)")]
        if "platform" not in market_cols:
            conn.execute("ALTER TABLE markets ADD COLUMN platform TEXT DEFAULT 'android'")

        # Only exact-package web adapters are on by default for a fresh install.
        for i, (key, name, adapter, trust, note) in enumerate(DEFAULT_MARKETS):
            conn.execute(
                "INSERT OR IGNORE INTO markets "
                "(id, name, adapter, enabled, trust_level, platform, sort_order) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, name, adapter, 1 if key in PACKAGE_READY_MARKETS else 0,
                 trust, MARKET_PLATFORMS.get(key, "android"), i),
            )

        # V9 统一所有用户界面的渠道正式名称。英文 ID 只用于内部接口与存储。
        names_v9 = conn.execute(
            "SELECT value FROM schema_meta WHERE key='market_names_v9'"
        ).fetchone()
        if not names_v9:
            conn.executemany(
                "UPDATE markets SET name=? WHERE id=?",
                [(name, key) for key, name, *_ in DEFAULT_MARKETS],
            )
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('market_names_v9','1')"
            )

        # 酷安暂时退出产品范围。保留历史结果用于审计，只停用渠道。
        conn.execute("UPDATE markets SET enabled=0 WHERE id='coolapk'")

        # Yesterday's build enabled every placeholder by default. Migrate that
        # unmistakable default state once, without repeatedly overriding choices.
        migrated = conn.execute(
            "SELECT value FROM schema_meta WHERE key='recommended_markets_v2'"
        ).fetchone()
        if not migrated:
            enabled_count = conn.execute("SELECT COUNT(*) FROM markets WHERE enabled=1").fetchone()[0]
            if enabled_count == len(DEFAULT_MARKETS):
                placeholders = [x[0] for x in DEFAULT_MARKETS if x[0] not in PACKAGE_READY_MARKETS]
                conn.executemany("UPDATE markets SET enabled=0 WHERE id=?", [(x,) for x in placeholders])
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('recommended_markets_v2','1')")

        # V3 新增了四个稳定网页采集器。只执行一次，把无需额外配置的网页渠道
        # 纳入现有数据库；USB 客户端渠道仍由用户自行选择是否启用。
        web_v3 = conn.execute(
            "SELECT value FROM schema_meta WHERE key='web_collectors_v3'"
        ).fetchone()
        if not web_v3:
            conn.executemany(
                "UPDATE markets SET enabled=1 WHERE id=?",
                [(market_id,) for market_id in PACKAGE_READY_MARKETS],
            )
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('web_collectors_v3','1')")

        # V4 增加 iOS、360、Google Play。显式刷新适配器与平台，
        # 兼容旧库中曾经保留但停用的 360 占位记录。
        web_v4 = conn.execute(
            "SELECT value FROM schema_meta WHERE key='web_collectors_v4'"
        ).fetchone()
        if not web_v4:
            market_defs = {row[0]: row for row in DEFAULT_MARKETS}
            for key in ("appstore", "qihu360"):
                row = market_defs[key]
                conn.execute(
                    "UPDATE markets SET name=?, adapter=?, enabled=1, trust_level=?, "
                    "platform=?, sort_order=? WHERE id=?",
                    (row[1], row[2], row[3], MARKET_PLATFORMS.get(key, "android"),
                     next(i for i, item in enumerate(DEFAULT_MARKETS) if item[0] == key), key),
                )
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('web_collectors_v4','1')")

        # 鸿蒙公开网页目前无法稳定取得版本。保留已有历史结果供审计，
        # 但从监测范围停用。
        remove_harmony_phone = conn.execute(
            "SELECT value FROM schema_meta WHERE key='remove_harmony_phone_v5'"
        ).fetchone()
        if not remove_harmony_phone:
            conn.execute("UPDATE markets SET enabled=0 WHERE id='harmony'")
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('remove_harmony_phone_v5','1')"
            )

        # V6 将旧版挂在 Android 监测对象下的 App Store 当前结果拆成独立
        # iOS 对象，避免平台模型升级后历史版本从报表中消失。
        split_ios = conn.execute(
            "SELECT value FROM schema_meta WHERE key='split_ios_apps_v6'"
        ).fetchone()
        if not split_ios:
            legacy_ios = conn.execute(
                """SELECT r.id result_id, r.app_id, r.detail, a.app_name,
                          a.company_name, a.discovery_query, a.note
                   FROM results r JOIN apps a ON a.id=r.app_id
                   WHERE r.market_id='appstore' AND COALESCE(a.platform,'android')='android'"""
            ).fetchall()
            for row in legacy_ios:
                bundles = re.findall(
                    r"\b[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}\b", row["detail"] or ""
                )
                if not bundles:
                    continue
                bundle_id = bundles[-1]
                target = conn.execute(
                    "SELECT id FROM apps WHERE platform='ios' AND ios_bundle_id=?",
                    (bundle_id,),
                ).fetchone()
                if target:
                    ios_app_id = target["id"]
                else:
                    cur = conn.execute(
                        """INSERT INTO apps
                           (app_name, platform, ios_bundle_id, company_name,
                            discovery_query, discovery_status, note)
                           VALUES (?,'ios',?,?,?,'migrated',?)""",
                        (row["app_name"], bundle_id, row["company_name"],
                         row["discovery_query"], row["note"]),
                    )
                    ios_app_id = cur.lastrowid
                conn.execute("UPDATE results SET app_id=? WHERE id=?",
                             (ios_app_id, row["result_id"]))
                conn.execute(
                    "UPDATE observations SET app_id=? WHERE app_id=? AND market_id='appstore'",
                    (ios_app_id, row["app_id"]),
                )
                binding = conn.execute(
                    "SELECT * FROM bindings WHERE app_id=? AND market_id='appstore'",
                    (row["app_id"],),
                ).fetchone()
                if binding:
                    conn.execute(
                        """INSERT OR IGNORE INTO bindings
                           (app_id,market_id,market_app_id,authority,baseline_sha256,baseline_sig)
                           VALUES (?,?,?,?,?,?)""",
                        (ios_app_id, "appstore", binding["market_app_id"], binding["authority"],
                         binding["baseline_sha256"], binding["baseline_sig"]),
                    )
                    conn.execute(
                        "DELETE FROM bindings WHERE app_id=? AND market_id='appstore'",
                        (row["app_id"],),
                    )
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('split_ios_apps_v6','1')"
            )

        # V7：Google Play 退出当前监测范围；历史结果仍保留在数据库中。
        # 同时把旧结果详情中的日期回填到独立字段，并清理曾直接展示的
        # ``update_time`` 内部字段名。
        standardize_results = conn.execute(
            "SELECT value FROM schema_meta WHERE key='standardize_results_v7'"
        ).fetchone()
        if not standardize_results:
            conn.execute("UPDATE markets SET enabled=0 WHERE id='google_play'")
            rows = conn.execute(
                "SELECT id, detail, published_at FROM results"
            ).fetchall()
            for row in rows:
                detail = row["detail"] or ""
                published_at = row["published_at"] or ""
                if not published_at:
                    epoch = re.search(r"update_time=(\d{10,13})", detail)
                    date = re.search(
                        r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", detail
                    )
                    if epoch:
                        stamp = int(epoch.group(1))
                        if len(epoch.group(1)) == 13:
                            stamp //= 1000
                        try:
                            published_at = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
                        except (OSError, OverflowError, ValueError):
                            published_at = ""
                    elif date:
                        published_at = f"{date.group(1)}-{int(date.group(2)):02d}-{int(date.group(3)):02d}"
                cleaned = re.sub(r"\s*[·|]?\s*update_time=\d{10,13}", "", detail).strip(" ·|")
                conn.execute(
                    "UPDATE results SET published_at=?, detail=? WHERE id=?",
                    (published_at, cleaned, row["id"]),
                )
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('standardize_results_v7','1')"
            )

        # V8 清理曾被应用宝搜索误识别为 Android App 的电脑版容器条目。
        # ``com.tencent.pcgame.*`` 对应应用宝电脑版运行入口，不是移动 APK 包名。
        remove_pcgame = conn.execute(
            "SELECT value FROM schema_meta WHERE key='remove_yyb_pcgame_v8'"
        ).fetchone()
        if not remove_pcgame:
            bad_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM apps WHERE lower(package_name) LIKE 'com.tencent.pcgame.%'"
            ).fetchall()]
            for app_id in bad_ids:
                conn.execute("DELETE FROM observations WHERE app_id=?", (app_id,))
                conn.execute("DELETE FROM results WHERE app_id=?", (app_id,))
                conn.execute("DELETE FROM bindings WHERE app_id=?", (app_id,))
                conn.execute("DELETE FROM apps WHERE id=?", (app_id,))
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('remove_yyb_pcgame_v8',?)",
                (str(len(bad_ids)),),
            )
        conn.commit()
        conn.close()


def query(sql, args=(), one=False):
    conn = get_conn()
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=(), many=None):
    with _lock:
        conn = get_conn()
        if many:
            conn.executemany(sql, many)
        else:
            conn.execute(sql, args)
        conn.commit()
        last = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return last


def delete_app(app_id: int):
    """原子移除监测对象及其按 App 关联的历史证据。"""
    with _lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM observations WHERE app_id=?", (app_id,))
            conn.execute("DELETE FROM artifacts WHERE app_id=?", (app_id,))
            conn.execute("DELETE FROM results WHERE app_id=?", (app_id,))
            conn.execute("DELETE FROM bindings WHERE app_id=?", (app_id,))
            conn.execute("DELETE FROM apps WHERE id=?", (app_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
