"""
研究性脚本（不进入生产代码路径）：对比"关键词子串匹配"与"SQLite FTS5 + jieba (BM25)"
召回长期记忆的效果。

方案一（子串匹配）：
- 输入一组关键词（keywords: list[str]）。
- 对每条 active 记忆，在 insight / reuse_condition 字段文本中做大小写不敏感子串匹配。
- 任一关键词命中即算命中；按命中关键词数量降序，数量相同按 updated_at 新旧排序。

方案二（FTS5 + BM25，本脚本的实验重点）：
- 用 jieba 对 insight / reuse_condition 预分词（搜索引擎模式），空格连接后写入
  SQLite FTS5 虚拟表（unicode61 分词器）。
- 查询时把关键词列表转成 FTS5 MATCH 表达式（关键词内部 AND、关键词之间 OR），
  用 FTS5 内置 bm25() 排序——IDF 自动抑制"测试"这类高频词的噪音，且支持按列权重。
- sqlite3 是 Python 标准库（FTS5 需编译进 SQLite，本地已验证可用）；
  jieba 是唯一新增依赖（纯 Python，MIT）。

筛选策略对比（两种都在同一批查询上跑，互相对照）：
- 固定相对阈值：score >= top1_score * RELATIVE_SCORE_THRESHOLD。简单，但比例是硬编码的，
  对分数分布差异很大的不同 query 不一定都合适。
- "最大间隙"双簇动态阈值（启发自用户提供的 K-Means 双簇过滤思路，改写为零依赖等价实现）：
  对一维（1D）已排序分数做双簇分割，等价于在最大间隙处切一刀——不需要 sklearn/numpy，
  排序后遍历一次相邻差值即可找到同样的切分点，没有迭代算法的随机性和收敛问题。
  分数全部足够接近（低分也达到最高分的一定比例）时全保留（对应原实现的"高分直通车"，
  但改写为相对判断，因为 BM25 分数无上界、不同 query 间不可比，不能用原实现里针对
  归一化相似度设计的绝对阈值 0.7）；否则在最大间隙处分高/低两簇，阈值取高分簇最小值
  向低分簇最大值下探一定比例，并兜底不低于低分簇最大值——按分数实际聚集形态自适应
  决定截断线，不是固定比例。

用法：
    python tests/research_keyword_recall.py
"""
import json
import logging
import sqlite3
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

import jieba  # noqa: E402
import jieba.analyse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import paths  # noqa: E402

jieba.setLogLevel(logging.ERROR)


def load_active_memories() -> list[dict]:
    memory_file = paths.workspace_memory_jsonl_file()
    records = []
    with memory_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("status") == "active":
                records.append(record)
    return records


def _searchable_text(record: dict) -> str:
    return "\n".join([
        record.get("insight", ""),
        record.get("reuse_condition", ""),
    ]).lower()


def keyword_recall(records: list[dict], keywords: list[str], top_k: int = 10) -> list[dict]:
    """对每条记忆做关键词子串匹配，返回命中记录及命中详情，按命中数/新旧排序。"""
    keywords = [kw.strip().lower() for kw in keywords if kw.strip()]
    if not keywords:
        return []

    scored = []
    for record in records:
        text = _searchable_text(record)
        matched = [kw for kw in keywords if kw in text]
        if matched:
            scored.append({
                "id": record.get("id", ""),
                "category": record.get("category", ""),
                "updated_at": record.get("updated_at", ""),
                "matched_keywords": matched,
                "match_count": len(matched),
                "insight_preview": (record.get("insight", "") or "")[:80],
            })

    scored.sort(key=lambda item: (-item["match_count"], item["updated_at"]), reverse=False)
    # 上面 updated_at 升序会把旧的排前面，这里换成：先按 match_count 降序，再按 updated_at 降序（新的在前）
    scored.sort(key=lambda item: item["updated_at"], reverse=True)
    scored.sort(key=lambda item: item["match_count"], reverse=True)
    return scored[:top_k]


FIELDS = ("insight", "reuse_condition")


def _segment_text(text: str) -> str:
    """jieba 预分词（搜索引擎模式提高召回），只保留含字母/数字且长度 >= 2 的 token，
    空格连接供 FTS5 使用。与查询侧 `_query_terms` 的单字过滤规则保持一致：查询侧永远
    不会产出单字 token 去匹配，索引侧保留单字 token 没有意义，统一过滤更干净。
    """
    tokens = [
        t for t in jieba.lcut_for_search(text or "")
        if any(ch.isalnum() for ch in t) and len(t) >= 2
    ]
    return " ".join(tokens)


def _query_terms(keyword: str) -> list[str]:
    """单个关键词 -> 查询词列表。

    纯 ASCII 关键词（变量名/标识符/命令）保留整体，交给 FTS5 tokenizer 按短语处理，
    保留 `memory_recall_model`、`sub-agent` 这类结构内部 token 的相邻性；
    含中文的关键词用 jieba 分词后按 AND 组合。

    过滤长度 < 2 的 token（如中文单字"子"），因为这类 token 几乎不含区分度，
    OR 模式下会大量引入噪音；英文单词天然长度 >= 2，不受影响。
    """
    if not any("\u4e00" <= ch <= "\u9fff" for ch in keyword):
        stripped = keyword.strip()
        return [stripped] if stripped and any(ch.isalnum() for ch in stripped) else []
    return [t for t in jieba.lcut_for_search(keyword) if any(ch.isalnum() for ch in t) and len(t) >= 2]


def build_match_query(keywords: list[str]) -> str:
    """关键词列表 -> FTS5 MATCH 表达式：全部拆成 token 后用 OR 连接。

    宽召回策略：允许少量假阳性，但不能漏掉真正相关的记忆——所以不要求同一关键词的
    分词全部命中（AND 太严格，容易因为分词边界漏判，例如"子代理委派"被切成
    ['子','代理','委派']，AND 要求三个都出现在同一字段里，但多数记忆只提到"子代理"
    而不提"委派"，会被整体漏掉）。BM25 本身会给命中更多 token、更稀有 token 的记忆
    更高分，OR 不会丢排序能力，只是放宽了"进入候选池"的门槛。
    """
    terms = []
    for keyword in keywords:
        terms.extend(_query_terms(keyword))
    if not terms:
        return ""
    quoted = ['"{}"'.format(t.replace('"', '""')) for t in terms]
    return " OR ".join(quoted)


HIGH_FREQUENCY_DF_RATIO = 0.4


def compute_document_frequency(records: list[dict], fields: tuple[str, ...] = FIELDS) -> dict[str, int]:
    """统计每个 token 在多少条记忆的指定字段里出现过（文档频率 DF）。"""
    df: dict[str, int] = {}
    for record in records:
        text = " ".join(str(record.get(field, "") or "") for field in fields)
        tokens = {
            t for t in jieba.lcut_for_search(text)
            if any(ch.isalnum() for ch in t) and len(t) >= 2
        }
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    return df


def build_match_query_with_df_cap(
    keywords: list[str],
    document_frequency: dict[str, int],
    total_docs: int,
    high_frequency_ratio: float = HIGH_FREQUENCY_DF_RATIO,
) -> str:
    """候选池控制：高频 token（DF 占比 > high_frequency_ratio，如"测试"覆盖 35.7% 记忆）
    不单独加入 OR 候选触发集，避免它单独就能把大量弱相关记忆拉入候选池。

    被排除的高频 token 不是被整体丢弃——它只是不再单独触发 MATCH；只要该记忆
    因为命中了其他稀有 token 而进入候选池，FTS5 的 bm25() 仍会用这个高频 token
    的真实出现次数参与计分（因为索引里的内容本身没有变，只是查询表达式里不再
    用它作为独立的 OR 分支）。如果一组关键词里全部 token 都是高频词（没有任何
    稀有词），则放弃过滤、全部保留，避免查询完全空载。
    """
    terms = []
    for keyword in keywords:
        terms.extend(_query_terms(keyword))
    if not terms:
        return ""

    high_frequency_cutoff = total_docs * high_frequency_ratio
    rare_terms = [t for t in terms if document_frequency.get(t, 0) <= high_frequency_cutoff]
    effective_terms = rare_terms if rare_terms else terms
    quoted = ['"{}"'.format(t.replace('"', '""')) for t in effective_terms]
    return " OR ".join(quoted)

RELATIVE_SCORE_THRESHOLD = 0.3
MIN_ABSOLUTE_SCORE = 1.5


class Fts5MemoryIndex:
    """SQLite FTS5 + BM25 检索：jieba 预分词写入 FTS5 表，用内置 bm25() 排序。"""

    def __init__(self, records: list[dict], fields: tuple[str, ...] = FIELDS):
        self.records = list(records)
        self.fields = fields
        self.conn = sqlite3.connect(":memory:")
        columns = ", ".join(fields)
        self.conn.execute(
            f"CREATE VIRTUAL TABLE memories USING fts5({columns}, tokenize='unicode61')"
        )
        placeholders = ", ".join("?" for _ in fields)
        insert_sql = f"INSERT INTO memories(rowid, {columns}) VALUES (?, {placeholders})"
        for rowid, record in enumerate(self.records, start=1):
            values = [_segment_text(str(record.get(field, "") or "")) for field in fields]
            self.conn.execute(insert_sql, (rowid, *values))
        self.conn.commit()
        self.document_frequency = compute_document_frequency(self.records, fields)

    def search(
        self,
        keywords: list[str],
        weights: tuple[float, ...] | None = None,
        cap_high_frequency_terms: bool = False,
    ) -> list[dict]:
        """返回 FTS5 MATCH 命中的全部候选（不设数量上限），按分数降序排列。

        不在这里做筛选：筛选策略（固定相对阈值 / 最大间隙二分）由调用方对
        返回结果独立应用，方便在同一批候选上对比不同策略。

        cap_high_frequency_terms=True 时，候选触发集排除高频 token（见
        `build_match_query_with_df_cap`），用于对比"候选池大小"的改善效果；
        默认 False 保持原有行为，方便回归对比。
        """
        if cap_high_frequency_terms:
            match_query = build_match_query_with_df_cap(
                keywords, self.document_frequency, len(self.records)
            )
        else:
            match_query = build_match_query(keywords)
        if not match_query:
            return []
        if weights:
            weight_args = ", ".join(str(w) for w in weights)
            score_expr = f"-bm25(memories, {weight_args})"
        else:
            score_expr = "-bm25(memories)"
        rows = self.conn.execute(
            f"SELECT rowid, {score_expr} AS score FROM memories "
            "WHERE memories MATCH ? ORDER BY score DESC",
            (match_query,),
        ).fetchall()
        if not rows:
            return []
        results = []
        for rowid, score in rows:
            record = self.records[rowid - 1]
            results.append({
                "id": record.get("id", ""),
                "category": record.get("category", ""),
                "updated_at": record.get("updated_at", ""),
                "score": score,
                "insight_preview": (record.get("insight", "") or "")[:80],
            })
        return results


def filter_relative_threshold(
    hits: list[dict],
    relative_threshold: float = RELATIVE_SCORE_THRESHOLD,
    min_absolute_score: float = MIN_ABSOLUTE_SCORE,
) -> list[dict]:
    """固定相对阈值 + 固定最低分数兜底：保留 score >= max(最高分 * relative_threshold,
    min_absolute_score) 的记录。

    相对阈值单独使用时，当 top1 本身分数就很弱（意味着连最佳匹配都是弱相关）时，
    按比例放出来的记录依然可能全是噪音——相对阈值无法感知"绝对质量"，只能感知
    "相对于本次最佳匹配有多接近"。固定最低分数作为二次兜底（AND 关系，取两者中
    更严格的那个），防止这种 top1 偏弱场景下相对阈值形同虚设。

    注意 BM25 分数不能跨查询直接比较（IDF 项 log(N/n) 随候选集合和命中词变化），
    所以这个固定阈值只能是一个粗略经验值，不是精确校准的绝对标准；基于已观察到的
    6 组真实查询分布（低分噪音普遍落在 1.0 以下），min_absolute_score 取 1.2。
    """
    if not hits:
        return []
    top_score = hits[0]["score"]
    relative_cutoff = top_score * relative_threshold if top_score > 0 else 0
    cutoff = max(relative_cutoff, min_absolute_score)
    return [item for item in hits if item["score"] >= cutoff]


MAX_GAP_MIN_RATIO = 0.3


def filter_max_gap(
    hits: list[dict],
    min_ratio: float = MAX_GAP_MIN_RATIO,
) -> list[dict]:
    """最大间隙二分：在已降序排列的分数序列里找相邻差值最大的切分点，分数两侧分别
    视为"高分簇"和"低分簇"，保留高分簇。

    等价于 1D K-Means（k=2）的效果，但零依赖、无随机性：k=2 的一维 K-Means 最优分割
    点必然落在排序后某个相邻间隙上，直接遍历相邻差值取最大值即可，不需要迭代算法。

    兜底：若最高分已经不比最低分高出 min_ratio（分数本身很集中/很接近），直接全保留，
    不做任何切分——对应用户参考实现里"分数全部达到高分直通车线时全保留"的意图，但
    改成相对判断，因为 BM25 分数无上界、不同 query 间不可比，不能用绝对阈值 0.7。
    """
    n = len(hits)
    if n < 2:
        return list(hits)

    scores = [item["score"] for item in hits]
    top_score, bottom_score = scores[0], scores[-1]
    if top_score <= 0 or bottom_score >= top_score * min_ratio:
        return list(hits)

    best_gap = -1.0
    best_cut = n  # 切分点：best_cut 之前（含）属于高分簇
    for i in range(n - 1):
        gap = scores[i] - scores[i + 1]
        if gap > best_gap:
            best_gap = gap
            best_cut = i + 1

    return hits[:best_cut]


def print_substring_hits(hits: list[dict], limit: int = 5) -> None:
    if not hits:
        print("  （无命中）")
        return
    for item in hits[:limit]:
        print(f"  - {item['id']} | match={item['matched_keywords']} | updated_at={item['updated_at']}")
        print(f"      insight: {item['insight_preview']}...")


def print_fts5_hits(hits: list[dict], limit: int | None = None) -> None:
    if not hits:
        print("  （无命中）")
        return
    shown = hits if limit is None else hits[:limit]
    for item in shown:
        print(f"  - {item['id']} | score={item['score']:.2f} | updated_at={item['updated_at']}")
        print(f"      insight: {item['insight_preview']}...")
    if limit is not None and len(hits) > limit:
        print(f"  ...（还有 {len(hits) - limit} 条，已省略）")


TEST_CASES = [
    ("查询1：pytest 相关问题", ["pytest", "测试"]),
    ("查询2：记忆配置默认值", ["memory_pre_recall", "默认值"]),
    ("查询3：Responses 协议重试", ["Responses", "重试", "retry"]),
    ("查询4：子代理委派", ["sub-agent", "子代理", "DelegateTasks"]),
    ("查询5：完全不相关的词", ["区块链", "blockchain"]),
    ("查询6：精确字段名", ["memory_recall_model"]),
]


def analyze_query_tokenization(query: str) -> dict:
    """对长 query 整体分词，展示原始分词结果与过滤后的最终 token。"""
    raw_tokens = jieba.lcut_for_search(query)
    final_terms = _query_terms(query) if not any("\u4e00" <= ch <= "\u9fff" for ch in query) else [
        t for t in raw_tokens if any(ch.isalnum() for ch in t) and len(t) >= 2
    ]
    dropped = [t for t in raw_tokens if t not in final_terms]
    return {
        "raw_token_count": len(raw_tokens),
        "raw_tokens": raw_tokens,
        "final_terms": final_terms,
        "final_term_count": len(final_terms),
        "dropped_tokens": dropped,
    }


LONG_QUERY_TOKEN_THRESHOLD = 20
EXTRACT_TAGS_TOP_K = 15


def extract_query_terms(query: str, top_k: int = EXTRACT_TAGS_TOP_K) -> list[str]:
    """长 query 的关键词提取：用 jieba.analyse.extract_tags（TF-IDF）取 top_k 个关键词，
    不做全量分词。

    用于替代"长文本直接整体分词"：全量分词会让 token 数随文本长度线性增长，候选池
    随之膨胀到接近全库规模，固定相对阈值在候选同质化（都含有若干共同高频技术词）时
    会失效（见实验记录：472 字 context_prompt 全量分词产出 86 个 token，阈值筛选后
    仍保留 34/70 条，等同没筛）。TF-IDF 关键词提取按词在该文本中的重要性打分，只取
    最具代表性的少量词，从源头控制候选池规模。
    """
    tags = jieba.analyse.extract_tags(query, topK=top_k)
    terms = []
    for tag in tags:
        if not any("\u4e00" <= ch <= "\u9fff" for ch in tag):
            if any(ch.isalnum() for ch in tag):
                terms.append(tag)
        elif len(tag) >= 2:
            terms.append(tag)
    return terms


def resolve_query_terms(query: str, long_query_threshold: int = LONG_QUERY_TOKEN_THRESHOLD) -> list[str]:
    """按 query 长度自动选择分词策略：短 query 全量分词，长 query 走 TF-IDF 关键词提取。

    用粗略分词后的 token 数而非字符数判断"长短"，因为中英文每字符信息密度差异很大，
    token 数更直接反映"过 OR 查询后会产生多少查询分支"。
    """
    raw_tokens = [t for t in jieba.lcut_for_search(query) if any(ch.isalnum() for ch in t)]
    if len(raw_tokens) <= long_query_threshold:
        return [t for t in raw_tokens if len(t) >= 2 or not any("\u4e00" <= ch <= "\u9fff" for ch in t)]
    return extract_query_terms(query)


LONG_QUERY_CASES = [
    (
        "用户请求预召回：用户原话（短句，日常提问）",
        "现在记忆召回工具的用法是怎样的？",
    ),
    (
        "用户请求预召回：用户原话（技术请求）",
        "另外记忆管理的时候会注入evidence字段吗？",
    ),
    (
        "子代理预召回：拼接 Task ID + Role + Context Prompt（长文本）",
        "# Sub-Agent Delegated Task\n\n"
        "## Task ID\n7\n\n"
        "## Role\nBackend Developer\n\n"
        "## Context Prompt\n"
        "实现记忆召回从 LLM 选择器切换为 SQLite FTS5 + BM25 的检索方案。"
        "需要修改 utils/memory.py 中的 recall_long_term_memories 函数，"
        "替换掉 select_relevant_memory_ids 的调用，改为对 query 分词后用 "
        "FTS5 查询 insight 和 reuse_condition 字段，并应用固定相对阈值 0.3 做筛选。"
        "同时需要在 requirements.txt 中加入 jieba 依赖，并验证 PyInstaller 打包时"
        "能正确收集 jieba 的词典数据文件。禁止删除 SelectRelevantMemories 工具定义"
        "之外的其他记忆管理工具（AppendLongTermMemory/UpdateLongTermMemory/"
        "DeleteLongTermMemory）。",
    ),
    (
        "子代理预召回：只用 Context Prompt（去掉 Task ID/Role 包装）",
        "实现记忆召回从 LLM 选择器切换为 SQLite FTS5 + BM25 的检索方案。"
        "需要修改 utils/memory.py 中的 recall_long_term_memories 函数，"
        "替换掉 select_relevant_memory_ids 的调用，改为对 query 分词后用 "
        "FTS5 查询 insight 和 reuse_condition 字段，并应用固定相对阈值 0.3 做筛选。"
        "同时需要在 requirements.txt 中加入 jieba 依赖，并验证 PyInstaller 打包时"
        "能正确收集 jieba 的词典数据文件。禁止删除 SelectRelevantMemories 工具定义"
        "之外的其他记忆管理工具（AppendLongTermMemory/UpdateLongTermMemory/"
        "DeleteLongTermMemory）。",
    ),
    (
        "Agent 主动调用 RecallLongTermMemory：模型自己写的 query",
        "MakeCode 项目中关于 FilePatch 和 FileEdit 的使用优先级规则，以及"
        "何时应该用 FileEdit 作为 fallback",
    ),
]


def main():
    records = load_active_memories()
    print(f"当前工作区 active 记忆数：{len(records)}")

    t0 = time.perf_counter()
    index = Fts5MemoryIndex(records)
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"FTS5 索引构建耗时：{build_ms:.1f} ms（字段：{', '.join(FIELDS)}，jieba 预分词）")

    for title, keywords in TEST_CASES:
        print(f"\n{'=' * 70}")
        print(f"[{title}] keywords={keywords}")

        substring_hits = keyword_recall(records, keywords, top_k=50)
        print(f"\n-- 方案一：子串匹配（命中 {len(substring_hits)} 条，展示前 5）--")
        print_substring_hits(substring_hits)

        t0 = time.perf_counter()
        all_hits = index.search(keywords)
        query_ms = (time.perf_counter() - t0) * 1000
        print(f"\nFTS5 + BM25 原始候选：{len(all_hits)} 条（查询耗时 {query_ms:.2f} ms）")

        relative_hits = filter_relative_threshold(all_hits)
        print(f"\n-- 筛选策略 A：固定相对阈值 {RELATIVE_SCORE_THRESHOLD}（命中 {len(relative_hits)} 条）--")
        print_fts5_hits(relative_hits)

        gap_hits = filter_max_gap(all_hits)
        print(f"\n-- 筛选策略 B：最大间隙二分（命中 {len(gap_hits)} 条）--")
        print_fts5_hits(gap_hits)

        capped_hits = index.search(keywords, cap_high_frequency_terms=True)
        capped_filtered = filter_relative_threshold(capped_hits)
        print(f"\n-- 候选池控制：排除 DF 占比 > {HIGH_FREQUENCY_DF_RATIO} 的高频 token 后 --")
        print(f"   候选池：{len(all_hits)} -> {len(capped_hits)}；阈值筛选后：{len(relative_hits)} -> {len(capped_filtered)}")
        print_fts5_hits(capped_filtered)

    print(f"\n{'=' * 70}")
    print("附加实验：字段权重（insight=1.0, reuse_condition=2.0）")
    for title, keywords in [TEST_CASES[0], TEST_CASES[2]]:
        weighted_all = index.search(keywords, weights=(1.0, 2.0))
        weighted_hits = filter_max_gap(weighted_all)
        print(f"\n[{title}] keywords={keywords}（reuse_condition 权重 x2，最大间隙二分）")
        print_fts5_hits(weighted_hits)


    print(f"\n{'=' * 70}")
    print("实验：长 query 整体分词（验证第四点——对 query 做分词而非要求关键词列表参数）")
    for title, query in LONG_QUERY_CASES:
        print(f"\n{'-' * 70}")
        print(f"[{title}]")
        print(f"query 长度：{len(query)} 字符")
        print(f"query 预览：{query[:120]}{'...' if len(query) > 120 else ''}")

        analysis = analyze_query_tokenization(query)
        print(f"jieba 原始分词：{analysis['raw_token_count']} 个 token")
        print(f"  {analysis['raw_tokens']}")
        print(f"过滤后最终查询 token：{analysis['final_term_count']} 个")
        print(f"  {analysis['final_terms']}")
        print(f"被过滤掉的 token（单字/纯符号等）：{analysis['dropped_tokens']}")

        t0 = time.perf_counter()
        hits = index.search(analysis["final_terms"])
        query_ms = (time.perf_counter() - t0) * 1000
        filtered = filter_relative_threshold(hits)
        print(f"[全量分词] FTS5 召回：候选 {len(hits)} 条，阈值筛选后 {len(filtered)} 条（查询耗时 {query_ms:.2f} ms）")
        print_fts5_hits(filtered, limit=5)

        tags = extract_query_terms(query)
        print(f"\nTF-IDF 关键词提取（top {EXTRACT_TAGS_TOP_K}）：{len(tags)} 个")
        print(f"  {tags}")
        t0 = time.perf_counter()
        tag_hits = index.search(tags)
        tag_query_ms = (time.perf_counter() - t0) * 1000
        tag_filtered = filter_relative_threshold(tag_hits)
        print(f"[TF-IDF 关键词] FTS5 召回：候选 {len(tag_hits)} 条，阈值筛选后 {len(tag_filtered)} 条（查询耗时 {tag_query_ms:.2f} ms）")
        print_fts5_hits(tag_filtered, limit=8)

        auto_terms = resolve_query_terms(query)
        print(f"\n按长度自动选择策略（阈值 {LONG_QUERY_TOKEN_THRESHOLD} token）：选用 "
              f"{'全量分词' if len(auto_terms) == analysis['final_term_count'] and auto_terms == analysis['final_terms'] else 'TF-IDF 提取'}，"
              f"最终 {len(auto_terms)} 个 token")

if __name__ == "__main__":
    main()
