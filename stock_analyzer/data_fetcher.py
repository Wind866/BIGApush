#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取模块
"""
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import time
import sys
from pathlib import Path
import json
import requests
import random
from typing import Dict, List, Optional, Any

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db_manager import DBManager
from utils.akshare_retry import akshare_call_with_retry


def _parse_cn_money(value: Any) -> Optional[float]:
    """把 '5.42亿' / '5416.94万' / '1.23' 这类中文金额字符串解析为「元」(float)。

    同花顺资金流返回的是带单位的中文金额字符串；解析失败返回 None。
    """
    if value is None:
        return None
    t = str(value).strip().replace(',', '')
    if t in ('', '-', '--'):
        return None
    try:
        if t.endswith('亿'):
            return float(t[:-1]) * 1e8
        if t.endswith('万'):
            return float(t[:-1]) * 1e4
        return float(t)
    except Exception:
        return None


# 同花顺「即时」资金流是全市场快照，单次调用开销较大且短时间重复调用易被限流，
# 因此进程内做 60s 缓存：一个请求只取一次快照，多股复用。
_THS_FF_CACHE: Dict[str, Any] = {"ts": 0.0, "df": None}
_THS_FF_TTL = 60.0


# ---------------------------------------------------------------- 周期换算
# 分析周期 -> 自然日窗口。注意技术分析要算出 MA200 需要 ≥200 个交易日
# （约 280+ 自然日），所以长周期窗口必须给足，否则 MA200 恒为 NaN。
PERIOD_DAYS: Dict[str, int] = {
    '30d': 30, '60d': 60, '90d': 90, '180d': 180,
    '400d': 400, '1y': 365, '2y': 730, '3y': 1095,
}
DEFAULT_PERIOD_DAYS = 30
# 支撑 MA200 所需的最少自然日窗口（200 交易日 ≈ 280 自然日，留足余量）
MA200_MIN_DAYS = 400


def period_to_days(period: Any) -> int:
    """把 '90d' / '1y' / '400d' / 90 这类周期描述换算成自然日天数。"""
    if isinstance(period, (int, float)) and period > 0:
        return int(period)
    key = str(period or '').strip().lower()
    if key in PERIOD_DAYS:
        return PERIOD_DAYS[key]
    try:
        if key.endswith('d'):
            return max(1, int(key[:-1]))
        if key.endswith('y'):
            return max(1, int(float(key[:-1]) * 365))
        if key.endswith('m'):
            return max(1, int(float(key[:-1]) * 30))
    except ValueError:
        pass
    return DEFAULT_PERIOD_DAYS


# 巨潮个股概况（名称/行业/市场）进程内缓存，1 小时有效
_CNINFO_PROFILE_CACHE: Dict[str, Any] = {}
_CNINFO_PROFILE_TTL = 3600.0


def _get_ths_ff_snapshot():
    """获取同花顺全市场即时资金流快照（带 60s 进程内缓存；失败不缓存）。"""
    now = time.time()
    cached = _THS_FF_CACHE.get("df")
    if cached is not None and (now - _THS_FF_CACHE.get("ts", 0.0)) < _THS_FF_TTL:
        return cached
    try:
        import akshare as ak
        df = akshare_call_with_retry(ak.stock_fund_flow_individual, symbol="即时")
        if df is not None and not df.empty:
            _THS_FF_CACHE["df"] = df
            _THS_FF_CACHE["ts"] = now
        return df
    except Exception as e:  # noqa: BLE001
        print(f"同花顺资金流快照获取失败: {e}")
        return None


class DataFetcher:
    """数据获取器"""
    
    def __init__(self):
        """初始化数据获取器"""
        # 初始化数据库管理器
        from utils.global_db import get_global_db
        self.db_manager = get_global_db()
        
        # 设置请求会话
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/javascript, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://quote.eastmoney.com/',
            'Connection': 'keep-alive',
        })
        
        # 备选A股股票列表（当网络获取失败时使用）
        self.DEFAULT_STOCK_LIST = {
            "600519": "贵州茅台", "600036": "招商银行", "601398": "工商银行",
            "600900": "长江电力", "601288": "农业银行", "601088": "中国神华",
            "601857": "中国石油", "600030": "中信证券", "601628": "中国人寿",
            "600276": "恒瑞医药", "601318": "中国平安", "600309": "万华化学",
            "600887": "伊利股份", "601166": "兴业银行", "600028": "中国石化",
            "601888": "中国中免", "600031": "三一重工", "601012": "隆基绿能",
            "603288": "海天味业", "600009": "上海机场", "600436": "片仔癀",
            "603259": "药明康德", "601668": "中国建筑", "600048": "保利发展",
            "600585": "海螺水泥", "601601": "中国太保", "603501": "韦尔股份",
            "600690": "海尔智家", "601818": "光大银行", "600893": "航发动力",
            "601688": "华泰证券", "601211": "国泰君安", "600837": "海通证券",
            "000001": "平安银行", "000002": "万科A", "000333": "美的集团",
            "000858": "五粮液", "002594": "比亚迪", "000568": "泸州老窖",
            "000538": "云南白药", "002415": "海康威视", "000725": "京东方A",
        }
    
    @staticmethod
    def _market_label(stock_code: str, market_hint: Optional[str] = None) -> str:
        """推断所属市场（沪市/深市/北交所），优先采用数据源给出的市场字段。"""
        hint = (market_hint or "").strip()
        if hint:
            if "北" in hint:
                return "北交所"
            if "沪" in hint or hint.startswith("上"):
                return "沪市"
            if "深" in hint:
                return "深市"
        code = str(stock_code).strip()
        if code.startswith(("60", "68", "9")):
            return "沪市"
        if code.startswith(("4", "8")):
            return "北交所"
        return "深市"

    @staticmethod
    def _parse_em_basic(stock_info: pd.DataFrame) -> Dict[str, str]:
        """解析东财 stock_individual_info_em 的两个返回形态。

        - 长表形态（当前 akshare）：列为 item/value，需要按 item 名取值；
          旧代码直接把 item 列第 0 行当股票名称，取到的是「股票代码」这个标签。
        - 宽表形态：列名即 股票名称 / 所属行业 / 所属板块。
        """
        out = {"name": "", "industry": "", "sector": "", "market": ""}
        if stock_info is None or stock_info.empty:
            return out

        if "item" in stock_info.columns and "value" in stock_info.columns:
            kv = {str(k).strip(): v for k, v in
                  zip(stock_info["item"], stock_info["value"])}
            out["name"] = str(kv.get("股票简称") or kv.get("股票名称") or "").strip()
            out["industry"] = str(kv.get("行业") or kv.get("所属行业") or "").strip()
            out["sector"] = str(kv.get("所属板块") or kv.get("板块") or "").strip()
            out["market"] = str(kv.get("所属市场") or "").strip()
            return out

        def _first(col: str) -> str:
            if col in stock_info.columns and not stock_info[col].empty:
                return str(stock_info[col].iloc[0]).strip()
            return ""

        out["name"] = _first("股票名称") or _first("股票简称")
        out["industry"] = _first("所属行业") or _first("行业")
        out["sector"] = _first("所属板块")
        out["market"] = _first("所属市场")
        return out

    def _get_cninfo_profile(self, stock_code: str) -> Dict[str, str]:
        """巨潮资讯个股概况（数据中心 IP 友好），取 简称/行业/市场。

        东财 push2 在云主机/数据中心出口常被拦截，此时用它兜底名称与行业。
        进程内 1 小时缓存，避免同一请求内重复拉取。
        """
        cached = _CNINFO_PROFILE_CACHE.get(stock_code)
        if cached and (time.time() - cached[0]) < _CNINFO_PROFILE_TTL:
            return cached[1]

        result: Dict[str, str] = {}
        try:
            profile = akshare_call_with_retry(
                ak.stock_profile_cninfo, cache_ttl=604800, symbol=stock_code
            )
            if profile is not None and not profile.empty:
                row = profile.iloc[0]

                def _val(*keys: str) -> str:
                    for k in keys:
                        if k in profile.columns:
                            v = row.get(k)
                            if v is not None and str(v).strip() not in ('', 'nan', 'None'):
                                return str(v).strip()
                    return ""

                result = {
                    "name": _val("A股简称", "公司名称"),
                    "industry": _val("所属行业"),
                    "market": _val("所属市场"),
                }
        except Exception as e:  # noqa: BLE001
            print(f"巨潮个股概况兜底失败 {stock_code}: {e}")

        _CNINFO_PROFILE_CACHE[stock_code] = (time.time(), result)
        return result

    def get_stock_basic(self, stock_code: str) -> Dict[str, Any]:
        """获取股票基本信息（名称/行业/板块/市场）

        取数顺序：东财 -> 巨潮资讯 -> 内置名单兜底。
        东财 push2 在数据中心 IP 下会被拦截，故必须有非东财兜底，
        否则线上名称会退化成股票代码、行业恒为「未知行业」。

        Args:
            stock_code: 股票代码

        Returns:
            dict: 股票基本信息
        """
        code = str(stock_code).strip()
        fallback_list = getattr(self, 'DEFAULT_STOCK_LIST', {}) or {}
        basic = {
            "code": code,
            "name": "",
            "industry": "",
            "sector": "",
            "market": self._market_label(code),
            "source": "",
        }

        # 1) 主源：东财（本地网络正常时字段最全）
        try:
            stock_info = akshare_call_with_retry(
                ak.stock_individual_info_em, cache_ttl=604800, symbol=code
            )
            em = self._parse_em_basic(stock_info)
            if em.get("name") or em.get("industry"):
                basic["name"] = em.get("name", "")
                basic["industry"] = em.get("industry", "")
                basic["sector"] = em.get("sector", "")
                basic["market"] = self._market_label(code, em.get("market"))
                basic["source"] = "eastmoney"
        except Exception as e:  # noqa: BLE001
            print(f"东财基本信息获取失败，改用巨潮兜底: {e}")

        # 2) 兜底：巨潮资讯（数据中心友好）
        if not basic["name"] or not basic["industry"]:
            profile = self._get_cninfo_profile(code)
            if profile:
                if not basic["name"] and profile.get("name"):
                    basic["name"] = profile["name"]
                if not basic["industry"] and profile.get("industry"):
                    basic["industry"] = profile["industry"]
                if profile.get("market"):
                    basic["market"] = self._market_label(code, profile["market"])
                if not basic["source"]:
                    basic["source"] = "cninfo"

        # 3) 收口：内置名单 + 占位文案
        if not basic["name"]:
            basic["name"] = fallback_list.get(code, code)
        if not basic["industry"]:
            basic["industry"] = "未知行业"
        if not basic["sector"]:
            basic["sector"] = basic["industry"] if basic["industry"] != "未知行业" else "未知板块"
        if not basic["source"]:
            basic["source"] = "builtin"
        return basic
    
    def get_stock_quote(self, stock_code: str, period: str = 'daily') -> pd.DataFrame:
        """获取历史行情数据
        
        Args:
            stock_code: 股票代码
            period: 周期 (daily, weekly, monthly) 或天数 (30d, 60d, 90d, 1y)
            
        Returns:
            pd.DataFrame: 历史行情数据
        """
        local_df = pd.DataFrame(columns=['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
        
        try:
            # 1. 优先从本地读取数据（csv_manager 可选，未配置则跳过本地缓存）
            local_df = pd.DataFrame()
            _csv = getattr(self, 'csv_manager', None)
            if _csv is not None:
                try:
                    local_df = _csv.read_stock(stock_code)
                except Exception as csv_err:  # noqa: BLE001
                    print(f"读取本地行情缓存失败，忽略: {csv_err}")
                    local_df = pd.DataFrame()
            if not local_df.empty:
                print(f"从本地获取 {stock_code} 历史数据: {len(local_df)} 条")
                
                # 计算需要的日期范围
                end_date = datetime.now()
                start_date = end_date - timedelta(days=period_to_days(period))
                
                # 过滤日期范围
                filtered_df = local_df[local_df['date'] >= start_date]
                if not filtered_df.empty:
                    # 确保列名正确
                    if 'date' in filtered_df.columns and 'close' in filtered_df.columns:
                        return filtered_df
                else:
                    # 本地数据不足，但有一些数据，返回全部本地数据
                    if 'date' in local_df.columns and 'close' in local_df.columns:
                        print(f"本地数据不足，返回全部本地数据: {len(local_df)} 条")
                        return local_df
            
            # 2. 本地数据为空，尝试从网络获取
            print(f"本地无数据，尝试从网络获取 {stock_code} 历史数据")
            
            # 计算日期范围
            end_date = datetime.now()
            start_date = end_date - timedelta(days=period_to_days(period))
            
            # 格式化日期
            start_date_str = start_date.strftime("%Y%m%d")
            end_date_str = end_date.strftime("%Y%m%d")
            
            # 1) 主源：东财 push2his（数据中心 IP 常被拦截，失败则回退新浪）
            data = None
            try:
                data = ak.stock_zh_a_hist(symbol=stock_code, start_date=start_date_str,
                                          end_date=end_date_str)
            except Exception as em_err:  # noqa: BLE001
                print(f"东财行情获取失败，尝试新浪兜底: {em_err}")

            # 2) 兜底：新浪 stock_zh_a_daily（数据中心友好，英文列名 date/open/high/low/close/volume/amount）
            if data is None or data.empty:
                sina_sym = ("sh" if stock_code.startswith("6") else "sz") + stock_code
                try:
                    sina_df = ak.stock_zh_a_daily(symbol=sina_sym, start_date=start_date_str,
                                                  end_date=end_date_str, adjust="")
                    if sina_df is not None and not sina_df.empty:
                        data = sina_df[["date", "open", "high", "low", "close",
                                        "volume", "amount"]].copy()
                        data["date"] = pd.to_datetime(data["date"])
                        data = data.sort_values("date")
                        print(f"已用新浪兜底获取 {stock_code} 行情: {len(data)} 条")
                        return data
                except Exception as sina_err:  # noqa: BLE001
                    print(f"新浪行情兜底也失败: {sina_err}")

            # 3) 东财列重命名 + 落本地缓存
            if data is not None and not data.empty:
                data = data.rename(columns={
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "成交额": "amount"
                })
                # 转换日期格式
                data['date'] = pd.to_datetime(data['date'])
                # 按日期排序
                data = data.sort_values('date')

                # 保存到本地（csv_manager 可选）
                if getattr(self, 'csv_manager', None) is not None:
                    try:
                        self.csv_manager.update_stock(stock_code, data)
                        print(f"已保存 {stock_code} 历史数据到本地")
                    except Exception as csv_err:  # noqa: BLE001
                        print(f"保存本地行情缓存失败，忽略: {csv_err}")

            return data if data is not None else pd.DataFrame(
                columns=['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
            
        except Exception as e:
            print(f"获取历史行情数据失败: {e}")
            # 即使网络失败，返回本地数据（如果有）
            if not local_df.empty:
                print(f"返回本地缓存数据: {len(local_df)} 条")
                return local_df
            # 返回空DataFrame
            return pd.DataFrame(columns=['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
    
    def get_financial_data(self, stock_code: str, report_type: str = 'annual') -> Dict[str, Any]:
        """获取财务数据
        
        Args:
            stock_code: 股票代码
            report_type: 报告类型 (annual, quarterly)
            
        Returns:
            dict: 财务数据
        """
        try:
            # 通过重试包装器从 akshare 获取财务数据
            if report_type == 'annual':
                financial_data = akshare_call_with_retry(
                    ak.stock_financial_analysis_indicator, symbol=stock_code
                )
            else:
                financial_data = akshare_call_with_retry(
                    ak.stock_financial_analysis_indicator, symbol=stock_code
                )
            
            if financial_data is not None and not financial_data.empty:
                def _col_first(col: str) -> float:
                    """取某列最新一期数值；列缺失/为空/无法解析时回落 0。"""
                    if col not in financial_data.columns or financial_data[col].empty:
                        return 0.0
                    try:
                        val = float(financial_data[col].iloc[-1])
                    except (TypeError, ValueError):
                        return 0.0
                    return 0.0 if val != val else val  # NaN -> 0

                return {
                    "revenue": _col_first("营业总收入"),
                    "profit": _col_first("净利润"),
                    "roe": _col_first("净资产收益率"),
                    "debt_ratio": _col_first("资产负债率"),
                }
            
            return {"revenue": 0, "profit": 0, "roe": 0, "debt_ratio": 0}
            
        except Exception as e:
            print(f"获取财务数据失败: {e}")
            return {"revenue": 0, "profit": 0, "roe": 0, "debt_ratio": 0}
    
    def get_fund_flow(self, stock_code: str, days: int = 30) -> Dict[str, Any]:
        """获取资金流向数据
        
        Args:
            stock_code: 股票代码
            days: 天数
            
        Returns:
            dict: 资金流向数据
        """
        try:
            # 1) 主源：东财个股资金流（数据中心 IP 常被拦截/返回残缺，仅有有效「主力净流入」才采信）
            fund_flow = None
            try:
                fund_flow = akshare_call_with_retry(
                    ak.stock_individual_fund_flow, stock=stock_code
                )
            except Exception as em_err:  # noqa: BLE001
                print(f"东财资金流获取失败，尝试同花顺兜底: {em_err}")

            if (fund_flow is not None and not fund_flow.empty
                    and "主力净流入" in fund_flow.columns):
                try:
                    main_inflow = float(fund_flow["主力净流入"].iloc[0])
                except Exception:
                    main_inflow = None
                if main_inflow is not None:
                    volume_change = "稳定"
                    if "成交量" in fund_flow.columns and len(fund_flow) >= 2:
                        cur = fund_flow["成交量"].iloc[0]
                        prev = fund_flow["成交量"].iloc[1]
                        volume_change = "增加" if cur > prev else "减少"
                    return {
                        "main_inflow": main_inflow,
                        "north_inflow": 0,  # akshare暂时没有北向资金数据
                        "volume_change": volume_change
                    }

            # 2) 兜底：同花顺即时资金流（数据中心友好；全市场快照按代码取「净额」）
            try:
                ths = _get_ths_ff_snapshot()
                if ths is not None and not ths.empty and "股票代码" in ths.columns:
                    codes = ths["股票代码"].astype(str).str.zfill(6)
                    row = ths[codes == str(stock_code).zfill(6)]
                    if not row.empty:
                        net = _parse_cn_money(row.iloc[0].get("净额"))
                        if net is not None:
                            print(f"同花顺兜底资金流 {stock_code}: 净额={net:,.0f} 元")
                            return {
                                "main_inflow": float(net),
                                "north_inflow": 0,
                                "volume_change": "稳定"
                            }
                        print(f"同花顺资金流 {stock_code}: 净额解析为空 ({row.iloc[0].get('净额')!r})")
                    else:
                        print(f"同花顺快照未含 {stock_code}（快照 {len(ths)} 只）")
                else:
                    print("同花顺资金流快照为空或结构异常")
            except Exception as ths_err:  # noqa: BLE001
                print(f"同花顺资金流兜底也失败: {ths_err}")

            return {"main_inflow": 0, "north_inflow": 0, "volume_change": "稳定"}

        except Exception as e:
            print(f"获取资金流向数据失败: {e}")
            return {"main_inflow": 0, "north_inflow": 0, "volume_change": "稳定"}
    
    def get_sector_data(self, stock_code: str) -> Dict[str, Any]:
        """获取板块数据
        
        Args:
            stock_code: 股票代码
            
        Returns:
            dict: 板块数据
        """
        try:
            # 获取股票基本信息
            stock_info = self.get_stock_basic(stock_code)
            industry = stock_info.get("industry", "")
            
            if industry:
                # 通过重试包装器尝试获取行业板块数据
                try:
                    sector_data = akshare_call_with_retry(
                        ak.stock_board_industry_summary, symbol=industry
                    )
                    if sector_data is not None and not sector_data.empty:
                        return {
                            "name": industry,
                            "rank": int(sector_data.get("涨跌幅排名", 0).iloc[0]) if not sector_data.get("涨跌幅排名").empty else 0,
                            "change": float(sector_data.get("涨跌幅", 0).iloc[0]) if not sector_data.get("涨跌幅").empty else 0
                        }
                except:
                    pass
            
            return {"name": industry, "rank": 0, "change": 0}
            
        except Exception as e:
            print(f"获取板块数据失败: {e}")
            return {"name": "", "rank": 0, "change": 0}
    
    def get_event_data(self, stock_code: str, days: int = 30) -> List[Dict[str, Any]]:
        """获取事件数据
        
        Args:
            stock_code: 股票代码
            days: 天数
            
        Returns:
            list: 事件数据列表
        """
        try:
            # 尝试从akshare获取
            events = []
            
            # 获取公告数据
            try:
                notices = akshare_call_with_retry(
                    ak.stock_zh_a_notice, symbol=stock_code
                )
                if notices is not None and not notices.empty:
                    for _, row in notices.head(5).iterrows():
                        events.append({
                            "type": "公告",
                            "content": row.get("标题", ""),
                            "date": row.get("发布日期", "")
                        })
            except:
                pass
            
            return events
            
        except Exception as e:
            print(f"获取事件数据失败: {e}")
            return []


if __name__ == "__main__":
    # 测试数据获取器
    fetcher = DataFetcher()
    
    # 测试获取股票基本信息
    stock_info = fetcher.get_stock_basic("600519")
    print("股票基本信息:", stock_info)
    
    # 测试获取历史行情数据
    quote_data = fetcher.get_stock_quote("600519", period="30d")
    print("历史行情数据:", quote_data.head())
    
    # 测试获取财务数据
    financial_data = fetcher.get_financial_data("600519")
    print("财务数据:", financial_data)
    
    # 测试获取资金流向数据
    fund_flow = fetcher.get_fund_flow("600519")
    print("资金流向数据:", fund_flow)
    
    # 测试获取板块数据
    sector_data = fetcher.get_sector_data("600519")
    print("板块数据:", sector_data)
    
    # 测试获取事件数据
    events = fetcher.get_event_data("600519")
    print("事件数据:", events)
