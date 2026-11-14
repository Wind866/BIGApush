#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术分析模块
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta


class TechnicalAnalyzer:
    """技术分析器"""
    
    def __init__(self):
        """初始化技术分析器"""
        pass
    
    def calculate_ma(self, data: pd.DataFrame, periods: List[int]) -> pd.DataFrame:
        """计算移动平均线
        
        Args:
            data: 历史行情数据
            periods: 移动平均线周期列表
            
        Returns:
            pd.DataFrame: 添加了移动平均线的数据集
        """
        if data is None or data.empty:
            return data
        
        for period in periods:
            data[f'ma{period}'] = data['close'].rolling(window=period).mean()
        return data
    
    def calculate_macd(self, data: pd.DataFrame, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> pd.DataFrame:
        """计算MACD指标
        
        Args:
            data: 历史行情数据
            fast_period: 快速移动平均线周期
            slow_period: 慢速移动平均线周期
            signal_period: 信号线周期
            
        Returns:
            pd.DataFrame: 添加了MACD指标的数据集
        """
        if data is None or data.empty:
            return data
        
        # 计算快速和慢速移动平均线
        data['ema12'] = data['close'].ewm(span=fast_period, adjust=False).mean()
        data['ema26'] = data['close'].ewm(span=slow_period, adjust=False).mean()
        
        # 计算MACD线
        data['macd'] = data['ema12'] - data['ema26']
        
        # 计算信号线
        data['signal'] = data['macd'].ewm(span=signal_period, adjust=False).mean()
        
        # 计算柱状图
        data['hist'] = data['macd'] - data['signal']
        
        return data
    
    def calculate_rsi(self, data: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """计算RSI指标
        
        Args:
            data: 历史行情数据
            period: RSI周期
            
        Returns:
            pd.DataFrame: 添加了RSI指标的数据集
        """
        if data is None or data.empty:
            return data
        
        # 计算价格变化
        delta = data['close'].diff()
        
        # 分离涨跌
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        # 计算RSI
        rs = gain / loss
        data['rsi'] = 100 - (100 / (1 + rs))
        
        return data
    
    def calculate_bollinger_bands(self, data: pd.DataFrame, period: int = 20, std_dev: float = 2) -> pd.DataFrame:
        """计算布林带
        
        Args:
            data: 历史行情数据
            period: 移动平均线周期
            std_dev: 标准差倍数
            
        Returns:
            pd.DataFrame: 添加了布林带的数据集
        """
        if data is None or data.empty:
            return data
        
        # 计算中轨
        data['bb_mid'] = data['close'].rolling(window=period).mean()
        
        # 计算上轨和下轨
        data['bb_std'] = data['close'].rolling(window=period).std()
        data['bb_upper'] = data['bb_mid'] + (data['bb_std'] * std_dev)
        data['bb_lower'] = data['bb_mid'] - (data['bb_std'] * std_dev)
        
        return data
    
    def calculate_kdj(self, data: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
        """计算KDJ指标
        
        Args:
            data: 历史行情数据
            n: 周期
            m1: K值平滑周期
            m2: D值平滑周期
            
        Returns:
            pd.DataFrame: 添加了KDJ指标的数据集
        """
        if data is None or data.empty:
            return data
        
        # 计算RSV
        data['low_n'] = data['low'].rolling(window=n).min()
        data['high_n'] = data['high'].rolling(window=n).max()
        data['rsv'] = (data['close'] - data['low_n']) / (data['high_n'] - data['low_n']) * 100
        
        # 计算K值
        data['k'] = data['rsv'].ewm(com=m1-1, adjust=False).mean()
        
        # 计算D值
        data['d'] = data['k'].ewm(com=m2-1, adjust=False).mean()
        
        # 计算J值
        data['j'] = 3 * data['k'] - 2 * data['d']
        
        return data
    
    def analyze_trend(self, data: pd.DataFrame) -> Dict[str, Any]:
        """分析趋势
        
        Args:
            data: 历史行情数据
            
        Returns:
            dict: 趋势分析结果
        """
        if data is None or data.empty:
            return {
                "trend": "未知",
                "strength": 0,
                "support": 0,
                "resistance": 0
            }
        
        # 计算关键指标
        data = self.calculate_ma(data, [20, 50, 200])
        data = self.calculate_macd(data)
        data = self.calculate_rsi(data)
        
        # 分析趋势
        trend = "未知"
        strength = 0
        
        # 基于MA判断趋势（自适应）
        # MA200 需要 ≥200 个交易日才非空；行情窗口不足时旧逻辑四个分支全部落空，
        # 趋势恒为「未知」。这里按可用均线降级：MA20/50/200 -> MA20/50 -> MA20。
        if 'ma20' in data.columns:
            latest_data = data.iloc[-1]
            ma20 = latest_data.get('ma20')
            ma50 = latest_data.get('ma50')
            ma200 = latest_data.get('ma200')
            has20 = pd.notna(ma20)
            has50 = pd.notna(ma50)
            has200 = pd.notna(ma200)

            if has20 and has50 and has200:
                if ma20 > ma50 > ma200:
                    trend = "上升"
                    strength = 3
                elif ma20 < ma50 < ma200:
                    trend = "下降"
                    strength = 3
                elif ma20 > ma50 < ma200:
                    trend = "震荡"
                    strength = 1
                elif ma20 < ma50 > ma200:
                    trend = "震荡"
                    strength = 1
            elif has20 and has50:
                # 数据不足以算 MA200：用 MA20/MA50 位置 + MA20 近 5 日斜率近似中期趋势
                ma20_prev = data['ma20'].iloc[-6] if len(data) >= 6 else None
                slope_up = pd.notna(ma20_prev) and ma20 > ma20_prev
                if ma20 > ma50:
                    trend, strength = ("上升", 2) if slope_up else ("震荡", 1)
                else:
                    trend, strength = ("下降", 2) if not slope_up else ("震荡", 1)
            elif has20:
                # 只有 MA20：用收盘价相对 MA20 的位置判断短期趋势
                close = latest_data.get('close')
                if pd.notna(close):
                    trend, strength = ("上升", 1) if close >= ma20 else ("下降", 1)
        
        # 基于MACD判断趋势
        if 'macd' in data.columns and 'signal' in data.columns:
            latest_data = data.iloc[-1]
            if latest_data['macd'] > latest_data['signal'] and latest_data['macd'] > 0:
                trend = "上升"
                strength += 1
            elif latest_data['macd'] < latest_data['signal'] and latest_data['macd'] < 0:
                trend = "下降"
                strength += 1
        
        # 计算支撑位和阻力位
        support = data['low'].tail(20).min()
        resistance = data['high'].tail(20).max()
        
        return {
            "trend": trend,
            "strength": strength,
            "support": support,
            "resistance": resistance
        }
    
    def analyze_volatility(self, data: pd.DataFrame) -> Dict[str, Any]:
        """分析波动率
        
        Args:
            data: 历史行情数据
            
        Returns:
            dict: 波动率分析结果
        """
        if data is None or data.empty:
            return {
                "volatility": 0,
                "volatility_level": "低",
                "bb_width": 0
            }
        
        # 计算波动率
        data = self.calculate_bollinger_bands(data)
        
        # 计算历史波动率
        returns = data['close'].pct_change()
        volatility = returns.std() * np.sqrt(252)  # 年化波动率
        
        # 确定波动率水平
        volatility_level = "低"
        if volatility > 0.4:
            volatility_level = "高"
        elif volatility > 0.2:
            volatility_level = "中"
        
        # 计算布林带宽度
        bb_width = 0
        if 'bb_upper' in data.columns and 'bb_lower' in data.columns and 'bb_mid' in data.columns:
            latest_data = data.iloc[-1]
            bb_width = (latest_data['bb_upper'] - latest_data['bb_lower']) / latest_data['bb_mid'] * 100
        
        return {
            "volatility": volatility,
            "volatility_level": volatility_level,
            "bb_width": bb_width
        }
    
    def analyze_momentum(self, data: pd.DataFrame) -> Dict[str, Any]:
        """分析动量
        
        Args:
            data: 历史行情数据
            
        Returns:
            dict: 动量分析结果
        """
        if data is None or data.empty:
            return {
                "momentum": 0,
                "momentum_strength": "弱",
                "rsi_level": "中性",
                "rsi": None
            }
        
        # 计算动量指标
        data = self.calculate_rsi(data)
        
        # 计算动量
        if len(data) >= 10:
            momentum = data['close'].iloc[-1] / data['close'].iloc[-10] - 1
        else:
            momentum = 0
        
        # 确定动量强度
        momentum_strength = "弱"
        if momentum > 0.1:
            momentum_strength = "强"
        elif momentum > 0.05:
            momentum_strength = "中等"
        elif momentum < -0.1:
            momentum_strength = "强"
        elif momentum < -0.05:
            momentum_strength = "中等"
        
        # 分析RSI水平
        rsi_level = "中性"
        latest_rsi = None
        if 'rsi' in data.columns:
            raw_rsi = data['rsi'].iloc[-1]
            if pd.notna(raw_rsi):
                latest_rsi = round(float(raw_rsi), 2)
            if latest_rsi is not None and latest_rsi > 70:
                rsi_level = "超买"
            elif latest_rsi is not None and latest_rsi < 30:
                rsi_level = "超卖"
        
        return {
            "momentum": momentum,
            "momentum_strength": momentum_strength,
            "rsi_level": rsi_level,
            # 真实 RSI 数值（此前前端拿 technical_score 当 RSI 展示）
            "rsi": latest_rsi
        }

    def analyze_volume(self, data: pd.DataFrame) -> Dict[str, Any]:
        """分析成交量
        
        Args:
            data: 历史行情数据
            
        Returns:
            dict: 成交量分析结果
        """
        if data is None or data.empty:
            return {
                "volume_trend": "稳定",
                "volume_change": 0,
                "volume_ratio": 1
            }
        
        # 计算成交量趋势
        volume_trend = "稳定"
        volume_change = 0
        volume_ratio = 1
        
        if 'volume' in data.columns:
            # 计算成交量变化
            if len(data) >= 2:
                current_volume = data['volume'].iloc[-1]
                prev_volume = data['volume'].iloc[-2]
                volume_change = (current_volume - prev_volume) / prev_volume
                
                # 计算成交量比率（与5日均量比较）
                if len(data) >= 5:
                    avg_volume_5 = data['volume'].tail(5).mean()
                    volume_ratio = current_volume / avg_volume_5
                
                # 确定成交量趋势
                if volume_change > 0.2:
                    volume_trend = "增加"
                elif volume_change < -0.2:
                    volume_trend = "减少"
        
        return {
            "volume_trend": volume_trend,
            "volume_change": volume_change,
            "volume_ratio": volume_ratio
        }
    
    def analyze_macd(self, data: pd.DataFrame) -> Dict[str, Any]:
        """MACD 真实状态：金叉/死叉/多头/空头

        此前前端展示的 MACD 是由「趋势强度」反推的，横盘时只能显示「未知」。
        这里直接看 DIF/DEA 与近两日柱状值变化，给出可解释的真实状态。

        Args:
            data: 历史行情数据

        Returns:
            dict: {status, dif, dea, hist, cross}
        """
        empty = {"status": "未知", "dif": None, "dea": None, "hist": None, "cross": None}
        if data is None or data.empty:
            return empty

        df = self.calculate_macd(data.copy())
        if len(df) < 2:
            return empty

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        dif, dea, hist = latest['macd'], latest['signal'], latest['hist']
        prev_hist = prev['hist']
        if pd.isna(dif) or pd.isna(dea) or pd.isna(hist) or pd.isna(prev_hist):
            return empty

        cross = None
        if prev_hist <= 0 < hist:
            status, cross = "金叉", "金叉"
        elif prev_hist >= 0 > hist:
            status, cross = "死叉", "死叉"
        elif dif > dea:
            status = "多头"
        elif dif < dea:
            status = "空头"
        else:
            status = "中性"

        return {
            "status": status,
            "dif": round(float(dif), 3),
            "dea": round(float(dea), 3),
            "hist": round(float(hist), 3),
            "cross": cross,
        }

    def analyze_kdj(self, data: pd.DataFrame) -> Dict[str, Any]:
        """KDJ 真实状态：超买/超卖/金叉/死叉/中性

        Args:
            data: 历史行情数据

        Returns:
            dict: {status, k, d, j, cross}
        """
        empty = {"status": "未知", "k": None, "d": None, "j": None, "cross": None}
        if data is None or data.empty or len(data) < 9:
            return empty

        df = self.calculate_kdj(data.copy())
        latest = df.iloc[-1]
        k, d, j = latest['k'], latest['d'], latest['j']
        if pd.isna(k) or pd.isna(d) or pd.isna(j):
            return empty

        cross = None
        if len(df) >= 2:
            prev = df.iloc[-2]
            if pd.notna(prev['k']) and pd.notna(prev['d']):
                if prev['k'] <= prev['d'] and k > d:
                    cross = "金叉"
                elif prev['k'] >= prev['d'] and k < d:
                    cross = "死叉"

        # 超买/超卖优先（风险提示更重要），其次看交叉
        if k > 80:
            status = "超买"
        elif k < 20:
            status = "超卖"
        elif cross:
            status = cross
        else:
            status = "中性"

        return {
            "status": status,
            "k": round(float(k), 2),
            "d": round(float(d), 2),
            "j": round(float(j), 2),
            "cross": cross,
        }

    def analyze_bollinger(self, data: pd.DataFrame) -> Dict[str, Any]:
        """布林带位置：突破上轨/突破下轨/通道内

        Returns:
            dict: {status, upper, mid, lower, bb_width}
        """
        empty = {"status": "未知", "upper": None, "mid": None, "lower": None, "bb_width": None}
        if data is None or data.empty or len(data) < 20:
            return empty

        df = self.calculate_bollinger_bands(data.copy())
        latest = df.iloc[-1]
        for col in ('bb_upper', 'bb_mid', 'bb_lower', 'close'):
            if col not in df.columns or pd.isna(latest[col]):
                return empty

        close, upper, mid, lower = (latest['close'], latest['bb_upper'],
                                    latest['bb_mid'], latest['bb_lower'])
        if close >= upper:
            status = "突破上轨"
        elif close <= lower:
            status = "突破下轨"
        else:
            status = "通道内"

        width = (upper - lower) / mid * 100 if mid else 0
        return {
            "status": status,
            "upper": round(float(upper), 2),
            "mid": round(float(mid), 2),
            "lower": round(float(lower), 2),
            "bb_width": round(float(width), 2),
        }

    def comprehensive_technical_analysis(self, data: pd.DataFrame) -> Dict[str, Any]:
        """综合技术分析
        
        Args:
            data: 历史行情数据
            
        Returns:
            dict: 综合技术分析结果
        """
        if data is None or data.empty:
            return {
                "trend_analysis": self.analyze_trend(data),
                "volatility_analysis": self.analyze_volatility(data),
                "momentum_analysis": self.analyze_momentum(data),
                "volume_analysis": self.analyze_volume(data),
                "indicator_analysis": {
                    "macd": self.analyze_macd(data),
                    "kdj": self.analyze_kdj(data),
                    "bollinger": self.analyze_bollinger(data),
                },
                "technical_score": 0,
                "technical_opinion": "数据不足"
            }
        
        # 分析各个维度
        trend_analysis = self.analyze_trend(data)
        volatility_analysis = self.analyze_volatility(data)
        momentum_analysis = self.analyze_momentum(data)
        volume_analysis = self.analyze_volume(data)
        # 真实指标状态（MACD/KDJ/布林带），供前端直接展示，无需再由趋势反推
        indicator_analysis = {
            "macd": self.analyze_macd(data),
            "kdj": self.analyze_kdj(data),
            "bollinger": self.analyze_bollinger(data),
        }
        
        # 计算技术分析评分（0-100）
        technical_score = 50  # 基础分
        
        # 趋势评分
        if trend_analysis['trend'] == "上升":
            technical_score += 20
        elif trend_analysis['trend'] == "下降":
            technical_score -= 20
        
        # 动量评分
        if momentum_analysis['momentum_strength'] == "强" and momentum_analysis['momentum'] > 0:
            technical_score += 15
        elif momentum_analysis['momentum_strength'] == "强" and momentum_analysis['momentum'] < 0:
            technical_score -= 15
        
        # RSI评分
        if momentum_analysis['rsi_level'] == "超买":
            technical_score -= 10
        elif momentum_analysis['rsi_level'] == "超卖":
            technical_score += 10
        
        # 成交量评分
        if volume_analysis['volume_trend'] == "增加":
            technical_score += 10
        elif volume_analysis['volume_trend'] == "减少":
            technical_score -= 10
        
        # 确保评分在0-100之间
        technical_score = max(0, min(100, technical_score))
        
        # 生成技术分析意见
        technical_opinion = "中性"
        if technical_score >= 70:
            technical_opinion = "看多"
        elif technical_score <= 30:
            technical_opinion = "看空"
        
        return {
            "trend_analysis": trend_analysis,
            "volatility_analysis": volatility_analysis,
            "momentum_analysis": momentum_analysis,
            "volume_analysis": volume_analysis,
            "indicator_analysis": indicator_analysis,
            "technical_score": technical_score,
            "technical_opinion": technical_opinion
        }


if __name__ == "__main__":
    # 测试技术分析器
    import sys
    from pathlib import Path
    
    # 添加项目根目录到路径
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from stock_analyzer.data_fetcher import DataFetcher
    
    # 获取测试数据
    fetcher = DataFetcher()
    data = fetcher.get_stock_quote("600519", period="30d")
    
    # 初始化技术分析器
    analyzer = TechnicalAnalyzer()
    
    # 测试综合技术分析
    result = analyzer.comprehensive_technical_analysis(data)
    print("综合技术分析结果:")
    print(f"趋势分析: {result['trend_analysis']}")
    print(f"波动率分析: {result['volatility_analysis']}")
    print(f"动量分析: {result['momentum_analysis']}")
    print(f"成交量分析: {result['volume_analysis']}")
    print(f"技术评分: {result['technical_score']}")
    print(f"技术意见: {result['technical_opinion']}")