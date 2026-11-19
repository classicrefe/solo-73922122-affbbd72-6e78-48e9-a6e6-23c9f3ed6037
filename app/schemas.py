"""HTTP 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, PositiveInt, model_validator

from .apportionment import BASIS_TOTAL


class SessionStatus(str, Enum):
    OPEN = "open"          # 可正常取页
    CLOSING = "closing"    # 关闭已受理，正在等待在途取页结束
    CLOSED = "closed"      # 事务已回滚、连接已归还、名额已释放
    EXPIRED = "expired"    # 到期后由清理线程转入的终态（语义同 closed）


class OpenSessionRequest(BaseModel):
    region: str = Field(..., min_length=1, max_length=64, description="保单地区")


class SessionInfo(BaseModel):
    session_id: str
    region: str
    status: SessionStatus
    opened_at: datetime
    expires_at: datetime
    last_page_at: datetime | None = None
    pages_served: int
    ttl_seconds: int


class ClaimRow(BaseModel):
    claim_id: int
    policy_id: int
    region: str
    amount: int


class ClaimPage(BaseModel):
    session_id: str
    region: str
    page: PositiveInt
    page_size: int
    has_more: bool
    snapshot_time: datetime
    rows: list[ClaimRow]


class PolicyIn(BaseModel):
    region: str = Field(..., min_length=1, max_length=64)


class PolicyOut(BaseModel):
    id: int
    region: str


class ClaimIn(BaseModel):
    policy_id: int
    amount: int = Field(..., ge=0, description="整数理赔金额，单位分")


class ClaimOut(BaseModel):
    id: int
    policy_id: int
    amount: int


class Message(BaseModel):
    detail: str


# ----------------------------------------------------------------------
# 再保报送清单（份额取整）
# ----------------------------------------------------------------------
class CedingPartyIn(BaseModel):
    """分保方约定：标识、基点比例（合计 10000）、核定总额（分）。"""

    party: str = Field(..., min_length=1, max_length=64, description="分保方标识")
    ratio_bps: int = Field(
        ..., ge=1, le=10000,
        description="约定比例（整数基点，1bp=0.01%），各方合计必须为 10000",
    )
    approved_total: int = Field(
        ..., ge=0, description="该分保方已核定的跨笔合计总额，单位：分"
    )


class CedingBookedItemIn(BaseModel):
    """已入账明细（可为空）：理赔 ID、分保方、整分金额，原样保留。"""

    claim_id: int = Field(..., ge=1, description="已入账理赔 ID")
    party: str = Field(..., min_length=1, max_length=64, description="分保方标识")
    amount: int = Field(..., ge=0, description="已入账整分金额，单位：分")


class CedingReportRequest(BaseModel):
    policy_id: int = Field(..., ge=1, description="指定保单 ID（须属于会话地区）")
    parties: list[CedingPartyIn] = Field(
        ..., min_length=2, max_length=5, description="2~5 家分保方的约定"
    )
    booked: list[CedingBookedItemIn] | None = Field(
        default=None, description="可为空的已入账明细，服务端原样保留其金额"
    )

    @model_validator(mode="after")
    def _validate_parties(self):
        names = [p.party for p in self.parties]
        if len(set(names)) != len(names):
            raise ValueError("分保方标识不得重复")
        total = sum(p.ratio_bps for p in self.parties)
        if total != BASIS_TOTAL:
            raise ValueError(
                f"各方约定比例（整数基点）合计必须为 {BASIS_TOTAL}，实际为 {total}"
            )
        return self


class CedingShareOut(BaseModel):
    party: str
    ratio_bps: int
    amount: int = Field(..., description="该笔给该分保方的整分金额")
    rounded: str = Field(
        ..., description="取整方向：up=向上取整、down=向下取整、exact=比例整除"
    )
    booked: bool = Field(..., description="是否为原样保留的已入账金额")


class CedingClaimOut(BaseModel):
    claim_id: int
    amount: int = Field(..., description="该笔理赔金额，单位：分")
    shares: list[CedingShareOut]


class CedingPartyTotalOut(BaseModel):
    party: str
    ratio_bps: int
    approved_total: int = Field(..., description="请求中的核定总额，单位：分")
    allocated_total: int = Field(
        ..., description="清单内该分保方跨笔合计，已核验等于核定总额"
    )


class CedingReport(BaseModel):
    session_id: str
    region: str
    policy_id: int
    claim_count: int
    snapshot_time: datetime
    parties: list[CedingPartyTotalOut]
    claims: list[CedingClaimOut] = Field(
        ..., description="按理赔 ID 升序的完整清单；无法满足约束时不返回本对象"
    )
