"""再保份额取整算法的纯单元测试（不依赖数据库/HTTP，可直接运行）。

运行：
    python -m unittest tests.test_apportionment -v
"""
from __future__ import annotations

import random
import unittest

from app.apportionment import (
    BASIS_TOTAL,
    AllocationError,
    BookedItem,
    Party,
    _floor_ceil,
    build_allocation,
)


def reference_largest_remainder(claims, ratios: list[int]):
    """独立参考实现：逐笔按余数降序补向上取整（最大余数法）。"""
    totals = [0] * len(ratios)
    rows = {}
    for cid, amount in claims:
        floors = [_floor_ceil(amount, w)[0] for w in ratios]
        remainders = [amount * w - floors[j] * BASIS_TOTAL for j, w in enumerate(ratios)]
        need = amount - sum(floors)
        ups = set(sorted(range(len(ratios)), key=lambda j: (-remainders[j], j))[:need])
        row = [floors[j] + (1 if j in ups else 0) for j in range(len(ratios))]
        rows[cid] = row
        for j, v in enumerate(row):
            totals[j] += v
    return totals, rows


class TestFloorCeil(unittest.TestCase):
    def test_exact_division(self):
        self.assertEqual(_floor_ceil(100, 5000), (50, 50))
        self.assertEqual(_floor_ceil(0, 1234), (0, 0))

    def test_fractional(self):
        # 101 * 5000 / 10000 = 50.5
        self.assertEqual(_floor_ceil(101, 5000), (50, 51))
        # 100 * 3333 / 10000 = 33.33
        self.assertEqual(_floor_ceil(100, 3333), (33, 34))


class TestBasicAllocation(unittest.TestCase):
    def setUp(self):
        self.claims = [(1, 100), (2, 101)]
        self.parties = [Party("A", 5000, 100), Party("B", 5000, 101)]

    def test_row_and_column_totals(self):
        result = build_allocation(self.claims, self.parties)
        self.assertEqual([s.amount for s in result[0].shares], [50, 50])
        # 101 -> 50.5/50.5，列核定 A=100、B=101 决定向上取整归 B
        self.assertEqual([s.amount for s in result[1].shares], [50, 51])
        self.assertEqual(result[0].shares[0].rounded, "exact")
        self.assertEqual(result[1].shares[0].rounded, "down")
        self.assertEqual(result[1].shares[1].rounded, "up")
        col = [0, 0]
        for ca in result:
            self.assertEqual(sum(s.amount for s in ca.shares), ca.amount)
            for j, s in enumerate(ca.shares):
                col[j] += s.amount
        self.assertEqual(col, [100, 101])

    def test_sorted_by_claim_id_regardless_of_input_order(self):
        shuffled = [(2, 101), (1, 100)]
        result = build_allocation(shuffled, self.parties)
        self.assertEqual([ca.claim_id for ca in result], [1, 2])

    def test_deterministic_same_input(self):
        r1 = build_allocation(self.claims, self.parties)
        r2 = build_allocation(self.claims, self.parties)
        self.assertEqual(r1, r2)

    def test_output_sorted_and_share_order_matches_parties(self):
        result = build_allocation(self.claims, self.parties)
        self.assertEqual([s.party for s in result[0].shares], ["A", "B"])

    def test_zero_amount_claim(self):
        result = build_allocation(
            [(1, 0), (2, 100)], [Party("A", 5000, 50), Party("B", 5000, 50)]
        )
        self.assertEqual([s.amount for s in result[0].shares], [0, 0])
        self.assertTrue(all(s.rounded == "exact" for s in result[0].shares))

    def test_empty_claims_requires_zero_totals(self):
        self.assertEqual(
            build_allocation([], [Party("A", 5000, 0), Party("B", 5000, 0)]), []
        )
        with self.assertRaises(AllocationError) as ctx:
            build_allocation([], [Party("A", 5000, 1), Party("B", 5000, 0)])
        self.assertEqual(ctx.exception.code, "totals_mismatch")


class TestInputValidation(unittest.TestCase):
    def setUp(self):
        self.claims = [(1, 100)]
        self.parties = [Party("A", 5000, 50), Party("B", 5000, 50)]

    def test_party_count_bounds(self):
        one = [Party("A", 10000, 100)]
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(self.claims, one)
        self.assertEqual(ctx.exception.code, "invalid_parties")
        six = [Party(f"P{i}", 1000, 0) for i in range(6)]
        # 比例合计也不合法，先命中数量检查
        with self.assertRaises(AllocationError):
            build_allocation(self.claims, six)

    def test_ratios_must_sum_to_basis(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, [Party("A", 4000, 50), Party("B", 5000, 50)]
            )
        self.assertEqual(ctx.exception.code, "invalid_ratios")

    def test_duplicate_party_names(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, [Party("A", 5000, 50), Party("A", 5000, 50)]
            )
        self.assertEqual(ctx.exception.code, "invalid_parties")

    def test_negative_approved_total(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, [Party("A", 5000, 101), Party("B", 5000, -1)]
            )
        self.assertEqual(ctx.exception.code, "invalid_approved_total")

    def test_totals_mismatch(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, [Party("A", 5000, 49), Party("B", 5000, 50)]
            )
        self.assertEqual(ctx.exception.code, "totals_mismatch")

    def test_too_many_claims(self):
        claims = [(i, 100) for i in range(1, 102)]
        # 核定总额按 50/50 给出，先构造合法总额再触发笔数检查
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                claims, [Party("A", 5000, 50 * 101), Party("B", 5000, 50 * 101)]
            )
        self.assertEqual(ctx.exception.code, "too_many_claims")


class TestBookedItems(unittest.TestCase):
    def setUp(self):
        self.claims = [(1, 100), (2, 101)]
        self.parties = [Party("A", 5000, 100), Party("B", 5000, 101)]

    def test_booked_amount_preserved_verbatim(self):
        # claim2 给 A 入账 51（ceil），核定总额相应调整为 A=101、B=100
        result = build_allocation(
            self.claims,
            [Party("A", 5000, 101), Party("B", 5000, 100)],
            booked=[BookedItem(2, "A", 51)],
        )
        share_a = next(s for s in result[1].shares if s.party == "A")
        share_b = next(s for s in result[1].shares if s.party == "B")
        self.assertEqual(share_a.amount, 51)
        self.assertTrue(share_a.booked)
        self.assertEqual(share_b.amount, 50)
        self.assertFalse(share_b.booked)

    def test_booked_floor_locks_direction(self):
        # claim2 给 B 入账 50（floor），则 A 必须取 51
        result = build_allocation(
            self.claims,
            [Party("A", 5000, 101), Party("B", 5000, 100)],
            booked=[BookedItem(2, "B", 50)],
        )
        self.assertEqual(result[1].shares[0].amount, 51)
        self.assertEqual(result[1].shares[1].amount, 50)
        self.assertTrue(result[1].shares[1].booked)

    def test_booked_not_roundable_rejected(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, self.parties, booked=[BookedItem(2, "A", 47)]
            )
        self.assertEqual(ctx.exception.code, "booked_not_roundable")

    def test_booked_unknown_claim(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, self.parties, booked=[BookedItem(99, "A", 50)]
            )
        self.assertEqual(ctx.exception.code, "booked_claim_unknown")

    def test_booked_unknown_party(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, self.parties, booked=[BookedItem(1, "Z", 50)]
            )
        self.assertEqual(ctx.exception.code, "booked_party_unknown")

    def test_duplicate_booked(self):
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims,
                self.parties,
                booked=[BookedItem(1, "A", 50), BookedItem(1, "A", 50)],
            )
        self.assertEqual(ctx.exception.code, "duplicate_booked")

    def test_booked_makes_column_infeasible(self):
        # A 入账 claim2=51（占掉向上格点），却核定 A=100（需要 0 个向上）
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims,
                [Party("A", 5000, 100), Party("B", 5000, 101)],
                booked=[BookedItem(2, "A", 51)],
            )
        self.assertEqual(ctx.exception.code, "allocation_infeasible")

    def test_booked_exact_division_mismatch_rejected(self):
        # claim1=100、50% 整除，只能是 50
        with self.assertRaises(AllocationError) as ctx:
            build_allocation(
                self.claims, self.parties, booked=[BookedItem(1, "A", 51)]
            )
        self.assertEqual(ctx.exception.code, "booked_not_roundable")


class TestCanonicalLargestRemainder(unittest.TestCase):
    """与独立参考实现交叉验证。

    注意：逐笔最大余数法只在行内最优，而列和（核定总额）约束可能要求
    个别理赔偏离行内首选；因此这里用参考实现仅生成"可行的核定总额"，
    对被测解校验全部硬约束，并通过交换论证验证其全局成本最优性
    （在满足列约束的前提下，向上取整优先给余数更大的格点）。
    均分比例且列目标恰好兼容时，解应与逐笔参考解逐格相等。
    """

    def _check_invariants(self, claims, ratios, parties, result, booked=None):
        totals = [p.approved_total for p in parties]
        # 重取一致（确定性）
        self.assertEqual(build_allocation(claims, parties, booked), result)
        # 按理赔 id 升序
        self.assertEqual([ca.claim_id for ca in result], sorted(c[0] for c in claims))
        col_sum = [0] * len(ratios)
        for ca in result:
            self.assertEqual(sum(s.amount for s in ca.shares), ca.amount)
            for j, s in enumerate(ca.shares):
                floor, ceil = _floor_ceil(ca.amount, ratios[j])
                self.assertGreaterEqual(s.amount, floor)
                self.assertLessEqual(s.amount, ceil)
                col_sum[j] += s.amount
        self.assertEqual(col_sum, totals)

    def _check_global_optimality(self, claims, ratios, parties, result):
        """枚举全部可行 0/1 矩阵，验证被测解是全局成本最优且唯一确定。

        成本序与生产代码一致：余数降序、再按理赔 id、分保方次序。
        只用于小实例（自由格点 <= 12），暴力 2^k 可承受。
        """
        import itertools

        claims_sorted = sorted(claims)
        n = len(claims_sorted)
        m = len(ratios)
        floors, rems, free = [], [], []
        for cid, amount in claims_sorted:
            f_row, r_row = [], []
            for j, w in enumerate(ratios):
                f = _floor_ceil(amount, w)[0]
                f_row.append(f)
                r_row.append(amount * w - f * BASIS_TOTAL)
                if _floor_ceil(amount, w)[1] > f:
                    free.append((cid, j))
            floors.append(f_row)
            rems.append(r_row)
        self.assertLessEqual(len(free), 12, "本穷举测试仅适用于小实例")
        cid_idx = {cid: i for i, (cid, _) in enumerate(claims_sorted)}
        ranked = sorted(
            range(len(free)),
            key=lambda k: (-rems[cid_idx[free[k][0]]][free[k][1]],
                           free[k][0], free[k][1]),
        )
        rank = {k: pos for pos, k in enumerate(ranked)}

        row_need = [
            amount - sum(floors[i]) for i, (_, amount) in enumerate(claims_sorted)
        ]
        col_need = [
            parties[j].approved_total
            - sum(floors[i][j] for i in range(n))
            for j in range(m)
        ]
        best_cost = None
        feasible_count = 0
        for bits in itertools.product((0, 1), repeat=len(free)):
            y = [[0] * m for _ in range(n)]
            for k, bit in enumerate(bits):
                cid, j = free[k]
                y[cid_idx[cid]][j] = bit
            if [sum(row) for row in y] != row_need:
                continue
            if [sum(y[i][j] for i in range(n)) for j in range(m)] != col_need:
                continue
            feasible_count += 1
            cost = sum(rank[k] for k, bit in enumerate(bits) if bit)
            best_cost = cost if best_cost is None else min(best_cost, cost)
        self.assertGreaterEqual(feasible_count, 1)

        got_cost = 0
        for ca in result:
            i = cid_idx[ca.claim_id]
            for j, s in enumerate(ca.shares):
                if s.rounded == "up":
                    got_cost += rank[free.index((ca.claim_id, j))]
        self.assertEqual(got_cost, best_cost)

    def _check(self, claims, ratios):
        totals, _ = reference_largest_remainder(claims, ratios)
        parties = [
            Party(f"P{j}", ratios[j], totals[j]) for j in range(len(ratios))
        ]
        result = build_allocation(claims, parties)
        self._check_invariants(claims, ratios, parties, result)
        return result, totals

    def _check_brute(self, claims, ratios):
        """小实例：穷举全部可行解验证全局最优 + 确定性。"""
        totals, _ = reference_largest_remainder(claims, ratios)
        parties = [
            Party(f"P{j}", ratios[j], totals[j]) for j in range(len(ratios))
        ]
        result = build_allocation(claims, parties)
        self._check_invariants(claims, ratios, parties, result)
        self._check_global_optimality(claims, ratios, parties, result)
        return result

    def test_equal_ratios_100_claims_5_parties(self):
        random.seed(42)
        claims = [(cid, random.randint(0, 500000)) for cid in range(1, 101)]
        ratios = [2000, 2000, 2000, 2000, 2000]
        totals, ref_rows = reference_largest_remainder(claims, ratios)
        parties = [Party(f"P{j}", ratios[j], totals[j]) for j in range(5)]
        result = build_allocation(claims, parties)
        self._check_invariants(claims, ratios, parties, result)
        # 对称比例下列目标与逐笔参考解天然兼容，应逐格相等
        for ca in result:
            self.assertEqual(
                [s.amount for s in ca.shares], ref_rows[ca.claim_id]
            )

    def test_unequal_ratios_100_claims_4_parties(self):
        random.seed(7)
        claims = [(cid, random.randint(1, 999999)) for cid in range(1, 101)]
        self._check(claims, [1234, 4321, 3000, 1445])

    def test_2_parties_odd_amounts(self):
        claims = [(i, 2 * i + 1) for i in range(1, 50)]
        self._check(claims, [5000, 5000])

    def test_brute_force_small_instances(self):
        # 小规模穷举：覆盖 2~4 家、整除/非整除、平局、零金额等
        cases = [
            ([(1, 101), (2, 101), (3, 101)], [5000, 5000]),
            ([(1, 100), (2, 101), (3, 7)], [3333, 3333, 3334]),
            ([(1, 0), (2, 333), (3, 77)], [2500, 2500, 2500, 2500]),
            ([(1, 1), (2, 2), (3, 3)], [1234, 4321, 3000, 1445]),
            ([(1, 9999), (2, 1)], [7000, 3000]),
        ]
        for claims, ratios in cases:
            with self.subTest(claims=claims, ratios=ratios):
                self._check_brute(claims, ratios)

    def test_brute_force_booked_still_optimal(self):
        # 带入账锁定的小实例：锁定值原样保留，其余格点仍取全局最优
        claims = [(1, 101), (2, 101), (3, 101)]
        ratios = [5000, 5000]
        # 每笔 floor 均为 [50,50]，共 3 个向上格点。锁定中间笔给 A=51，
        # 则 A 列还需 0 个向上、B 列需 2 个；核定总额据此构造。
        booked = [BookedItem(2, "A", 51)]
        parties = [Party("A", 5000, 151), Party("B", 5000, 152)]
        result = build_allocation(claims, parties, booked=booked)
        self._check_invariants(claims, ratios, parties, result, booked=booked)
        locked = next(
            s for ca in result if ca.claim_id == 2 for s in ca.shares
            if s.party == "A"
        )
        self.assertEqual(locked.amount, 51)
        self.assertTrue(locked.booked)
        # A 列其余两笔必须向下（50），B 列全部向上（51）
        for ca in result:
            a = next(s for s in ca.shares if s.party == "A")
            b = next(s for s in ca.shares if s.party == "B")
            if ca.claim_id == 2:
                self.assertEqual((a.amount, b.amount), (51, 50))
            else:
                self.assertEqual((a.amount, b.amount), (50, 51))
        self.assertTrue(all(
            s.booked == (ca.claim_id == 2 and s.party == "A")
            for ca in result for s in ca.shares
        ))

    def test_tie_breaking_is_stable(self):
        # 平局按理赔 id、分保方次序稳定打破：输入乱序不影响结果
        claims = [(1, 101), (2, 101), (3, 101)]
        totals, ref_rows = reference_largest_remainder(claims, [5000, 5000])
        parties = [Party("A", 5000, totals[0]), Party("B", 5000, totals[1])]
        r1 = build_allocation(claims, parties)
        r2 = build_allocation(list(reversed(claims)), parties)
        self.assertEqual(r1, r2)
        for ca in r1:
            self.assertEqual(
                [s.amount for s in ca.shares], ref_rows[ca.claim_id]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
