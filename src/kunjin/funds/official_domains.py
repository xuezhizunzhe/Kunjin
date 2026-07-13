from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Tuple

# A domain is trusted only together with one of its audited publisher names.
REGULATOR_AND_EXCHANGE_DOMAINS: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {
        "www.csrc.gov.cn": ("中国证券监督管理委员会", "中国证监会"),
        "www.sse.com.cn": ("上海证券交易所", "上交所"),
        "www.szse.cn": ("深圳证券交易所", "深交所"),
        "www.cninfo.com.cn": ("巨潮资讯网",),
    }
)


# Entries require an audited manager identity and official-link source. The
# initial entry supports the fund used by KunJin's real portfolio workflow.
FUND_COMPANY_DOMAINS: Mapping[str, str] = MappingProxyType(
    {
        "www.fund001.com": "交银施罗德基金管理有限公司",
    }
)

