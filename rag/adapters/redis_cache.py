"""
Redis 客户端工厂与失败原因翻译（rag/adapters/redis_cache.py）

为什么单独成文：Redis 是全仓唯一「不是注册适配器、却要手工建客户端」的外部依赖 ——
容器初始化建一处、单段热应用又建一处、配置页「测试连接」还要建一处。三处各写一套
超时与认证口径，就会出现"配置页测通了、保存却说不可用"，而两边结论都"有据可查"
（同 TS-014 / TS-015 / TS-017 / TS-019）。

约定：
- 建连/读写超时、自检预算、慢探测阈值都是**模块常量**，不是用户配置项 ——
  用户调它解决不了连接问题，只会让两条链路各说各话（TS-014 / TS-015）。
- 失败原因一律经 redis_failure_reason() 翻译成"该怎么处置"，不许只回一句
  "Redis 不可达"：那一句话同时盖住 认证 / DB 编号越界 / 地址端口 / 超时 四类故障，
  照它去查防火墙必然跑偏（TS-011 的"失败"二字、TS-016 的"地址不可达"）。
"""
from __future__ import annotations

import asyncio
import time

from rag.observability.logging import get_logger

log = get_logger("rag.redis")

REDIS_CONNECT_TIMEOUT_SEC = 3      # 建连超时（socket_connect_timeout）
REDIS_SOCKET_TIMEOUT_SEC = 5       # 读写超时（socket_timeout）
REDIS_HEALTH_BUDGET_SEC = 6.0      # 自检 / 配置页探测共用的总预算
REDIS_SLOW_PROBE_SEC = 2.0         # "慢但成功"的发声阈值


def make_client(cfg):
    """按配置建一个 redis.asyncio 客户端（只建对象，不建连、不 ping）"""
    import redis.asyncio as aioredis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    # 显式禁用 redis-py 8.x 默认的 10 次指数重试：服务不可达时默认重试会拖 ~36s
    # （且会吞掉 wait_for 的取消），这里改为单次尝试快速失败，由降级逻辑接管
    return aioredis.Redis(
        host=cfg.host, port=cfg.port, password=cfg.password or None,
        db=cfg.db, decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SEC,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SEC,
        retry=Retry(NoBackoff(), 0))


def redis_failure_reason(e: Exception, cfg=None) -> str:
    """异常 → 「该怎么处置」的可读原因（配置页提示与 degraded 记录共用）

    先按失败**性质**判、再落到网络层：认证与 DB 编号这两类都是"连得上但用不了"，
    如果按"连不上"去排查（防火墙、端口、服务是否启动）会一直是死路。
    """
    where = f"{getattr(cfg, 'host', '?')}:{getattr(cfg, 'port', '?')}"
    port = getattr(cfg, "port", "?")
    db = getattr(cfg, "db", None)
    raw = str(e).strip().replace("\n", " ")[:180]
    low = raw.lower()
    # 1) 认证类：服务端开着 requirepass / ACL，而密码错、缺、或被禁用
    if ("wrongpass" in low or "invalid username-password" in low
            or "auth failed" in low or "invalid password" in low):
        return f"认证失败：密码不正确（或被 ACL 用户禁用）（{raw}）"
    if "noauth" in low or "authentication required" in low:
        return f"服务端要求认证，但未填密码（{raw}）"
    # 2) DB 编号越界：不是"库不存在"，而是超出服务端 databases 配置
    if "db index is out of range" in low or "select failed" in low:
        return (f"DB 编号 {db} 超出服务端 databases 范围（{raw}）；"
                "请改用服务端已启用的编号（默认 0~15）")
    # 3) 服务端可用性（连得上，但服务端此刻拒绝服务）
    if "max number of clients reached" in low:
        return f"服务端连接数已达上限（{raw}）"
    if "loading" in low:
        return f"服务端正在载入持久化文件（LOADING），暂时拒绝命令（{raw}）"
    # 4) 网络层。
    #    注意：这里的关键词必须**英文 + 中文**都要覆盖 —— Windows 套接字错误文本
    #    是按系统语言本地化的（实测本机报 "Error 22 connecting to ... 远程计算机拒绝
    #    网络连接。"，一个英文关键词都不含），只按英文匹配会全部落到兜底分支、
    #    又退回"一句无用的话"。所以下面每个网络分支都同时收中英两套说法。
    if ("connection refused" in low or "actively refused" in low
            or "no connection could be made" in low or "connect call failed" in low
            or "10061" in low or "拒绝网络连接" in raw or "拒绝" in raw):
        # 第三种成因必须写出来：服务其实在跑，只是**只绑了回环**。此时在服务端
        # netstat/ss 看"一切正常"，从别的机器连却一律被拒（实测遇到过一次：
        # 服务端 netstat 显示 127.0.0.1:6379 + ::1:6379 LISTEN，应用侧报 10061）。
        # 只写"服务未启动或端口不对"会把用户引向"服务明明起着"的死胡同。
        return (f"无法连接 {where}：连接被拒绝（服务未启动 / 端口不对 / 只绑定了"
                f" 127.0.0.1 三者之一）（{raw}）；在**服务端**执行 "
                f"`ss -lntp | grep {port}` 看监听地址：若只有 127.0.0.1 或 ::1，"
                "说明 redis.conf 的 bind（含 docker 发布端口的绑定地址）只放开了"
                "回环，从别的机器连必然被拒")
    if ("name or service not known" in low or "nodename nor servname" in low
            or "temporary failure in name resolution" in low or "getaddrinfo" in low
            or "找不到主机" in raw or "名称解析" in raw):
        return f"主机名无法解析（{raw}）；请确认主机名拼写或改用 IP"
    if ("network is unreachable" in low or "no route to host" in low
            or "unreachable" in low or "网络不可达" in raw or "无法访问" in raw):
        return f"到 {where} 的网络不可达（{raw}）；检查路由/网段与防火墙放行"
    if "timed out" in low or "timeout" in low or "超时" in raw:
        return f"连接 {where} 超时（{raw}）；确认地址/端口、防火墙是否静默丢包"
    return f"{type(e).__name__}: {raw}"


async def probe(cfg) -> tuple[bool, str]:
    """按配置真实建连并 PING 一次（含认证与 SELECT DB），用完即关

    配置页「测试连接」与容器自检共用这一份口径，且探测对象是**用户此刻填的值**
    构建的临时客户端，不是已保存实例（TS-017 / TS-019）。
    """
    db = getattr(cfg, "db", 0)
    if isinstance(db, int) and db < 0:
        return False, f"DB 编号不能为负数（当前 {db}）"
    client = None
    started = time.perf_counter()
    try:
        try:
            client = make_client(cfg)
        except Exception as e:                  # redis 包缺失 / 配置非法
            return False, f"Redis 客户端初始化失败：{str(e)[:180]}"
        await asyncio.wait_for(client.ping(), timeout=REDIS_HEALTH_BUDGET_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("redis_probe_timeout", budget=REDIS_HEALTH_BUDGET_SEC,
                    host=getattr(cfg, "host", ""))
        return False, (f"探测超时（超过 {REDIS_HEALTH_BUDGET_SEC:g}s 无响应）："
                       f"{getattr(cfg, 'host', '')}:{getattr(cfg, 'port', '')} "
                       "未在预算内回应 PING；请确认地址/端口与防火墙")
    except Exception as e:
        return False, redis_failure_reason(e, cfg)
    finally:
        # 探测客户端是一次性的：真实建连之后必须释放，否则每点一次"测试连接"
        # 就攒下一个不回收的连接池（同 TS-016 第 5 条）
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
    elapsed = time.perf_counter() - started
    if elapsed >= REDIS_SLOW_PROBE_SEC:
        # "慢但成功"不会走任何失败分支，必须自己发声（同 mysql_slow_connect）
        log.warning("redis_slow_probe", elapsed=round(elapsed, 2),
                    host=getattr(cfg, "host", ""),
                    hint="Redis 响应明显偏慢：检查服务端负载与网络")
    return True, (f"Redis {getattr(cfg, 'host', '')}:{getattr(cfg, 'port', '')} "
                  f"连接正常（DB {getattr(cfg, 'db', 0)}，"
                  f"键前缀 {getattr(cfg, 'prefix', '') or '无'}）")
