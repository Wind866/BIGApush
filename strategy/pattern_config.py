"""
B1完美图形配置管理
新增案例只需在这里添加配置，无需改代码
为后续B2、B3等扩展预留空间

配置优先级：
1. 首先读取 config/strategy_params.yaml 中的 B1PatternMatch 配置
2. 如果YAML中未配置，使用本文件中的默认值
"""

import os
import yaml
from pathlib import Path

# 仓库根目录（strategy/ 的上一层）。云端 runner 上没有 /root/quant-csv，
# 之前把路径写死成绝对路径会让 Path.exists() 直接抛 PermissionError，
# 进而使 pattern_config / pattern_library 导入失败、B1 图形匹配策略线上不可用。
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_candidates():
    """按优先级返回配置文件候选路径：环境变量 → 仓库内。

    不再保留任何绝对路径兜底（例如旧部署机的 /root/...）：云端 runner 上
    访问这类路径会抛 PermissionError，正是它把整个策略注册表拖垮的。
    需要自定义位置时设置环境变量 STRATEGY_PARAMS_PATH。
    """
    env_path = os.environ.get("STRATEGY_PARAMS_PATH", "").strip()
    if env_path:
        yield Path(env_path)
    yield _REPO_ROOT / "config" / "strategy_params.yaml"


def _load_yaml_config():
    """从YAML配置文件加载B1PatternMatch配置

    任何取不到的情况（不存在 / 无权限 / 解析失败）都只降级为默认值，绝不抛错，
    避免一个可选配置文件把整个策略注册表拖垮。
    """
    for config_path in _config_candidates():
        try:
            if not config_path.is_file():
                continue
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            return config.get('B1PatternMatch', {}) or {}
        except (OSError, PermissionError):
            # 路径不可访问（如 /root/... 在 runner 上），静默跳到下一个候选
            continue
        except Exception as e:  # noqa: BLE001  解析失败同样降级
            print(f"⚠️ 加载B1PatternMatch配置失败({config_path}): {e}，使用默认值")
            continue
    return {}


# 加载YAML配置（失败即用文件内默认值）
try:
    _yaml_config = _load_yaml_config()
except Exception as e:  # noqa: BLE001  理论到不了这里，兜底防止模块导入失败
    print(f"⚠️ 读取B1配置异常，使用默认值: {e}")
    _yaml_config = {}

# B1完美图形案例配置（10个历史成功案例）
# 注：日期为"选股系统选出的买入日期"，不是突破日
B1_PERFECT_CASES = [
    {
        "id": "case_001",
        "name": "华纳药厂",
        "code": "688799",
        "breakout_date": "2025-05-12",
        "lookback_days": 25,
        "tags": ["科创板", "医药"],
        "description": "杯型整理+缩量+J值低位",
    },
    {
        "id": "case_002",
        "name": "宁波韵升",
        "code": "600366",
        "breakout_date": "2025-08-06",
        "lookback_days": 25,
        "tags": ["主板", "稀土永磁"],
        "description": "回落短期趋势线+量能平稳+J值中位",
    },
    {
        "id": "case_003",
        "name": "微芯生物",
        "code": "688321",
        "breakout_date": "2025-06-20",
        "lookback_days": 25,
        "tags": ["科创板", "医药"],
        "description": "平台整理+缩量后放量+J值低位",
    },
    {
        "id": "case_004",
        "name": "方正科技",
        "code": "600601",
        "breakout_date": "2025-07-23",
        "lookback_days": 25,
        "tags": ["主板", "科技"],
        "description": "靠近多空线+量能平稳+J值中位",
    },
    {
        "id": "case_006",
        "name": "国轩高科",
        "code": "002074",
        "breakout_date": "2025-08-04",
        "lookback_days": 25,
        "tags": ["中小板", "新能源"],
        "description": "靠近短期趋势线+量能平稳+J值低位",
    },
    {
        "id": "case_007",
        "name": "野马电池",
        "code": "605378",
        "breakout_date": "2025-08-01",
        "lookback_days": 25,
        "tags": ["主板", "电池"],
        "description": "持续缩量+J值深度低位+趋势下行",
    },
    {
        "id": "case_008",
        "name": "光电股份",
        "code": "600184",
        "breakout_date": "2025-07-10",
        "lookback_days": 25,
        "tags": ["主板", "军工"],
        "description": "缩量后放量+J值低位+趋势上行",
    },
    {
        "id": "case_009",
        "name": "新瀚新材",
        "code": "301076",
        "breakout_date": "2025-08-01",
        "lookback_days": 25,
        "tags": ["创业板", "化工"],
        "description": "缩量后放量+价格接近短期趋势线+J值中位",
    },
    {
        "id": "case_010",
        "name": "昂利康",
        "code": "002940",
        "breakout_date": "2025-07-11",
        "lookback_days": 25,
        "tags": ["中小板", "医药"],
        "description": "价格接近短期趋势线+缩量+顶部未放量",
    },
    {
        "id": "case_011",
        "name": "航天发展",
        "code": "000547",
        "breakout_date": "2025-11-12",
        "lookback_days": 25,
        "tags": ["主板", "军工"],
        "description": "航天军工+量能异动+趋势突破",
    },
]

# 相似度权重配置（优先从YAML读取，否则使用默认值）
_default_weights = {
    "trend_structure": 0.30,    # 知行趋势线结构
    "kdj_state": 0.20,          # KDJ动能状态
    "volume_pattern": 0.25,     # 量能特征
    "price_shape": 0.25,        # 价格形态
}
SIMILARITY_WEIGHTS = _yaml_config.get('weights', _default_weights)

# 匹配阈值（低于此值不显示）
MIN_SIMILARITY_SCORE = _yaml_config.get('min_similarity', 60.0)

# 回看天数（默认25天）
DEFAULT_LOOKBACK_DAYS = _yaml_config.get('lookback_days', 25)

# Top N 结果展示（优先从YAML读取）
TOP_N_RESULTS = _yaml_config.get('top_n_results', 15)

# 匹配容差参数（优先从YAML读取）
_default_tolerances = {
    "trend_ratio": 0.10,    # 趋势比值容差（±10%）
    "price_bias": 10,       # 价格偏离容差（±10%）
    "trend_spread": 10,     # 趋势发散容差（±10%）
    "j_value": 30,          # J值差异容差（±30）
    "drawdown": 15,         # 回撤幅度容差（±15%）
}
MATCH_TOLERANCES = _yaml_config.get('tolerances', _default_tolerances)
