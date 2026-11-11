import os
import sqlite3

class ChineseCallSignDatabase:
    def __init__(self, db_path='call_sign_history.db'):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """初始化中国呼号识别库数据库"""
        # 连接数据库（主数据库应该已经存在，所以不检查是否存在）
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建中国呼号规则表（如果不存在）
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chinese_call_sign_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefix TEXT UNIQUE NOT NULL,
            district TEXT NOT NULL,
            region TEXT,
            call_sign_type TEXT,
            description TEXT
        )
        ''')
        
        # 创建索引以提高查询效率
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_prefix ON chinese_call_sign_rules (prefix)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_district ON chinese_call_sign_rules (district)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_region ON chinese_call_sign_rules (region)')
        
        # 提交并关闭连接
        conn.commit()
        conn.close()
    
    def _insert_initial_rules(self, cursor):
        """插入初始的中国呼号规则数据"""
        # 中国业余无线电呼号规则数据
        # 这里只保留有效的呼号规则
        call_sign_rules = [
            # 仅保留实际需要的规则，移除过时的映射代码
        ]
        
        # 如果有新规则需要添加，取消注释下面的代码
        # if call_sign_rules:
        #     cursor.executemany(
        #         'INSERT INTO chinese_call_sign_rules (prefix, district, region, call_sign_type, description) VALUES (?, ?, ?, ?, ?)',
        #         call_sign_rules
        #     )
    
    def get_call_sign_info(self, call_sign):
        """
        根据呼号获取详细信息
        输入：呼号字符串
        输出：包含呼号信息的字典或None
        """
        if not call_sign or len(call_sign) < 2:
            return None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 尝试匹配不同长度的前缀，从最长的开始
        info = None
        max_prefix_len = min(3, len(call_sign))  # 最多匹配前3个字符
        
        for i in range(max_prefix_len, 1, -1):
            prefix = call_sign[:i].upper()
            cursor.execute(
                'SELECT prefix, district, region, call_sign_type, description FROM chinese_call_sign_rules WHERE prefix = ?',
                (prefix,)
            )
            result = cursor.fetchone()
            
            if result:
                info = {
                    'prefix': result[0],
                    'district': result[1],
                    'region': result[2],
                    'call_sign_type': result[3],
                    'description': result[4]
                }
                break
        
        # 如果没有匹配到更长的前缀，尝试匹配地区标识数字/字母
        if not info and len(call_sign) >= 4:
            # 从第3-4位字符（索引2和3）进行匹配
            region_code = call_sign[2:4].upper()
            cursor.execute(
                'SELECT prefix, district, region, call_sign_type, description FROM chinese_call_sign_rules WHERE prefix = ?',
                (region_code,)
            )
            result = cursor.fetchone()
            
            if result:
                info = {
                    'prefix': result[0],
                    'district': result[1],
                    'region': result[2],
                    'call_sign_type': result[3],
                    'description': result[4]
                }
            else:
                # 如果没有匹配到2位代码，尝试匹配第3位单个字符（向后兼容）
                if len(call_sign) >= 3:
                    char = call_sign[2].upper()
                    cursor.execute(
                        'SELECT prefix, district, region, call_sign_type, description FROM chinese_call_sign_rules WHERE prefix = ?',
                        (char,)
                    )
                    result = cursor.fetchone()
                    
                    if result:
                        info = {
                            'prefix': result[0],
                            'district': result[1],
                            'region': result[2],
                            'call_sign_type': result[3],
                            'description': result[4]
                        }
        
        conn.close()
        return info
        
    def query_province_by_code(self, code):
        """
        根据代码查询对应的省份
        输入：地区代码字符串
        输出：省份名称字符串或None
        """
        if not code:
            return None
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 确保代码大写
            code_upper = code.upper()
            
            # 查询代码对应的省份
            cursor.execute(
                'SELECT district FROM chinese_call_sign_rules WHERE prefix = ?',
                (code_upper,)
            )
            
            result = cursor.fetchone()
            
            if result:
                return result[0]
            return None
        except sqlite3.Error as e:
            print(f"查询省份时发生错误: {str(e)}")
            return None
        finally:
            conn.close()
    
    def add_call_sign_rule(self, prefix, district, region=None, call_sign_type=None, description=None):
        """添加新的呼号规则"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                'INSERT OR REPLACE INTO chinese_call_sign_rules (prefix, district, region, call_sign_type, description) VALUES (?, ?, ?, ?, ?)',
                (prefix.upper(), district, region, call_sign_type, description)
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"添加呼号规则失败: {str(e)}")
            return False
        finally:
            conn.close()
    
    def update_call_sign_rule(self, prefix, district=None, region=None, call_sign_type=None, description=None):
        """更新呼号规则"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 构建更新字段和参数
            update_fields = []
            params = []
            
            if district is not None:
                update_fields.append('district = ?')
                params.append(district)
            if region is not None:
                update_fields.append('region = ?')
                params.append(region)
            if call_sign_type is not None:
                update_fields.append('call_sign_type = ?')
                params.append(call_sign_type)
            if description is not None:
                update_fields.append('description = ?')
                params.append(description)
            
            if not update_fields:
                return True  # 没有字段需要更新
            
            # 添加前缀到参数列表末尾
            params.append(prefix.upper())
            
            # 执行更新
            cursor.execute(f"UPDATE chinese_call_sign_rules SET {', '.join(update_fields)} WHERE prefix = ?", params)
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            print(f"更新呼号规则失败: {str(e)}")
            return False
        finally:
            conn.close()
    
    def delete_call_sign_rule(self, prefix):
        """删除呼号规则"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('DELETE FROM chinese_call_sign_rules WHERE prefix = ?', (prefix.upper(),))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            print(f"删除呼号规则失败: {str(e)}")
            return False
        finally:
            conn.close()
    
    def get_all_rules(self):
        """获取所有呼号规则"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT prefix, district, region, call_sign_type, description FROM chinese_call_sign_rules ORDER BY prefix')
        results = cursor.fetchall()
        
        conn.close()
        
        return [{
            'prefix': result[0],
            'district': result[1],
            'region': result[2],
            'call_sign_type': result[3],
            'description': result[4]
        } for result in results]

# 测试代码
if __name__ == '__main__':
    # 创建数据库实例
    db = ChineseCallSignDatabase()
    
    # 测试获取呼号信息
    test_calls = ['BH2VLF', 'BA1AA', 'BD2AA', 'BG3AA', 'BI4AA']
    
    print("中国呼号识别测试:")
    print("=" * 50)
    
    for call in test_calls:
        info = db.get_call_sign_info(call)
        if info:
            print(f"呼号: {call}")
            print(f"  前缀: {info['prefix']}")
            print(f"  地区: {info['district']}")
            print(f"  区域: {info['region']}")
            print(f"  呼号类型: {info['call_sign_type']}")
            print(f"  描述: {info['description']}")
        else:
            print(f"呼号: {call} - 未找到匹配的规则")
        print()
        
    print("所有呼号规则:")
    print("=" * 50)
    for rule in db.get_all_rules():
        print(f"前缀: {rule['prefix']}, 地区: {rule['district']}, 类型: {rule['call_sign_type']}")