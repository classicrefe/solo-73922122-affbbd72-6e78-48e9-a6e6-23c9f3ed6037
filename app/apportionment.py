"""再保份额取整与报送清单生成（纯函数、无 IO，可独立单测）。

业务规则
========
1. 分保方约定比例以整数基点给出（1 个基点 = 0.01%），各方比例合计 10000。
2. 每笔理赔金额（非负整数，单位：分）按约定比例切给各分保方，每个份额
   只能是理论值 ``amount * ratio / 10000`` 向下取整（floor）或向上取整
   （ceil）到整分，二者在整除时相等。
3. 每笔理赔各方份额之和必须恰好等于该笔理赔金额（行和约束）。
4. 各分保方跨笔合计必须恰好等于已核定总额（列和约束）。
5. 已入账明细原样保留：入账值必须本身就是 floor/ceil 之一，其余份额在
   剩余可行空间内取整。
6. 无法同时满足时抛出 :class:`AllocationError`，绝不返回部分清单。

算法
====
对每个理赔 i、分保方 j，理论份额写成
``floor(i,j) + y(i,j)``，其中 ``y`` 只能取 0 或 1（整除时恒为 0）。
行和约束要求每行向上取整的格点数固定为
``r_i = amount_i - sum_j floor(i,j)``；
列和约束减去 floor 基数后，等价于每列向上取整的格点数固定为
``c_j = target_j - sum_i floor(i,j)``。

于是问题归约为二部图 b-匹配：左节点（理赔）出度 r_i、右节点（分保方）
入度 c_j，边仅在非整除格点（floor<ceil）且未被入账锁定时存在，已入账为
ceil 的格点直接预占。用最小费用最大流（Dijkstra + 势能）求可行流；边费
取余数排名（余数 ``amount*ratio - floor*10000`` 大者排名靠前、费用低，
平局再按理赔 id、分保方次序），使"余数大者优先向上取整"（最大余数法）
成为唯一的确定性规范解——同一快照与同一输入重取，结果逐格一致。
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq

# 基点总分：约定比例合计必须为 10000（即 100.00%）
BASIS_TOTAL = 10000
# 单次报送清单允许的最大理赔笔数
MAX_CLAIMS = 100


class AllocationError(Exception):
    """取整/约束无法同时满足。``code`` 供 API 层映射为稳定错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Party:
    """分保方约定：标识、基点比例、核定总额（分）。"""

    party: str
    ratio_bps: int
    approved_total: int


@dataclass(frozen=True)
class BookedItem:
    """已入账明细：某笔理赔给某分保方的整分金额，原样保留。"""

    claim_id: int
    party: str
    amount: int


@dataclass(frozen=True)
class ClaimShare:
    """清单中一笔理赔给一个分保方的份额。"""

    party: str
    ratio_bps: int
    amount: int
    rounded: str  # "up" | "down" | "exact"（整除时）
    booked: bool


@dataclass(frozen=True)
class ClaimAllocation:
    """一笔理赔的完整分保清单。"""

    claim_id: int
    amount: int
    shares: tuple[ClaimShare, ...]


def _floor_ceil(amount: int, ratio_bps: int) -> tuple[int, int]:
    """理论份额 amount*ratio/10000 的整分 floor / ceil（全程整数运算）。"""
    numerator = amount * ratio_bps
    floor = numerator // BASIS_TOTAL
    ceil = floor if numerator % BASIS_TOTAL == 0 else floor + 1
    return floor, ceil


# ----------------------------------------------------------------------
# 最小费用最大流（节点少、边稀疏；Dijkstra + 势能，确定性遍历）
# ----------------------------------------------------------------------
class _MinCostMaxFlow:
    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[dict]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> None:
        # 邻接表追加顺序固定，保证同费用下的遍历次序确定。
        forward = {"to": v, "cap": cap, "cost": cost, "rev": len(self.graph[v])}
        backward = {"to": u, "cap": 0, "cost": -cost, "rev": len(self.graph[u])}
        self.graph[u].append(forward)
        self.graph[v].append(backward)

    def flow(self, source: int, sink: int, required: int) -> int:
        """推送恰好 ``required`` 单位最小费用流；不可行时抛 AllocationError。"""
        n = self.n
        INF = 10**30
        potential = [0] * n  # 初始边费均非负，初始势能为 0
        achieved = 0
        total_cost = 0
        while achieved < required:
            dist = [INF] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[source] = 0
            # 所有费用经势能折算后非负，Dijkstra 适用。
            heap: list[tuple[int, int]] = [(0, source)]
            while heap:
                d, u = heapq.heappop(heap)
                if d != dist[u]:
                    continue
                for ei, edge in enumerate(self.graph[u]):
                    if edge["cap"] <= 0:
                        continue
                    reduced = edge["cost"] + potential[u] - potential[edge["to"]]
                    nd = d + reduced
                    if nd < dist[edge["to"]]:
                        dist[edge["to"]] = nd
                        prev_v[edge["to"]] = u
                        prev_e[edge["to"]] = ei
                        heapq.heappush(heap, (nd, edge["to"]))
            if dist[sink] == INF:
                raise AllocationError(
                    "allocation_infeasible",
                    "在满足每笔仅可按约定比例向上/向下取整整分、且已入账金额"
                    "原样保留的前提下，不存在让各方跨笔合计恰好等于核定总额的"
                    "取整方案（可行流无法达到所需向上取整格点数）",
                )
            for v in range(n):
                if dist[v] < INF:
                    potential[v] += dist[v]
            # 本路径只承载 1 单位流（每条边容量为 1），逐格点增广。
            v = sink
            while v != source:
                u = prev_v[v]
                edge = self.graph[u][prev_e[v]]
                edge["cap"] -= 1
                self.graph[v][edge["rev"]]["cap"] += 1
                v = u
            achieved += 1
            total_cost += potential[sink] - potential[source]
        return total_cost


def build_allocation(
    claims: list[tuple[int, int]],
    parties: list[Party],
    booked: list[BookedItem] | None = None,
) -> list[ClaimAllocation]:
    """生成按理赔 id 升序的完整分保清单。

    :param claims: ``(claim_id, amount)`` 列表，金额为非负整数（分），
        调用方需已保证笔数 <= 100；本函数内部按 claim_id 排序。
    :param parties: 2~5 家分保方，比例（基点）合计 10000。
    :param booked: 可为空的已入账明细。
    :raises AllocationError: 任何约束无法同时满足时抛出，不返回部分结果。
    """
    booked = list(booked or [])

    # ------------------------------------------------------------------
    # 基础校验（结构合法性）
    # ------------------------------------------------------------------
    if not (2 <= len(parties) <= 5):
        raise AllocationError(
            "invalid_parties", "分保方数量必须在 2~5 家之间"
        )
    if sum(p.ratio_bps for p in parties) != BASIS_TOTAL:
        raise AllocationError(
            "invalid_ratios",
            f"各方约定比例（整数基点）合计必须为 {BASIS_TOTAL}，"
            f"实际为 {sum(p.ratio_bps for p in parties)}",
        )
    if len({p.party for p in parties}) != len(parties):
        raise AllocationError("invalid_parties", "分保方标识不得重复")
    if any(p.approved_total < 0 for p in parties):
        raise AllocationError(
            "invalid_approved_total", "核定总额必须为非负整数（分）"
        )

    claims = sorted(claims, key=lambda c: c[0])
    if len(claims) > MAX_CLAIMS:
        raise AllocationError(
            "too_many_claims",
            f"单次报送清单至多包含 {MAX_CLAIMS} 笔理赔，实际 {len(claims)} 笔",
        )
    if any(amount < 0 for _, amount in claims):
        raise AllocationError("invalid_claim_amount", "理赔金额必须为非负整数（分）")

    party_index = {p.party: j for j, p in enumerate(parties)}
    claim_index = {cid: i for i, (cid, _) in enumerate(claims)}

    # 已入账明细合法性
    seen_booked: set[tuple[int, str]] = set()
    for item in booked:
        if item.claim_id not in claim_index:
            raise AllocationError(
                "booked_claim_unknown",
                f"已入账明细引用的理赔 id {item.claim_id} 不在本保单快照清单内",
            )
        if item.party not in party_index:
            raise AllocationError(
                "booked_party_unknown",
                f"已入账明细引用的分保方 {item.party!r} 不在约定分保方名单内",
            )
        if item.amount < 0:
            raise AllocationError(
                "invalid_booked_amount",
                f"理赔 {item.claim_id} 给 {item.party} 的已入账金额必须为非负整数（分）",
            )
        key = (item.claim_id, item.party)
        if key in seen_booked:
            raise AllocationError(
                "duplicate_booked",
                f"理赔 {item.claim_id} 给 {item.party} 的已入账明细重复",
            )
        seen_booked.add(key)

    n_claims = len(claims)
    n_parties = len(parties)

    # floor/ceil 基数表
    floors: list[list[int]] = [[0] * n_parties for _ in range(n_claims)]
    ceils: list[list[int]] = [[0] * n_parties for _ in range(n_claims)]
    for i, (_, amount) in enumerate(claims):
        for j, party in enumerate(parties):
            floors[i][j], ceils[i][j] = _floor_ceil(amount, party.ratio_bps)

    # 入账锁定矩阵：None=未入账；否则锁定为该整分值
    locked: list[list[int | None]] = [
        [None] * n_parties for _ in range(n_claims)
    ]
    for item in booked:
        i = claim_index[item.claim_id]
        j = party_index[item.party]
        if not (floors[i][j] <= item.amount <= ceils[i][j]):
            raise AllocationError(
                "booked_not_roundable",
                f"理赔 {item.claim_id} 给 {item.party} 的已入账金额 "
                f"{item.amount} 不是约定比例对应的整分取值"
                f"（仅允许 {floors[i][j]} 或 {ceils[i][j]}），无法原样保留",
            )
        locked[i][j] = item.amount

    # ------------------------------------------------------------------
    # 归约为 0/1 向上取整矩阵 y：y=1 表示该格取 ceil
    # ------------------------------------------------------------------
    y: list[list[int]] = [[0] * n_parties for _ in range(n_claims)]
    # 整除格点恒为 0；入账为 ceil 的格点预占为 1，入账为 floor 的预占为 0
    free_cells: list[tuple[int, int]] = []
    for i in range(n_claims):
        for j in range(n_parties):
            if floors[i][j] == ceils[i][j]:
                if locked[i][j] is not None and locked[i][j] != floors[i][j]:
                    raise AllocationError(
                        "booked_not_roundable",
                        f"理赔 {claims[i][0]} 给 {parties[j].party} 的比例整除，"
                        f"已入账金额只能为 {floors[i][j]}",
                    )
                y[i][j] = 0
                continue
            if locked[i][j] is not None:
                y[i][j] = 1 if locked[i][j] == ceils[i][j] else 0
            else:
                free_cells.append((i, j))

    # 每行必须向上取整的格点数
    row_need: list[int] = []
    for i, (_, amount) in enumerate(claims):
        need = amount - sum(floors[i])
        already = sum(y[i])
        if need < already:
            raise AllocationError(
                "allocation_infeasible",
                f"理赔 {claims[i][0]} 的已入账向上取整格点数（{already}）"
                f"超过该笔满足行和所需的向上取总格点数（{need}）",
            )
        row_need.append(need - already)

    # 全局总量先对齐：核定总额之和必须等于全部理赔金额之和，
    # 否则行和与列和不可能同时成立，给出最直观的失败原因。
    approved_sum = sum(p.approved_total for p in parties)
    claim_sum = sum(amount for _, amount in claims)
    if approved_sum != claim_sum:
        raise AllocationError(
            "totals_mismatch",
            f"各方核定总额之和（{approved_sum}）与保单全部理赔金额之和"
            f"（{claim_sum}）不相等，无法让每笔各方合计等于理赔金额且"
            "各方跨笔合计等于核定总额",
        )

    # 每列必须向上取整的格点数（由核定总额减去 floor 基数与已预占部分）
    col_need: list[int] = []
    for j, party in enumerate(parties):
        floor_sum = sum(floors[i][j] for i in range(n_claims))
        booked_sum = sum(
            locked[i][j] if locked[i][j] is not None else floors[i][j]
            for i in range(n_claims)
        )
        need = party.approved_total - booked_sum
        # 剩余自由格点能贡献的 0/1 区间
        free_in_col = [i for (i, jj) in free_cells if jj == j]
        if need < 0 or need > len(free_in_col):
            raise AllocationError(
                "allocation_infeasible",
                f"分保方 {party.party} 的核定总额 {party.approved_total} 无法达成："
                f"扣除已入账与向下取整基数后，还需 {need} 个自由格点向上取整，"
                f"但仅剩 {len(free_in_col)} 个可行格点",
            )
        col_need.append(need)

    if sum(row_need) != sum(col_need):
        # 理论上全局总量对齐后不会走到这里，留作防御性兜底。
        raise AllocationError(
            "allocation_infeasible",
            "行和与列和要求的向上取总格点数不一致，不存在满足全部约束的取整方案",
        )

    # ------------------------------------------------------------------
    # 最小费用最大流求自由格点的规范 0/1 解
    # 节点：0=源点，1..n_claims=理赔，其后 n_parties 个=分保方，末位=汇点
    # ------------------------------------------------------------------
    required = sum(row_need)
    if required:
        src = 0
        claim_base = 1
        party_base = claim_base + n_claims
        sink = party_base + n_parties
        mcmf = _MinCostMaxFlow(sink + 1)
        for i in range(n_claims):
            if row_need[i] > 0:
                mcmf.add_edge(src, claim_base + i, row_need[i], 0)
        for j in range(n_parties):
            if col_need[j] > 0:
                mcmf.add_edge(party_base + j, sink, col_need[j], 0)

        # 费用即余数排名：余数最大者排名 0、费用最低，最小费用流优先选它
        # 向上取整（最大余数法）；余数相同按理赔 id、再按分保方次序，
        # 排名全局唯一，因此规范解唯一且确定。
        def remainder(i: int, j: int) -> int:
            amount = claims[i][1]
            return amount * parties[j].ratio_bps - floors[i][j] * BASIS_TOTAL

        ranked = sorted(
            free_cells,
            key=lambda cell: (-remainder(*cell), claims[cell[0]][0], cell[1]),
        )
        rank = {cell: k for k, cell in enumerate(ranked)}
        # 显式记录每个自由格点对应的正向边，避免靠残余容量推断流量
        cell_edges: dict[tuple[int, int], dict] = {}
        for i, j in free_cells:
            u = claim_base + i
            v = party_base + j
            edge = {"to": v, "cap": 1, "cost": rank[(i, j)],
                    "rev": len(mcmf.graph[v])}
            backward = {"to": u, "cap": 0, "cost": -edge["cost"],
                        "rev": len(mcmf.graph[u])}
            mcmf.graph[u].append(edge)
            mcmf.graph[v].append(backward)
            cell_edges[(i, j)] = edge
        mcmf.flow(src, sink, required)

        # 正向边容量被耗尽即代表该格点分配到 1 单位向上取整流
        for i, j in free_cells:
            if cell_edges[(i, j)]["cap"] == 0:
                y[i][j] = 1

    # ------------------------------------------------------------------
    # 组装结果并做最终断言式复核（防御性，任何不满足即整体失败）
    # ------------------------------------------------------------------
    result: list[ClaimAllocation] = []
    col_totals = [0] * n_parties
    for i, (claim_id, amount) in enumerate(claims):
        shares: list[ClaimShare] = []
        row_total = 0
        for j, party in enumerate(parties):
            value = floors[i][j] + y[i][j]
            # 自由格点理论上只能是 floor/ceil；入账值原样保留
            if locked[i][j] is not None:
                value = locked[i][j]
            if not (floors[i][j] <= value <= ceils[i][j]):
                raise AllocationError(
                    "allocation_infeasible",
                    f"内部校验失败：理赔 {claim_id} 给 {party.party} 的份额 "
                    f"{value} 不在 floor/ceil 区间内",
                )
            if floors[i][j] == ceils[i][j]:
                rounded = "exact"
            else:
                rounded = "up" if value == ceils[i][j] else "down"
            shares.append(
                ClaimShare(
                    party=party.party,
                    ratio_bps=party.ratio_bps,
                    amount=value,
                    rounded=rounded,
                    booked=locked[i][j] is not None,
                )
            )
            row_total += value
            col_totals[j] += value
        if row_total != amount:
            raise AllocationError(
                "allocation_infeasible",
                f"内部校验失败：理赔 {claim_id} 各方份额合计 {row_total} "
                f"不等于理赔金额 {amount}",
            )
        result.append(
            ClaimAllocation(claim_id=claim_id, amount=amount, shares=tuple(shares))
        )

    for j, party in enumerate(parties):
        if col_totals[j] != party.approved_total:
            raise AllocationError(
                "allocation_infeasible",
                f"内部校验失败：分保方 {party.party} 跨笔合计 {col_totals[j]} "
                f"不等于核定总额 {party.approved_total}",
            )

    return result
