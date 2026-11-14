#!/usr/bin/env python3
"""
A股量化选股系统 - 主程序

使用方法:
    python main.py init      # 首次全量抓取
    python main.py update    # 每日增量更新（内部使用）
    python main.py select    # 执行选股
    python main.py run       # 完整流程（更新+选股+通知）
    python main.py schedule  # 启动定时调度
"""
import sys
import os
import argparse
import platform
import logging
from pathlib import Path
from datetime import datetime, time as dt_time
import time

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 版本信息
__version__ = "1.0.0"

# 收盘选股的目标档期整点（15:43 档）。档期键按「运行时刻 − 15h」归并，
# 当晚 23 点甚至次日 01:25 才跑到的迟到运行仍算当天的收盘档，
# 不会把次日的档期提前占掉（旧实现用固定 cutoff=12，跨日就串档）。
SELECTION_SLOT_HOUR = 15

from utils.akshare_fetcher import AKShareFetcher
from utils.db_manager import DBManager
from strategy.strategy_registry import get_registry
from utils.kline_chart import generate_kline_chart
from utils.db_initializer import init_databases_if_needed
from utils.stock_filter import StockFilter
import yaml


class QuantSystem:
    """量化系统主类"""
    
    def __init__(self, config_file="config/config.yaml"):
        # 初始化日志系统
        from utils.log_config import LogConfig
        LogConfig.setup_logging()
        
        self.config = self._load_config(config_file)
        self.data_dir = self.config.get('data_dir', 'data')
        
        # 初始化数据库（如果不存在）
        init_databases_if_needed(self.data_dir)
        
        # 初始化数据库管理器
        from utils.global_db import get_global_db
        self.db_manager = get_global_db()
        self.fetcher = AKShareFetcher(self.data_dir)
        self.registry = get_registry("config/strategy_params.yaml")
        # 初始化CSV管理器
        from utils.csv_manager import CSVManager
        self.csv_manager = CSVManager(self.data_dir)

        # 最近一次选股的股票池统计（数据基准日 / 剔除只数 / 策略异常），
        # 供推送消息标注「数据截至哪天」，避免旧数据选出来的结果看起来像今天的。
        self.last_universe_stats = {}
    
    def _load_config(self, config_file):
        """加载配置文件"""
        config_path = Path(config_file)
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        return {}
    

    def _load_stock_names(self, stock_data):
        """加载股票名称（从数据库读取，不再使用 stock_names.json）"""
        try:
            # 从数据库读取所有股票名称
            from utils.db_manager import DBManager
            from utils.global_db import get_global_db
            db_manager = get_global_db()
            stock_names = db_manager.get_all_stock_names()
            
            if stock_names:
                return stock_names
        except Exception as e:
            # 不静默吞掉：名称读不到时下面会退化成「股票600519」，
            # 卡片上看着像正常数据，实际上是降级态，必须留痕。
            logger.warning("读取股票名称失败，退化为代码占位名: %s", e)
        
        # 如果数据库读取失败，使用默认名称
        return {code: f"股票{code}" for code in stock_data.keys()}
    
    def init_data(self, max_stocks=None, years=1):
        """首次全量抓取"""
        print("=" * 60)
        print("🚀 首次全量数据抓取")
        print("=" * 60)
        self.fetcher.init_full_data(max_stocks=max_stocks, years=years)
        print("\n✓ 数据初始化完成")

    def _smart_update(self, max_stocks=None, check_latest=True):
        """智能更新：收盘前不更新，检查每只股票是否有当天数据（按北京时间）"""
        from datetime import datetime, timedelta, timezone
        import pandas as pd

        # 显式使用北京时间：GitHub Actions runner 默认是 UTC，若直接用 datetime.now()，
        # 15:30 北京时间会被当成 07:30「尚未收盘」，导致每天都跳过数据更新、
        # 选股永远基于首次初始化的旧数据。
        bj_now = datetime.now(timezone(timedelta(hours=8)))
        today = bj_now.date()
        current_time = bj_now.time()
        market_close_time = datetime.strptime("15:00", "%H:%M").time()

        # 手动触发时可跳过耗时的全量增量更新（用缓存数据库快速验证推送链路）
        if os.environ.get("SKIP_DATA_UPDATE") == "1":
            print("\n⏭️ 已设置 SKIP_DATA_UPDATE=1，跳过数据更新（使用缓存数据库）")
            return

        # 收盘前：不更新，使用旧数据
        if current_time < market_close_time:
            print("\n⏰ 当前时间尚未收盘 (15:00)")
            print("  使用本地已有数据，跳过网络更新")
            return

        # 检查每只股票是否有当天数据
        if check_latest:
            print("\n🔍 检查数据更新状态...")
            # 从数据库获取所有股票代码
            stock_codes = self.db_manager.list_all_stocks()
            if max_stocks:
                stock_codes = stock_codes[:max_stocks]

            total = len(stock_codes)
            has_today = 0
            no_today = 0
            check_limit = min(100, total)  # 抽样检查100只

            for code in stock_codes[:check_limit]:
                # 从数据库读取股票数据
                df = self.db_manager.read_stock(code)
                if not df.empty:
                    latest_date = pd.to_datetime(df.iloc[0]['date']).date()
                    if latest_date == today:
                        has_today += 1
                    else:
                        no_today += 1

            # 如果100%股票都有今天数据，跳过更新
            if check_limit > 0 and has_today == check_limit:
                print(f"  ✓ 已检查 {check_limit} 只股票，全部已有今天数据")
                print("  数据已是最新，跳过网络更新")
                return
            else:
                print(f"  已检查 {check_limit} 只，{has_today} 只有今天数据，{no_today} 只需要更新")

        # 执行更新
        print("\n🔄 执行数据更新...")
        self.fetcher.init_full_data(max_stocks=max_stocks, incremental=True)
        print("\n✓ 数据更新完成")

    def update_data(self, max_stocks=None):
        """每日增量更新（显式调用，不做「收盘前不更新」的短路）"""
        # 这里原本调 self.fetcher.daily_update()，而 AKShareFetcher 根本没有这个方法——
        # 也就是说这条路径从来就没成功过。它只被 Web 界面的「更新数据」按钮用到，
        # 报错还被后台线程的 except 吞成一句状态文案，所以一直没人发现。
        # 真正的增量入口是 init_full_data(incremental=True)，与 _smart_update 同一条路。
        self.fetcher.init_full_data(max_stocks=max_stocks, incremental=True)

    def select_stocks(self, category='all', max_stocks=None, return_data=False):
        """执行选股
        :param category: 股票分类筛选，'all'表示全部，其他值按分类筛选
        :param max_stocks: 限制处理的股票数量（用于快速测试）
        :param return_data: 是否返回股票数据字典（用于K线图生成）
        :return: (results, stock_names) 或 (results, stock_names, stock_data_dict)
        """
        print("=" * 60)
        print("🎯 执行选股策略")
        if max_stocks:
            print(f"   快速测试模式：只处理前 {max_stocks} 只股票")
        print("=" * 60)
        
        # 加载策略
        print("\n加载策略...")
        self.registry.auto_register_from_directory("strategy")
        
        if not self.registry.list_strategies():
            print("✗ 没有找到可用策略")
            return ({}, {}, {}) if return_data else ({}, {})
        
        print(f"已加载 {len(self.registry.list_strategies())} 个策略")
        
        # 输出当前策略参数
        print("\n当前策略参数:")
        for strategy_name, strategy_obj in self.registry.strategies.items():
            print(f"\n  🎯 {strategy_name}:")
            for param_name, param_value in strategy_obj.params.items():
                # 对特定参数添加说明
                note = ""
                if param_name == 'N':
                    note = " (成交量倍数)"
                elif param_name == 'M':
                    note = " (回溯天数)"
                elif param_name == 'CAP':
                    note = f" ({param_value/1e8:.0f}亿市值门槛)"
                elif param_name == 'J_VAL':
                    note = " (J值上限)"
                elif param_name in ['M1', 'M2', 'M3', 'M4']:
                    note = " (MA周期)"
                print(f"      {param_name}: {param_value}{note}")
        
        # 加载股票数据（批量读取 + 并行处理）
        print("\n执行选股...")
        from datetime import datetime as _dt
        import gc

        # 从数据库获取所有股票代码
        stock_codes = self.db_manager.list_all_stocks()

        if not stock_codes:
            print("✗ 没有股票数据，请先执行 init 或 update")
            # 不能只返回二元组：run_full 按 (results, stock_names, stock_data_dict) 解包，
            # 少一个值会变成 "not enough values to unpack"，把「库是空的」这条可执行的
            # 提示淹在 traceback 里。return_data 决定元数，两个分支都必须对齐。
            self.last_universe_stats = {'usable': 0, 'reference_date': None}
            return ({}, {}, {}) if return_data else ({}, {})

        print(f"共 {len(stock_codes)} 只股票")

        # 过滤：只保留主板
        stock_codes = [c for c in stock_codes if c[:3] in ('600','601','603','605','000','001','002','003')]
        process_codes = stock_codes[:max_stocks] if max_stocks else stock_codes
        print(f"过滤主板后: {len(process_codes)} 只股票")

        # 先获取股票名称
        stock_names = self._load_stock_names({})

        # 批量读取所有股票数据（单次SQL查询）
        t0 = _dt.now()
        print("批量加载K线数据...")
        batch_data = self.db_manager.read_stocks_batch(process_codes)
        print(f"批量加载完成: {len(batch_data)} 只，耗时 {(_dt.now()-t0).total_seconds():.1f}秒")

        # 预过滤无效股票：名称 / ST / 数据时效
        from utils.universe_guard import split_stale

        # 时效优先：先按「全市场最新交易日」把停牌、退市、数据没更新的票剔掉。
        # 以前这里只判断 len(df) >= 60，导致 600001（数据止于 2009-12-15）这类
        # 退市股也能算指标、出信号，且卡片上看不出任何异常。
        fresh_data, stale_stats = split_stale(batch_data)
        ref_day = stale_stats.get('reference')
        if stale_stats.get('stale'):
            _oldest = stale_stats.get('oldest')
            print(f"⚠️ 数据时效过滤: 剔除 {stale_stats['stale']}/{stale_stats['total']} 只"
                  f"（基准交易日 {ref_day or '未知'}"
                  + (f"，最老一根K线 {_oldest}" if _oldest else "") + "）")
            if stale_stats.get('samples'):
                _sample_txt = "、".join(f"{c}({d})" for c, d in stale_stats['samples'][:5])
                print(f"   样例: {_sample_txt}")

        invalid_keywords = ['退', '未知', '退市', '已退']
        valid_stocks = {}
        for code, df in fresh_data.items():
            name = stock_names.get(code, '未知')
            if any(kw in name for kw in invalid_keywords):
                continue
            if name.startswith('ST') or name.startswith('*ST'):
                continue
            if df.empty or len(df) < 60:
                continue
            valid_stocks[code] = (name, df)

        print(f"有效股票: {len(valid_stocks)} 只")
        self.last_universe_stats = {
            'reference_date': ref_day.isoformat() if ref_day else None,
            'total': stale_stats.get('total', 0),
            'stale_dropped': stale_stats.get('stale', 0),
            'stale_oldest': str(stale_stats.get('oldest')) if stale_stats.get('oldest') else None,
            'usable': len(valid_stocks),
            'strategy_errors': {},
        }

        # 并行执行策略
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import os
        _max_workers = min(os.cpu_count() or 4, len(self.registry.strategies), 8)

        results = {}
        indicators_dict = {}
        category_count = {}
        strategy_errors = {}

        def _run_strategy(item):
            sname, sobj = item
            sigs = []
            err_count = 0
            first_err = None
            for code, (name, df) in valid_stocks.items():
                try:
                    df_ind = sobj.calculate_indicators(df)
                    sl = sobj.select_stocks(df_ind, name)
                    if sl:
                        for s in sl:
                            cat = s.get('category', 'unknown')
                            category_count[cat] = category_count.get(cat, 0) + 1
                            if category == 'all' or cat == category:
                                sigs.append({'code': code, 'name': name, 'signals': [s]})
                                if return_data:
                                    indicators_dict[code] = df_ind
                except Exception as _e:
                    # 不再静默 pass：单只股票出错计数，超过阈值说明策略本身坏了，
                    # 否则「策略异常 0 只」和「今天没信号」在卡片上完全一样。
                    err_count += 1
                    if first_err is None:
                        first_err = f"{code}: {type(_e).__name__}: {_e}"
            if err_count:
                strategy_errors[sname] = {
                    'errors': err_count,
                    'of': len(valid_stocks),
                    'first': first_err,
                }
            return sname, sigs

        print(f"并行执行 {len(self.registry.strategies)} 个策略 (workers={_max_workers})")
        with ThreadPoolExecutor(max_workers=_max_workers) as pool:
            futures = {pool.submit(_run_strategy, item): item for item in self.registry.strategies.items()}
            for future in as_completed(futures):
                sname, sigs = future.result()
                results[sname] = sigs
                print(f"  ✓ {sname}: {len(sigs)} 只")

        # 策略健康度：任一策略在过半股票上抛异常，基本可以断定是代码/数据字段问题，
        # 把它显式打出来（Actions 里会变成 warning 标记），不要等到没人发现。
        self.last_universe_stats['strategy_errors'] = strategy_errors
        for _sname, _info in sorted(strategy_errors.items()):
            _ratio = _info['errors'] / _info['of'] if _info['of'] else 0
            if _ratio >= 0.5:
                print(f"  ⚠️ 策略【{_sname}】在 {_info['errors']}/{_info['of']} 只股票上抛异常，"
                      f"结果不可信；首条错误 {_info['first']}")
            else:
                print(f"  · 策略【{_sname}】有 {_info['errors']}/{_info['of']} 只计算失败（已跳过）")

        gc.collect()
        
        # 显示结果汇总
        print("\n" + "=" * 60)
        print("📊 选股结果汇总")
        print("=" * 60)
        
        for strategy_name, signals in results.items():
            print(f"\n{strategy_name}: {len(signals)} 只")
            for signal in signals:
                code = signal['code']
                name = signal.get('name', stock_names.get(code, '未知'))
                for s in signal['signals']:
                    cat_emoji = {'bowl_center': '🥣', 'near_duokong': '📊', 'near_short_trend': '📈'}.get(s.get('category'), '❓')
                    print(f"  {cat_emoji} {code} {name}: 价格={s.get('close','-')}, J={s.get('J','-')}, 理由={s.get('reasons','-')}")
        
        # 显示分类统计
        print("\n" + "-" * 60)
        print("分类统计:")
        print(f"  🥣 回落碗中: {category_count.get('bowl_center', 0)} 只")
        print(f"  📊 靠近多空线: {category_count.get('near_duokong', 0)} 只")
        print(f"  📈 靠近短期趋势线: {category_count.get('near_short_trend', 0)} 只")
        print("-" * 60)
        
        # 应用过滤条件
        print("\n应用过滤条件...")
        filter_config = self.config.get('filters', {})
        stock_filter = StockFilter(filter_config)
        
        # 构建股票数据字典用于过滤
        stock_data_for_filter = {}
        for code in stock_codes[:max_stocks] if max_stocks else stock_codes:
            if code in indicators_dict:
                name = stock_names.get(code, '未知')
                stock_data_for_filter[code] = (name, indicators_dict[code])
        
        # 应用过滤
        filtered_results, filter_stats = stock_filter.apply_filters(results, stock_data_for_filter)
        
        # 显示过滤统计
        if filter_stats.get('enabled', False):
            print(f"\n过滤统计:")
            print(f"  过滤前: {filter_stats['total_before']} 只")
            print(f"  过滤后: {filter_stats['total_after']} 只")
            print(f"  被过滤: {filter_stats['filtered_out']} 只")
            
            # 显示各过滤条件的统计
            for filter_name, count in filter_stats.get('filters_applied', {}).items():
                if count > 0:
                    print(f"    - {filter_name}: {count} 只")
            
            # 显示被过滤的股票信息（最多显示10只）
            filtered_stocks = filter_stats.get('filtered_stocks', [])
            if filtered_stocks:
                print(f"\n被过滤的股票（共{len(filtered_stocks)}只）:")
                for stock_info in filtered_stocks:
                    print(f"  ⚠️  {stock_info['code']} {stock_info['name']}: {stock_info['reason']}")
        
        # 使用过滤后的结果
        results = filtered_results
        
        # 如果需要返回数据字典（用于K线图生成）
        if return_data:
            # 返回计算了指标的数据（包含趋势线）
            return results, stock_names, indicators_dict
        
        return results, stock_names
    
    def run_full(self, category='all', max_stocks=None):
        """完整流程：更新 + 选股
        :param max_stocks: 限制处理的股票数量（用于快速测试）
        """
        from datetime import datetime
        import json
        from pathlib import Path

        print("=" * 60)
        print("🚀 执行完整流程")
        if max_stocks:
            print(f"   快速测试模式：只处理前 {max_stocks} 只股票")
        print("=" * 60)

        # 1. 更新数据（内置逻辑：3点前不更新，检查每只股票是否有当天数据）
        self._smart_update(max_stocks=max_stocks)

        # 2. 选股（返回数据和结果）
        results, stock_names, stock_data_dict = self.select_stocks(category=category, max_stocks=max_stocks, return_data=True)

        return results
    
    def select_with_b1_match(self, category='all', max_stocks=None, min_similarity=None, lookback_days=None):
        """
        执行选股 + B1完美图形匹配排序
        
        Args:
            category: 股票分类筛选，'all'表示全部
            max_stocks: 限制处理的股票数量
            min_similarity: 最小相似度阈值，低于此值不显示
            lookback_days: 回看天数，默认25天
            
        Returns:
            dict: 包含选股结果和匹配结果
        """
        # 从配置读取默认值
        from strategy.pattern_config import MIN_SIMILARITY_SCORE, DEFAULT_LOOKBACK_DAYS
        if min_similarity is None:
            min_similarity = MIN_SIMILARITY_SCORE
        if lookback_days is None:
            lookback_days = DEFAULT_LOOKBACK_DAYS
        
        print("=" * 60)
        print("🎯 执行选股 + B1完美图形匹配")
        if max_stocks:
            print(f"   快速测试模式：只处理前 {max_stocks} 只股票")
        print(f"   相似度阈值: {min_similarity}%")
        print(f"   回看天数: {lookback_days}天")
        print("=" * 60)
        
        # 1. 先执行原有选股逻辑
        print("\n[1/3] 执行策略选股...")
        results, stock_names, stock_data_dict = self.select_stocks(
            category=category, 
            max_stocks=max_stocks, 
            return_data=True
        )
        
        # 统计选股总数
        total_selected = sum(len(signals) for signals in results.values())
        if total_selected == 0:
            print("\n✗ 策略未选出任何股票，跳过匹配")
            return {'results': results, 'stock_names': stock_names, 'matched': []}
        
        print(f"\n✓ 策略选出 {total_selected} 只股票")
        
        # 2. 初始化B1完美图形库
        print("\n[2/3] 初始化B1完美图形库...")
        try:
            from strategy.pattern_library import B1PatternLibrary
            from strategy.pattern_config import MIN_SIMILARITY_SCORE
            
            library = B1PatternLibrary(self.csv_manager)
            
            if not library.cases:
                print("⚠️ 警告: 案例库为空，可能数据不足")
                return {'results': results, 'stock_names': stock_names, 'matched': []}
            
            print(f"✓ 案例库加载完成: {len(library.cases)} 个案例")
            
        except Exception as e:
            print(f"✗ 初始化案例库失败: {e}")
            import traceback
            traceback.print_exc()
            return {'results': results, 'stock_names': stock_names, 'matched': []}
        
        # 3. 对每只候选股进行匹配
        print("\n[3/3] 执行B1完美图形匹配...")
        matched_results = []
        
        for strategy_name, signals in results.items():
            for signal in signals:
                code = signal['code']
                name = signal.get('name', stock_names.get(code, '未知'))
                
                # 获取该股票的完整数据
                if code not in stock_data_dict:
                    continue
                
                df = stock_data_dict[code]
                if df.empty:
                    continue
                
                try:
                    # 匹配最佳案例（使用指定回看天数）
                    match_result = library.find_best_match(code, df, lookback_days=lookback_days)
                    
                    if match_result.get('best_match'):
                        best = match_result['best_match']
                        score = best.get('similarity_score', 0)
                        
                        # 只保留超过阈值的股票
                        if score >= min_similarity:
                            # 获取第一个信号的信息
                            s = signal['signals'][0] if signal.get('signals') else {}
                            
                            matched_results.append({
                                'stock_code': code,
                                'stock_name': name,
                                'strategy': strategy_name,
                                'category': s.get('category', 'unknown'),
                                'close': s.get('close', '-'),
                                'J': s.get('J', '-'),
                                'similarity_score': score,
                                'matched_case': best.get('case_name', ''),
                                'matched_date': best.get('case_date', ''),
                                'matched_code': best.get('case_code', ''),
                                'breakdown': best.get('breakdown', {}),
                                'tags': best.get('tags', []),
                                'all_matches': best.get('all_matches', []),
                            })
                            
                except Exception as e:
                    print(f"  ⚠️ 匹配 {code} 失败: {e}")
                    continue
        
        # 按相似度排序
        matched_results.sort(key=lambda x: x['similarity_score'], reverse=True)
        
        print(f"\n✓ 匹配完成: {len(matched_results)} 只股票超过阈值")
        
        # 显示Top N结果（使用配置）
        from strategy.pattern_config import TOP_N_RESULTS
        if matched_results:
            print("\n" + "=" * 60)
            print(f"📊 Top {TOP_N_RESULTS} B1完美图形匹配结果")
            print("=" * 60)
            for i, r in enumerate(matched_results[:TOP_N_RESULTS], 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                print(f"{emoji} {r['stock_code']} {r['stock_name']}")
                print(f"   相似度: {r['similarity_score']}% | 匹配: {r['matched_case']}")
                bd = r.get('breakdown', {})
                print(f"   趋势:{bd.get('trend_structure', 0)}% "
                      f"KDJ:{bd.get('kdj_state', 0)}% "
                      f"量能:{bd.get('volume_pattern', 0)}% "
                      f"形态:{bd.get('price_shape', 0)}%")
        
        return {
            'results': results,
            'stock_names': stock_names,
            'matched': matched_results,
            'total_selected': total_selected,
        }
    
    def run_with_b1_match(self, category='all', max_stocks=None, min_similarity=60.0, lookback_days=25):
        """
        完整流程：更新 + 选股 + B1完美图形匹配

        Args:
            category: 股票分类筛选
            max_stocks: 限制处理的股票数量
            min_similarity: 最小相似度阈值
            lookback_days: 回看天数，默认25天
        """
        from datetime import datetime

        print("=" * 60)
        print("🚀 执行完整流程（含B1完美图形匹配）")
        if max_stocks:
            print(f"   快速测试模式：只处理前 {max_stocks} 只股票")
        print(f"   回看天数: {lookback_days}天")
        print("=" * 60)

        # 1. 更新数据
        self._smart_update(max_stocks=max_stocks)

        # 2. 选股 + B1完美图形匹配
        match_result = self.select_with_b1_match(
            category=category,
            max_stocks=max_stocks,
            min_similarity=min_similarity,
            lookback_days=lookback_days
        )
        
        return match_result
    
    def run_schedule(self):
        """启动定时调度"""
        try:
            import schedule
        except ImportError:
            print("✗ 请安装 schedule: pip install schedule")
            return
        
        schedule_time = self.config.get('schedule', {}).get('time', '15:05')
        
        print("=" * 60)
        print(f"⏰ 启动定时调度")
        print(f"   每日 {schedule_time} 执行选股任务")
        print("=" * 60)
        
        # 设置定时任务
        schedule.every().day.at(schedule_time).do(self.run_full)
        
        print("\n按 Ctrl+C 停止")
        
        while True:
            schedule.run_pending()
            time.sleep(60)


def print_version():
    """打印版本信息"""
    import akshare
    import pandas
    
    print(f"A-Share Quant v{__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"akshare: {akshare.__version__}")
    print(f"pandas: {pandas.__version__}")
    print(f"System: {platform.system()}")
    print(f"B1 Pattern Match: 支持（基于双线+量比+形态三维匹配，10个历史案例）")


def main():
    parser = argparse.ArgumentParser(
        description='A股量化选股系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py init                          # 首次抓取6年历史数据
  python main.py run                           # 完整流程（更新+选股+通知）
  python main.py run --b1-match                # 完整流程+B1完美图形匹配排序
  python main.py run --b1-match --min-similarity 70  # 匹配+提高相似度阈值到70%
  python main.py run --b1-match --lookback-days 30   # 使用30天回看期
  python main.py web                           # 启动Web界面
  python main.py --version                     # 显示版本信息

分类说明:
  all              - 全部（回落碗中 + 靠近多空线 + 靠近短期趋势线）
  bowl_center      - 回落碗中（优先级最高）
  near_duokong     - 靠近多空线（±duokong_pct%，默认3%）
  near_short_trend - 靠近短期趋势线（±short_pct%，默认2%）

B1完美图形匹配:
  基于10个历史成功案例（双线+量比+形态三维相似度匹配）
  使用 --b1-match 参数启用，--lookback-days 调整回看天数（默认25天）
  使用 --min-similarity 调整匹配阈值（默认60%，范围0-100）
        """
    )

    parser.add_argument(
        '--version',
        action='store_true',
        help='显示版本信息并退出'
    )

    parser.add_argument(
        'command',
        choices=['init', 'run', 'web'],
        nargs='?',
        help='要执行的命令: init(初始化数据), run(执行选股), web(启动Web服务器)'
    )

    parser.add_argument(
        '--max-stocks',
        type=int,
        default=None,
        help='限制处理的股票数量（用于快速测试）'
    )

    parser.add_argument(
        '--config',
        default='config/config.yaml',
        help='配置文件路径'
    )

    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='Web服务器监听地址 (默认: 127.0.0.1 仅本机；'
             '要用局域网访问设 0.0.0.0 并同时设置 WEB_ALLOWED_HOSTS)'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=5000,
        help='Web服务器端口 (默认: 5000)'
    )
    
    parser.add_argument(
        '--category',
        type=str,
        choices=['all', 'bowl_center', 'near_duokong', 'near_short_trend'],
        default='all',
        help='筛选股票分类: all(全部), bowl_center(回落碗中), near_duokong(靠近多空线), near_short_trend(靠近短期趋势线)'
    )
    
    # 从配置读取B1PatternMatch默认值
    try:
        from strategy.pattern_config import MIN_SIMILARITY_SCORE, DEFAULT_LOOKBACK_DAYS
        default_min_similarity = MIN_SIMILARITY_SCORE
        default_lookback_days = DEFAULT_LOOKBACK_DAYS
    except:
        default_min_similarity = 60.0
        default_lookback_days = 25
    
    parser.add_argument(
        '--min-similarity',
        type=float,
        default=None,
        help=f'B1完美图形匹配的最小相似度阈值 (默认: {default_min_similarity})'
    )
    
    parser.add_argument(
        '--b1-match',
        action='store_true',
        help='启用B1完美图形匹配排序（在run命令中使用）'
    )
    
    parser.add_argument(
        '--lookback-days',
        type=int,
        default=None,
        help=f'B1完美图形匹配的回看天数 (默认: {default_lookback_days})'
    )

    args = parser.parse_args()

    # 处理 --version 参数
    if args.version:
        print_version()
        sys.exit(0)

    # 检查命令是否提供
    if not args.command:
        print("未指定命令，默认执行选股并推送飞书...")
        args.command = 'run'
    
    # 切换工作目录
    os.chdir(project_root)
    
    # 创建系统实例
    quant = QuantSystem(args.config)
    
    # 执行命令
    if args.command == 'init':
        quant.init_data(max_stocks=args.max_stocks)
    
    elif args.command == 'run':
            # 检查今天是否为交易日
            from utils.trade_date_utils import is_trading_day
            from datetime import datetime
            today_str = datetime.now().strftime('%Y-%m-%d')
            if not is_trading_day(today_str):
                print(f"今天 {today_str} 不是交易日，跳过选股")
                sys.exit(0)

            # 幂等守卫：外部准点触发 + GitHub cron 延迟触发，同一档期只推一次。
            # 只认「飞书确实推送成功」写入的台账，非交易日/调试运行不会误杀当天推送。
            from utils.push_guard import mark_pushed, should_skip
            if should_skip("selection.yml", cutoff_hour=12, slot_hour=SELECTION_SLOT_HOUR):
                print("⏭ 本档期已推送成功，跳过本次运行")
                sys.exit(0)
            
            # 原有选股流程（支持B1完美图形匹配）
            if args.b1_match:
                # 启用B1完美图形匹配
                # 如果命令行未指定，使用配置文件中的默认值
                min_sim = args.min_similarity if args.min_similarity is not None else default_min_similarity
                lookback = args.lookback_days if args.lookback_days is not None else default_lookback_days
                result = quant.run_with_b1_match(
                    category=args.category,
                    max_stocks=args.max_stocks,
                    min_similarity=min_sim,
                    lookback_days=lookback
                )
            else:
                # 原有选股流程（不带B1匹配）
                result = quant.run_full(category=args.category, max_stocks=args.max_stocks)

            # 飞书推送
            # 空 dict 只可能来自「没有策略 / 没有股票数据」这两个硬故障，不是「今天没信号」。
            # 后者会返回带空列表的 dict（走下面正常推送，卡片里写明无信号）。
            # 这里必须显式失败：否则整段推送被跳过、进程退出码 0，Actions 变绿却一条消息都没有，
            # 正是本项目一直想消灭的「假绿」。DB 缓存被清、init 失败时最容易撞上。
            if not result:
                msg = ("选股结果为空（没有可用策略或股票数据），本次未推送。"
                       "请先执行 `python main.py init`，或检查 Actions 的数据库缓存是否已失效。")
                if os.environ.get("CI"):
                    print(f"::error::{msg}")
                    sys.exit(1)
                print(f"::warning::{msg}")
                sys.exit(1)
            if result:
                from utils.feishu_notifier import FeishuNotifier
                feishu_cfg = quant.config.get('feishu', {})
                webhook_url = os.environ.get('FEISHU_WEBHOOK') or feishu_cfg.get('webhook_url', '')
                notifier = FeishuNotifier(webhook_url)

                # 加载策略中文名称映射
                display_names = {}
                try:
                    import yaml as _yaml
                    from pathlib import Path
                    cfg_path = Path("config/strategy_params.yaml")
                    if cfg_path.exists():
                        with open(cfg_path, 'r', encoding='utf-8') as _f:
                            _cfg = _yaml.safe_load(_f) or {}
                        for _k, _v in _cfg.get('strategies', {}).items():
                            display_names[_k] = _v.get('display_name', _k)
                except Exception as _e:
                    # 只影响卡片上显示中文策略名，回落到英文 key，但要留痕
                    logger.debug("加载策略中文名映射失败，卡片将显示英文策略名: %s", _e)

                lines = [f"📊 缅A每日推送 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})", ""]

                # 数据基准日：必须写出来。数据更新被跳过（15:00 前 / 抽样命中）时，
                # 选股用的是旧 K 线，但卡片头只有「当前时间」，用户无法分辨。
                _us = getattr(quant, 'last_universe_stats', {}) or {}
                _ref = _us.get('reference_date')
                if _ref:
                    _today_str_for_ref = datetime.now().strftime('%Y-%m-%d')
                    if _ref != _today_str_for_ref:
                        lines.append(f"⚠️ 本次选股基于 {_ref} 的 K 线（非今日数据，请留意）")
                    else:
                        lines.append(f"数据截至 {_ref}")
                if _us.get('stale_dropped'):
                    lines.append(
                        f"已剔除 {_us['stale_dropped']} 只停牌/数据未更新标的"
                        + (f"（最老 K 线 {_us['stale_oldest']}）" if _us.get('stale_oldest') else "")
                    )
                if _us:
                    lines.append("")

                total = 0
                all_stocks = []
                if isinstance(result, dict):
                    for strategy_name, signals in result.items():
                        if signals:
                            cn_name = display_names.get(strategy_name, strategy_name)
                            lines.append(f"【{cn_name}】: {len(signals)} 只")
                            for s in signals:
                                name = s.get('name', '')
                                code = s.get('code', '')
                                sig = s.get('signals', [])
                                if sig:
                                    lines.append(f"  {code} {name} 价格:{sig[0].get('close','-')}")
                                else:
                                    lines.append(f"  {code} {name}")
                                all_stocks.append({'code': code, 'name': name})
                            lines.append("")
                            total += len(signals)

                # 0 只结果要能区分「今天确实没信号」和「策略/数据坏了」。
                # 不标注的话，两种情况的卡片长得一模一样。
                if total == 0:
                    _broken = [
                        (n, i) for n, i in (quant.last_universe_stats.get('strategy_errors') or {}).items()
                        if i.get('of') and i['errors'] / i['of'] >= 0.5
                    ]
                    if _broken:
                        lines.append("⚠️ 今日无信号，但以下策略大面积计算失败，结果不可信：")
                        for _n, _i in _broken[:5]:
                            lines.append(f"  · {_n}：{_i['errors']}/{_i['of']} 只失败")
                        lines.append("")
                    elif quant.last_universe_stats.get('usable', 0) == 0:
                        lines.append("⚠️ 今日无信号：可用股票池为 0（数据未就绪？请检查数据更新日志）")
                        lines.append("")
                    else:
                        lines.append("今日各策略均无信号")
                        lines.append("")

                # 与上一日对比：新增/去除
                if total > 0:
                    try:
                        from utils.selection_record_manager import SelectionRecordManager
                        from datetime import date as _date
                        _srm = SelectionRecordManager()
                        _today_codes = {s['code'] for s in all_stocks}
                        _today_str = datetime.now().strftime('%Y-%m-%d')

                        _prev_result = _srm.get_selection_history(
                            filters={'end_date': _today_str}, page=1, limit=5000
                        )
                        _prev_stocks = {}
                        for _r in (_prev_result.get('data') or []):
                            _d = _r.get('selection_date', '')
                            if _d and _d < _today_str:
                                if _d not in _prev_stocks:
                                    _prev_stocks[_d] = set()
                                _prev_stocks[_d].add(_r.get('stock_code', ''))

                        if _prev_stocks:
                            _prev_date = max(_prev_stocks.keys())
                            _prev_codes = _prev_stocks[_prev_date]
                            _new_codes = _today_codes - _prev_codes
                            _removed_codes = _prev_codes - _today_codes

                            if _new_codes or _removed_codes:
                                lines.append("━━━━━━━━━━━━━━━━━━━━")
                                lines.append(f"📋 与 {_prev_date} 对比")
                                lines.append("")
                                if _new_codes:
                                    _new_names = [f"{s['code']} {s['name']}" for s in all_stocks if s['code'] in _new_codes]
                                    lines.append(f"  🟢 新增 ({len(_new_codes)}只):")
                                    for _n in _new_names:
                                        lines.append(f"    + {_n}")
                                if _removed_codes:
                                    lines.append(f"  🔴 去除 ({len(_removed_codes)}只):")
                                    for _rc in _removed_codes:
                                        lines.append(f"    - {_rc}")
                                lines.append("")
                    except Exception as _diff_err:
                        print(f"  ⚠️ 对比历史选股失败: {_diff_err}")

                # ─── 买入建议分析 ───
                if all_stocks:
                    try:
                        from simple_analyzer import analyze_stock, format_analysis_message
                        _analyses = []
                        for _stock in all_stocks:
                            _a = analyze_stock(_stock['code'], _stock['name'])
                            if _a['score'] > 0:
                                _analyses.append(_a)
                        if _analyses:
                            _analysis_msg = format_analysis_message(_analyses)
                            if _analysis_msg:
                                lines.append(_analysis_msg)
                    except Exception as _a_err:
                        print(f"  ⚠️ 买入建议分析失败: {_a_err}")

                # ─── 每日大盘复盘 + 新闻 ───
                try:
                    import requests as _req

                    # 1) 主要指数行情
                    _index_codes = [
                        ('sh000001', '上证指数'),
                        ('sz399001', '深证成指'),
                        ('sz399006', '创业板指'),
                        ('sh000688', '科创50'),
                    ]
                    _idx_lines = []
                    for _code, _name in _index_codes:
                        try:
                            _url = f"https://qt.gtimg.cn/q={_code}"
                            _resp = _req.get(_url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
                            _data = _resp.text.split('~')
                            if len(_data) > 45:
                                _price = _data[3]
                                _pct = _data[32]
                                _vol = _data[37]
                                try:
                                    _vol_yi = float(_vol) / 10000
                                    _vol_str = f"{_vol_yi:.0f}亿"
                                except Exception:
                                    _vol_str = ''
                                _emoji = '🔴' if float(_pct) > 0 else '🟢' if float(_pct) < 0 else '⚪'
                                _idx_lines.append(f"  {_emoji} {_name}: {_price} ({_pct}%) {_vol_str}")
                        except Exception:
                            pass
                    if _idx_lines:
                        lines.append("━━━━━━━━━━━━━━━━━━━━")
                        lines.append(f"📈 今日大盘复盘 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
                        lines.append("")
                        lines.extend(_idx_lines)
                        lines.append("")

                    # 2) 板块涨跌热力图
                    try:
                        import re as _re, json as _json
                        from sector_data import format_sector_message as _sector_msg
                        _sector_text = _sector_msg()
                        if _sector_text:
                            lines.append("━━━━━━━━━━━━━━━━━━━━")
                            lines.append(_sector_text)
                    except Exception:
                        pass

                    # 3) 财经要闻
                    try:
                        _news_resp = _req.get(
                            'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=12&page=1',
                            timeout=8,
                            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'}
                        )
                        _news_list = _news_resp.json().get('result', {}).get('data', [])
                        if _news_list:
                            lines.append("━━━━━━━━━━━━━━━━━━━━")
                            lines.append("📰 今日财经要闻")
                            lines.append("")
                            for _n in _news_list[:8]:
                                _title = _n.get('title', '')[:50]
                                _intro = _n.get('intro', '')
                                if _title:
                                    lines.append(f"  • {_title}")
                                    if _intro:
                                        lines.append(f"    {_intro}")
                            lines.append("")
                    except Exception:
                        pass

                except Exception as _idx_err:
                    print(f"  ⚠️ 获取大盘数据失败: {_idx_err}")

                lines.insert(1, f"共 {total} 只股票入选")
                # 推送失败必须让 Actions 变红，否则「运行成功却没收到消息」很难排查
                if not notifier.send_text("\n".join(lines)):
                    if not webhook_url:
                        msg = "FEISHU_WEBHOOK 未配置，选股结果已生成但不会推送，请在仓库 Settings → Secrets 中配置"
                        if os.environ.get("CI"):
                            # CI 里没配 webhook 属于配置错误：必须让 Actions 变红，否则"绿了但没消息"最难发现
                            print(f"::error::{msg}")
                            sys.exit(1)
                        print(f"::warning::{msg}")
                    else:
                        print("❌ 收盘选股飞书推送失败")
                        sys.exit(1)
                else:
                    print("✅ 收盘选股飞书推送成功")
                    mark_pushed("selection.yml", cutoff_hour=12, slot_hour=SELECTION_SLOT_HOUR)
    
    elif args.command == 'web':
        # 启动Web服务器
        from web_server import run_web_server
        run_web_server(host=args.host, port=args.port)


if __name__ == '__main__':
    main()
