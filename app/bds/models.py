# -*- coding: utf-8 -*-
"""bds 应用的 SQLAlchemy 2.0 模型。

继承 (Base, BaseModel)：Base 来自 server_fast.common.db，
BaseModel mixin 提供 id/create_time/update_time 通用字段及
to_dtype/map_fields/db_fields/to_dict 方法。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    JSON,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from server_fast.common.db import Base
from server_fast.common.models import BaseModel


def _on_insert_update(model):
    """装饰器：把函数同时注册为 model 的 before_insert + before_update 钩子。

    在 flush 前对 target 实例赋值计算字段（与 `app/irs/models.py` 同款机制；
    两边各持一份是因为 bds 不得反向依赖 irs，而这段实现只有 4 行）。
    """

    def decorator(fn):
        event.listens_for(model, "before_insert")(fn)
        event.listens_for(model, "before_update")(fn)
        return fn

    return decorator


class SymbolInfo(Base, BaseModel):
    """证券信息模型（对应表 bds_symbol_info）。"""

    __tablename__ = "bds_symbol_info"

    # ---- 基础信息 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="代码")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="名称")
    industry: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="所属行业")
    # ---- 市场 / 板块：由 symbol 前缀推导（services/symbol_info/market_board.py）----
    # em（东财 Excel）与 gm（get_instruments）两条通道都写；值为 symbol 的纯函数
    # ⇒ 两通道结果必然相同、无漂移风险，故无需 keep_columns 护栏。
    # 非 A 股（4 只沪指 + SZSE.399006 + SPX/NDX/NDX100/GC00Y）为 NULL。
    market: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, comment="市场：上交所/深交所/北交所；非 A 股为 NULL"
    )
    board: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, comment="板块：主板/创业板/科创板/北交所；非 A 股为 NULL"
    )
    online_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="上市日期")

    # ---- 估值指标：均为 Numeric(12, 2) ----
    # 本表只放【静态】证券信息；日频行情值（最新价/涨幅/昨收/涨跌停/复权因子）随行情
    # 每日变化，不落本表（原 price / rate 两列已于 2026-10-16 删除）。
    pe_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(静)")
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PE(TTM)")
    pb: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="PB")
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="股息(TTM%)")
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="ROE(%)")
    yoy_in: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="总收入(%)")
    yoy_np: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="净利润(%)")
    gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="毛利率(%)")
    dar: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="负债率(%)")

    # ---- 报告期 ----
    # 来源：东财 Excel 列「最新报告期(含快报)」，为该标的最新财报所属报告期。
    # 注意与财务四表（bds_fund_*）的 rpt_date 区分：那里是每行自身的报告期，这里是快照值。
    latest_rpt_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="报告期：最新报告期(含快报)"
    )

    # ---- gm 补充字段（POST /bds/sync/symbol-info 的 gm 通道写入）----
    # ⚠️ 本组字段只由 gm 路径维护；东财 Excel 的 df 中不含它们，故
    #    ON DUPLICATE KEY UPDATE 不会清空（机制同 sync_at，见 MEMORY §1.11）。
    sec_abbr: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="拼音缩写(gm)")
    delisted_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True,
        comment="退市日期；在市为 NULL（gm 占位值 2038-01-01 已转 NULL），可能为未来日期",
    )

    # ---- 批量同步标记 ----
    # 注意：本列不由『东财 Excel 全量 upsert』维护（Excel 无此列），
    # 仅由 POST /bds/sync/mark-synced 写入。upsert_df_to_db 的
    # ON DUPLICATE KEY UPDATE 子句只含 df 中出现的列，故同步证券信息不会清空本列。
    # ⚠️ 若日后把 upsert_symbol_info_excel_sql 改成「先 DELETE 再 INSERT」，
    #    或给 upsert_df_to_db 显式传入全部列，本列会被清空。
    sync_at: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="同步日期：批量同步完成当日写今日，重跑据此过滤"
    )

    # ---- 附加类属性：被 BaseModel 的 map_fields() 等方法使用 ----
    # 两条数据通道的表头完全不同，各持一份 {db_field: [别名...]}，
    # 调用时显式传入：SymbolInfo.map_fields(SymbolInfo.cols_map_fields_em / _gm)
    # ⚠️ 两份都刻意没有 market / board：它们是 symbol 推导列、不对应任何外部表头，
    #    加映射反而会让含「市场」/「板块」表头的 Excel 直接覆盖推导值。

    # (1) 代码信息文件夹内的 Excel：东财导出 + 手动添加
    cols_map_fields_em = {
        "symbol": ["代码"],
        "name": ["名称"],
        "industry": ["所属行业"],
        "online_date": ["上市日期"],
        "delisted_date": ["退市日期"],   # 仅「手动添加」表有该列（东财导出无）
        "pe_lyr": ["市盈率(静)"],
        "pe_ttm": ["市盈率(TTM)"],
        "pb": ["市净率"],
        "dy_ttm": ["股息率TTM%"],
        "roe": ["加权净资产收益率%"],
        "yoy_in": ["营业总收入同比%"],
        "yoy_np": ["归属净利润同比%"],
        "gpm": ["销售毛利率%"],
        "dar": ["资产负债比率%"],
        "latest_rpt_date": ["最新报告期(含快报)"],
    }
    # (2) gm get_instruments 的列名 -> 本表字段
    # sec_name / listed_date 是【回填】进已有的 name / online_date（只补空、不覆盖）
    cols_map_fields_gm = {
        "symbol": ["symbol"],
        "name": ["sec_name"],
        "online_date": ["listed_date"],
        "sec_abbr": ["sec_abbr"],
        "delisted_date": ["delisted_date"],
    }
    # 唯一键字段列表
    unique_keys = ["symbol"]
    # 需要 '—' 替换的字段列表（按本表字段名；实际替换前须先取与 df 的列交集）
    fields_replace = [
        "online_date", "delisted_date", "pe_lyr", "pe_ttm", "pb", "dy_ttm",
        "roe", "yoy_in", "yoy_np", "gpm", "dar", "latest_rpt_date",
    ]

    # symbol 已有 unique=True（隐式唯一索引），无需额外单列索引
    __table_args__ = ()

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"{self.symbol}-{self.name}"


class TradeDate(Base, BaseModel):
    """交易日历模型（对应表 bds_trade_date）。"""

    __tablename__ = "bds_trade_date"

    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日")

    def __str__(self) -> str:
        """模型实例字符串表示，便于调试与日志输出。"""
        return f"交易日:{self.trade_date}"


class IndexHistory(Base, BaseModel):
    """指数历史行情模型（对应表 bds_index_history）。"""

    __tablename__ = "bds_index_history"

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="开盘价")
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最高价")
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="最低价")
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True, comment="收盘价")
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="成交额")
    volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="成交量")
    position: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="持仓量")

    # 联合唯一约束 + 单列索引
    # symbol 是 uk(symbol, trade_date) 的最左前缀，单独 k_symbol 冗余已移除
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uk_bds_index_history_symbol_date"),
    )

    # BaseModel 工具方法所需元数据
    cols_map_fields = {
        "symbol": ["代码"],
        "trade_date": ["交易日期"],
        "open": ["开盘价"],
        "high": ["最高价"],
        "low": ["最低价"],
        "close": ["收盘价"],
        "amount": ["成交额"],
        "volume": ["成交量"],
        "position": ["持仓量"],
    }
    unique_keys = ["symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.trade_date}"


class IndexConstituent(Base, BaseModel):
    """指数成分股模型（对应表 bds_index_constituent）。"""

    __tablename__ = "bds_index_constituent"

    index_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="指数代码")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="成分股代码")
    weight: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4),
        nullable=True,
        comment="权重（2026-10-21 弃用：gm 未稳定返回，加权一律用 bds_daily_mktvalue.tot_mv 现算）",
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")

    # 联合唯一约束 + trade_date 单列索引
    # index_code 是 uk(index_code, symbol, trade_date) 的最左前缀，单独 k_index_code 冗余已移除
    __table_args__ = (
        UniqueConstraint("index_code", "symbol", "trade_date", name="uk_bds_index_constituent"),
        Index("k_bds_index_constituent_trade_date", "trade_date"),
    )

    unique_keys = ["index_code", "symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.index_code}-{self.symbol}-{self.trade_date}"


class IndexConstituentSync(Base, BaseModel):
    """指数成分核查水位（对应表 bds_index_constituent_sync，P0-0）。

    ⚠️ **为什么必须单独一张表**：`bds_index_constituent` 按「名单变化」落行（压缩存储），
    名单没变的交易日**没有行可挂**，于是「已核查到哪天」这个事实无处存放。后果是
    同步静默失败（gm 超时、进程被杀）与「名单确实未变」的表现完全一致，没人能发现。

    ⚠️ **三个维度必须分开读**：
    - `checked_to`：已**处理**到哪天（含失败日与拒存日）→ 增量起点 = `checked_to + 1`
    - `gap_days` / `first_gap`：其中哪些日 gm 调用失败、结果不确定 → 补核查入口
    - `dirty_days` / `first_dirty`：其中哪些日 gm 返回了名单但被只数校验拒存
      （疑伪快照：新旧并存 / 缺股 / 回退，见 `index_constituent._judge_change`）
      → 这些日**没有**成分快照，该日成分回退到上一个干净名单

    ⚠️ **新鲜度只看 `checked_to`**，不能用 `MAX(bds_index_constituent.trade_date)` 代替
    ——那个值表达的是「末次名单变化日」，不是「核查进度」。

    不变量：`checked_to >= last_change_date`；`gap_days = 0 ⟺ first_gap IS NULL`；
    `dirty_days = 0 ⟺ first_dirty IS NULL`；
    `last_change_date = MAX(bds_index_constituent.trade_date)`（成分行被手工删过的指数除外）。
    """

    __tablename__ = "bds_index_constituent_sync"

    index_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="指数代码")
    checked_to: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="已处理到的交易日（含未确认日，见 gap_days）")
    last_change_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="最近一次名单变化（留存快照）的交易日")
    constituent_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="末次确认的成分股数量")
    gap_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="已处理但结果不确定（失败）的交易日数")
    first_gap: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="最早一个未确认交易日（补核查入口）")
    no_data_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="累计 gm 空返回日数")
    dirty_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="被拒存（只数校验不通过、疑伪快照）的交易日数")
    first_dirty: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="最早一个被拒存的交易日（脏点定位入口）")
    last_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="末次核查时间")

    __table_args__ = (
        UniqueConstraint("index_code", name="uk_bds_index_constituent_sync_index_code"),
    )

    unique_keys = ["index_code"]

    def __str__(self) -> str:
        return f"{self.index_code}-checked_to={self.checked_to}"


class IndexValuation(Base, BaseModel):
    """指数加权估值日序列（对应表 bds_index_valuation，P0-1）。

    口径见 `docs/指数分位方案_2026-10-20.md` §2：PE/PB 用**整体法**
    `ΣMV / Σ(MV/指标)`（等价于市值加权调和平均，亏损股计入），不是市值加权平均 PE
    （后者在 000300 上会把 13 倍算成 28 倍）。

    ⚠️ **这是「可重算的派生表」，不是不可变事实表**：补成分快照、个股估值源数据覆盖、
    财报追溯调整都会改变历史行的值。凡引用历史值必须同时标注 `calc_at`。

    **股息率**分母是全部成分市值（`dy_ttm` 为 NULL 的成分按 0 计入分母）—— 只需把
    「有分红」的成分放进分母会系统性高估（科创50 +74.7%）。

    **覆盖率门禁**：`n_total / expected_total < 0.98` 的交易日，`pe_ttm`/`pb_mrq`/`dy_ttm`/
    `pe_median`/`ps_ttm`/`pcf_ttm_oper` 一律写 NULL（分位查询自动跳过），避免源端缺股时静默产出偏离值。

    **PS/PCF 口径与 PE 同构**（`ΣMV / Σ(MV/指标)`），但 PCF 有一处**故意不同**：
    只让 `pcf_ttm_oper > 0.5` 的成分参与，**分子分母都用该子集**——负现金流成分剔除。
    理由：经营现金流总和趋零甚至为负时「市值/现金流」没有经济学解释（含负口径下
    创业板指 2012 年 PCF = 439.6）；而净利润为负时整体法 PE 仍有意义，故 PE 保留亏损计入。
    分子若误用 `mv_total`（全部市值）会把 PCF 高估最多 53.6%（科创50），见方案 §3.2.2。
    """

    __tablename__ = "bds_index_valuation"

    index_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="指数代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")
    close: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="指数收盘价（来自 bds_index_history）")
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="整体法 PE_TTM（亏损计入，主口径）")
    pe_median: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="成分 PE 横截面中位数（仅 PE>0 样本）")
    pb_mrq: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="整体法 PB_MRQ")
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="市值加权股息率 TTM(%)=Σ(dy×MV)/ΣMV，分母含全部成分市值（无分红记 0）")
    ps_ttm: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="整体法市销率 TTM=ΣMV/Σ(MV/ps)；营收无负值，分母不剔除")
    pcf_ttm_oper: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True,
        comment="整体法市现率 TTM（经营现金流）；**仅 pcf>0.5 的成分参与分子与分母**，"
                "负现金流剔除（与 PE 亏损计入的口径故意不同）")
    mv_total: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 2), nullable=True, comment="成分总市值合计（元）")
    profit_total: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 2), nullable=True, comment="成分净利润合计（元，含负值）")
    n_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="成分股数（有估值数据）")
    expected_total: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="该快照应有成分数（覆盖率分母）；n_total/expected_total < 0.98 时指标留空")
    n_valid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="有效样本数（PE 参与计算）")
    n_loss: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="亏损/无 PE 数")
    n_cf_neg: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="负/无经营现金流成分数（PCF 剔除样本审计）")
    snapshot_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="所用成分快照日期（可追溯）")
    calc_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="本行计算时间（重算会刷新）")

    __table_args__ = (
        UniqueConstraint("index_code", "trade_date", name="uk_bds_index_valuation_idx_date"),
        Index("k_bds_index_valuation_trade_date", "trade_date"),
        # 覆盖索引：序列/分位查询只读这几列，避免 18k 行逐行走主键随机读（128MB buffer pool 下曾 0.9s）
        Index("k_bds_index_valuation_cover", "index_code", "trade_date",
              "pe_ttm", "pb_mrq", "dy_ttm", "pe_median"),
    )

    unique_keys = ["index_code", "trade_date"]

    def __str__(self) -> str:
        return f"{self.index_code}-{self.trade_date}"


class FundBalance(Base, BaseModel):
    """资产负债表模型（对应表 bds_fund_balance）。"""

    __tablename__ = "bds_fund_balance"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 资产类字段 ----
    mny_cptl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="货币资金")
    acct_rcv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="应收账款")
    invt: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="存货")
    ttl_cur_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="流动资产合计")
    fix_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="固定资产")
    lt_eqy_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="长期股权投资")
    intg_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="无形资产")
    gw: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="商誉")
    ttl_ncur_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="非流动资产合计")
    ttl_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="资产总计")

    # ---- 负债类字段 ----
    sht_ln: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="短期借款")
    acct_pay: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="应付账款")
    ttl_cur_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="流动负债合计")
    lt_ln: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="长期借款")
    ttl_ncur_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="非流动负债合计")
    ttl_liab: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="负债合计")

    # ---- 权益类字段 ----
    cptl_rsv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="资本公积")
    ret_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="未分配利润")
    ttl_eqy_pcom: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="归母股东权益合计")
    ttl_eqy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="股东权益合计")

    # 联合唯一约束 + 列表页排序索引（见 docs/财务四表列表页卡顿改进方案_2026-10-19.md）
    # k_*_rpt：支撑 ORDER BY rpt_date DESC, pub_date DESC, id DESC 的索引前缀；InnoDB 二级索引末尾
    #   隐含主键 ⇒ 物理序即 (rpt_date, pub_date, id)，配合「先取 id 再回表」可全程走索引，避免 34 万行 filesort
    # k_*_type_rpt：rpt_type 等值筛选 + 同序排序
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_balance"),
        Index("k_bds_fund_balance_rpt", "rpt_date", "pub_date"),
        Index("k_bds_fund_balance_type_rpt", "rpt_type", "rpt_date", "pub_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FundIncome(Base, BaseModel):
    """利润表模型（对应表 bds_fund_income）。"""

    __tablename__ = "bds_fund_income"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 收入类字段 ----
    ttl_inc_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业总收入")
    inc_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业收入")
    # ---- 成本费用类字段 ----
    ttl_cost_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业总成本")
    cost_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业成本")
    exp_sell: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="销售费用")
    exp_adm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="管理费用")
    exp_rd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="研发费用")
    exp_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="财务费用")
    # ---- 其他经营收益 ----
    inc_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资收益")
    inc_fv_chg: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="公允价值变动")
    # ---- 利润类字段 ----
    oper_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业利润")
    ttl_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="利润总额")
    inc_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="所得税费用")
    net_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="净利润")
    net_prof_pcom: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="归母净利润")
    # ---- 每股收益 ----
    eps_base: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="基本每股收益")
    eps_dil: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="稀释每股收益")
    # ---- 综合收益及其他 ----
    inc_noper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业外收入")
    exp_noper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="营业外支出")
    ttl_comp_inc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="综合收益总额")

    # 联合唯一约束 + 列表页排序索引（同 FundBalance）
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_income"),
        Index("k_bds_fund_income_rpt", "rpt_date", "pub_date"),
        Index("k_bds_fund_income_type_rpt", "rpt_type", "rpt_date", "pub_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FundCashflow(Base, BaseModel):
    """现金流量表模型（对应表 bds_fund_cashflow）。"""

    __tablename__ = "bds_fund_cashflow"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 经营活动现金流 ----
    cash_rcv_sale: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="销售商品、提供劳务收到的现金")
    cf_in_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动现金流入小计")
    cash_pur_gds_svc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="购买商品、接受劳务支付的现金")
    cash_pay_emp: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="支付给职工以及为职工支付的现金")
    cash_pay_tax: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="支付的各项税费")
    cf_out_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动现金流出小计")
    net_cf_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="经营活动产生的现金流量净额")
    # ---- 投资活动现金流 ----
    cash_rcv_sale_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="收回投资收到的现金")
    cf_in_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资活动现金流入小计")
    pur_fix_intg_ast: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="购建固定资产、无形资产和其他长期资产支付的现金")
    net_cf_inv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="投资活动产生的现金流量净额")
    # ---- 筹资活动现金流 ----
    brw_rcv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="取得借款收到的现金")
    cf_in_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="筹资活动现金流入小计")
    cash_rpay_brw: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="偿还债务支付的现金")
    net_cf_fin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="筹资活动产生的现金流量净额")
    # ---- 汇总 ----
    net_prof: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="净利润")
    efct_er_chg_cash: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="汇率变动对现金及现金等价物的影响")
    net_incr_cash_eq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="现金及现金等价物净增加额")
    cash_cash_eq_bgn: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="期初现金及现金等价物余额")
    cash_cash_eq_end: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="期末现金及现金等价物余额")

    # 联合唯一约束 + 列表页排序索引（同 FundBalance）
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_fund_cashflow"),
        Index("k_bds_fund_cashflow_rpt", "rpt_date", "pub_date"),
        Index("k_bds_fund_cashflow_type_rpt", "rpt_type", "rpt_date", "pub_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class FinanceDeriv(Base, BaseModel):
    """财务指标模型（对应表 bds_finance_deriv）。"""

    __tablename__ = "bds_finance_deriv"

    # ---- 元数据字段 ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    rpt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="报告日期")
    rpt_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="报表类型")
    data_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="数据类型")

    # ---- 收益率字段 ----
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(摊薄)")
    roe_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(加权)")
    roe_avg: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净资产收益率ROE(平均)")
    roa: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产报酬率ROA")
    roic: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="投入资本回报率ROIC")
    # ---- 盈利能力字段 ----
    sale_gpm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="销售毛利率")
    sale_npm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="销售净利率")
    ebitda_toi: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="EBITDA/营业总收入")
    ebit_toi: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="息税前利润/营业总收入")
    # ---- 偿债能力字段 ----
    ast_liab_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="资产负债率")
    curr_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="流动比率")
    quick_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="速动比率")
    liab_eqy_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="产权比率")
    # ---- 营运能力字段 ----
    ttl_ast_turnover_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产周转率")
    acct_rcv_turnover_days: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="应收账款周转天数(含应收票据)")
    inv_turnover_days: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="存货周转天数")
    # ---- 增长能力字段 ----
    net_prof_pcom_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="归属母公司股东的净利润同比增长率")
    ttl_inc_oper_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="营业总收入同比增长率")
    net_prof_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="净利润同比增长率")
    ttl_asset_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="总资产同比增长率")

    # 联合唯一约束 + 列表页排序索引（同 FundBalance）
    __table_args__ = (
        UniqueConstraint("symbol", "rpt_date", name="uk_bds_finance_deriv"),
        Index("k_bds_finance_deriv_rpt", "rpt_date", "pub_date"),
        Index("k_bds_finance_deriv_type_rpt", "rpt_type", "rpt_date", "pub_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.rpt_date}"


class DailyValuation(Base, BaseModel):
    """估值指标模型（对应表 bds_daily_valuation）。

    每日交易数据（非财报周期数据），元数据仅 symbol + trade_date，
    无 rpt_type/data_type/pub_date。
    """

    __tablename__ = "bds_daily_valuation"

    # ---- 元数据字段（每日交易数据，无 rpt_type/data_type/pub_date） ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")

    # ---- 市盈率 PE 字段 ----
    pe_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(TTM)")
    pe_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新年报LYR)")
    pe_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新报告期MRQ)")
    pe_ttm_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(TTM)扣除非经常性损益")
    pe_lyr_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新年报LYR)扣除非经常性损益")
    pe_mrq_cut: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市盈率(最新报告期MRQ)扣除非经常性损益")
    # ---- 市净率 PB 字段 ----
    pb_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市净率(最新年报LYR)")
    pb_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市净率(最新报告期MRQ)")
    # ---- 市现率 PCF 字段 ----
    pcf_ttm_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(经营现金流,TTM)")
    pcf_ttm_ncf: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(现金净流量,TTM)")
    pcf_lyr_oper: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(经营现金流,最新年报LYR)")
    pcf_lyr_ncf: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市现率(现金净流量,最新年报LYR)")
    # ---- 市销率 PS 字段 ----
    ps_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(TTM)")
    ps_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(最新年报LYR)")
    ps_mrq: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="市销率(最新报告期MRQ)")
    # ---- PEG 字段 ----
    peg_lyr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年年报增长率)")
    peg_1q: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年1季*4较上年年报增长率)")
    peg_3q: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="历史PEG值(当年3季*4/3较上年年报增长率)")
    # ---- 股息率 DY 字段 ----
    dy_ttm: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="股息率(滚动12月TTM)")
    dy_lfy: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="股息率(上一财年LFY)")

    # 联合唯一约束 + trade_date DESC 单列索引（支撑 ORDER BY trade_date DESC LIMIT 的高频查询，
    # 避免 120 万行全表扫描 + filesort）
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uk_bds_daily_valuation"),
        Index("k_dv_td", "trade_date", postgresql_using="btree",
              mysql_using="btree"),  # DESC 方向由 MySQL 8.0 自然提供，单列索引足够
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.trade_date}"


class DailyMktvalue(Base, BaseModel):
    """每日市值模型（对应表 bds_daily_mktvalue）。

    对应 gm 接口 stk_get_daily_mktvalue，存储总市值/A股市值/B股市值/企业价值等
    每日市值数据，按 symbol + trade_date 联合唯一去重，直接覆盖更新。
    """

    __tablename__ = "bds_daily_mktvalue"

    # ---- 元数据字段（每日交易数据，无 rpt_type/data_type/pub_date） ----
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="股票代码")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="交易日期")

    # ---- 市值/价值类字段（单位：元） ----
    tot_mv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="总市值")
    tot_mv_csrc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="总市值(证监会算法)")
    a_mv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="A股市值")
    a_mv_ex_ltd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="A股市值(剔除限售股)")
    b_mv: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="B股市值")
    b_mv_ex_ltd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="B股市值(剔除限售股)")
    ev: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="企业价值")
    ev_ex_curr: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="企业价值(剔除货币资金)")
    equity_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="股权价值")
    # ---- 比率类字段（单位：倍） ----
    ev_ebitda: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="企业价值/EBITDA")

    # 联合唯一约束 + trade_date DESC 单列索引（支撑 ORDER BY trade_date DESC LIMIT 的高频查询）
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uk_bds_daily_mktvalue"),
        Index("k_dm_td", "trade_date", postgresql_using="btree", mysql_using="btree"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["symbol", "trade_date"]

    def __str__(self) -> str:
        return f"{self.symbol}-{self.trade_date}"


class EconomicIndicator(Base, BaseModel):
    """宏观经济指标模型（对应表 bds_economic_indicator）。

    存储 akshare/FRED 采集的各国核心宏观经济指标（利率/通胀/就业/增长/制造业/消费/收益率），
    按 indicator_code + report_date 联合唯一去重。
    """

    __tablename__ = "bds_economic_indicator"

    # ---- 元数据字段 ----
    indicator_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="指标代码")
    indicator_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="指标名称")
    indicator_short_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="指标简称")
    category: Mapped[str] = mapped_column(String(32), nullable=False, comment="类别")
    country: Mapped[str] = mapped_column(String(32), nullable=False, server_default="美国", comment="国别")
    report_date: Mapped[date] = mapped_column(Date, nullable=False, comment="报告日期")
    pub_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="发布日期")
    # ---- 数值字段 ----
    # value 允许为空：wscn 未发布数据（actual 为空）也入库，发布后下次同步覆盖
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="数值")
    value_prev: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="前值")
    value_expected: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="预期值")
    # ---- wscn 扩展字段 ----
    importance: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="重要性")
    revised: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="修正值")
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, comment="标题")
    foresight: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="前瞻")
    # ---- 单位与频率 ----
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, comment="单位")
    frequency: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="频率")

    # 联合唯一约束 + 复合索引
    __table_args__ = (
        UniqueConstraint("indicator_code", "report_date", name="uk_bds_economic_indicator"),
        Index("ix_economic_indicator_category_date", "category", "report_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["indicator_code", "report_date"]

    def __str__(self) -> str:
        return f"{self.indicator_code}-{self.report_date}"


class GoldReserve(Base, BaseModel):
    """黄金储备模型（对应表 bds_gold_reserve）。

    存储 IMF SDMX IFS 数据集中各国央行月度黄金储备数据（RAXG_USD 指标，USD 计价），
    按 country_code + rpt_date 联合唯一去重。
    """

    __tablename__ = "bds_gold_reserve"

    # ---- 元数据字段 ----
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="国家代码")
    country_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="国家名称")
    rpt_date: Mapped[date] = mapped_column(Date, nullable=False, comment="报告日期")
    # ---- 数值字段 ----
    gold_holdings_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True, comment="黄金储备(USD)")
    # ---- 单位与频率 ----
    unit: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="单位")
    frequency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="频率")

    # 联合唯一约束 + 单列索引
    __table_args__ = (
        UniqueConstraint("country_code", "rpt_date", name="uk_bds_gold_reserve"),
        Index("k_bds_gold_reserve_date", "rpt_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["country_code", "rpt_date"]

    def __str__(self) -> str:
        return f"{self.country_code}-{self.rpt_date}"


class DailyIndicator(Base, BaseModel):
    """美债收益率指标模型（对应表 bds_daily_indicator）。

    存储 FRED API `/series/observations` 同步的 4 个日频收益率指标
    （2Y/10Y/10Y-2Y 利差/10Y TIPS），按 indicator_code + report_date
    联合唯一去重。相比 EconomicIndicator 移除 wscn 专用字段
    （pub_date/value_prev 等），仅保留 FRED observation 中的 date/value
    两个字段（realtime_start/realtime_end 已废弃）。
    """

    __tablename__ = "bds_daily_indicator"

    # ---- Config 元信息字段 ----
    indicator_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="指标代码")
    indicator_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="指标名称")
    indicator_short_name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, comment="指标简称")
    category: Mapped[str] = mapped_column(String(32), nullable=False, comment="类别")
    country: Mapped[str] = mapped_column(String(32), nullable=False, server_default="美国", comment="国别")
    # ---- FRED 数据字段（来自 observation） ----
    report_date: Mapped[date] = mapped_column(Date, nullable=False, comment="报告日期")
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True, comment="数值")
    # ---- 单位与频率 ----
    unit: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="单位")
    frequency: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="频率")

    # 联合唯一约束 + 单列索引
    __table_args__ = (
        UniqueConstraint("indicator_code", "report_date", name="uk_bds_daily_indicator"),
        Index("k_bds_daily_indicator_date", "report_date"),
    )

    # 供 upsert 使用的唯一键
    unique_keys = ["indicator_code", "report_date"]

    def __str__(self) -> str:
        return f"{self.indicator_code}-{self.report_date}"


class BdsSyncTask(Base, BaseModel):
    """批量同步任务（对应表 bds_sync_task）。

    后端后台线程启动，进度与结果实时写 DB，前端轮询 /sync-tasks 端点查看。
    """

    __tablename__ = "bds_sync_task"

    # ---- 任务参数 ----
    # ⚠️ 列宽 String(16)：新增 scope 取值必须 <= 16 字符，否则 MySQL 会截断/报 1406
    # index_backfill  = 指数成分股同步/回填（POST /bds/sync/index-constituent）
    # index_valuation = 指数估值重算（POST /bds/sync/index-valuation，15 字符）
    # macro_sync      = 一键同步（POST /bds/sync/macro，10 字符；基础数据+宏观数据七段顺序执行。
    #                   名字沿用不改——历史任务靠它被前端卡片接管，改了会导致老任务刷不出来）
    scope: Mapped[str] = mapped_column(String(16), nullable=False, comment="同步范围: all/index/industry/explicit/index_backfill/index_valuation/macro_sync")
    scope_params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, comment="范围参数: {index_code/industry_name/symbols} 或 {index_codes/start_date} 或 {stages}")
    tables: Mapped[list] = mapped_column(JSON, nullable=False, comment="要同步的表列表")
    mark_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", comment="全部成功后是否写 sync_at: 1=是 0=否")
    # 跳过已同步的「起始日期」语义：剔除 sync_at >= 该日期 的标的（NULL=不跳过）
    # 取代旧列 exclude_synced_today（只能按「服务器当天」判定，跨夜续跑失效，2026-10-19 删除）
    exclude_synced_since: Mapped[Optional[date]] = mapped_column(Date, nullable=True, comment="剔除 sync_at >= 该日期 的标的；NULL=不跳过")
    # 退市口径（三态，与 /bds/symbol-infos 的 trading_status 同枚举；不传该端点时=全部）
    # ⚠️ 列宽 String(8) 刚好容纳 delisted（8 字符）
    # ⚠️ 仅作用于 all / industry / index；explicit 手写清单=用户显式意图，不做退市过滤
    trading_status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="trading", comment="退市口径: trading=仅在市/delisted=仅退市/all=全部")
    max_symbols: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="最多处理几只 symbol: 0=不限")

    # ---- 任务状态 ----
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", comment="状态: pending/running/done/failed")
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="任务总数 (symbol × table)")
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="已完成数")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="success 数")
    no_data_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="no_data 数")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="error 数")
    # 仅 index_backfill（成分股）产生下表三列；其余 scope 恒为 0
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="skipped 数（名单未变或已存跳过，index_backfill 专用）")
    dirty_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="被拒存（只数校验不通过、疑 gm 伪快照）的交易日数，index_backfill 专用")
    aborted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="提前中止的指数个数（被停止/连续失败超限，index_backfill 专用）")
    progress: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default="0", comment="进度% (0-100)")
    errors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, comment="失败明细: [{symbol, table, message}]")
    # 结构化结果摘要（与 errors 分离：errors 是"出错了"，result 是"做成了什么"）。
    # 目前仅 index_valuation 使用：{per_index: {code: {rows/ranges/aborted/audit}},
    #                            gated: {code: [日期]}, lagging: {code: {last, start}}, start_date, end_date}
    result: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, comment="任务结果摘要（估值审计/覆盖率告警等结构化输出，非失败信息）")

    # ---- 时间戳 ----
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="实际开始时间")
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="实际结束时间")

    unique_keys = []

    def __str__(self) -> str:
        return f"task-{self.id}({self.scope})"


class OptionInfo(Base, BaseModel):
    """期权合约字典（对应表 bds_option_info）：openctp 静态合约表本地副本。

    **这张表回答「合约是什么」**，`bds_option_config` 回答「要同步哪些合约」，
    `bds_option_market` 回答「今天多少价」。三者不重叠。

    为什么需要它：合约乘数、到期日、A 类调整行权价这三项，行情接口要么不给
    （`option_finance_board` 无 settle/delta/乘数），要么给的不是权威值（自研
    `_calc_delisted_date()` 对 `fu_o` 差一周）。交易所字典是这些事实的唯一权威源。

    🔴 **累积不删**：数据源只返回**当前挂牌**合约（实测最早一条就是次日），
    若按全量覆盖做，表会越刷越短，已退市合约的历史行情行就再也查不到规格。
    规则：只 UPSERT 永不 DELETE；命中的行置 `is_listed=1` + `last_seen_date=今天`；
    全量成功后把 `last_seen_date < 今天` 的置 `is_listed=0`（带行数哨兵，见服务层）。

    - `contract_id`：openctp 原生主键（`IO2611-P-4000` / `au2612P840` / ETF 8 位码
      `10012562`），25,910 行无重复。**唯一键用它而不是 `symbol`**——`symbol` 是我们
      自己拼的派生值，用派生值做身份是本末倒置。
    - `symbol`：系统标准码。非 ETF 与 `contract_id` 相同；ETF 由
      `CP + YYMM + (M|A) + 5 位名义价` 拼装（实测 510500/202612 的 58 张 58/58 命中）。
      ⚠️ **`A` 不能统一成 `M`**：`C2612A07500`（乘数 10180）与 `C2612M07500`（乘数
      10000）名义价同为 7.500 且并存，合并即撞码。
    - `strike` vs `nominal_strike`：前者是交易所给的真实行权价（A 类为调整后的 7.367），
      后者是「按品种默认乘数折算的名义价」（7.367 × 10180 ÷ 10000 = 7.500），只用于
      生成标准码。**乘数与行权价必须成对使用**，混用差 1.8% 市值。
    - `last_trade_date`：权威到期日，取代 `_calc_delisted_date()` 的推算。
      后者降级为交叉校验（au/IO/HO/MO/ETF 全部逐日相同，`fu_o` 差一周）。
    - `product_id` 有 92 个，只有 3 个在 `Config.OPTION_VARIETIES` 里有中文名映射；
      其余照存（将来加品种零迁移），`resolve()` 对未知品种返回 `None` 而不报错。
    """

    __tablename__ = "bds_option_info"

    contract_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="openctp 原生合约ID")
    symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="系统标准码（派生）")
    contract_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="数据源原始名称（已 strip）")
    exchange: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="交易所：CFFEX/SHFE/SSE/SZSE/...")
    product_id: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="品种ID：IO/HO/MO/au_o/ETF_O/AP ...")
    underlying_contract_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="标的合约ID：IO2611 / 510500 / au2612")

    cp: Mapped[str] = mapped_column(
        String(1), nullable=False, comment="C=认购 P=认沽（源 1/2）")
    strike: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, comment="行权价（交易所原始值，A类已调整）")
    nominal_strike: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, comment="名义行权价（生成标准码用）")
    multiplier: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="合约乘数（A类 10180）")
    tick_size: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 6), nullable=True, comment="最小变动价位")

    list_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="上市日期")
    last_trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="最后交易日（权威到期日）")
    delivery_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="交割日")
    expire_year_month: Mapped[str] = mapped_column(
        String(6), nullable=False,
        comment="期权到期年月 YYYYMM（由 last_trade_date 派生，非数据源的交割月）")

    is_listed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="1=最近一次拉取仍挂牌")
    last_seen_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="最后一次出现在数据源的日期")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="本次写入时刻")

    __table_args__ = (
        UniqueConstraint("contract_id", name="uk_opt_info_contract"),
        Index("k_opt_info_symbol", "symbol"),
        Index("k_opt_info_und", "product_id", "underlying_contract_id", "last_trade_date"),
        Index("k_opt_info_listed", "is_listed", "last_seen_date"),
        Index("k_opt_info_expiry", "expire_year_month"),
    )


class OptionConfig(Base, BaseModel):
    """期权**同步清单**（对应表 bds_option_config）。

    > **2026-10-27 瘦身：17 列 → 10 列。** 原来这里还存着合约规格
    > （`cp` / `strike` / `multiplier` / `margin_type` / `expiry_date`）和
    > 品种级派生（`underlying_symbol` / `und_key`），都是 `bds_option_info` 的副本。
    > 副本的三个硬伤：① A 类调整合约的乘数（10180）拿不到；② 2027 到期的合约
    > 算不出来；③ 手工拼码导致 10-26「ETF symbol 多 6 位」那类 bug。
    > 详细论证与删列验收见 `docs/bds期权合约字典方案_2026-10-27.md`。
    >
    > **2026-10-27 二次瘦身：10 列 → 8 列**，删 `source` / `note`。`source` 是
    > `import-holding` 那条已下线通道的遗留（6 行全是 `holding`，再也没有写入方）；
    > `note` 6 行全是「从持仓导入（xxx）」，内容就是 `broker_symbol` 的复述，
    > 而 `broker_symbol` 本身已可内联编辑 ⇒ 两个纯冗余列。删前快照在
    > `_backup_2026-10-27/db/_backup_option_config_note_source_20261027.sql`。

    三张表的分工（判据是「这条事实的权威在谁手里」）：

    | 表 | 回答的问题 | 权威 | 粒度 |
    |---|---|---|---|
    | `bds_option_info` | 合约**是什么**（乘数/到期日/行权价） | 交易所 | 每个合约一行 |
    | `bds_option_config`（本表） | **要不要同步、怎么拉** | 我 | 每个合约一行 |
    | `bds_option_market` | 这个合约**今天多少价** | 行情接口 | `(contract_id, trade_date)` |

    ## 字段分工：本表是**三把钥匙**，每把只对接一个外部系统

    判据是「这个字段跟谁对齐」——三个来源各不相干，所以是三列而不是一列：

    | 字段 | 对齐谁 | 用途 | 缺失后果 |
    |---|---|---|---|
    | `contract_id` | `bds_option_info.contract_id` | **查合约详情**（乘数/行权价/到期日/方向） | 建不了行（唯一键）；行情写入被跳过 |
    | `broker_symbol` | 账单 `bills_group.symbol` | **认领实盘持仓** | 风险情景直接 raise，不是少算 |
    | `option_name` + `end_month` | akshare / 上期所行情接口 | **拉行情**（分组入参） | 无法发起请求 |

    ⚠️ **三者不可互推**，这是最容易想错的地方：
    - `contract_id → broker_symbol`：实测 6/6 只是「交易所前缀 + contract_id」
      （`CFFEX.IO2611-P-4000` / `SHQQ.10012562` / `SHFE.au2612P840`），**看起来能拼**。
      但前缀因交易所而异、且券商是否有独立编码未经全量验证 ⇒ 账单对账必须用
      **真实账单码**，绝不能用推导值（拼错了是"对不上"而不是"报错"，最难查）。
    - `contract_id → option_name/end_month`：理论上可 join 字典反推，但保留为实列
      才不用每行一次 join，且它是用户新增时**必须表态**的入参
      （POST 用它与 `contract_id` 的归属做交叉校验）。
    - `option_name` **不参与唯一键**，唯一身份只有 `contract_id`。

    ### `contract_id`
    **openctp 的「合约ID」，与 `bds_option_info.contract_id` 1:1**。
    🔄 2026-10-27 由 `symbol` 改为本列，理由是 `symbol` 做身份有结构性缺陷：
    ETF 标准码沿用交易所「合约简称」口径、**不含标的 6 位**，9 个 ETF 标的价格区间
    重叠 ⇒ 跨标的必然同码（`C2612M01350` 同属 588000/588080，全量 422 组）。
    当时只能把唯一键放宽成 `(option_name, symbol)`，再逼所有消费方用二元组做键。
    换成 `contract_id` 后同码现象在结构上消失：不同标的的同码合约有不同的
    `contract_id`（ETF 即券商码数字部分，如 `10012562`），单列唯一即可。
    ⚠️ **`symbol` 不再是本表字段**，响应体里的 `symbol` 由 `bds_option_info` 派生。
    ⚠️ 它同时是 `bds_option_market` 的身份（`(contract_id, trade_date)`），
    所以「配置里有、字典里没有」的合约既写不进行情、也查不到规格。

    ### `broker_symbol`
    **券商账单口径的原始码，是认领实盘持仓的唯一钥匙**——不是可选的备注列。
    `risk_scenario.load_context()` 用 `contracts = {broker_symbol: cfg}` 去匹配
    `bills_group.symbol`，匹配不上**直接 raise**「期权 xxx 不在同步清单里，拒绝估算」
    ⇒ 一张有持仓的合约漏填它，整个 `/bills/risk-scenario` 返回 500，而不是少算一条。
    所以：**持有实盘仓位的合约必须填；纯观察合约可以留空**（此时退回字典标准码兜底，
    但账单码带交易所前缀、标准码不带，实际匹配不上，等于该行不参与情景推演）。
    保 `broker_symbol` 而不由 `contract_id` 推导的原因见上表下方「三者不可互推」。
    ⚠️ 目前无唯一约束：两行填同一个券商码会让 `contracts` 字典静默后写覆盖先写。

    ### `option_name`
    akshare 的入参是中文品种名（"沪深300股指期权"），不是代码。`_sync_group`
    按 (品种, 到期月) 分组调接口，删了就无法发起请求。

    ### `end_month`
    股指/ETF 必须指定（`option_finance_board` 按到期月拉）；
    **商品期权填 `ALL`**——上期所 `.dat` 一次返回当日全部到期月（实测 813~817 行），
    没有按月筛选的参数，填实际月份反而会造成"以为只拉了一个月"的误解。

    ### `is_enabled`
    人工列（铁律 4.9），同步任务永不写本表。

    > 2026-10-27 二次瘦身删掉了 `source` / `note`：前者是 `import-holding` 已下线
    > 通道的遗留（恒 `holding`），后者恒为「从持仓导入（券商码）」的复述 ——
    > `broker_symbol` 可内联编辑后，两个字段都没有存在的理由。
    """

    __tablename__ = "bds_option_config"

    contract_id: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="openctp 合约ID（与 bds_option_info.contract_id 1:1，本表唯一身份）")
    broker_symbol: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="券商账单代码（认领实盘持仓的关键，缺失则风险情景直接报错）")

    option_name: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="akshare 品种名")
    end_month: Mapped[Optional[str]] = mapped_column(
        String(6), nullable=True, comment="到期年月 YYYYMM；商品期权填 ALL")
    is_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="1=参与一键同步")

    __table_args__ = (
        # 唯一键只锁 contract_id：它 1:1 对应交易所的一个合约，本身就是唯一身份。
        # 相比先前的 (option_name, symbol) 二元组，这里不再需要品种上下文做键的一部分
        # ——「同一标准码登记到两个标的」的消歧工作从数据库约束降级为自然的表结构。
        UniqueConstraint("contract_id", name="uk_opt_cfg"),
        Index("k_cfg_group", "option_name", "end_month"),
    )

    unique_keys = ["contract_id"]

    def __str__(self) -> str:
        return f"{self.contract_id}({self.option_name})"


class OptionMarket(Base, BaseModel):
    """期权行情日快照（对应表 bds_option_market）。

    **按日累积**：唯一键 `(contract_id, trade_date)`，同日重跑幂等覆盖，跨日自然成为时间序列。
    这是相对 `irs_option_monitor`（只存最新一版、一同步历史就没了）的核心收益。

    > **2026-10-27 瘦身：删 9 列、加 1 列。** 删掉 `broker_symbol` / `underlying_symbol` /
    > `und_key` / `option_type` / `delisted_date` / `days_left` / `multiplier` /
    > `symbol`（同日第二轮删）与（一并删的）逐行品种副本，全部改为
    > `join bds_option_info` 现算；新增 `contract_id` 作为**唯一身份**。理由与逐值验收见
    > `docs/期权台账合并与行情表瘦身方案_2026-10-27.md`。
    >
    > 🔴 **只删不加会把表变成「认不出是谁」**：本表没有 `option_name` 列，品种身份原先
    > 全靠 `und_key` + `underlying_symbol` 承载；而 ETF 标准码不含标的 6 位
    > （`C2612M01350` 同属 588000/588080），删掉这两列后无法判断该行属于哪个标的，
    > `uk(symbol, trade_date)` 还会把两个标的判成同一行、后写覆盖先写
    > ⇒ 唯一键必须是 `(contract_id, trade_date)`。

    三列价格必须分开（`irs_option_monitor` 把它们挤在一列是历史缺陷）：

    | 列 | 含义 | IO | 510500 | au |
    |---|---|---|---|---|
    | `price` | 最新价（盘中实时/盘后收盘） | lastprice | 当前价 | 收盘价 |
    | `pre_settle` | 前结算价 | **NULL** | 前结价 | 前结算价 |
    | `settle` | 结算价 | **NULL** | **NULL** | 结算价 ✅ |

    ⇒ `settle` / `delta_opt` / `volume` / `open_interest` 允许 NULL，**不硬造**。
    下游要用结算价时按 `COALESCE(settle, pre_settle, price)` 降级，**降级链只在服务层一处**，
    展示层如实留空（拿前结价冒充结算价会误导）。

    ⚠️ ETF 接口的「数量」列恒等于返回行数（实测 26 行 → 全部为 26），不是成交量，故
    ETF 的 `volume`/`open_interest` 一律 NULL。

    **为什么 `price_strike` 仍留在本表**：它记的是「当日事实」。按「可派生即删」的
    口径它也能从 info 现算，但 A 类调整合约的 `strike` 在标的除权后会被交易所改写
    ⇒ 现算会让**历史行的内在价值跟着变**，这是删列方案里唯一真正的信息损失点。
    `delisted_date` / `days_left` 则留得没必要（到期日一旦定下就不变），
    改成按 `info.last_trade_date` 现算。

    **value_i / value_t 的口径（2026-10-27「修法 B」拍板）**：与 atm_i / ratio_*
    同属**计算值**，由模型钩子在每次写入（含历史行重刷）时重算，value_t 始终跟随
    最新 price——与「事实列」（price_ud / volume，历史行冻结）刻意分开。
    老规则「历史行衍生字段不覆盖」就此废止。

    **`symbol` 同理删掉**（同日第二轮）：它只是「展示/模糊搜索」用，值和
    `bds_option_info.symbol` 逐行相同（实测 11 行零差异），存两份只会两头漂移。
    响应体里 `symbol` 仍在，改为 join 字典现取 ⇒ **字典缺行时为 NULL**，
    调用方必须容忍空值（前端主表用 `contract_id` 兜底）。
    """

    __tablename__ = "bds_option_market"

    contract_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="openctp 合约ID=唯一身份，join bds_option_info")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="行情日期（交易日）")
    price_strike: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False,
        comment="行权价（当日接口口径；A 类为名义价，与 info.strike 可能不同）")
    price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="最新价")
    pre_settle: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="前结算价")
    settle: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="结算价（仅商品期权）")
    price_ud: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="标的价（同标的同交易日唯一）")
    value_i: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="内在价值（钩子落库时计算，每次写入重算）")
    value_t: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="时间价值（钩子落库时计算，每次写入重算；可为负）")
    atm_i: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 2), nullable=True, comment="平值(%)（钩子落库时计算）")
    ratio_t: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 2), nullable=True, comment="时间(%)（钩子落库时计算）")
    ratio_i: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 2), nullable=True, comment="内在(%)（钩子落库时计算）")
    ratio_t_y: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 2), nullable=True, comment="时间(%Y)（钩子落库时计算）")
    ratio_i_y: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(9, 2), nullable=True, comment="内在(%Y)（钩子落库时计算）")
    delta_opt: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 6), nullable=True, comment="delta（仅商品期权，认沽为负）")
    volume: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="成交量（ETF 恒 NULL）")
    open_interest: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="持仓量（ETF 恒 NULL）")
    quote_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="接口返回的行情时点")
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="shfe_direct / akshare_board")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="本次写入时间")

    __table_args__ = (
        # 唯一身份是 (合约, 交易日)。用 symbol 会在 ETF 跨标的同码时撞车
        UniqueConstraint("contract_id", "trade_date", name="uk_opt_market"),
        Index("k_opt_trade_date", "trade_date"),
        Index("k_opt_mkt_contract", "contract_id"),
    )

    unique_keys = ["contract_id", "trade_date"]

    def __str__(self) -> str:
        return f"{self.contract_id}@{self.trade_date}"


@_on_insert_update(OptionMarket)
def _compute_option_market(mapper, connection, target):
    """OptionMarket 衍生字段计算（落库时自动算，任何写路径都触发）。

    计算口径的唯一实现在 `services/market_data/option_derived.py`（irs OptionMonitor
    钩子已改为委托同一实现，2026-10-27 起两表口径强一致）。

    本表没有 option_type / days_left 物理列（10-27 瘦身删了），按 contract_id 现查
    `bds_option_info` 补齐：
    - option_type ← info.cp（C/P → call/put）
    - days_left   ← info.last_trade_date − target.trade_date（快照 as-of 口径；
      当日快照下与 irs 的「到期日 − today」相等，历史行不随今天流逝而变）

    字典缺行 ⇒ 七个衍生字段全部清空 —— 与读路径 `info_missing` 的处理一致，
    宁可空也不猜。`option_type` / `days_left` 以普通 Python 属性挂在实例上
    （无对应列，不会入库），仅作为共享函数的输入。

    口径（2026-10-27「修法 B」用户拍板）：衍生字段是**计算值**，每次写入都重算，
    历史行被重刷时 value_t 跟随最新 price——冻结的只是「事实列」（price_ud /
    volume，服务层 `_TODAY_ONLY_COLS` 保护）。计算值与事实刻意的双轨，不要"顺手
    统一"：统一成冻结会让 value_t 停在过期价上，统一成重算会让盘前同步把今日
    price_ud 贴到昨日行上（10-27 08:04 事故）。

    ⚠️ 延迟导入：本模块不能在顶层 import services —— `services/market_data/__init__`
    会拉起全部子模块、它们又 import 本模块，顶层导入必循环。flush 时一切已加载，安全。
    """
    from server_fast.app.bds.services.market_data.option_derived import (  # noqa: E402
        DERIVED_FIELDS,
        compute_option_derived,
    )

    row = connection.execute(
        select(OptionInfo.cp, OptionInfo.last_trade_date)
        .where(OptionInfo.contract_id == target.contract_id)
    ).first()
    if row is None or row.cp is None or row.last_trade_date is None or target.trade_date is None:
        for f in DERIVED_FIELDS:
            setattr(target, f, None)
        return
    target.option_type = {"C": "call", "P": "put"}.get(row.cp)
    if target.option_type is None:
        # cp 异常（字典侧只该有 C/P，防御性兜底）：无法判方向 ⇒ 全清空，不猜成 put
        for f in DERIVED_FIELDS:
            setattr(target, f, None)
        return
    target.days_left = (row.last_trade_date - target.trade_date).days
    compute_option_derived(target)


class BdsTableStat(Base, BaseModel):
    """列表页总数缓存（对应表 bds_table_stat）。

    用途：bds 列表端点的分页 total 若走 `COUNT(*)`，在 600 万行级的日频表
    （bds_daily_valuation / bds_daily_mktvalue）上无筛选时需 5~8 秒
    （innodb_buffer_pool_size 仅 128MB，二级索引 ~192MB 全读盘，重复执行不复用），
    而真正取数只 6ms。本表缓存「表总行数」，无筛选时 O(1) 读到。

    - 写入时机：批量同步收尾重算（+ 手工 POST /bds/refresh-table-stat）
    - 读取口径：见 services/table_stat.py 的 resolve_total
    - 统计时间 = BaseModel.update_time（行被重算时刷新），不另设 updated_at 列
    """

    __tablename__ = "bds_table_stat"

    table_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="目标表名")
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="该表总行数")
    is_exact: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="1=精确统计 0=估算"
    )

    __table_args__ = (
        UniqueConstraint("table_name", name="uk_bds_table_stat"),
    )
