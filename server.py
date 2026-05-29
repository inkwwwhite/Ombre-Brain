# ============================================================
# Module: MCP Server + Web Dashboard (server.py)
# 模块：MCP 服务器 + Web 管理界面
#
# 在原作者基础上增加：
#   - JWT 登录系统（用户名 + 密码）
#   - REST API（给前端 PWA 用的 CRUD 接口）
#   - 静态文件托管（/app 路径下挂载 PWA 前端）
#   - 原 MCP 接口完全保留，不影响 Claude 调用
# ============================================================

import os
import sys
import random
import logging
import asyncio
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt
from passlib.context import CryptContext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from utils import load_config, setup_logging

# --- Load config & init logging ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Initialize three core components ---
bucket_mgr = BucketManager(config)
dehydrator = Dehydrator(config)
decay_engine = DecayEngine(config, bucket_mgr)

# --- Auth config ---
# 从环境变量读取，不会被写进代码里
AUTH_USERNAME = os.getenv("OMBRE_WEB_USERNAME", "")
AUTH_PASSWORD = os.getenv("OMBRE_WEB_PASSWORD", "")
JWT_SECRET = os.getenv("OMBRE_JWT_SECRET", "")

# 如果 JWT_SECRET 没设，自动生成一个（重启后会失效，所以建议在 Zeabur 设环境变量）
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.warning(
        "OMBRE_JWT_SECRET not set, using random one. "
        "All sessions will be lost on restart. "
        "Please set OMBRE_JWT_SECRET in your environment."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30  # token 有效期 30 天

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 启动时把明文密码哈希一次缓存起来
AUTH_PASSWORD_HASH = pwd_context.hash(AUTH_PASSWORD) if AUTH_PASSWORD else ""

AUTH_ENABLED = bool(AUTH_USERNAME and AUTH_PASSWORD)
if not AUTH_ENABLED:
    logger.warning(
        "Web auth disabled (OMBRE_WEB_USERNAME or OMBRE_WEB_PASSWORD not set). "
        "Anyone can access /app and /api endpoints!"
    )

# --- Create MCP server instance ---
mcp = FastMCP("Ombre Brain", host="0.0.0.0", port=8000)


# =============================================================
# 工具函数
# =============================================================

def _create_token(username: str) -> str:
    """生成 JWT token"""
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_token(token: str) -> Optional[str]:
    """验证 token，返回用户名或 None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


async def _require_auth(request) -> Optional[str]:
    """检查请求里的 token，没认证返回 None。认证成功返回用户名。"""
    if not AUTH_ENABLED:
        return "anonymous"  # 没启用认证就放行

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    return _verify_token(token)


def _json_response(data, status=200):
    from starlette.responses import JSONResponse
    return JSONResponse(data, status_code=status)


def _unauthorized():
    return _json_response({"error": "Unauthorized"}, status=401)


# =============================================================
# 公开端点（不需要登录）
# =============================================================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """系统健康检查（原作者的接口，保留）"""
    try:
        stats = await bucket_mgr.get_stats()
        return _json_response({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "auth_enabled": AUTH_ENABLED,
        })
    except Exception as e:
        return _json_response({"status": "error", "detail": str(e)}, status=500)


@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    """SessionStart hook，原作者的接口，保留"""
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        pinned = [b for b in all_buckets
                  if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") != "permanent"
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved,
                        key=lambda b: decay_engine.calculate_score(b["metadata"]),
                        reverse=True)
        top = scored[:2]

        parts = []
        for b in pinned:
            summary = await dehydrator.dehydrate(
                b["content"],
                {k: v for k, v in b["metadata"].items() if k != "tags"}
            )
            parts.append(f"📌 [核心准则] {summary}")
        for b in top:
            summary = await dehydrator.dehydrate(
                b["content"],
                {k: v for k, v in b["metadata"].items() if k != "tags"}
            )
            await bucket_mgr.touch(b["id"])
            parts.append(summary)

        if not parts:
            return PlainTextResponse("")
        return PlainTextResponse("[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts))
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# 认证端点
# =============================================================

@mcp.custom_route("/api/login", methods=["POST"])
async def api_login(request):
    """登录接口，验证用户名密码，返回 JWT token"""
    if not AUTH_ENABLED:
        return _json_response({
            "token": _create_token("anonymous"),
            "warning": "auth disabled"
        })

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, status=400)

    username = body.get("username", "")
    password = body.get("password", "")

    if username != AUTH_USERNAME:
        # 用 sleep 防止时序攻击
        await asyncio.sleep(0.3)
        return _json_response({"error": "用户名或密码错误"}, status=401)

    if not pwd_context.verify(password, AUTH_PASSWORD_HASH):
        await asyncio.sleep(0.3)
        return _json_response({"error": "用户名或密码错误"}, status=401)

    token = _create_token(username)
    return _json_response({"token": token, "expires_days": JWT_EXPIRY_DAYS})


@mcp.custom_route("/api/me", methods=["GET"])
async def api_me(request):
    """验证 token 是否有效"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()
    return _json_response({"username": user, "auth_enabled": AUTH_ENABLED})


# =============================================================
# REST API：记忆桶 CRUD
# =============================================================

@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_list_buckets(request):
    """获取所有记忆桶列表"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()

    include_archive = request.query_params.get("include_archive", "false").lower() == "true"

    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return _json_response({"error": str(e)}, status=500)

    result = []
    for b in buckets:
        meta = b.get("metadata", {})
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        result.append({
            "id": b["id"],
            "name": meta.get("name", b["id"]),
            "content_preview": (b.get("content", "") or "")[:120],
            "type": meta.get("type", "dynamic"),
            "pinned": bool(meta.get("pinned") or meta.get("protected")),
            "resolved": bool(meta.get("resolved", False)),
            "domain": meta.get("domain", []),
            "tags": meta.get("tags", []),
            "valence": meta.get("valence", 0.5),
            "arousal": meta.get("arousal", 0.3),
            "importance": meta.get("importance", 5),
            "score": round(score, 3),
            "created_at": meta.get("created_at", ""),
            "last_active": meta.get("last_active", ""),
        })

    return _json_response({
        "buckets": result,
        "stats": {
            "permanent": stats["permanent_count"],
            "dynamic": stats["dynamic_count"],
            "archive": stats["archive_count"],
            "total_kb": round(stats["total_size_kb"], 1),
            "decay_running": decay_engine.is_running,
        }
    })


@mcp.custom_route("/api/buckets/{bucket_id}", methods=["GET"])
async def api_get_bucket(request):
    """获取单个记忆桶完整内容"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()

    bucket_id = request.path_params["bucket_id"]
    try:
        bucket = await bucket_mgr.get(bucket_id)
    except Exception as e:
        return _json_response({"error": str(e)}, status=500)

    if not bucket:
        return _json_response({"error": "Not found"}, status=404)

    meta = bucket.get("metadata", {})
    try:
        score = decay_engine.calculate_score(meta)
    except Exception:
        score = 0.0

    return _json_response({
        "id": bucket["id"],
        "content": bucket.get("content", ""),
        "metadata": meta,
        "score": round(score, 3),
    })


@mcp.custom_route("/api/buckets", methods=["POST"])
async def api_create_bucket(request):
    """新建记忆桶（走自动打标流程，类似 hold）"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, status=400)

    content = (body.get("content") or "").strip()
    if not content:
        return _json_response({"error": "内容不能为空"}, status=400)

    importance = max(1, min(10, int(body.get("importance", 5))))
    pinned = bool(body.get("pinned", False))
    extra_tags = body.get("tags", [])
    if isinstance(extra_tags, str):
        extra_tags = [t.strip() for t in extra_tags.split(",") if t.strip()]

    await decay_engine.ensure_started()

    # 自动打标
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    valence = analysis["valence"]
    arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")
    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
        )
        return _json_response({
            "id": bucket_id,
            "action": "pinned",
            "domain": domain,
        })

    # 走合并或新建
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception:
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + all_tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=valence,
                    arousal=arousal,
                )
                return _json_response({
                    "id": bucket["id"],
                    "action": "merged",
                    "name": bucket["metadata"].get("name", bucket["id"]),
                })
            except Exception as e:
                logger.warning(f"Merge failed: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=suggested_name or None,
    )
    return _json_response({
        "id": bucket_id,
        "action": "created",
        "domain": domain,
    })


@mcp.custom_route("/api/buckets/{bucket_id}", methods=["PATCH"])
async def api_update_bucket(request):
    """修改记忆桶元数据或内容"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()

    bucket_id = request.path_params["bucket_id"]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, status=400)

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return _json_response({"error": "Not found"}, status=404)

    updates = {}
    if "content" in body and body["content"]:
        updates["content"] = body["content"]
    if "name" in body and body["name"]:
        updates["name"] = body["name"]
    if "domain" in body and isinstance(body["domain"], list):
        updates["domain"] = body["domain"]
    if "tags" in body and isinstance(body["tags"], list):
        updates["tags"] = body["tags"]
    if "valence" in body:
        v = float(body["valence"])
        if 0 <= v <= 1:
            updates["valence"] = v
    if "arousal" in body:
        a = float(body["arousal"])
        if 0 <= a <= 1:
            updates["arousal"] = a
    if "importance" in body:
        i = int(body["importance"])
        if 1 <= i <= 10:
            updates["importance"] = i
    if "resolved" in body:
        updates["resolved"] = bool(body["resolved"])
    if "pinned" in body:
        updates["pinned"] = bool(body["pinned"])
        if updates["pinned"]:
            updates["importance"] = 10

    if not updates:
        return _json_response({"error": "没有需要修改的字段"}, status=400)

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return _json_response({"error": "修改失败"}, status=500)

    return _json_response({"id": bucket_id, "updated": list(updates.keys())})


@mcp.custom_route("/api/buckets/{bucket_id}", methods=["DELETE"])
async def api_delete_bucket(request):
    """删除记忆桶"""
    user = await _require_auth(request)
    if not user:
        return _unauthorized()

    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.delete(bucket_id)
    if not success:
        return _json_response({"error": "Not found"}, status=404)
    return _json_response({"id": bucket_id, "deleted": True})


# =============================================================
# 静态文件托管：/app 路径下挂载 PWA 前端
# =============================================================

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    """根路径重定向到 /app"""
    from starlette.responses import RedirectResponse
    return RedirectResponse("/app/")


@mcp.custom_route("/app", methods=["GET"])
async def app_redirect(request):
    """/app 无尾斜杠时重定向到 /app/"""
    from starlette.responses import RedirectResponse
    return RedirectResponse("/app/")


@mcp.custom_route("/app/", methods=["GET"])
async def serve_index(request):
    from starlette.responses import FileResponse, JSONResponse
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index):
        return JSONResponse({"error": "Frontend not deployed"}, status_code=404)
    return FileResponse(index)


@mcp.custom_route("/app/{filename}", methods=["GET"])
async def serve_static(request):
    from starlette.responses import FileResponse, JSONResponse
    filename = request.path_params["filename"]
    # 防止目录穿越
    if ".." in filename or filename.startswith("/"):
        return JSONResponse({"error": "Bad path"}, status_code=400)
    filepath = os.path.join(STATIC_DIR, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(filepath)


# =============================================================
# 内部辅助函数（原作者的，保留）
# =============================================================

async def _merge_or_create(content, tags, importance, domain, valence, arousal, name=""):
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=valence,
                    arousal=arousal,
                )
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed: {e}")

    bucket_id = await bucket_mgr.create(
        content=content, tags=tags, importance=importance,
        domain=domain, valence=valence, arousal=arousal,
        name=name or None,
    )
    return bucket_id, False


# =============================================================
# 原作者的 5 个 MCP 工具（完整保留）
# =============================================================

@mcp.tool()
async def breath(query: Optional[str] = None, max_results: int = 3,
                 domain: str = "", valence: float = -1, arousal: float = -1) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。domain逗号分隔,valence/arousal 0~1(-1忽略)。"""
    await decay_engine.ensure_started()

    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets: {e}")
            return "记忆系统暂时无法访问。"

        pinned_buckets = [b for b in all_buckets
                          if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        pinned_results = []
        for b in pinned_buckets:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(b["content"], clean_meta)
                pinned_results.append(f"📌 [核心准则] {summary}")
            except Exception:
                continue

        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") != "permanent"
                      and not b["metadata"].get("pinned", False)
                      and not b["metadata"].get("protected", False)]
        scored = sorted(unresolved,
                        key=lambda b: decay_engine.calculate_score(b["metadata"]),
                        reverse=True)
        top = scored[:2]
        dynamic_results = []
        for b in top:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(b["content"], clean_meta)
                await bucket_mgr.touch(b["id"])
                score = decay_engine.calculate_score(b["metadata"])
                dynamic_results.append(f"[权重:{score:.2f}] {summary}")
            except Exception:
                continue

        if not pinned_results and not dynamic_results:
            return "权重池平静，没有需要处理的记忆。"

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        return "\n\n".join(parts)

    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await bucket_mgr.search(query, limit=max_results,
                                          domain_filter=domain_filter,
                                          query_valence=q_valence,
                                          query_arousal=q_arousal)
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return "检索过程出错，请稍后重试。"

    results = []
    for bucket in matches:
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            summary = await dehydrator.dehydrate(bucket["content"], clean_meta)
            await bucket_mgr.touch(bucket["id"])
            results.append(summary)
        except Exception:
            continue

    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [b for b in all_buckets
                          if b["id"] not in matched_ids
                          and decay_engine.calculate_score(b["metadata"]) < 2.0]
            if low_weight:
                drifted = random.sample(low_weight,
                                        min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(b["content"], clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception:
            pass

    if not results:
        return "未找到相关记忆。"
    return "\n---\n".join(results)


@mcp.tool()
async def hold(content: str, tags: str = "", importance: int = 5, pinned: bool = False) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。"""
    await decay_engine.ensure_started()
    if not content or not content.strip():
        return "内容为空，无法存储。"
    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed: {e}")
        analysis = {"domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                    "tags": [], "suggested_name": ""}

    domain = analysis["domain"]
    valence = analysis["valence"]
    arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")
    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content, tags=all_tags, importance=10,
            domain=domain, valence=valence, arousal=arousal,
            name=suggested_name or None, bucket_type="permanent", pinned=True,
        )
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    result_name, is_merged = await _merge_or_create(
        content=content, tags=all_tags, importance=importance,
        domain=domain, valence=valence, arousal=arousal, name=suggested_name,
    )
    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(domain)}"


@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()
    if not content or not content.strip():
        return "内容为空，无法整理。"

    if len(content.strip()) < 30:
        try:
            analysis = await dehydrator.analyze(content)
        except Exception:
            analysis = {"domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                        "tags": [], "suggested_name": ""}

        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0
    for item in items:
        try:
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )
            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(f"Failed to process diary item: {e}")
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


@mcp.tool()
async def trace(bucket_id: str, name: str = "", domain: str = "",
                valence: float = -1, arousal: float = -1, importance: int = -1,
                tags: str = "", resolved: int = -1, pinned: int = -1,
                delete: bool = False) -> str:
    """修改记忆元数据。resolved=1沉底/0激活,pinned=1钉选/0取消,delete=True删除。只传需改的,-1或空=不改。"""
    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    if delete:
        success = await bucket_mgr.delete(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    changed = ", ".join(f"{k}={v}" for k, v in updates.items())
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    return f"已修改记忆桶 {bucket_id}: {changed}"


@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"

        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0

        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )
    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# 启动入口
# =============================================================

if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport} | auth: {AUTH_ENABLED}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        async def _keepalive_loop():
            await asyncio.sleep(10)
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get("http://localhost:8000/health", timeout=5)
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()

        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled")

        uvicorn.run(_app, host="0.0.0.0", port=8000)
    else:
        mcp.run(transport=transport)
