import os
import sqlite3
from chinese_call_sign_database import ChineseCallSignDatabase

class CallSignProvinceMapper:
    # 数据库实例
    _db_instance = None
    
    @classmethod
    def get_db_instance(cls):
        """获取数据库实例，懒加载模式，连接到主数据库"""
        if cls._db_instance is None:
            try:
                cls._db_instance = ChineseCallSignDatabase('call_sign_history.db')
            except Exception as e:
                print(f"初始化中国呼号识别数据库失败: {str(e)}")
                cls._db_instance = False  # 标记为初始化失败
        return cls._db_instance
    
    @staticmethod
    def recognize_province(call_sign):
        """
        根据呼号判断对应的省份
        - 港澳台呼号：通过第1-2位（索引0-1）字符匹配
        - 北京呼号：通过第3位（索引2）字符匹配
        - 其他呼号：通过第3-4位（索引2-3）字符匹配
        输入：呼号字符串
        输出：省份名称字符串或'未知省份'
        """
        # 检查呼号是否有效
        if not call_sign or not call_sign.strip():
            return '未知省份'
        
        call_sign = call_sign.strip().upper()
        
        try:
            # 获取数据库实例
            db = CallSignProvinceMapper.get_db_instance()
            if db is False:
                return '未知省份'
            
            # 1. 首先尝试通过第1-2位匹配港澳台呼号
            if len(call_sign) >= 2:
                first_two_chars = call_sign[:2]
                
                # 特殊匹配规则：港澳台呼号
                # 台湾省特殊呼号规则
                if first_two_chars in ['BM', 'BN', 'BO', 'BP', 'BQ', 'BU', 'BV', 'BW', 'BX']:
                    return '台湾省'
                # 香港特殊呼号规则
                elif first_two_chars in ['VS', 'VR']:
                    return '香港'
                # 澳门特殊呼号规则
                elif first_two_chars == 'XX':
                    return '澳门'
                
                # 查询第1-2位字符对应的地区（包括其他港澳台呼号）
                result = db.query_province_by_code(first_two_chars)
                if result:
                    return result
            
            # 2. 对于B开头的呼号，尝试通过第3位匹配北京
            if call_sign.startswith('B') and len(call_sign) >= 3:
                # 提取呼号的第3位字符
                third_char = call_sign[2]
                
                # 数据库中北京对应的代码是'1'
                if third_char == '1':
                    return '北京市'
            
            # 3. 尝试通过第3-4位匹配全国各省（适用于所有呼号，不仅限于B开头）
            if len(call_sign) >= 4:
                # 提取呼号的第3-4位字符
                region_code = call_sign[2:4]
                
                # 查询该两位代码对应的省份
                result = db.query_province_by_code(region_code)
                if result:
                    return result
            
            # 4. 对于长度为5或6的呼号，如果前几位没有匹配到，尝试使用第3位单个字符进行匹配
            if call_sign.startswith('B') and len(call_sign) >= 3:
                # 提取呼号的第3位字符
                third_char = call_sign[2]
                
                # 查询该字符对应的省份
                result = db.query_province_by_code(third_char)
                if result:
                    return result
            
            return '未知省份'
        except Exception as e:
            print(f"识别省份时发生错误: {str(e)}")
            return '未知省份'
    
    @staticmethod
    def recognize_province_with_details(call_sign):
        """
        根据呼号判断对应的省份，并返回详细的匹配信息
        输入：呼号字符串
        输出：包含省份名称、匹配类型、匹配字符等信息的字典
        """
        # 检查呼号是否有效
        if not call_sign or not call_sign.strip():
            return {
                'province': '未知省份',
                'match_type': '无效呼号',
                'matched_chars': '',
                'position': '',
                'is_valid': False
            }
        
        call_sign = call_sign.strip().upper()
        
        try:
            # 获取数据库实例
            db = CallSignProvinceMapper.get_db_instance()
            if db is False:
                return {
                    'province': '未知省份',
                    'match_type': '数据库错误',
                    'matched_chars': '',
                    'position': '',
                    'is_valid': False
                }
            
            # 1. 首先尝试通过第1-2位匹配港澳台呼号
            if len(call_sign) >= 2:
                first_two_chars = call_sign[:2]
                
                # 特殊匹配规则：港澳台呼号
                # 台湾省特殊呼号规则
                if first_two_chars in ['BM', 'BN', 'BO', 'BP', 'BQ', 'BU', 'BV', 'BW', 'BX']:
                    return {
                        'province': '台湾省',
                        'match_type': '港澳台特殊呼号匹配',
                        'matched_chars': first_two_chars,
                        'position': '呼号第1-2位',
                        'is_valid': True
                    }
                # 香港特殊呼号规则
                elif first_two_chars in ['VS', 'VR']:
                    return {
                        'province': '香港',
                        'match_type': '港澳台特殊呼号匹配',
                        'matched_chars': first_two_chars,
                        'position': '呼号第1-2位',
                        'is_valid': True
                    }
                # 澳门特殊呼号规则
                elif first_two_chars == 'XX':
                    return {
                        'province': '澳门',
                        'match_type': '港澳台特殊呼号匹配',
                        'matched_chars': first_two_chars,
                        'position': '呼号第1-2位',
                        'is_valid': True
                    }
                
                # 查询第1-2位字符对应的地区（包括其他港澳台呼号）
                result = db.query_province_by_code(first_two_chars)
                if result:
                    return {
                        'province': result,
                        'match_type': '港澳台呼号匹配',
                        'matched_chars': first_two_chars,
                        'position': '呼号第1-2位',
                        'is_valid': True
                    }
            
            # 2. 对于B开头的呼号，尝试通过第3位匹配北京
            if call_sign.startswith('B') and len(call_sign) >= 3:
                # 提取呼号的第3位字符
                third_char = call_sign[2]
                
                # 数据库中北京对应的代码是'1'
                if third_char == '1':
                    return {
                        'province': '北京市',
                        'match_type': '1位规则匹配',
                        'matched_chars': third_char,
                        'position': '呼号第3位',
                        'is_valid': True
                    }
            
            # 3. 尝试通过第3-4位匹配全国各省（适用于所有呼号，不仅限于B开头）
            if len(call_sign) >= 4:
                # 提取呼号的第3-4位字符
                region_code = call_sign[2:4]
                
                # 查询该两位代码对应的省份
                result = db.query_province_by_code(region_code)
                if result:
                    return {
                        'province': result,
                        'match_type': '2位规则匹配',
                        'matched_chars': region_code,
                        'position': '呼号第3-4位',
                        'is_valid': True
                    }
            
            # 4. 对于长度为5或6的呼号，如果前几位没有匹配到，尝试使用第3位单个字符进行匹配
            if call_sign.startswith('B') and len(call_sign) >= 3:
                # 提取呼号的第3位字符
                third_char = call_sign[2]
                
                # 查询该字符对应的省份
                result = db.query_province_by_code(third_char)
                if result:
                    return {
                        'province': result,
                        'match_type': '1位规则匹配',
                        'matched_chars': third_char,
                        'position': '呼号第3位',
                        'is_valid': True
                    }
            
            # 5. 如果呼号长度不足4位
            if len(call_sign) < 4:
                return {
                    'province': '未知省份',
                    'match_type': '呼号长度不足',
                    'matched_chars': '',
                    'position': '',
                    'is_valid': False
                }
            
            return {
                'province': '未知省份',
                'match_type': '未匹配',
                'matched_chars': '',
                'position': '',
                'is_valid': False
            }
        except Exception as e:
            print(f"识别省份时发生错误: {str(e)}")
            return {
                'province': '未知省份',
                'match_type': '错误',
                'matched_chars': '',
                'position': '',
                'is_valid': False
            }

# 创建实例以保持向后兼容性
call_sign_province_mapper = CallSignProvinceMapper()