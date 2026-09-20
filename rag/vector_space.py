"""
向量空间指纹（rag/vector_space.py）

一个 collection 里的向量必须来自**同一个向量空间**：同一套 embedding 实现 + 同一个
模型 + 同一个维度。混用会让 ANN 检索**静默失准** —— Milvus 的内积度量永远返回
"最近的 top_k"，而 RRF 只按 rank 计票、不看 score，于是噪声向量与正确答案拿到
一样的票数，谁都没报错。典型触发场景有两个：

1. 演示/联调时向量模型退化为本地替身（mock），此时入库写的是**哈希伪向量**；
   之后用户配好真实向量模型（base_url 一填），检索立刻用真向量去查那批伪向量；
2. 换了真实向量模型但维度相同（如两个 1024 维模型），Milvus 不会报
   dimension mismatch，两套向量就悄悄住进了同一个集合。

这套判据刻意**不是**"当前是不是 mock"：真模型之间换同样会静默污染，而维度变了
的情况本就有 Milvus 的 dimension mismatch 顶着。本模块只做纯计算（建指纹 /
读指纹 / 给结论），落在库侧的那一份由向量库适配器按自己的载体读写，编排由容器
负责（见 container.sync_vector_space / vector_write_blocked）：

- 写侧：指纹不一致 → **拒绝写入**并记录"有意跳过"，绝不伪装成写入失败；
- 读侧：指纹不一致 → **关掉向量路**并给出可读原因，而不是拿噪声去查。

为什么"拒绝"而不是"照写"：伪向量一旦进库就与真向量不可区分，事后既检不出来、
也删不干净（只能整集合重建），而它造成的失准又不会报错 —— 属于"安静算错"。
"""
from __future__ import annotations

# 指纹格式版本。提升版本号时必须同时给出兼容策略：解析不出来的旧指纹会被判成
# "与当前不一致"，从而停写 + 关向量路（宁可停，也不放噪声进去）。
TAG_VERSION = "v1"
# 指纹在向量库里的载体名（Milvus 落在 collection properties 的一个键上）
TAG_KEY = "rag_space"


def space_tag(runtime: str, model: str, dim: int, *, is_local: bool) -> str:
    """当前向量空间指纹：``v1|<实现>|<模型>|<维度>|<real|local>``

    刻意不含 base_url / api_key：换个地址不等于换个向量空间，把它们算进去只会
    让"改了个端口"被判成换模型，天天误报（同 TS-001 对 base_url 的处理）。

    本地替身（mock）不记模型名：它的 config.model 仍是配置里那个真实模型名，
    记进去只会让"两个 mock 部署改了个模型名"被判成换了空间（本该放行），
    反而误伤。

    末位单列 real/local 标记，而不是"模型名为空 = 本地替身"：真实向量模型也可能
    没写模型名（noconnection 演示模式就会把 embedding 段清成 mock 配置），
    靠猜会把两件完全不同的事——"伪向量"与"没登记模型名"——说成同一句话，
    而这个标签正是用户在事故现场唯一能看到的东西，不能猜。
    """
    m = "" if is_local else (model or "").strip()
    kind = "local" if is_local else "real"
    return f"{TAG_VERSION}|{runtime or '?'}|{m}|{int(dim or 0)}|{kind}"


def parse_tag(tag: str | None) -> tuple[str, str, int, bool] | None:
    """指纹 → (实现, 模型, 维度, 是否本地替身)；无法解析返回 None"""
    if not tag:
        return None
    parts = str(tag).split("|")
    if len(parts) != 5 or parts[0] != TAG_VERSION:
        return None
    try:
        dim = int(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], dim, parts[4] == "local"


def tag_label(tag: str | None) -> str:
    """指纹 → 人话（看日志/界面的用户不该去数竖线）"""
    parsed = parse_tag(tag)
    if parsed is None:
        return str(tag) if tag else "未知"
    runtime, model, dim, is_local = parsed
    if is_local:
        return f"{runtime}（本地替身，{dim} 维）"
    if not model:
        return f"{runtime}（未登记模型名，{dim} 维）"
    return f"{model}（{runtime}，{dim} 维）"


def judge_space(current: str | None, stored: str | None, rows: int | None,
                *, is_local: bool) -> tuple[bool, bool, str]:
    """判定 (写入是否允许, 检索是否可用, 原因)

    `rows` 为 None 表示读不到集合行数（权限/异常/不支持）：按"可能有数据"处理
    —— 拿不准时偏向保守，宁可少写一次，也不放噪声进去。

    唯一一律放行的是**空集合**：里面没有向量，既无可污染、也无可被误导的数据
    （此时上层会顺手补打当前指纹）。

    `stored is None` 有两种来源，处置不同：新集合（无数据）与"本功能上线前写入
    的存量集合"。后者无法确认来源，也对不上账，只能分两种：
    - 当前是本地替身（伪向量）：**停**（写入会污染、检索会被自己的噪声挤掉）；
    - 当前是真实模型：按**同源**处理（这是改造前的既有行为），但给出告警，
      提醒用户"若刚换过向量模型，请重跑这批文档的入库"。
    """
    if not current:
        return True, True, ""            # 判据缺失（拿不到当前指纹）：不阻断
    if stored == current:
        return True, True, ""
    if rows == 0:
        return True, True, ""            # 空集合：无数据可污染 / 可被误导

    counted = rows if rows else "若干"
    cur = tag_label(current)
    if stored is None:
        if is_local:
            return False, False, (
                f"该集合里已有 {counted} 条向量，却没有空间指纹（由此前的版本写入）："
                f"无法确认它们与当前向量模型（{cur}）同源，本地替身既不再写入、"
                f"也不参与检索；请改用真实向量模型，或换一个集合前缀另起一套集合")
        return True, True, (
            f"该集合里已有 {counted} 条向量，但没有空间指纹（由此前的版本写入）："
            f"按与当前向量模型（{cur}）同源处理；若刚换过向量模型，"
            f"请重跑这批文档的入库")
    return False, False, (
        f"向量空间不一致：集合里的向量由 {tag_label(stored)} 写入，"
        f"当前向量模型是 {cur}。继续检索会返回同维但不同源的向量"
        f"（按 rank 计票的 RRF 淘汰不掉它们），继续写入会永久污染该集合；"
        f"请重跑入库，或换一个集合前缀另起一套集合")
